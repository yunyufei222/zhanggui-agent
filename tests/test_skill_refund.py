"""T2.9：退款申请技能（自动取 orderItemId）。

判据：一条退款能从头走到尾，而且每一步都能被账本证明。
1. 已支付订单 → 提交成功，回复带退款单号，`refund_request` 落一行 pending，金额 = 明细应付；
2. 未支付订单 → 明说不能退，**不许**插行；
3. 没给单号 → 追问，不瞎猜；
4. 同一明细重复申请 → 第二次回「处理中」，仍然只有一行。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_one
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
            "remark": "pytest refund skill",
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["orderId"])


def pay(client, user_headers, user, order_id):
    created = client.post(
        f"/api/v1/orders/{order_id}/payments",
        headers=user_headers(user["user_id"]),
        json={"paymentChannelCode": "wechat_pay"},
    ).json()["data"]
    payment = fetch_one(
        "SELECT payment_no, order_id, payment_channel, amount FROM payment_record WHERE id = %s",
        (created["paymentId"],),
    )
    response = client.post(
        "/api/v1/payment-notifications/mock",
        headers={"X-Demo-Payment-Signature": "mock-payment-signature"},
        json={
            "paymentNo": payment["payment_no"],
            "orderId": payment["order_id"],
            "paymentChannelCode": payment["payment_channel"],
            "amount": str(payment["amount"]),
            "tradeStatus": "paid",
            "thirdPartyTradeNo": "TP_REFUND_SKILL",
        },
    )
    assert response.status_code == 200, response.text


def order_no_of(client, user_headers, user, order_id) -> str:
    return client.get(
        f"/api/v1/orders/{order_id}", headers=user_headers(user["user_id"])
    ).json()["data"]["orderNo"]


def chat(client, user_id, message, session_key=None):
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_refund_on_paid_order(main_client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(main_client, user_headers, user, cohort)
    pay(main_client, user_headers, user, order_id)
    order_no = order_no_of(main_client, user_headers, user, order_id)

    data = chat(agent_client, user["user_id"], "我要退款 " + order_no)
    assert data["intent"] == "refund"
    assert "退款申请" in data["reply"], data["reply"]
    assert "RF" in data["reply"]

    row = fetch_one(
        "SELECT refund_no, refund_status, apply_amount, order_item_id FROM refund_request "
        "WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user["user_id"],),
    )
    assert row is not None, "没有落 refund_request"
    assert row["refund_status"] == "pending"
    assert row["refund_no"] in data["reply"]
    item = fetch_one(
        "SELECT payable_amount FROM order_item WHERE id = %s", (row["order_item_id"],)
    )
    assert float(row["apply_amount"]) == float(item["payable_amount"])


def test_refund_on_unpaid_order_is_refused(main_client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(main_client, user_headers, user, cohort)
    order_no = order_no_of(main_client, user_headers, user, order_id)

    before = fetch_one("SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user["user_id"],))["n"]
    data = chat(agent_client, user["user_id"], "我要退款 " + order_no)
    after = fetch_one("SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user["user_id"],))["n"]

    assert "不能" in data["reply"] or "不允许" in data["reply"], data["reply"]
    assert int(before) == int(after), "不能退的时候仍然插了退款申请"


def test_refund_without_order_number_asks(agent_client, samples):
    user = samples.user_with_student
    data = chat(agent_client, user["user_id"], "我要退款")
    assert data["need"] == "orderNo"
    assert "订单号" in data["reply"]


def test_duplicate_refund_is_reported(main_client, agent_client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order_id = create_order(main_client, user_headers, user, cohort)
    pay(main_client, user_headers, user, order_id)
    order_no = order_no_of(main_client, user_headers, user, order_id)

    first = chat(agent_client, user["user_id"], "我要退款 " + order_no)
    second = chat(agent_client, user["user_id"], "我要退款 " + order_no, first["sessionKey"])
    assert "RF" in first["reply"]
    assert "处理中" in second["reply"] or "已经" in second["reply"], second["reply"]

    item = fetch_one(
        "SELECT order_item_id FROM refund_request WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user["user_id"],),
    )
    rows = fetch_one(
        "SELECT COUNT(*) AS n FROM refund_request WHERE order_item_id = %s AND refund_status = 'pending'",
        (item["order_item_id"],),
    )
    assert int(rows["n"]) == 1
