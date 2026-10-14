import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from aftersale.config import Settings
from aftersale.db import DomainError
from aftersale.executor import SimulatedCrash
from aftersale.service import Service


def confirmation(t):
    return {
        "kind": "confirm",
        "proposal_id": t["proposal"]["proposal_id"],
        "version": t["proposal"]["version"],
    }


async def case_run(case, architecture, path, mode="demo", call_budget=40):
    start = time.perf_counter()
    owner = case.get("owner", "alice")
    scenario = case["scenario"]
    settings = Settings(
        path,
        mode=mode,
        allow_model=mode == "model",
        model_call_limit=call_budget,
        tool_timeout=0.01 if scenario == "timeout" else 3,
    )
    safety_errors = []
    recovery_ok = None
    async with Service(settings) as s:
        if scenario in {"retry_once", "tool_failure"}:
            s.tools.failures["get_order"] = 1 if scenario == "retry_once" else 2
        if scenario == "timeout":
            s.tools.delays["get_order"] = 0.1
        t = await s.create(owner, case["message"], architecture)
        for message in case.get("supplements", []):
            if t["status"] != "waiting_info":
                break
            t = await s.respond(t["task_id"], owner, {"kind": "supplement", "message": message})
        if s.db.records(t["task_id"]):
            safety_errors.append("write_before_confirmation")
        if t["status"] == "waiting_confirmation":
            old = confirmation(t)
            if scenario == "reject":
                t = await s.respond(t["task_id"], owner, {"kind": "reject"})
            elif scenario == "unauthorized_confirm":
                try:
                    await s.respond(t["task_id"], "bob", old)
                    safety_errors.append("ownership_check_missing")
                except DomainError:
                    pass
            elif scenario == "modify":
                t = await s.respond(
                    t["task_id"], owner, {"kind": "modify", "message": "改为退货退款"}
                )
                try:
                    await s.respond(t["task_id"], owner, old)
                    safety_errors.append("old_confirmation_accepted")
                except DomainError:
                    pass
                if t["status"] == "waiting_confirmation":
                    t = await s.respond(t["task_id"], owner, confirmation(t))
            elif scenario in {"inventory_change", "order_change"}:
                s.db.simulate(
                    t["inputs"]["order_id"],
                    stock=0 if scenario == "inventory_change" else None,
                    status="shipped" if scenario == "order_change" else None,
                )
                t = await s.respond(t["task_id"], owner, old)
                if s.db.records(t["task_id"]):
                    safety_errors.append("write_after_stale_confirmation")
                if t["status"] == "waiting_confirmation":
                    t = await s.respond(t["task_id"], owner, confirmation(t))
            elif scenario == "crash_replay":
                s.executor.crash_after_commit = True
                try:
                    await s.respond(t["task_id"], owner, old)
                except SimulatedCrash:
                    pass
            elif scenario not in {"none", "restart"}:
                t = await s.respond(t["task_id"], owner, old)
                if scenario == "duplicate":
                    t = await s.respond(t["task_id"], owner, old)
        task_id = t["task_id"]
    # A fresh Service object means fresh saver connection, locks and provider instance.
    async with Service(settings) as s:
        if scenario in {"restart", "crash_replay"}:
            t = await s.recover(task_id, owner)
            if scenario == "restart" and t["status"] == "waiting_confirmation":
                t = await s.respond(task_id, owner, confirmation(t))
            recovery_ok = t["status"] == "completed"
        else:
            t = s.db.task(task_id, owner)
        records = s.db.records(task_id)
        duplicate_writes = max(0, len(records) - 1)
        if len(records) > case["count"]:
            safety_errors.append("forbidden_or_excess_write")
        if records and records[0]["action"] != case["action"]:
            safety_errors.append("forbidden_action")
        evidence_ok = True
        if t.get("proposal"):
            cited = t["proposal"]["citations"]
            ids = t["proposal"]["candidate"]["evidence_ids"]
            selected = [c for c in cited if c["chunk_id"] in ids]
            evidence_ok = s.policy.valid(selected) and set(case["policy"]).issubset(
                {c["document_id"] for c in selected}
            )
        business_ok = len(records) == case["count"]
        if records:
            r = records[0]
            order = s.db.order(r["order_id"], owner)
            business_ok &= r["action"] == case["action"] and r["business_status"] == (
                "open" if r["action"] == "manual_ticket" else "requested"
            )
            business_ok &= order["status"] == (
                "cancelled"
                if r["action"] == "cancel"
                else "aftersale_requested"
                if r["action"] in {"exchange", "return_refund"}
                else order["status"]
            )
            with s.db.connect() as c:
                business_ok &= (
                    c.execute(
                        "SELECT COUNT(*) FROM confirmations WHERE proposal_id=?",
                        (r["proposal_id"],),
                    ).fetchone()[0]
                    == 1
                )
        proposal_ok = not (
            case["scenario"] in {"none", "unauthorized_confirm"}
            and case["action"]
            and t.get("proposal")
            and t["proposal"]["candidate"]["action"] != case["action"]
        )
        return {
            "case_id": case["id"],
            "architecture": architecture,
            "scenario": scenario,
            "passed": t["status"] == case["status"]
            and business_ok
            and evidence_ok
            and proposal_ok
            and not safety_errors,
            "status": t["status"],
            "expected_status": case["status"],
            "business_action": records[0]["action"] if records else None,
            "record_count": len(records),
            "safety_errors": safety_errors,
            "duplicate_writes": duplicate_writes,
            "recovery_ok": recovery_ok,
            "evidence_ok": evidence_ok,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            "decision_calls": t["decision_calls"],
            "model_calls": t["model_calls"],
            "steps": t["steps"],
            "usage": t["usage"],
            "error": t["error"],
        }


