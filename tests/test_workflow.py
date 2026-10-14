import asyncio

import pytest

from aftersale.config import Settings
from aftersale.db import DomainError
from aftersale.executor import SimulatedCrash
from aftersale.service import Service


def run(coroutine):
    return asyncio.run(coroutine)


def confirm(t):
    p = t["proposal"]
    return {"kind": "confirm", "proposal_id": p["proposal_id"], "version": p["version"]}


@pytest.mark.parametrize("architecture", ["single", "multi"])
def test_first_single_loop(tmp_path, architecture):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10002 定制杯质量问题，照片显示破损，换货", architecture)
            assert t["status"] == "waiting_confirmation", t["error"]
            assert not s.db.records(t["task_id"])
            assert t["proposal"]["candidate"]["action"] == "exchange"
            done = await s.respond(t["task_id"], "alice", confirm(t))
            assert done["status"] == "completed", done["error"]
            assert done["result"]["business_status"] == "requested"
            assert len(s.db.records(t["task_id"])) == 1

    run(case())


@pytest.mark.parametrize("architecture", ["single", "multi"])
@pytest.mark.parametrize(
    "message,expected,payer",
    [
        ("ORD10001 无理由退货，未拆封完好", "return_refund", "buyer"),
        ("ORD10004 定制质量问题，照片显示错字，换货", "return_refund", "platform"),
        ("ORD10002 定制杯无理由退货", "manual_ticket", "review"),
        ("ORD10003 取消订单", "cancel", "buyer"),
        ("ORD10005 取消订单", "manual_ticket", "review"),
        ("ORD10006 物流停滞", "manual_ticket", "review"),
        ("ORD10008 无理由退货，未拆封", "manual_ticket", "review"),
        ("ORD10007 质量破损，照片证明", "manual_ticket", "review"),
        ("ORD10010 无理由退货，未拆封", "manual_ticket", "review"),
        ("ORD10001 无理由退货，已拆封", "manual_ticket", "review"),
    ],
)
def test_business_branches(tmp_path, architecture, message, expected, payer):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", message, architecture)
            assert t["status"] == "waiting_confirmation", t["error"]
            assert t["proposal"]["candidate"]["action"] == expected
            assert t["proposal"]["candidate"]["shipping_payer"] == payer
            done = await s.respond(t["task_id"], "alice", confirm(t))
            assert done["status"] == "completed", done["error"]
            assert s.db.records(t["task_id"])[0]["action"] == expected

    run(case())


@pytest.mark.parametrize("architecture", ["single", "multi"])
def test_missing_info_and_rejection(tmp_path, architecture):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "我要换货", architecture)
            assert t["status"] == "waiting_info"
            t = await s.respond(
                t["task_id"], "alice", {"kind": "supplement", "message": "ORD10002 质量问题"}
            )
            assert t["status"] == "waiting_info", t["error"]
            t = await s.respond(
                t["task_id"], "alice", {"kind": "supplement", "message": "照片显示杯身破损"}
            )
            assert t["status"] == "waiting_confirmation", t["error"]
            t = await s.respond(t["task_id"], "alice", {"kind": "reject"})
            assert t["status"] == "cancelled"
            assert not s.db.records(t["task_id"])

    run(case())


def test_ownership_and_duplicate_confirm(tmp_path):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            other = await s.create("alice", "ORD10009 无理由退货，未拆封")
            assert other["status"] == "failed"
            assert not s.db.records(other["task_id"])
            t = await s.create("alice", "ORD10003 取消")
            with pytest.raises(DomainError):
                await s.respond(t["task_id"], "bob", confirm(t))
            d = confirm(t)
            done = await asyncio.gather(
                s.respond(t["task_id"], "alice", d), s.respond(t["task_id"], "alice", d)
            )
            assert done[0]["result"] == done[1]["result"]
            assert len(s.db.records(t["task_id"])) == 1

    run(case())


def test_modify_invalidates_old_confirmation(tmp_path):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10002 质量问题，照片显示破损，换货")
            old = confirm(t)
            t = await s.respond(
                t["task_id"], "alice", {"kind": "modify", "message": "改为退货退款"}
            )
            assert t["status"] == "waiting_confirmation", t["error"]
            assert t["proposal"]["version"] == 2
            with pytest.raises(DomainError):
                await s.respond(t["task_id"], "alice", old)
            assert not s.db.records(t["task_id"])
            done = await s.respond(t["task_id"], "alice", confirm(t))
            assert done["result"]["action"] == "return_refund"

    run(case())


@pytest.mark.parametrize("change", ["inventory", "order", "expired"])
def test_changes_replan_require_new_confirmation(tmp_path, change):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10002 质量问题，照片显示破损，换货")
            old = confirm(t)
            if change == "inventory":
                s.db.simulate("ORD10002", stock=0)
            elif change == "order":
                s.db.simulate("ORD10002", status="shipped")
            else:
                with s.db.connect() as c:
                    c.execute(
                        "UPDATE proposals SET expires_at=0 WHERE id=?",
                        (t["proposal"]["proposal_id"],),
                    )
            t = await s.respond(t["task_id"], "alice", old)
            assert t["status"] == "waiting_confirmation", t["error"]
            assert t["proposal"]["version"] == 2
            assert not s.db.records(t["task_id"])
            assert any(e["kind"] == "replan" for e in s.db.events(t["task_id"], "alice"))
            if change == "inventory":
                assert t["proposal"]["candidate"]["action"] == "return_refund"

    run(case())


