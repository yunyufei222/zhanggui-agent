"""T2.6：课程咨询技能——回复里的每个数字都得是库里的真值。

P⑲ 的病根是「上游信息残缺，下游硬编」：老实现只按 series_name 做 LIKE，
问「通用编程入门班多少钱」整句拿去匹配名字，必然落空；就算命中，也只有
名字 + 价格区间，没有授课方式、班期、余位，更没有「你已经报过这门课」。
这里把「订单事实集（状态 + 金额 + 课程）」和班期事实一起拼进回复。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import fetch_all, fetch_one


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def chat(client, user_id, message):
    response = client.post("/chat", json={"userId": user_id, "message": message})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def demo_series() -> dict:
    row = fetch_one(
        "SELECT id, series_name, delivery_mode FROM series WHERE series_code = %s",
        ("general_purpose_programming_foundation_online_live",),
    )
    assert row is not None
    cheapest = fetch_one(
        "SELECT MIN(sale_price) AS price FROM series_cohort WHERE series_id = %s AND yn = 1",
        (row["id"],),
    )
    return {"id": int(row["id"]), "name": row["series_name"], "price": float(cheapest["price"])}


def test_course_price_and_delivery_come_from_db(client, samples):
    user = samples.user_with_student
    series = demo_series()
    data = chat(client, user["user_id"], "通用编程入门班·直播 多少钱")

    assert data["intent"] == "course"
    assert f"{series['price']:.0f}" in data["reply"], data["reply"]
    assert "授课" in data["reply"], data["reply"]
    assert "班期" in data["reply"], data["reply"]


def test_live_vs_recorded_tie_asks_which(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "通用编程入门班多少钱")
    assert "哪一门" in data["reply"] or "相近" in data["reply"], data["reply"]


def test_unknown_course_says_not_found(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "量子力学的课程多少钱")
    assert "没找到" in data["reply"], data["reply"]
    assert "元" not in data["reply"].replace("没找到", ""), data["reply"]


def test_listing_uses_real_series_names(client, samples):
    user = samples.user_with_student
    names = [
        row["series_name"]
        for row in fetch_all("SELECT series_name FROM series WHERE sale_status = 'on_sale' ORDER BY id LIMIT 3")
    ]
    data = chat(client, user["user_id"], "有什么课程")
    hit = [name for name in names if name in data["reply"]]
    assert len(hit) >= 2, data["reply"]


def test_owned_course_brings_order_facts(client, samples):
    user = samples.user_with_student
    owned = fetch_one(
        """
        SELECT s.id, s.series_name, o.order_no, o.order_status
        FROM `order` AS o
        JOIN order_item AS i ON i.order_id = o.id
        JOIN series_cohort AS c ON c.id = i.cohort_id
        JOIN series AS s ON s.id = c.series_id
        WHERE o.user_id = %s AND o.order_status IN ('paid', 'completed', 'refunded')
        ORDER BY o.id DESC LIMIT 1
        """,
        (user["user_id"],),
    )
    assert owned is not None, "演示学员没有历史订单，先跑 e2e.py"
    data = chat(client, user["user_id"], f"{owned['series_name']} 课程多少钱")

    assert owned["series_name"] in data["reply"]
    assert "已报名" in data["reply"] or "已报过" in data["reply"], data["reply"]
    assert owned["order_no"] in data["reply"], data["reply"]
