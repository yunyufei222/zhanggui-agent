"""T2.5：四类澄清（conflict / multi_intent / ambiguous / missing_param）。

判据（P26「低置信也照答」的正面反例）：
1. 一句话两件事 → 先问先办哪件，且**不许**偷偷建退款单；
2. 规则只蹭到弱词 → 追问具体一点，不硬答；
3. 想退又不想退 → 把冲突说破；
4. 意图清楚但缺单号 → 追问（missing_param），不是转人工；
5. 正常问题仍然直接答（action=answer），澄清不能把所有对话都变成反问。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_one


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def chat(client, user_id, message, session_key=None):
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_multi_intent_asks_which_first(client, samples):
    user = samples.user_with_student
    before = fetch_one(
        "SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user["user_id"],)
    )["n"]

    data = chat(client, user["user_id"], "我要退款，另外帮我查下订单")
    assert data["action"] == "clarify"
    assert data["clarifyKind"] == "multi_intent"
    assert "先办哪一件" in data["reply"]

    after = fetch_one(
        "SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user["user_id"],)
    )["n"]
    assert int(before) == int(after), "还没确认就先把退款单建了"


def test_ambiguous_asks_for_detail(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "付款")
    assert data["action"] == "clarify"
    assert data["clarifyKind"] == "ambiguous"
    assert "具体" in data["reply"]


def test_conflict_is_spelled_out(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "我要退款但先不退了")
    assert data["action"] == "clarify"
    assert data["clarifyKind"] == "conflict"
    assert "先不退" in data["reply"] or "还是" in data["reply"]


def test_missing_order_number_is_clarify_not_handoff(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "我要退款")
    assert data["action"] == "clarify"
    assert data["clarifyKind"] == "missing_param"
    assert data["need"] == "orderNo"
    assert data["state"] != "handoff"


def test_normal_question_still_answers(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "退款多久到账")
    assert data["action"] == "answer"
    assert data["state"] == "answering"
    assert "退款" in data["reply"]
