"""Actual process restart/crash probe used by tests; no network or paid model."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from aftersale.config import Settings
from aftersale.service import Service
from evaluation.run import confirmation


async def probe(args):
    marker = args.runtime_dir / "probe.json"
    async with Service(Settings(args.runtime_dir)) as s:
        if args.action == "begin":
            t = await s.create("alice", "ORD10003 取消")
            marker.write_text(
                json.dumps({"task_id": t["task_id"], "confirmation": confirmation(t)}),
                encoding="utf-8",
            )
        else:
            saved = json.loads(marker.read_text(encoding="utf-8"))
            task_id = saved["task_id"]
            if args.action == "crash":
                original = s.executor.execute

                def crash(state):
                    original(state)
                    os._exit(23)

                s.executor.execute = crash
                t = await s.respond(task_id, "alice", saved["confirmation"])
            elif args.action == "recover":
                t = await s.recover(task_id, "alice")
            else:
                t = await s.respond(task_id, "alice", saved["confirmation"])
        print(json.dumps({"status": t["status"], "record_count": len(s.db.records(t["task_id"]))}))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["begin", "confirm", "crash", "recover"])
    p.add_argument("--runtime-dir", required=True, type=Path)
    asyncio.run(probe(p.parse_args()))


if __name__ == "__main__":
    main()
