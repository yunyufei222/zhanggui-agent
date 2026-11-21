"""工单提交技能（T2.10）。

转人工不该只有一个「已转」的口头承诺：有订单上下文就落一张 `service_ticket`，
把人能拿走的工单号给出去；没有订单上下文（政策类问题）就不硬造——
`service_ticket.order_item_id` 是 NOT NULL，造假单号比不给更糟。

同一个（用户, 明细）已经有未关闭工单时复用，不重复建。
"""

from __future__ import annotations

from typing import Any

from app.database import db_cursor, fetch_one
from app.utils import local_now, make_no

TYPE_CN = {"after_sales": "售后", "complaint": "投诉", "refund": "退款"}
SLA_HOURS = 4


def find_order_item(user_id: int, order_no: str) -> dict[str, Any] | None:
    if not order_no:
        return None
    return fetch_one(
        """
        SELECT i.id, i.institution_id, i.student_id, i.order_id
        FROM order_item AS i
        JOIN `order` AS o ON o.id = i.order_id
        WHERE o.order_no = %s AND o.user_id = %s
        ORDER BY i.id
        LIMIT 1
        """,
        (order_no, user_id),
    )


def open_ticket(
    user_id: int,
    order_no: str,
    title: str,
    content: str,
    ticket_type: str = "after_sales",
    priority: str = "medium",
) -> dict[str, Any] | None:
    """有单就建/复用一张工单；没单返回 None（调用方退回纯对话转人工）。"""
    item = find_order_item(user_id, order_no)
    if item is None:
        return None

    existing = fetch_one(
        """
        SELECT id, ticket_no
        FROM service_ticket
        WHERE user_id = %s AND order_item_id = %s AND ticket_status IN ('pending', 'in_progress')
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, item["id"]),
    )
    if existing is not None:
        return {"ticketId": int(existing["id"]), "ticketNo": existing["ticket_no"], "reused": True}

    now = local_now()
    ticket_no = make_no("TK")
    with db_cursor() as (_, cursor):
        cursor.execute(
            """
            INSERT INTO service_ticket (
                institution_id, ticket_no, user_id, student_id, order_item_id,
                refund_request_id, ticket_type, ticket_source, priority_level,
                ticket_status, assignee_user_id, title, ticket_content, yn,
                first_response_at, closed_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, NULL, %s, 'customer_service', %s,
                'pending', NULL, %s, %s, 1, NULL, NULL, %s, %s)
            """,
            (
                item["institution_id"],
                ticket_no,
                user_id,
                item["student_id"],
                item["id"],
                ticket_type,
                priority,
                title[:255],
                content[:2000],
                now,
                now,
            ),
        )
        ticket_id = int(cursor.lastrowid)
    return {"ticketId": ticket_id, "ticketNo": ticket_no, "reused": False}
