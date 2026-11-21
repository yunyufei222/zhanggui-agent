"""M1 第一刀：对话入口的四个用例（三问得答 + 一问兜底）。"""

from __future__ import annotations


def uid(samples):
    """样例里的用户 id（键名不硬编码，取到就用）。"""
    row = samples.user_with_student
    for k in ("userId", "user_id", "id"):
        if k in row:
            return row[k]
    return next(iter(row.values()))


def auth(user_headers, samples):
    return user_headers(uid(samples))


def payload(response):
    body = response.json()
    return body.get("data", body)


def test_chat_course(client, user_headers, samples):
    r = client.post("/api/v1/chat", json={"message": "我想了解数据分析课程"}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["intent"] == "course"
    assert "trace" in p and len(p["trace"]) >= 3
    assert p["trace"][1]["value"] == "course"
    assert isinstance(p["data"], list)


def test_chat_order(client, user_headers, samples):
    r = client.post("/api/v1/chat", json={"message": "帮我查下我的订单"}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["intent"] == "order"
    assert "订单" in p["reply"]


def test_chat_progress(client, user_headers, samples):
    r = client.post("/api/v1/chat", json={"message": "我的学习进度到哪了"}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["intent"] == "progress"
    assert isinstance(p["data"], list)


def test_chat_trace_persisted(client, user_headers, samples):
    from app.database import fetch_one

    r = client.post("/api/v1/chat", json={"message": "帮我查下订单"}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    key = payload(r)["sessionKey"]
    assert key, "没拿到 sessionKey"
    row = fetch_one("SELECT COUNT(*) AS n FROM dialogue_trace WHERE session_key = %s", (key,))
    assert int(row["n"]) >= 3, row


def test_chat_unknown_intent(client, user_headers, samples):
    r = client.post("/api/v1/chat", json={"message": "今天天气怎么样"}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["intent"] == "unknown"
    assert "答不了" in p["reply"]
