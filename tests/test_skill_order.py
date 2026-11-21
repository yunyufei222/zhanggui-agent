"""T2.7：订单查询技能。

判据是「拿真单据问，答案能对上单据」：
- 给单号 → 回复里必须出现该单号、状态、以及每一门课的名称和金额；
- 单号查不到 → 明说查不到，不编；
- 不给单号 → 追问，并且把最近的单号列出来（别让人去翻订单页）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_all, fetch_one


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def real_order():
    row = fetch_one(
        """
        SELECT o.order_no, o.user_id, o.order_status, o.payable_amount
        FROM `order` o
        JOIN order_item i ON i.order_id = o.id
        GROUP BY o.id
        ORDER BY o.id DESC
        LIMIT 1
        """
    )
    assert row is not None, "测试库里没有带明细的订单"
    return row


def chat(client, user_id, message):
    response = client.post("/chat", json={"userId": int(user_id), "message": message})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_order_query_reports_status_and_items(client, real_order):
    data = chat(client, real_order["user_id"], "查订单 " + real_order["order_no"])
    assert data["state"] == "answering"
    reply = data["reply"]
    assert real_order["order_no"] in reply
    assert "状态" in reply

    items = fetch_all(
        "SELECT item_name, payable_amount FROM order_item WHERE order_id = "
        "(SELECT id FROM `order` WHERE order_no = %s)",
        (real_order["order_no"],),
    )
    assert items
    for item in items:
        assert item["item_name"] in reply, f"明细里少了 {item['item_name']}"
        assert f"{float(item['payable_amount']):.2f}" in reply


def test_order_query_unknown_number_is_honest(client, real_order):
    data = chat(client, real_order["user_id"], "查订单 ORD-NOT-EXIST")
    assert data["state"] == "answering"
    assert "没查到" in data["reply"] or "查不到" in data["reply"]
    assert "ORD-NOT-EXIST" in data["reply"]


def test_order_query_without_number_lists_recent(client, real_order):
    data = chat(client, real_order["user_id"], "我的订单到哪了")
    assert data["need"] == "orderNo"
    assert "订单号" in data["reply"]
    recent = fetch_all(
        "SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 3",
        (real_order["user_id"],),
    )
    assert any(row["order_no"] in data["reply"] for row in recent), "追问时没有给出最近单号"
