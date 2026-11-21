"""T2.10：工单提交（转人工时给一个真工单号）。

判据：
1. 有订单上下文时转人工 → `service_ticket` 里真的要有一张工单（类型、用户、明细对得上），
   回复里带工单号；
2. 没有订单上下文时不许硬造工单（service_ticket.order_item_id 是 NOT NULL），
   仍然走对话层转人工，话术里不出现假单号；
3. 同一会话重复转人工不该每次新建——已有未关闭工单就复用。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_one

# 复用主应用的 client：先用它建单，再用 agent 的 /chat 去问
from app.main import app as main_app


@pytest.fixture(scope="module")
def main_client():
    with TestClient(main_app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def agent_client():
    with TestClient(app) as test_client:
        yield test_client


def create_order(client, user_headers, user, cohort):
    response = client.post(
        "/api/v1/orders",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "cohortId": cohort["cohort_id"],
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "pytest ticket skill",
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["orderId"])


def chat(client, user_id, message, session_key=None):
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_policy_handoff_creates_service_ticket(main_client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(main_client, user_headers, user, cohort)
    order_no = main_client.get(
        f"/api/v1/orders/{order_id}", headers=user_headers(user["user_id"])
    ).json()["data"]["orderNo"]

    first = chat(agent_client, user["user_id"], "查订单 " + order_no)
    weak = chat(agent_client, user["user_id"], "顺延", first["sessionKey"])
    assert weak["state"] == "handoff"
    assert "工单" in weak["reply"], weak["reply"]
    assert "TK" in weak["reply"]

    ticket = fetch_one(
        "SELECT ticket_no, user_id, ticket_type, ticket_status, ticket_content "
        "FROM service_ticket WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user["user_id"],),
    )
    assert ticket is not None, "转人工没有落 service_ticket"
    assert ticket["ticket_no"] in weak["reply"]
    assert ticket["ticket_status"] == "pending"
    assert ticket["ticket_type"] in {"after_sales", "complaint", "refund"}
    assert order_no in ticket["ticket_content"]


def test_handoff_without_order_does_not_fake_ticket(agent_client, samples):
    user = samples.user_with_student
    data = chat(agent_client, user["user_id"], "顺延")
    assert data["state"] == "handoff"
    assert "TK" not in data["reply"], "没有订单上下文却给了工单号"
    assert "转人工" in data["reply"]


def test_repeat_handoff_reuses_open_ticket(main_client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(main_client, user_headers, user, cohort)
    order_no = main_client.get(
        f"/api/v1/orders/{order_id}", headers=user_headers(user["user_id"])
    ).json()["data"]["orderNo"]

    first = chat(agent_client, user["user_id"], "查订单 " + order_no)
    one = chat(agent_client, user["user_id"], "顺延", first["sessionKey"])
    two = chat(agent_client, user["user_id"], "顺延", first["sessionKey"])

    def ticket_no(reply: str) -> str:
        for token in reply.replace("，", " ").replace("。", " ").split():
            if token.startswith("TK"):
                return token
        return ""

    assert ticket_no(one["reply"]) == ticket_no(two["reply"]), "重复转人工建了新工单"