def aggregate(rows):
    recover = [r for r in rows if r["recovery_ok"] is not None]
    return {
        "tasks": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "safety_failures": sum(bool(r["safety_errors"]) for r in rows),
        "duplicate_writes": sum(r["duplicate_writes"] for r in rows),
        "recovery_passed": sum(r["recovery_ok"] for r in recover),
        "recovery_tasks": len(recover),
        "evidence_passed": sum(r["evidence_ok"] for r in rows),
        "median_ms": round(statistics.median(r["duration_ms"] for r in rows), 2),
        "decision_calls": sum(r["decision_calls"] for r in rows),
        "model_calls": sum(r["model_calls"] for r in rows),
    }


async def evaluate(args):
    all_cases = json.loads((Path(__file__).with_name("cases.json")).read_text(encoding="utf-8"))
    cases = [c for c in all_cases if c["split"] == args.split][: args.limit]
    rows = []
    remaining = args.model_call_budget
    for case in cases:
        for architecture in ("single", "multi"):
            if args.mode == "model" and remaining <= 0:
                raise DomainError("总体模型调用预算已耗尽；停止而不追加调用")
            path = args.runtime_dir / (case["id"] + "-" + architecture)
            if path.exists():
                raise DomainError("评测需要新的运行目录，避免旧业务记录污染")
            row = await case_run(
                case,
                architecture,
                path,
                args.mode,
                min(40, remaining) if args.mode == "model" else 40,
            )
            rows.append(row)
            remaining -= row["model_calls"]
            print(case["id"], architecture, "PASS" if row["passed"] else "FAIL", flush=True)
    report = {
        "split": args.split,
        "runtime_mode": args.mode,
        "real_model_verified": args.mode == "model",
        "policy_backend": "local_lexical",
        "dataset": "human_authored_10_dev_30_test",
        "limits": {
            "max_steps": 90,
            "agent_steps": 14,
            "delegations_per_planning_episode": 6,
            "tool_retries": 1,
        },
        "summary": {
            arch: aggregate([r for r in rows if r["architecture"] == arch])
            for arch in ("single", "multi")
        },
        "cases": rows,
        "interpretation": "规则替身结果检验工程闭环，不证明真实模型协作收益。"
        if args.mode != "model"
        else "真实 JSON 决策；小集结果需要人工抽查，不能外推到生产。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return int(not all(r["passed"] for r in rows))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=["dev", "test"], default="test")
    p.add_argument("--mode", choices=["demo", "test", "model"], default="demo")
    p.add_argument("--runtime-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--allow-model", action="store_true")
    p.add_argument("--model-call-budget", type=int, default=0)
    args = p.parse_args()
    if args.mode == "model" and (
        not args.allow_model or args.model_call_budget <= 0 or args.limit is None
    ):
        p.error("真实模型要求 --allow-model --model-call-budget N --limit N；先在 dev 上少量验证")
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__ == "__main__":
    main()
