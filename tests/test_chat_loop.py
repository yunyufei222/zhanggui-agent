"""M2 闭环：转人工 → 人工回复 → 用户收到（不写业务表）。"""

from __future__ import annotations

import uuid


def payload(r):
    body = r.json()
    return body.get("data", body)


def uid(samples):
    row = samples.user_with_student
    for k in ("userId", "user_id", "id"):
        if k in row:
            return row[k]
    return next(iter(row.values()))


def auth(user_headers, samples):
    return user_headers(uid(samples))


def order_no(user_id):
    from app.database import fetch_one

    return str(fetch_one("SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))["order_no"])


def open_ticket(client, user_headers, samples, no):
    key = "m2-loop-" + uuid.uuid4().hex[:8]
    client.post("/api/v1/chat", json={"message": "我要投诉", "sessionKey": key}, headers=auth(user_headers, samples))
    client.post("/api/v1/chat", json={"message": "订单号是 " + no, "sessionKey": key}, headers=auth(user_headers, samples))
    last = client.post("/api/v1/chat", json={"message": "课程视频一直打不开", "sessionKey": key}, headers=auth(user_headers, samples))
    p = payload(last)
    assert p["handoff"]["reason"] == "ticket", p
    return key, p["handoff"]["id"]


def test_handoff_queue_and_answer_roundtrip(client, user_headers, samples):
    from app.database import fetch_one

    tickets_before = int(fetch_one("SELECT COUNT(*) AS n FROM ticket")["n"]) if fetch_one("SHOW TABLES LIKE 'ticket'") else 0
    key, hid = open_ticket(client, user_headers, samples, order_no(uid(samples)))
    assert hid, "转人工没有生成工单 id"

    q = client.get("/api/v1/chat/handoffs", params={"status": "pending"}, headers=auth(user_headers, samples))
    assert q.status_code == 200, q.text
    ids = [i["id"] for i in payload(q)["items"]]
    assert hid in ids, "工单没进待处理队列"

    a = client.post(f"/api/v1/chat/handoffs/{hid}/answer", json={"answer": "已给你开通了新的观看权限，重新登录即可。", "answeredBy": "teacher.li"}, headers=auth(user_headers, samples))
    assert a.status_code == 200, a.text

    again = client.post(f"/api/v1/chat/handoffs/{hid}/answer", json={"answer": "重复回复"}, headers=auth(user_headers, samples))
    assert again.status_code == 409, "同一条工单不该被回复两次"

    back = client.post("/api/v1/chat", json={"message": "在吗", "sessionKey": key}, headers=auth(user_headers, samples))
    p = payload(back)
    assert p["intent"] == "handoff_reply", p
    assert "已给你开通了新的观看权限" in p["reply"]

    row = fetch_one("SELECT status FROM dialogue_handoff WHERE id = %s", (hid,))
    assert row["status"] == "delivered", row

    nxt = client.post("/api/v1/chat", json={"message": "谢谢", "sessionKey": key}, headers=auth(user_headers, samples))
    assert payload(nxt)["intent"] != "handoff_reply", "回复被重复播报了"

    if tickets_before is not None and fetch_one("SHOW TABLES LIKE 'ticket'"):
        after = int(fetch_one("SELECT COUNT(*) AS n FROM ticket")["n"])
        assert after == tickets_before, "D3=A：不许写业务工单表"
