import asyncio
from contextlib import AsyncExitStack

from langgraph.types import Command

from aftersale.db import Database, DomainError, new_id
from aftersale.executor import Executor, SimulatedCrash
from aftersale.graph import Workflow
from aftersale.models import HumanResponse
from aftersale.persistence import get_sqlite_saver
from aftersale.policy import PolicyIndex
from aftersale.providers import DemoProvider, ModelProvider, ScriptedProvider
from aftersale.tools import ToolRunner


class Service:
    def __init__(self, settings, provider=None):
        self.settings = settings.prepare()
        self.db = Database(settings.runtime_dir / "business.db")
        self.policy = PolicyIndex(settings.runtime_dir, settings.rag_url)
        self.tools = ToolRunner(self.db, self.policy, settings)
        self.provider = provider or (
            ModelProvider(settings)
            if settings.mode == "model"
            else ScriptedProvider([])
            if settings.mode == "test"
            else DemoProvider()
        )
        self.executor = Executor(self.db, self.policy)
        self.provider.on_progress = self.db.save_task
        self.workflow = Workflow(
            self.db, self.policy, self.tools, self.provider, self.executor, settings
        )
        self.locks = {}
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        saver = await self.stack.enter_async_context(
            get_sqlite_saver(self.settings.runtime_dir / "checkpoints.db")
        )
        await saver.setup()
        self.graph = self.workflow.build(saver)
        return self

    async def __aexit__(self, *args):
        await self.stack.aclose()

    def register(self, owner, message, architecture="multi"):
        task_id = new_id("T_")
        state = {
            "task_id": task_id,
            "owner": owner,
            "run_id": new_id("R_"),
            "architecture": architecture,
            "message": message,
            "inputs": {},
            "facts": {},
            "evidence": [],
            "resolution": {},
            "proposal": {},
            "decision": {},
            "status": "running",
            "route": "",
            "question": "",
            "result": {},
            "steps": 0,
            "delegations": 0,
            "decision_calls": 0,
            "model_calls": 0,
            "usage": [],
            "ttl": self.settings.proposal_ttl,
            "backend": self.provider.backend,
            "error": "",
        }
        self.db.create_task(owner, state)
        return task_id

    def config(self, task_id):
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 100}

    async def run(self, task_id, owner, initial=False, response=None):
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            state = self.db.task(task_id, owner)
            if state["status"] in {"completed", "cancelled"}:
                if response and response.kind == "confirm":
                    row = self.db.proposal(task_id, response.proposal_id, owner)
                    if row["status"] != "executed" or row["version"] != response.version:
                        raise DomainError("该确认不对应已执行方案")
                return state
            graph_input = (
                state if initial else Command(resume=response.model_dump()) if response else None
            )
            if response:
                if response.kind == "confirm":
                    if state["status"] != "waiting_confirmation":
                        raise DomainError("任务未在等待确认")
                    p = state["proposal"]
                    if response.proposal_id != p.get("proposal_id") or response.version != p.get(
                        "version"
                    ):
                        raise DomainError("旧方案确认已失效")
                elif response.kind == "supplement" and state["status"] != "waiting_info":
                    raise DomainError("请使用 modify 修改待确认方案")
                elif state["status"] not in {"waiting_info", "waiting_confirmation"}:
                    raise DomainError("任务未在等待人工输入")
            try:
                self.db.active_runs[task_id] = new_id("R_")
                self.db.event(
                    state,
                    "Backend",
                    "run_started",
                    {"resume": bool(response), "backend": self.provider.backend},
                )
                if response:
                    state["status"] = "executing" if response.kind == "confirm" else "running"
                    self.db.save_task(state)
                result = await self.graph.ainvoke(graph_input, self.config(task_id))
                state = {k: v for k, v in result.items() if not k.startswith("__")}
                self.db.save_task(state)
            except SimulatedCrash:
                raise
            except Exception as exc:
                snap = await self.graph.aget_state(self.config(task_id))
                progress = self.db.task(task_id, owner)
                state = dict(snap.values) or state
                for key in ("steps", "decision_calls", "model_calls"):
                    state[key] = max(state[key], progress[key])
                if len(progress["usage"]) > len(state["usage"]):
                    state["usage"] = progress["usage"]
                state["status"] = "failed"
                # Never store raw HTTP exceptions (may contain keys / provider payload).
                state["error"] = str(exc) if isinstance(exc, DomainError) else type(exc).__name__
                self.db.event(
                    state,
                    "Backend",
                    "failed",
                    {
                        "error": state["error"],
                        "steps": state["steps"],
                        "model_calls": state["model_calls"],
                    },
                )
                self.db.save_task(state)
            return state

    async def create(self, owner, message, architecture="multi"):
        task_id = self.register(owner, message, architecture)
        return await self.run(task_id, owner, initial=True)

    async def respond(self, task_id, owner, response):
        response = HumanResponse.model_validate(response)
        return await self.run(task_id, owner, response=response)

    async def recover(self, task_id, owner):
        state = self.db.task(task_id, owner)
        self.db.active_runs[task_id] = new_id("R_")
        records = self.db.records(task_id)
        if records:
            # A hard process exit can precede the checkpoint flush. The atomic
            # business commit is authoritative; reconcile both projections.
            record = records[0]
            row = self.db.proposal(task_id, record["proposal_id"], owner)
            if row["status"] != "executed":
                raise DomainError("业务记录与方案提交状态冲突")
            import json

            if json.loads(row["result"]) != record:
                raise DomainError("业务记录与保存结果冲突")
            state.update(status="completed", result=record, route="end")
            await self.graph.aupdate_state(self.config(task_id), state, as_node="execute")
            self.db.event(
                state,
                "Backend",
                "reconciled",
                {"record_id": record["record_id"], "source": "atomic_business_commit"},
            )
            self.db.save_task(state)
            return state
        snap = await self.graph.aget_state(self.config(task_id))
        if not snap.values:
            return await self.run(task_id, owner, initial=True)
        if snap.tasks and any(t.interrupts for t in snap.tasks):
            return state
        if snap.next:
            return await self.run(task_id, owner)
        return state
