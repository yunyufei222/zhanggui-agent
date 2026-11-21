"""M2 收尾：按 D3 = A（只读 + 转人工）生成可核对的转人工请求。"""

from __future__ import annotations

import uuid


def fresh(prefix: str) -> str:
    """每次跑都是新会话：槽位不该跨测试复用。"""
    return prefix + "-" + uuid.uuid4().hex[:8]


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
    assert row is not None
    return str(row["order_no"])


def test_refund_hands_off_and_never_writes_business_tables(client, user_headers, samples):
    from app.database import fetch_one

    before = int(fetch_one("SELECT COUNT(*) AS n FROM refund_request")["n"])
    key = fresh("pytest-m2-refund")
    no = a_real_order_no(uid(samples))
    client.post("/api/v1/chat", json={"message": "我要退款", "sessionKey": key}, headers=auth(user_headers, samples))
    r = client.post("/api/v1/chat", json={"message": "订单号是 " + no, "sessionKey": key}, headers=auth(user_headers, samples))
    assert r.status_code == 200, r.text
    p = payload(r)
    assert p["handoff"]["reason"] == "refund", p
    assert "人工" in p["reply"]
    after = int(fetch_one("SELECT COUNT(*) AS n FROM refund_request")["n"])
    assert after == before, "D3=A 明确不许自动提交退款，但业务表被写了"


def test_ticket_asks_detail_then_hands_off(client, user_headers, samples):
    from app.database import fetch_one

    key = fresh("pytest-m2-ticket")
    no = a_real_order_no(uid(samples))
    client.post("/api/v1/chat", json={"message": "我要投诉", "sessionKey": key}, headers=auth(user_headers, samples))
    mid = client.post("/api/v1/chat", json={"message": "订单号是 " + no, "sessionKey": key}, headers=auth(user_headers, samples))
    assert payload(mid)["need"] == "detail", payload(mid)
    last = client.post("/api/v1/chat", json={"message": "老师一直没有回复我", "sessionKey": key}, headers=auth(user_headers, samples))
    p = payload(last)
    assert p["handoff"]["reason"] == "ticket", p
    assert "人工" in p["reply"]
    row = fetch_one("SELECT COUNT(*) AS n FROM dialogue_slot WHERE session_key = %s AND slot_key = 'detail' AND source = 'user'", (key,))
    assert int(row["n"]) >= 1, "没记下用户描述的问题"
