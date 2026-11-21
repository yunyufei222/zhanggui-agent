"""T0.4：退款审批（单事务改 4 表 + 报名关系）。

判据：批一次退款，四张表必须在**同一个事务**里一起变，少一张就是账实不符。
1. 全额批：refund_request=refunded、payment=refunded、order=refunded、order_item=refunded、enrollment=refunded；
2. 部分批：payment=partial_refunded、order=partial_refunded，明细与报名保持不动；
3. 重复批 / 批不存在的 → 409 / 404，且不改任何表。
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


def create_order(client, user_headers, user, cohort_id):
    response = client.post(
        "/api/v1/orders",
        headers=user_headers(user["user_id"]),
        json={
            "studentId": user["student_id"],
            "cohortId": cohort_id,
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "pytest refund approve",
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
            "thirdPartyTradeNo": "TP_APPROVE",
        },
    )
    assert response.status_code == 200, response.text
    return int(created["paymentId"])


def paid_order_with_refund(client, user_headers, user, cohort) -> dict:
    order_id = create_order(client, user_headers, user, cohort["cohort_id"])
    pay(client, user_headers, user, order_id)
    item = fetch_one("SELECT id, payable_amount FROM order_item WHERE order_id = %s LIMIT 1", (order_id,))
    created = client.post(
        f"/api/v1/order-items/{item['id']}/refund-requests",
        headers=user_headers(user["user_id"]),
        json={
            "refundType": "personal_reason",
            "refundReason": "pytest 审批",
            "applyAmount": float(item["payable_amount"]),
            "remark": "",
        },
    )
    assert created.status_code == 200, created.text
    return {
        "orderId": order_id,
        "orderItemId": int(item["id"]),
        "payable": float(item["payable_amount"]),
        "refundId": int(created.json()["data"]["refundRequestId"]),
    }


def test_full_approve_updates_four_tables_and_enrollment(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    batch = paid_order_with_refund(client, user_headers, user, cohort)

    response = client.post(
        f"/api/v1/refund-requests/{batch['refundId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": batch["payable"], "remark": "同意全额退款"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["refundStatusCode"] == "refunded"
    assert float(data["approvedAmount"]) == pytest.approx(batch["payable"], abs=0.01)

    refund = fetch_one("SELECT refund_status, approved_amount FROM refund_request WHERE id = %s", (batch["refundId"],))
    assert refund["refund_status"] == "refunded"
    assert float(refund["approved_amount"]) == pytest.approx(batch["payable"], abs=0.01)

    payment = fetch_one("SELECT payment_status FROM payment_record WHERE order_id = %s", (batch["orderId"],))
    assert payment["payment_status"] == "refunded"

    order = fetch_one("SELECT order_status FROM `order` WHERE id = %s", (batch["orderId"],))
    assert order["order_status"] == "refunded"

    item = fetch_one("SELECT order_item_status FROM order_item WHERE id = %s", (batch["orderItemId"],))
    assert item["order_item_status"] == "refunded"

    rel = fetch_one(
        "SELECT enroll_status FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (user["student_id"], cohort["cohort_id"]),
    )
    assert rel is not None and rel["enroll_status"] == "refunded"


def test_partial_approve_keeps_item_and_enrollment(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    batch = paid_order_with_refund(client, user_headers, user, cohort)
    half = round(batch["payable"] / 2, 2)

    response = client.post(
        f"/api/v1/refund-requests/{batch['refundId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": half, "remark": "只退一半"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["refundStatusCode"] == "refunded"

    payment = fetch_one("SELECT payment_status FROM payment_record WHERE order_id = %s", (batch["orderId"],))
    assert payment["payment_status"] == "partial_refunded"

    order = fetch_one("SELECT order_status FROM `order` WHERE id = %s", (batch["orderId"],))
    assert order["order_status"] == "partial_refunded"

    item = fetch_one("SELECT order_item_status FROM order_item WHERE id = %s", (batch["orderItemId"],))
    assert item["order_item_status"] == "paid", "部分退款不该把明细打成已退款"

    rel = fetch_one(
        "SELECT enroll_status FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (user["student_id"], cohort["cohort_id"]),
    )
    assert rel["enroll_status"] == "active", "部分退款不该关掉报名关系"


def test_approve_twice_is_conflict(client, samples, user_headers):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    batch = paid_order_with_refund(client, user_headers, user, cohort)

    first = client.post(
        f"/api/v1/refund-requests/{batch['refundId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": batch["payable"]},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/refund-requests/{batch['refundId']}/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": batch["payable"]},
    )
    assert second.status_code == 409


def test_approve_unknown_refund_is_404(client, samples, user_headers):
    user = samples.user_with_student
    response = client.post(
        "/api/v1/refund-requests/99999999/approve",
        headers=user_headers(user["user_id"]),
        json={"approvedAmount": 1},
    )
    assert response.status_code == 404
