"""订单查询技能（T2.7）。

取数走 T0.1（按 orderNo 精确查）+ T0.2（详情带 items），话术只做一件事：
把单据上的东西按人话排出来，不加工、不推测。
"""

from __future__ import annotations

from typing import Any

from app.database import fetch_all, fetch_one

STATUS_CN = {
    "pending": "待支付",
    "paid": "已支付",
    "completed": "已完成",
    "cancelled": "已取消",
    "refunded": "已退款",
}


def lookup(user_id: int, order_no: str) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT id, order_no, order_status, total_amount, discount_amount, payable_amount, paid_amount "
        "FROM `order` WHERE order_no = %s AND user_id = %s",
        (order_no, user_id),
    )


def items_of(order_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT item_name, order_item_status, payable_amount, service_period_days "
        "FROM order_item WHERE order_id = %s ORDER BY id",
        (order_id,),
    )


def recent_numbers(user_id: int, limit: int = 3) -> list[str]:
    rows = fetch_all(
        "SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )
    return [row["order_no"] for row in rows]


def describe(order: dict[str, Any], items: list[dict[str, Any]]) -> str:
    status = STATUS_CN.get(str(order["order_status"]), str(order["order_status"]))
    lines = [
        f"订单 {order['order_no']} 状态 {status}（{order['order_status']}），"
        f"应付 {float(order['payable_amount']):.2f} 元，已付 {float(order['paid_amount']):.2f} 元，共 {len(items)} 门："
    ]
    for item in items:
        item_status = STATUS_CN.get(str(item["order_item_status"]), str(item["order_item_status"]))
        lines.append(
            f"· {item['item_name']}（{item_status}）¥{float(item['payable_amount']):.2f}"
            f"，服务期 {int(item['service_period_days'])} 天"
        )
    return "\n".join(lines)


def answer(user_id: int, order_no: str) -> str:
    order = lookup(user_id, order_no)
    if order is None:
        return f"没查到订单号 {order_no} 名下的订单。是不是单号抄错了？"
    return describe(order, items_of(int(order["id"])))


def ask_with_recent(user_id: int) -> str:
    numbers = recent_numbers(user_id)
    hint = ("你最近有：" + "、".join(numbers) + "。") if numbers else "你名下暂时还没有订单。"
    return "请提供订单号（ORD 开头的那个），我好帮你查。" + hint
