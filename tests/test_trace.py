"""T2.13：trace 每次转换落库。

判据（P㉑ 的正面）：状态一变就有一行，且能看出「从哪到哪、因为什么」；
状态没变就不许刷行；trace 要能被查出来（否则「落库」只是口号）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_all


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


def transitions(session_key: str) -> list[str]:
    rows = fetch_all(
        "SELECT value FROM dialogue_trace WHERE session_key = %s AND step = 'transition' ORDER BY id",
        (session_key,),
    )
    return [str(row["value"]) for row in rows]


def test_transition_is_traced_with_reason(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "我要退款")  # 缺单号 → 澄清 → filling
    rows = transitions(data["sessionKey"])

    assert rows, "状态变了却没有 transition 行"
    assert rows[0].startswith("idle→filling"), rows[0]
    assert "clarify" in rows[0] or "missing_param" in rows[0], rows[0]


def test_transition_chain_across_turns(client, samples):
    user = samples.user_with_student
    first = chat(client, user["user_id"], "我要退款")
    key = first["sessionKey"]
    second = chat(client, user["user_id"], "退款多久到账", key)  # 有答案 → answering

    rows = transitions(key)
    assert any("filling→answering" in row for row in rows), rows


def test_no_transition_no_row(client, samples):
    user = samples.user_with_student
    first = chat(client, user["user_id"], "我要退款")  # idle→filling
    key = first["sessionKey"]
    before = len(transitions(key))
    chat(client, user["user_id"], "退款有什么规则", key)  # 还是 filling？不，会 answering
    after = transitions(key)

    assert len(after) == before + 1, f"多写了行：{after}"
    assert not any("filling→filling" in row for row in after)


def test_trace_endpoint_lists_steps(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "我要退款")
    response = client.get(f"/sessions/{data['sessionKey']}/trace")
    assert response.status_code == 200, response.text
    steps = [row["step"] for row in response.json()["data"]["rows"]]
    assert "input" in steps and "intent" in steps and "transition" in steps
