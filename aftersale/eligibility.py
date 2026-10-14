from datetime import date

from aftersale.models import Candidate


def qualify(order, inputs, stock=None):
    """Deterministic rules backed by the fictional policy files, not an LLM verdict."""
    reason = inputs.get("reason", "unknown")
    proof = inputs.get("proof", "")
    result = {
        "options": [],
        "gaps": [],
        "needs": [],
        "required_policies": ["operations.md"],
        "conflicts": [],
    }
    days = (
        (date.today() - date.fromisoformat(order["delivered_at"])).days
        if order["delivered_at"]
        else None
    )

    def add(action, payer, conditions=None):
        result["options"].append(
            Candidate(
                action=action,
                order_id=order["id"],
                amount_cents=order["amount_cents"],
                shipping_payer=payer,
                reason=reason,
                evidence_ids=[],
                conditions=conditions or [],
            ).model_dump()
        )

    if order["status"] in {"cancelled", "aftersale_requested"}:
        result["gaps"] = ["订单已终结或已有售后，请查询已有结果"]
        return result
    conflict = (order["status"] == "delivered" and order["logistics"] != "delivered") or (
        order["status"] == "shipped" and order["logistics"] == "delivered"
    )
    if conflict or order["logistics"] == "stalled_72h" or reason == "logistics":
        result["required_policies"] += ["logistics_exception.md", "shipping.md"]
        result["conflicts"] = ["订单与物流状态不一致"] if conflict else []
        add("manual_ticket", "review", ["人工核实物流，不承诺退款"])
    elif reason == "cancel":
        if order["status"] == "paid":
            add("cancel", "buyer")
        else:
            add("manual_ticket", "review", ["已发货不能取消，咨询拦截/后续退货"])
    elif reason == "unknown":
        result["gaps"] = ["请补充售后原因：质量问题、无理由、取消或物流异常"]
    elif order["status"] != "delivered":
        add("manual_ticket", "review", ["尚未签收，人工核实处理方式"])
    elif reason == "quality":
        result["required_policies"] += [
            "return_policy.md",
            "return_custom.md",
            "refund_timeline.md",
        ]
        if not proof:
            result["gaps"] = ["请提供质量问题描述及凭证声明，例如：照片显示杯身破损"]
        elif days is not None and 0 <= days <= 15:
            add("return_refund", "platform", ["凭证仍须后续人工审核，受理不代表退款到账"])
            if stock is None:
                result["needs"] = ["inventory"]
            elif stock["quantity"] > 0:
                add("exchange", "platform", ["事务内预留一个库存；后续审核/寄回"])
            else:
                add("manual_ticket", "review", ["换货缺库存，可申请退货退款或人工协调"])
        else:
            add("manual_ticket", "review", ["超出质量问题演示窗口，人工复核"])
    elif reason == "no_reason":
        result["required_policies"] += ["return_policy.md", "return_custom.md"]
        if order["customized"]:
            add("manual_ticket", "review", ["定制商品不适用无理由，人工复核不承诺批准"])
        elif inputs.get("sealed") is None:
            result["gaps"] = ["请说明商品是否未拆封且完好、不影响二次销售"]
        elif inputs["sealed"] and days is not None and 1 <= days <= 7:
            add("return_refund", "buyer")
            if stock is None:
                result["needs"] = ["inventory"]
            elif stock["quantity"] > 0:
                add("exchange", "buyer")
        else:
            add("manual_ticket", "review", ["不满足无理由条件，可人工复核"])
    return result
