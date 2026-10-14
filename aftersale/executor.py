import json
import time

from aftersale.db import DomainError, dumps, new_id
from aftersale.eligibility import qualify


class StateChanged(DomainError):
    pass


class SimulatedCrash(RuntimeError):
    """Test-only crash after the business commit and before the graph checkpoint."""


class Executor:
    def __init__(self, db, policy):
        self.db, self.policy = db, policy
        self.crash_after_commit = False

    def execute(self, state):
        d = state["decision"]
        p = state["proposal"]
        if (
            d.get("kind") != "confirm"
            or d.get("proposal_id") != p["proposal_id"]
            or d.get("version") != p["version"]
        ):
            raise DomainError("确认没有绑定当前具体方案")
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = c.execute("SELECT owner FROM tasks WHERE id=?", (state["task_id"],)).fetchone()
            if task is None or task[0] != state["owner"]:
                raise DomainError("任务身份不一致")
            row = c.execute(
                "SELECT * FROM proposals WHERE id=? AND task_id=?",
                (p["proposal_id"], state["task_id"]),
            ).fetchone()
            if row is None or row["version"] != d["version"]:
                raise DomainError("方案版本不一致")
            if row["status"] == "executed":
                return json.loads(row["result"])
            if row["status"] != "pending":
                raise DomainError("旧方案已失效")
            saved = json.loads(row["body"])
            if saved != p:
                raise DomainError("方案参数与服务端保存值不一致")
            if row["expires_at"] < time.time():
                raise StateChanged("方案过期，需要重新确认")
            if saved["policy_fingerprint"] != self.policy.fingerprint() or not self.policy.valid(
                saved["citations"]
            ):
                raise StateChanged("政策发生变化或引用失效")
            candidate = saved["candidate"]
            order = self.db.order(candidate["order_id"], state["owner"], c)
            if order != saved["facts"]["order"]:
                raise StateChanged("订单状态或事实发生变化")
            stock = self.db.stock(order["sku"], c)
            if candidate["action"] == "exchange" and stock != saved["facts"].get("inventory"):
                raise StateChanged("库存快照发生变化")
            permitted = qualify(order, saved["inputs"], stock)
            match = [x for x in permitted["options"] if x["action"] == candidate["action"]]
            if not match or any(
                candidate[k] != match[0][k]
                for k in ("order_id", "amount_cents", "shipping_payer", "reason", "conditions")
            ):
                raise StateChanged("当前业务约束不允许该动作")
            cited = {x["chunk_id"]: x["document_id"] for x in saved["citations"]}
            if (
                not candidate["evidence_ids"]
                or not set(candidate["evidence_ids"]).issubset(cited)
                or not set(permitted["required_policies"]).issubset(
                    {cited[k] for k in candidate["evidence_ids"]}
                )
            ):
                raise DomainError("方案缺少必要政策证据")
            if c.execute("SELECT 1 FROM records WHERE order_id=?", (order["id"],)).fetchone():
                raise DomainError("该订单已有业务记录，请查询已有申请")
            # Confirmation, reservation, result, record are one atomic commit.
            c.execute(
                "INSERT INTO confirmations VALUES(?,?,?,?)",
                (p["proposal_id"], state["owner"], p["version"], time.time()),
            )
            action = candidate["action"]
            if action == "exchange":
                changed = c.execute(
                    "UPDATE inventory SET quantity=quantity-1,revision=revision+1 WHERE sku=? AND quantity>0 AND revision=?",
                    (order["sku"], stock["revision"]),
                ).rowcount
                if changed != 1:
                    raise StateChanged("库存预留失败")
            record = {
                "record_id": new_id("LG_" if action == "manual_ticket" else "AS_"),
                "task_id": state["task_id"],
                "proposal_id": p["proposal_id"],
                "order_id": order["id"],
                "action": action,
                "business_status": "open" if action == "manual_ticket" else "requested",
                "amount_cents": order["amount_cents"],
                "shipping_payer": candidate["shipping_payer"],
                "notice": "模拟申请已受理；不代表审核通过、退款完成或到账",
            }
            c.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                (
                    record["record_id"],
                    state["task_id"],
                    p["proposal_id"],
                    order["id"],
                    action,
                    record["business_status"],
                    dumps(record),
                ),
            )
            # Only cancellation changes the order to cancelled. Other requests remain pending.
            if action in {"exchange", "return_refund", "cancel"}:
                c.execute(
                    "UPDATE orders SET status=?,revision=revision+1 WHERE id=?",
                    ("cancelled" if action == "cancel" else "aftersale_requested", order["id"]),
                )
            c.execute(
                "UPDATE proposals SET status='executed',result=? WHERE id=?",
                (dumps(record), p["proposal_id"]),
            )
        if self.crash_after_commit:
            self.crash_after_commit = False
            raise SimulatedCrash("Injected crash after commit")
        return record
