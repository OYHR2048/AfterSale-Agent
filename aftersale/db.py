import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from aftersale.config import PACKAGE


class DomainError(Exception):
    pass


def new_id(prefix):
    return prefix + uuid.uuid4().hex[:20]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.active_runs = {}
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders(
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
              sku TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),
              customized INTEGER NOT NULL, delivered_at TEXT, logistics TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity>=0), revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, state_json TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), version INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, expires_at REAL NOT NULL, result TEXT, UNIQUE(task_id,version));
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id), order_id TEXT NOT NULL UNIQUE REFERENCES orders(id), action TEXT NOT NULL, business_status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS confirmations(proposal_id TEXT PRIMARY KEY REFERENCES proposals(id), owner TEXT NOT NULL, version INTEGER NOT NULL, confirmed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            if not c.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
                seed = json.loads((PACKAGE / "data/orders.json").read_text(encoding="utf-8"))
                today = date.today()
                for o in seed:
                    delivered = (
                        (today - timedelta(days=o["days"])).isoformat()
                        if o["days"] is not None
                        else None
                    )
                    c.execute(
                        "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,1)",
                        (
                            o["id"],
                            o["owner"],
                            o["status"],
                            o["sku"],
                            o["amount_cents"],
                            o["customized"],
                            delivered,
                            o["logistics"],
                        ),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO inventory VALUES(?,?,1)", (o["sku"], o["stock"])
                    )

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def order(self, order_id, owner, conn=None):
        if conn is None:
            with self.connect() as c:
                return self.order(order_id, owner, c)
        row = conn.execute(
            "SELECT * FROM orders WHERE id=? AND owner=?", (order_id, owner)
        ).fetchone()
        if row is None:
            raise DomainError("订单不存在或不属于当前身份")
        return dict(row)

    def stock(self, sku, conn=None):
        if conn is None:
            with self.connect() as c:
                return self.stock(sku, c)
        row = conn.execute("SELECT * FROM inventory WHERE sku=?", (sku,)).fetchone()
        if row is None:
            raise DomainError("库存资料缺失")
        return dict(row)

    def create_task(self, owner, state):
        with self.connect() as c:
            c.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?)",
                (state["task_id"], owner, "running", dumps(state), time.time()),
            )

    def save_task(self, state):
        with self.connect() as c:
            c.execute(
                "UPDATE tasks SET status=?,state_json=? WHERE id=?",
                (state["status"], dumps(state), state["task_id"]),
            )

    def task(self, task_id, owner):
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)
            ).fetchone()
        if row is None:
            raise DomainError("任务不存在或不属于当前身份")
        return json.loads(row["state_json"])

    def event(self, state, agent, kind, payload):
        state["run_id"] = self.active_runs.get(state["task_id"], state["run_id"])
        with self.connect() as c:
            c.execute(
                "INSERT INTO events(task_id,run_id,agent,kind,payload,created_at) VALUES(?,?,?,?,?,?)",
                (state["task_id"], state["run_id"], agent, kind, dumps(payload), time.time()),
            )

    def events(self, task_id, owner, after=0):
        self.task(task_id, owner)
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, after)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def propose(self, state, candidate, fingerprint):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Replay before checkpoint completion returns the same active proposal.
            row = c.execute(
                "SELECT * FROM proposals WHERE task_id=? AND status='pending'", (state["task_id"],)
            ).fetchone()
            if row:
                return json.loads(row["body"])
            version = c.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE task_id=?",
                (state["task_id"],),
            ).fetchone()[0]
            body = {
                "proposal_id": new_id("P_"),
                "version": version,
                "candidate": candidate,
                "facts": state["facts"],
                "citations": state["evidence"],
                "inputs": state["inputs"],
                "policy_fingerprint": fingerprint,
                "expires_at": time.time() + state["ttl"],
            }
            c.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,NULL)",
                (
                    body["proposal_id"],
                    state["task_id"],
                    version,
                    "pending",
                    dumps(body),
                    body["expires_at"],
                ),
            )
        return body

    def invalidate(self, task_id, status="superseded"):
        with self.connect() as c:
            c.execute(
                "UPDATE proposals SET status=? WHERE task_id=? AND status='pending'",
                (status, task_id),
            )

    def proposal(self, task_id, proposal_id, owner):
        self.task(task_id, owner)
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM proposals WHERE id=? AND task_id=?", (proposal_id, task_id)
            ).fetchone()
        if row is None:
            raise DomainError("方案不存在")
        return dict(row)

    def records(self, task_id):
        with self.connect() as c:
            return [
                json.loads(r[0])
                for r in c.execute("SELECT body FROM records WHERE task_id=?", (task_id,))
            ]

    def simulate(self, order_id, *, status=None, stock=None, logistics=None):
        """Local demo mutation, invoked only by authenticated development controls."""
        with self.connect() as c:
            if status:
                c.execute(
                    "UPDATE orders SET status=?,revision=revision+1 WHERE id=?", (status, order_id)
                )
            if logistics:
                c.execute(
                    "UPDATE orders SET logistics=?,revision=revision+1 WHERE id=?",
                    (logistics, order_id),
                )
            if stock is not None:
                c.execute(
                    "UPDATE inventory SET quantity=?,revision=revision+1 WHERE sku=(SELECT sku FROM orders WHERE id=?)",
                    (stock, order_id),
                )
