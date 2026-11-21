"""T0.6 / P23：给契约改动补回归保护（13 例）。

P23 的原话是「改 W0 时无法知道是否打坏现状」。这 13 条一一对应本轮改过的契约：
订单过滤与明细 ID、支付回调的四种结局、退款类工单的前置条件、满意度评价的三道闸、
多门课退款的追问、审批金额的两条边界。每条都断言「不该发生的事没发生」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app as agent_app
from app.database import db_cursor, fetch_one
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def agent_client():
    """对话在 agent 应用上，主应用的 /chat 是 404——测试文件别把两个 app 混用。"""
    with TestClient(agent_app) as test_client:
        yield test_client


def create_order(client, user_headers, user, cohort) -> int:
    response = client.post(
        "/api/v1/orders",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "cohortId": cohort["cohort_id"],
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "pytest contract",
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["orderId"])


def order_item_of(order_id: int) -> dict:
    row = fetch_one(
        "SELECT id, payable_amount, student_id, cohort_id FROM order_item WHERE order_id = %s LIMIT 1",
        (order_id,),
    )
    assert row is not None
    return row


def make_payment(client, user_headers, user, order_id: int) -> dict:
    created = client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=user_headers(user["user_id"]),
        json={"paymentChannelCode": "wechat_pay"},
    )
    assert created.status_code == 200, created.text
    row = fetch_one(
        "SELECT payment_no, order_id, payment_channel, amount FROM payment_record WHERE id = %s",
        (int(created.json()["data"]["paymentId"]),),
    )
    return row


def notify(client, payment, status="paid", **overrides):
    body = {
        "paymentNo": payment["payment_no"],
        "orderId": payment["order_id"],
        "paymentChannelCode": payment["payment_channel"],
        "amount": str(payment["amount"]),
        "tradeStatus": status,
        "thirdPartyTradeNo": "TP_CONTRACT",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/payment-notifications/mock",
        headers={"X-Demo-Payment-Signature": "mock-payment-signature"},
        json=body,
    )


def test_order_list_filters_by_status(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    pending_id = create_order(client, user_headers, user, cohort)
    paid_id = create_order(client, user_headers, user, cohort)
    notify(client, make_payment(client, user_headers, user, paid_id))

    paid_no = client.get(f"/api/v1/orders/{paid_id}", headers=user_headers(user["user_id"])).json()["data"]["orderNo"]
    pending_no = client.get(
        f"/api/v1/orders/{pending_id}", headers=user_headers(user["user_id"])
    ).json()["data"]["orderNo"]

    data = client.get("/api/v1/orders", headers=user_headers(user["user_id"]), params={"status": "paid"}).json()["data"]
    numbers = [row["orderNo"] for row in data["list"]]
    assert paid_no in numbers
    assert pending_no not in numbers, "按状态过滤没有生效"


def test_order_detail_returns_order_item_id(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)

    data = client.get(f"/api/v1/orders/{order_id}", headers=user_headers(user["user_id"])).json()["data"]
    assert data["items"], "订单详情没有明细"
    assert int(data["items"][0]["orderItemId"]) == int(item["id"]), "细节里少 orderItemId，退款就没法定位课程"


def test_failed_payment_keeps_order_pending(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    payment = make_payment(client, user_headers, user, order_id)

    response = notify(client, payment, status="failed")
    assert response.status_code == 200, response.text

    status_row = fetch_one(
        "SELECT payment_status FROM payment_record WHERE payment_no = %s", (payment["payment_no"],)
    )
    assert status_row is not None and status_row["payment_status"] == "failed"
    order = fetch_one("SELECT order_status FROM `order` WHERE id = %s", (order_id,))
    assert order["order_status"] == "pending"
    # 失败回调不该推进任何状态（报名关系是「学生+班期」唯一键，历史行会干扰计数）
    item_row = fetch_one("SELECT order_item_status FROM order_item WHERE id = %s", (item["id"],))
    assert item_row["order_item_status"] == "pending", "没付款却把明细推进了"


def test_repeat_paid_notification_is_idempotent(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    payment = make_payment(client, user_headers, user, order_id)

    assert notify(client, payment).status_code == 200
    assert notify(client, payment).status_code == 200

    rel = fetch_one(
        "SELECT COUNT(*) AS n FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s",
        (item["student_id"], item["cohort_id"]),
    )
    assert int(rel["n"]) == 1, "重复回调把报名关系建了两次"


def test_payment_amount_mismatch_is_rejected(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    payment = make_payment(client, user_headers, user, order_id)

    response = notify(client, payment, amount=str(float(payment["amount"]) + 1))
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_AMOUNT_MISMATCH"


def test_payment_signature_is_required(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    payment = make_payment(client, user_headers, user, order_id)

    response = client.post(
        "/api/v1/payment-notifications/mock",
        headers={"X-Demo-Payment-Signature": "not-the-signature"},
        json={
            "paymentNo": payment["payment_no"],
            "orderId": payment["order_id"],
            "paymentChannelCode": payment["payment_channel"],
            "amount": str(payment["amount"]),
            "tradeStatus": "paid",
        },
    )
    assert response.status_code == 401


def test_refund_ticket_needs_refund_request(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)

    response = client.post(
        "/api/v1/service-tickets",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "orderItemId": int(item["id"]),
            "ticketType": "refund",
            "ticketSource": "user_app",
            "priorityLevel": "medium",
            "title": "退款工单",
            "ticketContent": "我要退款",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_REFUND_REQUEST"


def closed_ticket(client, user_headers, user, cohort) -> int:
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    created = client.post(
        "/api/v1/service-tickets",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "orderItemId": int(item["id"]),
            "ticketType": "complaint",
            "ticketSource": "user_app",
            "priorityLevel": "medium",
            "title": "投诉",
            "ticketContent": "老师回得慢",
        },
    )
    ticket_id = int(created.json()["data"]["ticketId"])
    client.post(f"/api/v1/service-tickets/{ticket_id}/accept", headers=user_headers(user["user_id"]), json={})
    client.post(f"/api/v1/service-tickets/{ticket_id}/close", headers=user_headers(user["user_id"]), json={})
    return ticket_id


def test_survey_requires_closed_ticket(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    created = client.post(
        "/api/v1/service-tickets",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "orderItemId": int(item["id"]),
            "ticketType": "complaint",
            "ticketSource": "user_app",
            "priorityLevel": "medium",
            "title": "未关闭",
            "ticketContent": "还没处理完",
        },
    )
    ticket_id = int(created.json()["data"]["ticketId"])

    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/satisfaction-surveys",
        headers=user_headers(user["user_id"]),
        json={"scoreValue": 5},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TICKET_NOT_CLOSED"


def test_survey_score_out_of_range(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = closed_ticket(client, user_headers, user, cohort)

    response = client.post(
        f"/api/v1/service-tickets/{ticket_id}/satisfaction-surveys",
        headers=user_headers(user["user_id"]),
        json={"scoreValue": 9},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SCORE"


def test_survey_is_single_shot(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    ticket_id = closed_ticket(client, user_headers, user, cohort)

    first = client.post(
        f"/api/v1/service-tickets/{ticket_id}/satisfaction-surveys",
        headers=user_headers(user["user_id"]),
        json={"scoreValue": 5, "commentText": "满意"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/v1/service-tickets/{ticket_id}/satisfaction-surveys",
        headers=user_headers(user["user_id"]),
        json={"scoreValue": 4},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "SURVEY_EXISTS"


def test_multi_item_refund_asks_which_course(client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)

    other = fetch_one("SELECT id FROM series_cohort WHERE id <> %s AND yn = 1 LIMIT 1", (cohort["cohort_id"],))
    with db_cursor() as (_, cursor):
        cursor.execute(
            """
            INSERT INTO order_item (institution_id, order_id, user_id, student_id, cohort_id,
                                    item_name, unit_price, discount_amount, payable_amount,
                                    service_period_days, order_item_status, created_at, updated_at)
            SELECT institution_id, order_id, user_id, student_id, %s,
                   '契约测试·第二门课', unit_price, discount_amount, payable_amount,
                   service_period_days, order_item_status, created_at, updated_at
            FROM order_item WHERE id = %s
            """,
            (int(other["id"]), int(item["id"])),
        )

    order_no = client.get(f"/api/v1/orders/{order_id}", headers=user_headers(user["user_id"])).json()["data"]["orderNo"]
    response = agent_client.post("/chat", json={"userId": user["user_id"], "message": "我要退款 " + order_no})
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["need"] == "orderItem", data
    assert "退哪一门" in data["reply"], data["reply"]


def test_approve_amount_over_payable_is_rejected(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    notify(client, make_payment(client, user_headers, user, order_id))
    refund = client.post(
        f"/api/v1/order-items/{item['id']}/refund-requests",
        headers=user_headers(user["user_id"]),
        json={"refundType": "personal_reason", "refundReason": "契约测试", "applyAmount": float(item["payable_amount"])},
    ).json()["data"]

    response = client.post(
        f"/api/v1/refund-requests/{refund['refundRequestId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": float(item["payable_amount"]) + 100},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "REFUND_AMOUNT_EXCEEDED"
    pending = fetch_one("SELECT refund_status FROM refund_request WHERE id = %s", (refund["refundRequestId"],))
    assert pending["refund_status"] == "pending", "越界被拒后状态却变了"


def test_approve_zero_amount_is_rejected(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(client, user_headers, user, cohort)
    item = order_item_of(order_id)
    notify(client, make_payment(client, user_headers, user, order_id))
    refund = client.post(
        f"/api/v1/order-items/{item['id']}/refund-requests",
        headers=user_headers(user["user_id"]),
        json={"refundType": "personal_reason", "refundReason": "契约测试", "applyAmount": float(item["payable_amount"])},
    ).json()["data"]

    response = client.post(
        f"/api/v1/refund-requests/{refund['refundRequestId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": 0},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REFUND_AMOUNT"
