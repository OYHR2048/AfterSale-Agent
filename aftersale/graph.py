from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aftersale.db import DomainError
from aftersale.eligibility import qualify
from aftersale.executor import StateChanged
from aftersale.tools import WHITELISTS, consume


class TaskState(TypedDict):
    task_id: str
    owner: str
    run_id: str
    architecture: str
    message: str
    inputs: dict
    facts: dict
    evidence: list
    resolution: dict
    proposal: dict
    decision: dict
    status: str
    route: str
    question: str
    result: dict
    steps: int
    delegations: int
    decision_calls: int
    model_calls: int
    usage: list
    ttl: int
    backend: str
    error: str


class Workflow:
    def __init__(self, db, policy, tools, provider, executor, settings):
        self.db, self.policy, self.tools, self.provider, self.executor, self.settings = (
            db,
            policy,
            tools,
            provider,
            executor,
            settings,
        )

    def emit(self, state, role, kind, payload):
        self.db.event(state, role, kind, payload)

    def persist(self, state):
        self.db.save_task(state)
        return state

    def sync_progress(self, state):
        saved = self.db.task(state["task_id"], state["owner"])
        for key in ("steps", "decision_calls", "model_calls"):
            state[key] = max(state[key], saved[key])
        if len(saved["usage"]) > len(state["usage"]):
            state["usage"] = saved["usage"]

    async def parse(self, state):
        self.sync_progress(state)
        state["inputs"] = await self.provider.intent(
            state, state["message"], state["inputs"] or None
        )
        return self.persist(state)

    async def planner(self, state):
        self.sync_progress(state)
        consume(state, self.settings.max_steps)
        if state["architecture"] == "single":
            choice = (
                "ask"
                if not state["inputs"].get("order_id")
                else "single"
                if not state["resolution"]
                else "ask"
                if state["resolution"].get("gaps")
                else "propose"
            )
        else:
            context = {k: state[k] for k in ("inputs", "facts", "resolution")}
            decision = await self.provider.decide(state, "Supervisor", context, [])
            choice = decision.kind
            permitted = {"evidence", "resolution", "ask", "propose", "stop"}
            if choice not in permitted:
                raise DomainError("Supervisor 不能执行工具或业务写入")
            # Deterministic protocol invariants constrain model delegation choices.
            if choice == "propose" and (
                not state["resolution"].get("options")
                or state["resolution"].get("needs")
                or state["resolution"].get("gaps")
            ):
                raise DomainError("Supervisor 提案前证据不完整")
            if choice == "resolution" and not state["facts"].get("order"):
                raise DomainError("方案 Agent 缺少订单事实")
        self.emit(
            state,
            "Supervisor" if state["architecture"] == "multi" else "SingleAgent",
            "route",
            {
                "next": choice,
                "steps": state["steps"],
                "remaining": self.settings.max_steps - state["steps"],
            },
        )
        if choice in {"evidence", "resolution", "single"}:
            state["delegations"] += 1
            if state["delegations"] > self.settings.max_delegations:
                raise DomainError("重复委派预算耗尽")
            self.emit(
                state,
                "Supervisor",
                "delegate",
                {
                    "agent": {
                        "evidence": "EvidenceAgent",
                        "resolution": "ResolutionAgent",
                        "single": "SingleAgent",
                    }[choice],
                    "focus": state["resolution"].get("needs", []),
                },
            )
        if choice == "ask":
            state["question"] = (
                "请提供订单号，例如 ORD10002"
                if not state["inputs"].get("order_id")
                else "；".join(state["resolution"].get("gaps", ["请补充希望采取的售后方式"]))
            )
            state["status"] = "waiting_info"
            state["route"] = "wait"
            self.emit(state, "Supervisor", "waiting_info", {"question": state["question"]})
        elif choice == "stop":
            state["status"] = "failed"
            state["error"] = "当前资料不能形成允许方案"
            state["route"] = "end"
        else:
            state["route"] = choice
        return self.persist(state)

    async def loop(self, state, role, focus=""):
        self.sync_progress(state)
        # A fresh local context per delegation. No shared message list or scratch reasoning.
        local = {
            "brief": state["message"],
            "inputs": dict(state["inputs"]),
            "observations": {},
            "focus": focus,
        }
        if role == "ResolutionAgent":
            local["facts"] = state["facts"]
            local["evidence"] = [
                {k: c[k] for k in ("chunk_id", "document_id", "content", "policy_version")}
                for c in state["evidence"]
            ]
        elif focus:
            local["facts"] = {"order": state["facts"]["order"]}
        for step in range(self.settings.agent_steps):
            consume(state, self.settings.max_steps)
            decision = await self.provider.decide(state, role, local, sorted(WHITELISTS[role]))
            self.emit(
                state,
                role,
                "decision",
                {
                    "kind": decision.kind,
                    "tool": decision.tool,
                    "local_step": step + 1,
                    "backend": self.provider.backend,
                },
            )
            if decision.kind == "finish":
                return local["observations"]
            if decision.kind != "tool" or not decision.tool:
                raise DomainError("专业 Agent 必须选择只读工具或 finish")
            if (
                decision.tool == "get_inventory"
                and role == "SingleAgent"
                and "get_order" in local["observations"]
            ):
                state["facts"]["order"] = local["observations"]["get_order"]
            observation = await self.tools.call(state, role, decision.tool, decision.args)
            local["observations"][decision.tool] = observation
        raise DomainError("专业 Agent 步数耗尽：" + role)

    def merge_evidence(self, state, observations):
        mapping = {
            "get_order": "order",
            "get_items": "items",
            "get_logistics": "logistics",
            "get_inventory": "inventory",
        }
        for tool, key in mapping.items():
            if tool in observations:
                state["facts"][key] = observations[tool]
        if "search_policy" in observations:
            state["evidence"] = observations["search_policy"]["citations"]
        state["resolution"] = {}

    def resolution_from_tools(self, state, observations):
        if "check_eligibility" not in observations:
            raise DomainError("专业 Agent 未进行资格校验")
        if not all(k in state["facts"] for k in ("order", "items", "logistics")):
            raise DomainError("专业 Agent 缺少必要只读事实")
        if not self.policy.valid(state["evidence"]):
            raise DomainError("没有有效政策证据")
        # Tool supplies bounded preliminary options. Backend reconciles inventory & cited rules.
        result = qualify(state["facts"]["order"], state["inputs"], state["facts"].get("inventory"))
        required = set(result["required_policies"])
        cited = {c["document_id"]: c["chunk_id"] for c in state["evidence"]}
        if not required.issubset(cited):
            result["needs"].append("policy")
        for option in result["options"]:
            option["evidence_ids"] = [cited[name] for name in sorted(required) if name in cited]
        result["fact_sources"] = {
            "order": "get_order",
            "customized": "get_items",
            "logistics": "get_logistics",
            "inventory": "get_inventory",
        }
        return result

    async def single(self, state):
        observations = await self.loop(state, "SingleAgent")
        self.merge_evidence(state, observations)
        state["resolution"] = self.resolution_from_tools(state, observations)
        if state["resolution"].get("needs"):
            raise DomainError("单 Agent 未补齐工具资料")
        return self.persist(state)

    async def evidence(self, state):
        needs = state["resolution"].get("needs", [])
        focus = "inventory" if "inventory" in needs else "policy" if "policy" in needs else ""
        observations = await self.loop(state, "EvidenceAgent", focus)
        self.merge_evidence(state, observations)
        if not observations:
            raise DomainError("证据调查没有新增工具观察")
        self.emit(
            state,
            "EvidenceAgent",
            "handoff",
            {
                "facts": list(state["facts"]),
                "citations": [c["chunk_id"] for c in state["evidence"]],
            },
        )
        return self.persist(state)

    async def resolution(self, state):
        observations = await self.loop(state, "ResolutionAgent")
        state["resolution"] = self.resolution_from_tools(state, observations)
        self.emit(state, "ResolutionAgent", "handoff", state["resolution"])
        return self.persist(state)

    async def propose(self, state):
        result = state["resolution"]
        if result.get("needs") or result.get("gaps") or not result.get("options"):
            raise DomainError("不能保存证据不足的方案")
        desired = state["inputs"]["desired"]
        options = result["options"]
        selected = next((o for o in options if o["action"] == desired), options[0])
        state["proposal"] = self.db.propose(state, selected, self.policy.fingerprint())
        state["status"] = "waiting_confirmation"
        self.emit(
            state,
            "Supervisor",
            "proposal",
            {
                "proposal_id": state["proposal"]["proposal_id"],
                "version": state["proposal"]["version"],
                "selected": selected,
                "alternatives": options,
                "fallback": selected["action"] != desired,
            },
        )
        return self.persist(state)

    async def wait(self, state):
        # Pattern adapted from toolkit interrupt_agent, MIT. No recursion or writes before interrupt.
        response = interrupt(
            {
                "status": state["status"],
                "question": state["question"],
                "proposal": state["proposal"],
            }
        )
        state["decision"] = response
        return state

    async def human(self, state):
        self.sync_progress(state)
        d = state["decision"]
        if d["kind"] == "reject":
            self.db.invalidate(state["task_id"], "rejected")
            state["status"] = "cancelled"
            state["route"] = "end"
            self.emit(state, "Human", "rejected", {"writes": 0})
        elif d["kind"] == "confirm":
            state["status"] = "executing"
            state["route"] = "execute"
        else:
            self.db.invalidate(state["task_id"])
            state["message"] = d["message"]
            state["status"] = "running"
            state["inputs"] = await self.provider.intent(state, d["message"], state["inputs"])
            state["proposal"] = {}
            state["resolution"] = {}
            state["facts"] = {}
            state["evidence"] = []
            state["status"] = "running"
            state["delegations"] = 0
            state["route"] = "planner"
            self.emit(state, "Human", "updated", {"kind": d["kind"]})
        return self.persist(state)

    async def execute(self, state):
        state["status"] = "executing"
        self.persist(state)
        try:
            state["result"] = self.executor.execute(state)
        except StateChanged as exc:
            self.db.invalidate(state["task_id"])
            state["facts"] = {}
            state["evidence"] = []
            state["resolution"] = {}
            state["proposal"] = {}
            state["status"] = "running"
            state["delegations"] = 0
            state["route"] = "planner"
            self.emit(
                state, "Executor", "replan", {"reason": str(exc), "old_confirmation_invalid": True}
            )
        else:
            state["status"] = "completed"
            state["route"] = "end"
            self.emit(state, "Executor", "business_result", state["result"])
        return self.persist(state)

    def build(self, checkpointer):
        graph = StateGraph(TaskState)
        for name in (
            "parse",
            "planner",
            "single",
            "evidence",
            "resolution",
            "propose",
            "wait",
            "human",
            "execute",
        ):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "parse")
        graph.add_edge("parse", "planner")
        graph.add_conditional_edges(
            "planner",
            lambda s: s["route"],
            {
                "single": "single",
                "evidence": "evidence",
                "resolution": "resolution",
                "propose": "propose",
                "wait": "wait",
                "end": END,
            },
        )
        for name in ("single", "evidence", "resolution"):
            graph.add_edge(name, "planner")
        graph.add_edge("propose", "wait")
        graph.add_edge("wait", "human")
        for name in ("human", "execute"):
            graph.add_conditional_edges(
                name, lambda s: s["route"], {"planner": "planner", "execute": "execute", "end": END}
            )
        return graph.compile(checkpointer=checkpointer)
