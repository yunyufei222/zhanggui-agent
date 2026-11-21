"""T2.3：对话主流程（POST /chat + /chat/stream）。

判据按顺序钉住四条路：
1. 政策问句 → 知识库强命中，直接给原文；
2. 订单问句但没给单号 → 追问，不硬猜；
3. 弱命中/要人工 → 转人工，并在 dialogue_handoff 留一条 pending；
4. 同一个 sessionKey 接着聊 → 轮次继续、状态不重置；SSE 有 done 事件。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_all, fetch_one


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def user_id() -> int:
    row = fetch_one("SELECT id FROM sys_user WHERE yn = 1 ORDER BY id LIMIT 1")
    assert row is not None
    return int(row["id"])


def chat(client, user_id, message, session_key=None):
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_policy_question_answered(client, user_id):
    data = chat(client, user_id, "退款多久到账")
    assert data["matched"] is True
    assert data["state"] == "answering"
    article = fetch_one("SELECT content FROM kb_article WHERE article_no = %s", (data["articleNo"],))
    assert data["reply"] == article["content"]


def test_order_without_number_asks(client, user_id):
    data = chat(client, user_id, "我的订单到哪了")
    assert data["need"] == "orderNo"
    assert "订单号" in data["reply"]
    assert data["state"] == "filling"
    rows = fetch_all("SELECT turn_no, role FROM dialogue_turn WHERE session_key = %s ORDER BY turn_no", (data["sessionKey"],))
    assert [row["role"] for row in rows] == ["user", "assistant"]


def test_order_with_number_answers(client, user_id):
    data = chat(client, user_id, "查订单 ORD-NOT-EXIST")
    assert data["need"] is None
    assert data["state"] == "answering"
    assert "查不到" in data["reply"] or "没查到" in data["reply"]


def test_weak_policy_hands_off(client, user_id):
    data = chat(client, user_id, "顺延")
    assert data["state"] == "handoff"
    assert "转人工" in data["reply"]
    rows = fetch_all(
        "SELECT status FROM dialogue_handoff WHERE session_key = %s ORDER BY id DESC", (data["sessionKey"],)
    )
    assert rows and rows[0]["status"] == "pending"


def test_human_request_hands_off(client, user_id):
    data = chat(client, user_id, "我要找人工客服")
    assert data["state"] == "handoff"
    assert "转人工" in data["reply"] or "人工" in data["reply"]


def test_session_continues(client, user_id):
    first = chat(client, user_id, "退款多久到账")
    second = chat(client, user_id, "那发票呢", first["sessionKey"])
    assert second["sessionKey"] == first["sessionKey"]
    rows = fetch_all(
        "SELECT turn_no FROM dialogue_turn WHERE session_key = %s ORDER BY turn_no", (first["sessionKey"],)
    )
    assert [row["turn_no"] for row in rows] == [1, 2, 3, 4]


def test_stream_emits_done(client, user_id):
    with client.stream("POST", "/chat/stream", json={"userId": user_id, "message": "退款多久到账"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert "event: done" in body
    assert "data: " in body
    payloads = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    assert payloads and json.loads(payloads[-1])["state"] in {"answering", "handoff"}


def test_chat_reports_intent_metadata(client, user_id):
    """T2.4：主流程要把意图、置信度、来源一并交出来（留给前端与留痕）。"""
    data = chat(client, user_id, "退款多久到账")
    assert data["intent"] == "policy"
    assert 0.0 <= data["intentConfidence"] <= 1.0
    assert data["intentSource"] in {"rule", "llm"}
