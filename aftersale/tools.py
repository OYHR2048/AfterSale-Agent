import asyncio
import time

from aftersale.db import DomainError
from aftersale.eligibility import qualify
from aftersale.models import EligibilityInput, OrderInput, SearchInput, StockInput

CARDS = {
    "get_order": (OrderInput, "查询当前身份订单：金额、日期、状态；不存在/越权同样拒绝"),
    "get_items": (OrderInput, "读取当前身份订单明细：SKU、定制标志、数量"),
    "get_logistics": (OrderInput, "读取物流观察，可能与订单冲突"),
    "get_inventory": (StockInput, "只读库存快照；不预留、不承诺换货"),
    "search_policy": (SearchInput, "检索虚构政策全文、ID、版本、来源"),
    "check_eligibility": (
        EligibilityInput,
        "确定性比较允许动作，返回缺口/库存需求；不创建业务记录",
    ),
}
WHITELISTS = {
    "EvidenceAgent": {"get_order", "get_items", "get_logistics", "get_inventory", "search_policy"},
    "ResolutionAgent": {"check_eligibility"},
    "SingleAgent": set(CARDS),
}


class BudgetExceeded(DomainError):
    pass


def consume(state, limit):
    state["steps"] += 1
    if state["steps"] > limit:
        raise BudgetExceeded("全局步骤预算耗尽")


class ToolRunner:
    def __init__(self, db, policy, settings):
        self.db, self.policy, self.settings = db, policy, settings
        self.failures = {}
        self.delays = {}

    async def _execute(self, name, args, owner):
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            raise TimeoutError("Injected local tool failure")
        if self.delays.get(name):
            await asyncio.sleep(self.delays[name])
        if name == "search_policy":
            return {
                "citations": await self.policy.search(args["query"]),
                "backend": "local_lexical" if not self.policy.url else "remote_validated",
            }
        if name == "get_inventory":
            return self.db.stock(args["sku"])
        o = self.db.order(args["order_id"], owner)
        if name == "get_order":
            return o
        if name == "get_items":
            return {"sku": o["sku"], "customized": bool(o["customized"]), "quantity": 1}
        if name == "get_logistics":
            return {
                "status": o["logistics"],
                "source": "local_mock_logistics",
                "revision": o["revision"],
            }
        if name == "check_eligibility":
            return qualify(o, args)
        raise DomainError("Unknown tool")

    async def call(self, state, role, name, args):
        if name not in WHITELISTS[role]:
            raise DomainError("工具不在角色白名单")
        parsed = CARDS[name][0].model_validate(args).model_dump()
        # Binding prevents models silently switching the user's target order/SKU.
        if parsed.get("order_id") and parsed["order_id"] != state["inputs"].get("order_id"):
            raise DomainError("工具订单与任务目标不一致")
        if name == "get_inventory" and parsed["sku"] != state["facts"].get("order", {}).get("sku"):
            raise DomainError("库存 SKU 与已验证订单不一致")
        if name == "check_eligibility":
            expected = {
                k: state["inputs"].get(k) for k in ("order_id", "reason", "proof", "sealed")
            }
            if parsed != expected:
                raise DomainError("资格参数必须来自用户补充，不能由模型改写")
        for attempt in range(self.settings.retries + 1):
            consume(state, self.settings.max_steps)
            self.db.save_task(state)
            start = time.perf_counter()
            self.db.event(
                state,
                role,
                "tool_start",
                {
                    "tool": name,
                    "attempt": attempt + 1,
                    "args": {
                        k: ("[redacted text]" if k in {"proof", "query"} else v)
                        for k, v in parsed.items()
                    },
                },
            )
            try:
                output = await asyncio.wait_for(
                    self._execute(name, parsed, state["owner"]), self.settings.tool_timeout
                )
                self.db.event(
                    state,
                    role,
                    "tool_end",
                    {
                        "tool": name,
                        "ok": True,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                        "summary": list(output),
                        "data_source": "simulated_business"
                        if name != "search_policy"
                        else "fictional_policy",
                    },
                )
                return output
            except (TimeoutError, http_error_types()) as exc:
                self.db.event(
                    state,
                    role,
                    "tool_error",
                    {
                        "tool": name,
                        "error": type(exc).__name__,
                        "attempt": attempt + 1,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                    },
                )
                if attempt == self.settings.retries:
                    raise DomainError("工具重试耗尽：" + name) from exc
            except Exception:
                self.db.event(
                    state,
                    role,
                    "tool_error",
                    {"tool": name, "error": "validation_or_business_error", "attempt": attempt + 1},
                )
                raise


def http_error_types():
    import httpx

    return httpx.HTTPError
