"""T_M2.2 补口：人工回复必须能在 agent 的 /chat 里送到用户手上。

原来只有主应用的 /api/v1/chat 会播报人工回复；用户走 agent 的 /chat 时，
客服回的话永远躺在 dialogue_handoff 里（answered 状态），闭环是断的。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app as agent_app
from app.database import db_cursor, fetch_one
from app.main import app as main_app


@pytest.fixture(scope="module")
def main_client():
    with TestClient(main_app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def agent_client():
    with TestClient(agent_app) as test_client:
        yield test_client


def make_handoff(agent_client, user) -> tuple[str, int]:
    order = fetch_one(
        "SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user["user_id"],)
    )
    assert order is not None
    first = agent_client.post("/chat", json={"userId": user["user_id"], "message": "查订单 " + order["order_no"]}).json()[
        "data"
    ]
    key = first["sessionKey"]
    weak = agent_client.post(
        "/chat", json={"userId": user["user_id"], "message": "顺延", "sessionKey": key}
    ).json()["data"]
    return key, int(weak["handoffId"])


def test_agent_chat_delivers_human_answer_once(agent_client, main_client, samples):
    user = samples.user_with_student
    key, handoff_id = make_handoff(agent_client, user)

    answered = main_client.post(
        f"/api/v1/chat/handoffs/{handoff_id}/answer",
        json={"answer": "已安排老师今晚回电", "answeredBy": "teacher_li"},
    )
    assert answered.status_code == 200, answered.text

    first = agent_client.post(
        "/chat", json={"userId": user["user_id"], "message": "然后呢", "sessionKey": key}
    ).json()["data"]
    assert "【人工回复" in first["reply"], first["reply"]
    assert "已安排老师今晚回电" in first["reply"]
    assert first["handoffId"] == handoff_id
    assert first["intent"] == "handoff_reply"

    row = fetch_one("SELECT status FROM dialogue_handoff WHERE id = %s", (handoff_id,))
    assert row["status"] == "delivered"

    second = agent_client.post(
        "/chat", json={"userId": user["user_id"], "message": "还有吗", "sessionKey": key}
    ).json()["data"]
    assert "【人工回复" not in second["reply"], "人工回复播报了两次"


def test_delivery_keeps_user_message_in_turns(agent_client, main_client, samples):
    user = samples.user_with_student
    key, handoff_id = make_handoff(agent_client, user)
    main_client.post(
        f"/api/v1/chat/handoffs/{handoff_id}/answer", json={"answer": "工单已处理"}
    )

    before = fetch_one("SELECT COUNT(*) AS n FROM dialogue_turn WHERE session_key = %s", (key,))["n"]
    agent_client.post("/chat", json={"userId": user["user_id"], "message": "在吗", "sessionKey": key})
    after = fetch_one("SELECT COUNT(*) AS n FROM dialogue_turn WHERE session_key = %s", (key,))["n"]

    assert int(after) == int(before) + 2, "播报这条也要落 user + assistant 两轮"
