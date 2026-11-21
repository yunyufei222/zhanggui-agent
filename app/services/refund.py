"""退款申请的规则层（T2.9）。

路由和 agent 都只能走这里：同一个校验、同一段插入 SQL。
之前这些规则写在路由函数里，agent 想复用只能复制一遍——复制的那一刻，
两条路径的规则就开始分叉了。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..database import db_cursor, fetch_one
from ..errors import bad_request, conflict, not_found
from ..utils import local_now, make_no

REFUND_TYPES = {"personal_reason", "course_unsatisfied", "schedule_conflict", "duplicate_purchase"}


def get_item(user_id: int, order_item_id: int) -> dict[str, Any]:
    item = fetch_one(
        "SELECT * FROM order_item WHERE id = %s AND user_id = %s",
        (order_item_id, user_id),
    )
    if item is None:
        raise not_found("ORDER_ITEM_NOT_FOUND", "订单明细不存在")
    return item


def create_refund(
    user_id: int,
    order_item_id: int,
    refund_type: str,
    refund_reason: str,
    apply_amount: Any,
    remark: str = "",
) -> dict[str, Any]:
    if refund_type not in REFUND_TYPES:
        raise bad_request("INVALID_REFUND_TYPE", "退款类型不合法")
    amount = Decimal(str(apply_amount))
    if amount <= 0:
        raise bad_request("INVALID_REFUND_AMOUNT", "申请退款金额必须大于 0")

    item = get_item(user_id, order_item_id)
    if item["order_item_status"] not in {"paid", "completed"}:
        raise conflict("ORDER_ITEM_NOT_REFUNDABLE", "订单明细当前状态不允许退款")
    if amount > Decimal(str(item["payable_amount"])):
        raise conflict("REFUND_AMOUNT_EXCEEDED", "申请退款金额超过可退金额")

    payment = fetch_one(
        """
        SELECT id FROM payment_record
        WHERE order_id = %s AND payment_status IN ('paid', 'partial_refunded', 'refunded')
        ORDER BY id DESC LIMIT 1
        """,
        (item["order_id"],),
    )
    if payment is None:
        raise conflict("PAYMENT_NOT_FOUND", "订单没有可退款支付记录")

    in_progress = fetch_one(
        "SELECT id FROM refund_request WHERE order_item_id = %s AND refund_status IN ('pending', 'approved') LIMIT 1",
        (order_item_id,),
    )
    if in_progress is not None:
        raise conflict("REFUND_IN_PROGRESS", "该订单明细存在处理中的退款申请")

    now = local_now()
    with db_cursor() as (_, cursor):
        cursor.execute(
            """
            INSERT INTO refund_request (
                institution_id, refund_no, order_id, order_item_id, payment_id,
                user_id, student_id, refund_type, refund_reason, refund_status,
                apply_amount, approved_amount, approver_user_id, remark, yn,
                applied_at, approved_at, refunded_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending',
                %s, NULL, NULL, %s, 1, %s, NULL, NULL, %s, %s)
            """,
            (
                item["institution_id"],
                make_no("RF"),
                item["order_id"],
                order_item_id,
                payment["id"],
                user_id,
                item["student_id"],
                refund_type,
                refund_reason,
                amount,
                remark,
                now,
                now,
                now,
            ),
        )
        refund_id = int(cursor.lastrowid)
    row = fetch_one("SELECT refund_no FROM refund_request WHERE id = %s", (refund_id,))
    return {
        "refundRequestId": refund_id,
        "refundNo": row["refund_no"] if row else None,
        "refundStatusCode": "pending",
        "applyAmount": float(amount),
        "orderItemId": order_item_id,
    }
