import asyncio
import json

import httpx
import pytest

from aftersale.config import Settings
from aftersale.providers import Intent
from aftersale.service import Service


def test_model_http_protocol_with_mock_transport(tmp_path, monkeypatch):
    """Mock HTTP only; this MUST NOT be counted as real model verification."""
    monkeypatch.setenv("AFTERSALE_MODEL_API_KEY", "unit-test-placeholder")
    replies = [
        Intent(order_id="ORD10003", reason="cancel", desired="cancel").model_dump(),
        {"kind": "tool", "tool": "get_order", "args": {"order_id": "ORD10003"}},
        {"kind": "tool", "tool": "get_items", "args": {"order_id": "ORD10003"}},
        {"kind": "tool", "tool": "get_logistics", "args": {"order_id": "ORD10003"}},
        {"kind": "tool", "tool": "search_policy", "args": {"query": "未发货取消政策"}},
        {
            "kind": "tool",
            "tool": "check_eligibility",
            "args": {"order_id": "ORD10003", "reason": "cancel", "proof": "", "sealed": None},
        },
        {"kind": "finish"},
    ]
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(replies.pop(0))}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )

    async def case():
        async with Service(Settings(tmp_path, mode="model", allow_model=True)) as s:
            t = await s.create("alice", "ORD10003 取消", "single")
            assert t["status"] == "waiting_confirmation", t["error"]
            assert t["model_calls"] == 7
            assert len(t["usage"]) == 7
            assert not s.db.records(t["task_id"])

    asyncio.run(case())
    assert len(requests) == 7


def test_model_failure_counts_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERSALE_MODEL_API_KEY", "unit-test-placeholder")
    original = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(401, json={"error": "provider may echo confidential data"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )

    async def case():
        async with Service(
            Settings(tmp_path, mode="model", allow_model=True, model_call_limit=1)
        ) as s:
            t = await s.create("alice", "ORD10003 取消", "single")
            assert t["status"] == "failed"
            assert t["model_calls"] == 1
            assert t["error"] == "HTTPStatusError"
            assert "confidential" not in json.dumps(s.db.events(t["task_id"], "alice"))
            recovered = await s.recover(t["task_id"], "alice")
            assert recovered["model_calls"] == 1
            assert not s.db.records(t["task_id"])

    asyncio.run(case())


def test_real_mode_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ValueError, match="explicit"):
        Settings(tmp_path, mode="model").prepare()
