import json

from fastapi.testclient import TestClient

from aftersale.api import create_app
from aftersale.config import Settings


def stream_state(client, task_id):
    text = client.get(f"/api/tasks/{task_id}/events").text
    states = [
        json.loads(line[6:])
        for block in text.split("\n\n")
        if block.startswith("event: state")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]
    return states[-1], text


def test_api_cookie_sse_and_identity(tmp_path):
    app = create_app(Settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/health").json()["business"] == "local_simulated"
        assert c.get("/api/orders").status_code == 401
        token = app.state.credentials["alice"]
        assert c.post("/api/login", json={"token": token}).json()["owner"] == "alice"
        assert (
            c.post("/api/tasks", json={"message": "ORD10003 取消", "user_id": "bob"}).status_code
            == 422
        )
        assert (
            c.post(
                "/api/tasks",
                json={"message": "ORD10003 取消"},
                headers={"Origin": "http://evil.test"},
            ).status_code
            == 403
        )
        task_id = c.post("/api/tasks", json={"message": "ORD10003 取消"}).json()["task_id"]
        t, events = stream_state(c, task_id)
        assert t["status"] == "waiting_confirmation", t["error"]
        assert "event: step" in events and "tool_start" in events and "event: idle" in events
        assert c.get(f"/api/tasks/{task_id}/records").json() == []
        assert (
            c.post(
                f"/api/tasks/{task_id}/respond",
                json={"kind": "confirm", "proposal_id": "wrong", "version": 1},
            ).status_code
            == 409
        )
        p = t["proposal"]
        d = {"kind": "confirm", "proposal_id": p["proposal_id"], "version": p["version"]}
        assert c.post(f"/api/tasks/{task_id}/respond", json=d).status_code == 202
        done, _ = stream_state(c, task_id)
        assert done["status"] == "completed", done["error"]
        assert len(c.get(f"/api/tasks/{task_id}/records").json()) == 1
        bob = {"Authorization": "Bearer " + app.state.credentials["bob"]}
        assert c.get(f"/api/tasks/{task_id}", headers=bob).status_code == 409
        assert (
            c.post("/api/demo/simulate", json={"order_id": "ORD10002", "stock": 0}).status_code
            == 403
        )
        admin = {"Authorization": "Bearer " + app.state.credentials["admin"]}
        assert (
            c.post(
                "/api/demo/simulate", headers=admin, json={"order_id": "ORD10002", "stock": 0}
            ).status_code
            == 200
        )
        assert "text/html" in c.get("/").headers["content-type"]
        assert c.get("/static/app.js").status_code == 200


def test_api_restart_waiting(tmp_path):
    settings = Settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as c:
        token = app.state.credentials["alice"]
        c.post("/api/login", json={"token": token})
        task_id = c.post("/api/tasks", json={"message": "ORD10003 取消"}).json()["task_id"]
        before, _ = stream_state(c, task_id)
    app = create_app(settings)
    with TestClient(app) as c:
        c.post("/api/login", json={"token": token})
        assert c.get(f"/api/tasks/{task_id}").json()["proposal"] == before["proposal"]
        p = before["proposal"]
        c.post(
            f"/api/tasks/{task_id}/respond",
            json={"kind": "confirm", "proposal_id": p["proposal_id"], "version": p["version"]},
        )
        done, _ = stream_state(c, task_id)
        assert done["status"] == "completed"
