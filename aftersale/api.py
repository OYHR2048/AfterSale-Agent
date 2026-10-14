import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from aftersale.config import PACKAGE, Settings
from aftersale.db import DomainError
from aftersale.models import HumanResponse, StrictModel, TaskRequest
from aftersale.service import Service


class Login(StrictModel):
    token: str = Field(min_length=1, max_length=200)


class Simulation(StrictModel):
    order_id: str = Field(pattern=r"^ORD[0-9]{5}$")
    status: str | None = Field(default=None, pattern=r"^(paid|shipped|delivered|cancelled)$")
    stock: int | None = Field(default=None, ge=0, le=1000)
    logistics: str | None = Field(
        default=None, pattern=r"^(delivered|not_shipped|in_transit|stalled_72h)$"
    )
    fail_tool: str | None = None
    fail_count: int = Field(default=0, ge=0, le=10)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    jobs = set()
    active = {}

    @asynccontextmanager
    async def lifespan(app):
        async with Service(settings) as s:
            app.state.service = s
            auth_file = settings.runtime_dir / "auth.json"
            if not auth_file.exists():
                auth_file.write_text(
                    json.dumps(
                        {name: secrets.token_urlsafe(32) for name in ("alice", "bob", "admin")}
                    ),
                    encoding="utf-8",
                )
            app.state.credentials = json.loads(auth_file.read_text(encoding="utf-8"))
            yield
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)

    app = FastAPI(title="AfterSale-Agent (fictional local demo)", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    def service():
        return app.state.service

    def identity(request: Request):
        bearer = request.headers.get("authorization", "")
        token = (
            bearer.removeprefix("Bearer ")
            if bearer.startswith("Bearer ")
            else request.cookies.get("aftersale_session", "")
        )
        for owner, value in app.state.credentials.items():
            if secrets.compare_digest(token, value):
                if request.method not in {"GET", "HEAD"} and not bearer:
                    origin = request.headers.get("origin")
                    if origin and origin != str(request.base_url).rstrip("/"):
                        raise HTTPException(403, "来源不匹配")
                return owner
        raise HTTPException(401, "请使用服务端生成的开发凭据登录")

    def schedule(coroutine, task_id):
        job = asyncio.create_task(coroutine)
        jobs.add(job)
        active.setdefault(task_id, set()).add(job)

        def done(completed):
            jobs.discard(completed)
            active[task_id].discard(completed)
            if not completed.cancelled():
                completed.exception()  # observe rejected duplicate/racing inputs

        job.add_done_callback(done)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/")
    def index():
        return FileResponse(PACKAGE / "static/index.html")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "mode": settings.mode,
            "decision_backend": service().provider.backend,
            "business": "local_simulated",
            "policy": "fictional",
            "single_process": True,
        }

    @app.post("/api/login")
    def login(body: Login, response: Response):
        owner = next(
            (
                name
                for name, value in app.state.credentials.items()
                if secrets.compare_digest(value, body.token)
            ),
            None,
        )
        if not owner:
            raise HTTPException(401, "无效开发凭据")
        response.set_cookie("aftersale_session", body.token, httponly=True, samesite="strict")
        return {"owner": owner}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("aftersale_session")
        return {"ok": True}

    @app.get("/api/orders")
    def orders(owner=Depends(identity)):
        with service().db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM orders WHERE owner=?", (owner,))]

    @app.get("/api/tasks")
    def task_list(owner=Depends(identity)):
        with service().db.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT id,status,created_at FROM tasks WHERE owner=? ORDER BY created_at DESC LIMIT 30",
                    (owner,),
                )
            ]

    @app.post("/api/tasks", status_code=202)
    async def create(body: TaskRequest, owner=Depends(identity)):
        task_id = service().register(owner, body.message, body.architecture)
        schedule(service().run(task_id, owner, initial=True), task_id)
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, owner=Depends(identity)):
        return service().db.task(task_id, owner)

    @app.get("/api/tasks/{task_id}/records")
    def records(task_id: str, owner=Depends(identity)):
        service().db.task(task_id, owner)
        return service().db.records(task_id)

    @app.post("/api/tasks/{task_id}/respond", status_code=202)
    async def respond(task_id: str, body: HumanResponse, owner=Depends(identity)):
        state = service().db.task(task_id, owner)
        if body.kind == "confirm":
            p = state.get("proposal", {})
            if body.proposal_id != p.get("proposal_id") or body.version != p.get("version"):
                raise DomainError("旧方案确认已失效")
            if state["status"] not in {"waiting_confirmation", "completed"}:
                raise DomainError("任务没有等待确认")
        elif state["status"] not in {"waiting_info", "waiting_confirmation"}:
            raise DomainError("任务没有等待人工输入")
        if body.kind == "supplement" and state["status"] != "waiting_info":
            raise DomainError("请使用 modify 修改方案")
        schedule(service().respond(task_id, owner, body), task_id)
        return {"task_id": task_id}

    @app.post("/api/tasks/{task_id}/recover", status_code=202)
    async def recover(task_id: str, owner=Depends(identity)):
        service().db.task(task_id, owner)
        schedule(service().recover(task_id, owner), task_id)
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0, owner=Depends(identity)):
        service().db.task(task_id, owner)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid Last-Event-ID") from None

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                rows = service().db.events(task_id, owner, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: step\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                state = service().db.task(task_id, owner)
                yield "event: state\ndata: " + json.dumps(state, ensure_ascii=False) + "\n\n"
                if state["status"] not in {"running", "executing"} and not active.get(task_id):
                    yield "event: idle\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.15)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/demo/simulate")
    def simulate(body: Simulation, owner=Depends(identity)):
        if owner != "admin" or settings.mode == "model":
            raise HTTPException(403, "仅 demo/test 的管理员开发凭据允许故障注入")
        if body.fail_tool:
            from aftersale.tools import CARDS

            if body.fail_tool not in CARDS:
                raise HTTPException(422, "未知工具")
            service().tools.failures[body.fail_tool] = body.fail_count
        service().db.simulate(
            body.order_id, status=body.status, stock=body.stock, logistics=body.logistics
        )
        return {"ok": True, "data_source": "local_simulated"}

    return app


app = create_app()
