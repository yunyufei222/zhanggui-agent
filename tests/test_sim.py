"""T3.2：业务对象消息模拟（付款 / 工单 / 退款）。

判据：从调试台能推着业务走，而不是只能看对话。
1. 给订单号点一下 → 订单和支付都变 paid，报名关系建起来；
2. 工单按号受理 → 完结 → 可重开，状态一路对得上；
3. 退款单号一键审批 → 退款/支付/订单三处同步变；
4. 号写错了就 404 说清楚，不猜也不静默。

这些接口是真的用 HTTP 打主应用（同 start-all.bat 起的两个服务），
所以这里也起一个真主应用——mock 掉就等于没测到「模拟」这件事。
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from agent import sim
from agent.app import app as agent_app
from app.database import fetch_one
from app.main import app as main_app

import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def main_app_server():
    """起一个真主应用（真 HTTP），把 sim 的 MAIN_BASE 指过去。"""
    port = _free_port()
    thread = threading.Thread(
        target=uvicorn.run,
        args=("app.main:app",),
        kwargs={"host": "127.0.0.1", "port": port, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:  # noqa: BLE001 —— 起服务期间连不上是正常的
            time.sleep(0.25)
    else:
        pytest.fail("主应用没起来")

    original = sim.MAIN_BASE
    sim.MAIN_BASE = base
    yield base
    sim.MAIN_BASE = original


@pytest.fixture(scope="module")
def client(main_app_server):
    """主应用：下单、建工单、提退款都走它——和浏览器里做的是同一件事。"""
    with TestClient(main_app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def sim_client(main_app_server):
    """agent 应用：/sim/* 与 /debug 在它身上。"""
    with TestClient(agent_app) as test_client:
        yield test_client


def create_pending_order(client, samples) -> dict:
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    headers = {"X-User-Id": str(user["user_id"])}
    response = client.post(
        "/api/v1/orders",
        headers=headers,
        json={
            "studentId": user["student_id"],
            "cohortId": cohort["cohort_id"],
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "pytest sim",
        },
    )
    return response.json()["data"]


def test_sim_payment_marks_order_paid_and_enrolls(client, sim_client, samples):
    order = create_pending_order(client, samples)
    user = samples.user_with_student

    response = sim_client.post("/sim/payment", json={"orderNo": order["orderNo"]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["orderStatus"] == "paid"
    assert data["paymentStatus"] == "paid"

    item = fetch_one("SELECT id, cohort_id FROM order_item WHERE order_id = %s", (order["orderId"],))
    rel = fetch_one(
        "SELECT enroll_status FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (user["student_id"], item["cohort_id"]),
    )
    assert rel is not None and rel["enroll_status"] == "active"


def test_sim_ticket_flow(client, sim_client, samples):
    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    order = create_pending_order(client, samples)
    item = fetch_one("SELECT id FROM order_item WHERE order_id = %s", (order["orderId"],))
    ticket = client.post(
        "/api/v1/service-tickets",
        headers={"X-User-Id": str(user["user_id"])},
        json={
            "studentId": user["student_id"],
            "orderItemId": int(item["id"]),
            "ticketType": "complaint",
            "ticketSource": "user_app",
            "priorityLevel": "medium",
            "title": "模拟流转",
            "ticketContent": "测试工单",
        },
    ).json()["data"]

    accepted = sim_client.post("/sim/ticket", json={"ticketNo": ticket["ticketNo"], "action": "accept"}).json()["data"]
    assert accepted["ticketStatus"] == "in_progress"

    closed = sim_client.post("/sim/ticket", json={"ticketNo": ticket["ticketNo"], "action": "close"}).json()["data"]
    assert closed["ticketStatus"] == "closed"

    reopened = sim_client.post("/sim/ticket", json={"ticketNo": ticket["ticketNo"], "action": "reopen"}).json()["data"]
    assert reopened["ticketStatus"] == "in_progress"


def test_sim_refund_approves(client, sim_client, samples):
    user = samples.user_with_student
    order = create_pending_order(client, samples)
    sim_client.post("/sim/payment", json={"orderNo": order["orderNo"]})

    item = fetch_one("SELECT id, payable_amount FROM order_item WHERE order_id = %s", (order["orderId"],))
    refund = client.post(
        f"/api/v1/order-items/{item['id']}/refund-requests",
        headers={"X-User-Id": str(user["user_id"])},
        json={
            "refundType": "personal_reason",
            "refundReason": "模拟审批",
            "applyAmount": float(item["payable_amount"]),
        },
    ).json()["data"]

    data = sim_client.post("/sim/refund", json={"refundNo": refund["refundNo"]}).json()["data"]
    assert data["refundStatus"] == "refunded"
    assert data["paymentStatus"] == "refunded"
    assert data["orderStatus"] == "refunded"


def test_sim_unknown_numbers_are_404(sim_client, main_app_server):
    assert sim_client.post("/sim/payment", json={"orderNo": "ORD_NOT_EXIST"}).status_code == 404
    assert sim_client.post("/sim/ticket", json={"ticketNo": "TK_NOT_EXIST"}).status_code == 404
    assert sim_client.post("/sim/refund", json={"refundNo": "RF_NOT_EXIST"}).status_code == 404


def test_debug_page_exposes_sim_actions(sim_client):
    page = sim_client.get("/debug").text
    for path in ('"/sim/payment"', '"/sim/ticket"', '"/sim/refund"'):
        assert path in page, f"调试台没有接 {path}"
