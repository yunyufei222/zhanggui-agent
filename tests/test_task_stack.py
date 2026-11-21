"""T2.11：任务栈（收集 / 切换 / 恢复 / 取消）。

判据：
1. 栈本身：push/pop/cancel/JSON 往返，同类型连推不叠两层；
2. 落盘：存进 dialogue_slot 的 taskStack，换一个 Store 实例也能读回来；
3. 对话里：查订单 → 中途问政策 → 「继续刚才那个」要回到订单并继续追问单号；
   「算了不查了」要真的把这件事取消掉，不许阴魂不散。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from agent.store import Store, new_session_key
from agent.tasks import TaskStack


def test_stack_push_pop_cancel():
    stack = TaskStack()
    stack.push("order", {"need": "orderNo"})
    stack.push("policy")
    assert [task.kind for task in stack.items] == ["order", "policy"]
    assert stack.top().kind == "policy"
    stack.pop()
    assert stack.top().kind == "order"
    stack.cancel()
    assert stack.top() is None
    assert not stack.items


def test_stack_same_kind_does_not_duplicate():
    stack = TaskStack()
    stack.push("order", {"a": "1"})
    stack.push("order", {"b": "2"})
    assert len(stack.items) == 1
    assert stack.top().slots == {"a": "1", "b": "2"}


def test_stack_json_roundtrip():
    stack = TaskStack()
    stack.push("order", {"need": "orderNo"})
    stack.push("refund")
    restored = TaskStack.from_json(stack.to_json())
    assert [task.kind for task in restored.items] == ["order", "refund"]
    assert restored.top().slots == {}


def test_stack_persists_through_store():
    store = Store()
    key = new_session_key()
    store.start_session(key, user_id=1)
    stack = TaskStack()
    stack.push("order", {"need": "orderNo"})
    store.put_slot(key, "taskStack", stack.to_json(), "ask")
    reloaded = TaskStack.from_json(store.slots(key)["taskStack"])
    assert reloaded.top().kind == "order"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def user_id():
    from app.database import fetch_one

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


def test_switch_and_resume_returns_to_order(client, user_id):
    first = chat(client, user_id, "我的订单到哪了")
    assert first["need"] == "orderNo"

    second = chat(client, user_id, "退款多久到账", first["sessionKey"])
    assert second["state"] == "answering"

    third = chat(client, user_id, "继续刚才那个", first["sessionKey"])
    assert third["need"] == "orderNo", third
    assert third["state"] == "filling"


def test_cancel_clears_the_task(client, user_id):
    first = chat(client, user_id, "我的订单到哪了")
    assert first["state"] == "filling"

    second = chat(client, user_id, "算了不查了", first["sessionKey"])
    assert "算了" in second["reply"] or "不办" in second["reply"] or "先不" in second["reply"]
    assert second["state"] == "idle"

    third = chat(client, user_id, "继续刚才那个", first["sessionKey"])
    assert third["need"] is None, "取消之后不该还能恢复到那个任务"
