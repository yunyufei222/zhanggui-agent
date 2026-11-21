"""工单流转（T0.5）。

状态机只有三条边，故意做窄：
    pending ──受理──> in_progress ──完结──> closed
                          ^                  │
                          └──────重开────────┘
跳步（待受理直接关闭）一律 409，并且**不改任何字段**——
「先关掉再说」是客服系统里最常见的数据污染。
"""

from __future__ import annotations

from typing import Any

from ..database import db_cursor, fetch_one
from ..errors import conflict, not_found
from ..utils import local_now

STATUS_CN = {"pending": "待受理", "in_progress": "处理中", "closed": "已关闭"}
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress"},
    "in_progress": {"closed"},
    "closed": {"in_progress"},
}
FOLLOW_TYPES = {"reply_user", "status_update", "refund_review", "escalation", "internal_note"}


def get_ticket(ticket_id: int) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM service_ticket WHERE id = %s", (ticket_id,))
    if row is None:
        raise not_found("TICKET_NOT_FOUND", "工单不存在")
    return row


def _check(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise conflict(
            "TICKET_ILLEGAL_TRANSITION",
            f"工单当前是「{STATUS_CN.get(current, current)}」，不能直接变成「{STATUS_CN.get(target, target)}」",
        )


def _follow(cursor, ticket_id: int, operator_id: int, follow_type: str, content: str, result: str | None, now) -> None:
    cursor.execute(
        """
        INSERT INTO service_ticket_follow_record
            (ticket_id, follow_user_id, follow_type, follow_channel, follow_result,
             follow_content, followed_at, created_at, updated_at)
        VALUES (%s, %s, %s, 'web', %s, %s, %s, %s, %s)
        """,
        (ticket_id, operator_id, follow_type, result, content[:1000], now, now, now),
    )


def transition(ticket_id: int, operator_id: int, target: str, remark: str = "") -> dict[str, Any]:
    ticket = get_ticket(ticket_id)
    _check(str(ticket["ticket_status"]), target)

    now = local_now()
    text = remark or f"状态变更为{STATUS_CN[target]}"
    with db_cursor() as (_, cursor):
        if target == "in_progress":
            cursor.execute(
                """
                UPDATE service_ticket
                SET ticket_status = 'in_progress',
                    assignee_user_id = %s,
                    first_response_at = COALESCE(first_response_at, %s),
                    closed_at = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (operator_id, now, now, ticket_id),
            )
        else:
            cursor.execute(
                "UPDATE service_ticket SET ticket_status = 'closed', closed_at = %s, updated_at = %s WHERE id = %s",
                (now, now, ticket_id),
            )
        _follow(cursor, ticket_id, operator_id, "status_update", text, target, now)

    return {"ticketId": ticket_id, "ticketNo": ticket["ticket_no"], "ticketStatus": target}


def add_follow(
    ticket_id: int,
    operator_id: int,
    follow_type: str,
    content: str,
    result: str | None = None,
) -> dict[str, Any]:
    ticket = get_ticket(ticket_id)
    if follow_type not in FOLLOW_TYPES:
        raise conflict("INVALID_FOLLOW_TYPE", "跟进类型不合法")
    if ticket["ticket_status"] == "closed":
        raise conflict("TICKET_CLOSED", "工单已关闭，先重开再跟进")

    now = local_now()
    with db_cursor() as (_, cursor):
        _follow(cursor, ticket_id, operator_id, follow_type, content, result, now)
        cursor.execute("UPDATE service_ticket SET updated_at = %s WHERE id = %s", (now, ticket_id))

    return {"ticketId": ticket_id, "ticketNo": ticket["ticket_no"], "followType": follow_type}
