"""M2 第一刀：多轮槽位（追问 → 给号 → 记住来源）。"""

from __future__ import annotations


def payload(response):
    body = response.json()
    return body.get("data", body)


def uid(samples):
    row = samples.user_with_student
    for k in ("userId", "user_id", "id"):
        if k in row:
            return row[k]
    return next(iter(row.values()))


def auth(user_headers, samples):
    return user_headers(uid(samples))


def a_real_order_no(user_id: int) -> str:
    from app.database import fetch_one

    row = fetch_one("SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
    assert row is not None, "样例用户没有订单"
    return str(row["order_no"])


def test_refund_asks_for_order_no_first(client, user_headers, samples):
    key = "pytest-m2-1"
    r = client.post("/api/v1/chat", json={"message": "我要退款", "sessionKey": key}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["need"] == "orderNo", p
    assert "订单号" in p["reply"]
    assert p["sessionKey"] == key

    from app.database import fetch_one

    row = fetch_one("SELECT COUNT(*) AS n FROM dialogue_slot WHERE session_key = %s AND source = 'ask'", (key,))
    assert int(row["n"]) >= 1, "没记下「追问」这个动作"


def test_refund_continues_after_order_no(client, user_headers, samples):
    key = "pytest-m2-2"
    first = client.post("/api/v1/chat", json={"message": "我要退款", "sessionKey": key}, headers=auth(user_headers, samples))
    assert first.status_code == 200
    no = a_real_order_no(uid(samples))
    second = client.post("/api/v1/chat", json={"message": "订单号是 " + no, "sessionKey": key}, headers=auth(user_headers, samples))
    assert second.status_code == 200, second.text
    p = payload(second)
    assert "订单" in p["reply"], p["reply"]
    assert p.get("need") is None

    from app.database import fetch_one

    row = fetch_one("SELECT COUNT(*) AS n FROM dialogue_slot WHERE session_key = %s AND source = 'user' AND slot_value = %s", (key, no))
    assert int(row["n"]) >= 1, "没记下用户给的订单号"
