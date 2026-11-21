"""退款申请技能（T2.9）：自动取 orderItemId，规则全部走 app.services.refund。

为什么不自己写 SQL：退款有四道闸（明细状态、金额上限、有无支付记录、是否在处理中），
路由已经有一份；这里再抄一份，两边迟早不一致。所以规则只在 services 里，路由和 agent 都调它。
"""

from __future__ import annotations

from typing import Any

from app.database import fetch_all
from app.errors import AppError
from app.services.refund import create_refund

from .order import STATUS_CN

TYPE_HINTS = (
    ("不满", "course_unsatisfied"),
    ("不好", "course_unsatisfied"),
    ("时间冲突", "schedule_conflict"),
    ("没时间", "schedule_conflict"),
    ("重复", "duplicate_purchase"),
)


def items_of(user_id: int, order_no: str) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT i.id, i.item_name, i.order_item_status, i.payable_amount
        FROM order_item AS i
        JOIN `order` AS o ON o.id = i.order_id
        WHERE o.order_no = %s AND o.user_id = %s
        ORDER BY i.id
        """,
        (order_no, user_id),
    )


def _refund_type(message: str) -> str:
    for word, kind in TYPE_HINTS:
        if word in message:
            return kind
    return "personal_reason"


def apply(user_id: int, order_no: str, message: str) -> dict[str, Any]:
    items = items_of(user_id, order_no)
    if not items:
        return {"ok": False, "reply": f"没查到订单号 {order_no} 下的明细。"}

    item = None
    if len(items) == 1:
        item = items[0]
    else:
        for candidate in items:
            if candidate["item_name"] and candidate["item_name"] in message:
                item = candidate
                break
        if item is None:
            names = "、".join(str(row["item_name"]) for row in items)
            return {
                "ok": False,
                "need": "orderItem",
                "reply": f"这一单有 {len(items)} 门课，退哪一门？{names}",
            }

    status = str(item["order_item_status"])
    if status not in {"paid", "completed"}:
        cn = STATUS_CN.get(status, status)
        return {
            "ok": False,
            "reply": f"「{item['item_name']}」当前是 {cn}，还不能退款——只有已支付或已完成的明细可以申请。",
        }

    try:
        result = create_refund(
            user_id,
            int(item["id"]),
            _refund_type(message),
            message[:200],
            item["payable_amount"],
        )
    except AppError as error:
        return {"ok": False, "reply": error.message}

    return {
        "ok": True,
        "refundNo": result["refundNo"],
        "amount": result["applyAmount"],
        "reply": (
            f"已提交退款申请 {result['refundNo']}，金额 {result['applyAmount']:.2f} 元，"
            "我们会在 1 个工作日内审核，退款按原路退回。"
        ),
    }
