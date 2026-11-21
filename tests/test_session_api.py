"""T2.12：会话 API（messages / state / resume）。

判据：一个客户端（或人）拿到 sessionKey 之后，能取出
1. 完整对话（按 turn_no，角色不乱）；
2. 当前状态 + 槽位 + 任务栈（不用去猜数据库）；
3. 把已经关闭的会话重新打开，并接着之前的轮次继续；
4. 不存在的 key 给 404，不返回空对象假装成功。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from agent.store import Store, new_session_key
from app.database import fetch_one


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def user_id():
    row = fetch_one("SELECT id FROM sys_user WHERE yn = 1 ORDER BY id LIMIT 1")
    assert row is not None
    return int(row["id"])


def chat(client, user, message, session_key=None):
    body = {"userId": user, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_messages_returns_history_in_order(client, user_id):
    first = chat(client, user_id, "退款多久到账")
    chat(client, user_id, "那发票呢", first["sessionKey"])

    response = client.get(f"/sessions/{first['sessionKey']}/messages")
    assert response.status_code == 200
    rows = response.json()["data"]["messages"]
    assert [row["turnNo"] for row in rows] == [1, 2, 3, 4]
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert "退款" in rows[0]["content"]


def test_state_reports_slots_and_task_stack(client, user_id):
    first = chat(client, user_id, "我的订单到哪了")
    response = client.get(f"/sessions/{first['sessionKey']}/state")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "filling"
    assert data["status"] == "open"
    assert data["taskStack"] and data["taskStack"][-1]["kind"] == "order"
    assert data["intent"] is not None


def test_resume_reopens_closed_session(client, user_id):
    first = chat(client, user_id, "退款多久到账")
    key = first["sessionKey"]
    Store().close(key)

    before = client.get(f"/sessions/{key}/state").json()["data"]
    assert before["status"] == "closed"

    resumed = client.post(f"/sessions/{key}/resume")
    assert resumed.status_code == 200
    data = resumed.json()["data"]
    assert data["status"] == "open"
    assert data["messages"], "恢复会话要把历史一起带回来"

    after = client.get(f"/sessions/{key}/state").json()["data"]
    assert after["status"] == "open"


def test_unknown_session_is_404(client):
    missing = new_session_key()
    assert client.get(f"/sessions/{missing}/messages").status_code == 404
    assert client.get(f"/sessions/{missing}/state").status_code == 404
    assert client.post(f"/sessions/{missing}/resume").status_code == 404


def test_messages_limit(client, user_id):
    first = chat(client, user_id, "退款多久到账")
    chat(client, user_id, "那发票呢", first["sessionKey"])
    response = client.get(f"/sessions/{first['sessionKey']}/messages", params={"limit": 2})
    assert response.status_code == 200
    rows = response.json()["data"]["messages"]
    assert [row["turnNo"] for row in rows] == [3, 4], "limit 应该取最近的 N 轮"
