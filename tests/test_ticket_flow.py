"""T0.5：工单跟进与状态流转。

判据：工单必须真的走完 pending → in_progress → closed，且每一步留痕。
1. 受理：pending→in_progress，落受理人、首次响应时间，并写一条 status_update 跟进；
2. 完结：in_progress→closed，落 closed_at；
3. 跳步：pending 直接 close → 409，状态不变；
4. 重开：closed→in_progress，closed_at 清空；
5. 跟进记录：POST 一条 reply_user，内容和人都在库里。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import fetch_one
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def new_ticket(client, user_headers, user, cohort) -> int:
    """工单必须挂在一条订单明细上（表里 order_item_id 是 NOT NULL），所以先开单。"""
    order = client.post(
        "/api/v1/orders",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "cohortId": cohort["cohort_id"],
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "pytest ticket flow",
        },
    )
    assert order.status_code == 200, order.text
    item = fetch_one(
        "SELECT id FROM order_item WHERE order_id = %s LIMIT 1", (int(order.json()["data"]["orderId"]),)
    )
    response = client.post(
        "/api/v1/service-tickets",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "orderItemId": int(item["id"]),
            "ticketType": "complaint",
            "ticketSource": "user_app",
            "priorityLevel": "medium",
            "title": "pytest 工单流转",
            "ticketContent": "老师回复太慢，想投诉一下",
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["ticketId"])


def test_accept_sets_assignee_and_first_response(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = new_ticket(client, user_headers, user, cohort)

    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/accept",
        headers=user_headers(user["user_id"]),
        json={"remark": "我来跟"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ticketStatus"] == "in_progress"

    row = fetch_one(
        "SELECT ticket_status, assignee_user_id, first_response_at FROM service_ticket WHERE id = %s",
        (ticket_id,),
    )
    assert row["ticket_status"] == "in_progress"
    assert int(row["assignee_user_id"]) == int(user["user_id"])
    assert row["first_response_at"] is not None

    follow = fetch_one(
        "SELECT follow_type, follow_user_id FROM service_ticket_follow_record "
        "WHERE ticket_id = %s ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    )
    assert follow is not None
    assert follow["follow_type"] == "status_update"


def test_close_after_accept(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = new_ticket(client, user_headers, user, cohort)
    client.post(
        f"/api/v1/service-tickets/{ticket_id}/accept",
        headers=user_headers(user["user_id"]),
        json={},
    )
    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/close",
        headers=user_headers(user["user_id"]),
        json={"remark": "已电话沟通，用户满意"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["ticketStatus"] == "closed"

    row = fetch_one("SELECT ticket_status, closed_at FROM service_ticket WHERE id = %s", (ticket_id,))
    assert row["ticket_status"] == "closed"
    assert row["closed_at"] is not None


def test_close_without_accept_is_rejected(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = new_ticket(client, user_headers, user, cohort)

    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/close",
        headers=user_headers(user["user_id"]),
        json={},
    )
    assert response.status_code == 409
    row = fetch_one("SELECT ticket_status FROM service_ticket WHERE id = %s", (ticket_id,))
    assert row["ticket_status"] == "pending", "非法流转不该改状态"


def test_reopen_closed_ticket(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = new_ticket(client, user_headers, user, cohort)
    client.post(f"/api/v1/service-tickets/{ticket_id}/accept", headers=user_headers(user["user_id"]), json={})
    client.post(f"/api/v1/service-tickets/{ticket_id}/close", headers=user_headers(user["user_id"]), json={})

    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/reopen",
        headers=user_headers(user["user_id"]),
        json={"remark": "用户又来了"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["ticketStatus"] == "in_progress"

    row = fetch_one("SELECT ticket_status, closed_at FROM service_ticket WHERE id = %s", (ticket_id,))
    assert row["ticket_status"] == "in_progress"
    assert row["closed_at"] is None


def test_add_follow_record(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = new_ticket(client, user_headers, user, cohort)
    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/follow-records",
        headers=user_headers(user["user_id"]),
        json={"followType": "reply_user", "followContent": "已安排老师今晚回电", "followResult": "contacted"},
    )
    assert response.status_code == 200, response.text

    row = fetch_one(
        "SELECT follow_type, follow_content, follow_user_id FROM service_ticket_follow_record "
        "WHERE ticket_id = %s ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    )
    assert row["follow_type"] == "reply_user"
    assert "今晚回电" in row["follow_content"]
    assert int(row["follow_user_id"]) == int(user["user_id"])