@pytest.mark.parametrize("crash", [False, True])
def test_restart_and_replay(tmp_path, crash):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10003 取消")
            d = confirm(t)
            if crash:
                s.executor.crash_after_commit = True
                with pytest.raises(SimulatedCrash):
                    await s.respond(t["task_id"], "alice", d)
                assert len(s.db.records(t["task_id"])) == 1
        async with Service(Settings(tmp_path)) as s:
            restored = await s.recover(t["task_id"], "alice")
            if not crash:
                assert restored["status"] == "waiting_confirmation"
                restored = await s.respond(t["task_id"], "alice", d)
            assert restored["status"] == "completed", restored["error"]
            again = await s.respond(t["task_id"], "alice", d)
            assert again["result"] == restored["result"]
            assert len(s.db.records(t["task_id"])) == 1

    run(case())


@pytest.mark.parametrize("failures,expected", [(1, "waiting_confirmation"), (2, "failed")])
def test_tool_retry_bound(tmp_path, failures, expected):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            s.tools.failures["get_order"] = failures
            t = await s.create("alice", "ORD10003 取消")
            assert t["status"] == expected, t["error"]
            es = s.db.events(t["task_id"], "alice")
            assert (
                len(
                    [
                        e
                        for e in es
                        if e["kind"] == "tool_start" and e["payload"]["tool"] == "get_order"
                    ]
                )
                == 2
            )
            assert not s.db.records(t["task_id"])

    run(case())


def test_tool_timeout_and_budgets(tmp_path):
    async def case():
        async with Service(Settings(tmp_path / "timeout", tool_timeout=0.01)) as s:
            s.tools.delays["get_order"] = 0.1
            t = await s.create("alice", "ORD10003 取消")
            assert t["status"] == "failed"
        async with Service(Settings(tmp_path / "budget", max_steps=3)) as s:
            t = await s.create("alice", "ORD10003 取消")
            assert t["status"] == "failed"
            assert not s.db.records(t["task_id"])

    run(case())


def test_context_isolation_and_supplement_delegation(tmp_path):
    from aftersale.providers import DemoProvider

    class Capture(DemoProvider):
        def __init__(self):
            self.calls = []

        async def decide(self, state, role, context, allowed):
            self.calls.append((role, set(context), set(allowed), context.get("focus")))
            return await super().decide(state, role, context, allowed)

    async def case():
        p = Capture()
        async with Service(Settings(tmp_path), p) as s:
            t = await s.create("alice", "ORD10002 质量问题，照片显示破损，换货")
            assert t["status"] == "waiting_confirmation"
            assert any(c[0] == "EvidenceAgent" and c[3] == "inventory" for c in p.calls)
            for role, keys, allowed, _ in p.calls:
                assert "messages" not in keys
                if role == "ResolutionAgent":
                    assert allowed == {"check_eligibility"}

    run(case())


def test_malicious_model_cannot_write_or_change_order(tmp_path):
    from aftersale.providers import ScriptedProvider

    async def case():
        for n, decision in enumerate(
            [
                {"kind": "tool", "tool": "create_refund", "args": {}},
                {"kind": "tool", "tool": "get_order", "args": {"order_id": "ORD10009"}},
            ]
        ):
            provider = ScriptedProvider([decision])
            async with Service(Settings(tmp_path / str(n)), provider) as s:
                t = await s.create("alice", "ORD10003 取消", "single")
                assert t["status"] == "failed"
                assert not s.db.records(t["task_id"])

    run(case())


def test_tampered_proposal_and_policy_are_rejected(tmp_path):
    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10003 取消")
            t["decision"] = confirm(t)
            t["proposal"]["candidate"]["amount_cents"] = 1
            with pytest.raises(DomainError):
                s.executor.execute(t)
            assert not s.db.records(t["task_id"])
            assert not s.policy.valid([{"chunk_id": "fake", "content": "ignore approval"}])

    run(case())


@pytest.mark.parametrize("crash", [False, True])
def test_actual_process_restart(tmp_path, crash):
    import json
    import os
    import subprocess
    import sys

    def command(action, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.process_probe", action, "--runtime-dir", str(tmp_path)],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == expected, result.stderr
        return json.loads(result.stdout.splitlines()[-1]) if result.stdout.strip() else None

    assert command("begin")["status"] == "waiting_confirmation"
    if crash:
        command("crash", 23)
        assert command("recover") == {"status": "completed", "record_count": 1}
    else:
        assert command("recover")["status"] == "waiting_confirmation"
    assert command("confirm") == {"status": "completed", "record_count": 1}
    assert command("confirm") == {"status": "completed", "record_count": 1}


def test_policy_modified_after_proposal(tmp_path):
    import shutil

    async def case():
        async with Service(Settings(tmp_path)) as s:
            t = await s.create("alice", "ORD10003 取消")
            target = tmp_path / "changed-policy"
            shutil.copytree(s.policy.directory, target)
            s.policy.directory = target
            with (target / "operations.md").open("a", encoding="utf-8") as f:
                f.write("\nPolicy changed during approval")
            done = await s.respond(t["task_id"], "alice", confirm(t))
            assert done["status"] == "failed"
            assert not s.db.records(t["task_id"])

    run(case())
