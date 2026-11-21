"""T2.8：学习进度（班次名模糊匹配 + 同名班次不瞎猜）。

P⑯ 说的两个错配都要挡住：
1. 简称/中文数字：用户说「五期」「第5期」，得对上同一个班次；
2. 同名班次：库里真有两期同名的班（202611期 各一期），不能挑一个就答——
   要靠开课日期把选择权交回用户。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from app.database import db_cursor, fetch_one
from app.utils import local_now


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def enrolled(samples):
    """给演示学员挂一个「第5期」的测试班次——真实库里没有这个写法，得造。"""
    user = samples.user_with_student
    now = local_now()
    start = date.today() - timedelta(days=10)
    teacher = fetch_one(
        "SELECT head_teacher_id FROM series_cohort WHERE head_teacher_id IS NOT NULL LIMIT 1"
    )
    with db_cursor() as (_, cursor):
        # 清掉上一轮留下的测试班次（否则同名匹配会命中一堆自己造的数据）
        cursor.execute(
            "DELETE rel FROM student_cohort_rel AS rel JOIN series_cohort AS c ON c.id = rel.cohort_id "
            "WHERE c.cohort_name = '测试班·编程 第5期'"
        )
        cursor.execute("DELETE FROM order_item WHERE item_name = '测试班·编程 第5期'")
        cursor.execute("DELETE FROM series_cohort WHERE cohort_name = '测试班·编程 第5期'")
        cursor.execute(
            """
            INSERT INTO series_cohort (institution_id, series_id, head_teacher_id, cohort_code, cohort_name,
                                       sale_price, max_student_count, current_student_count, yn,
                                       start_date, end_date, created_at, updated_at)
            VALUES (1, 1, %s, %s, '测试班·编程 第5期', 1999.00, 30, 12, 1, %s, %s, %s, %s)
            """,
            (int(teacher["head_teacher_id"]), f"PYT{int(now.timestamp())}", start,
             start + timedelta(days=80), now, now),
        )
        cohort_id = int(cursor.lastrowid)
        # rel.order_item_id 是 NOT NULL：挂到该学员最近一张订单上的一条明细
        template = fetch_one(
            "SELECT order_id, unit_price, discount_amount, payable_amount, service_period_days "
            "FROM order_item WHERE user_id = %s ORDER BY id DESC LIMIT 1",
            (user["user_id"],),
        )
        assert template is not None, "演示学员没有订单，先跑 e2e.py"
        cursor.execute(
            """
            INSERT INTO order_item (institution_id, order_id, user_id, student_id, cohort_id, item_name,
                                    unit_price, discount_amount, payable_amount, service_period_days,
                                    order_item_status, created_at, updated_at)
            VALUES (1, %s, %s, %s, %s, '测试班·编程 第5期', %s, %s, %s, %s, 'paid', %s, %s)
            """,
            (template["order_id"], user["user_id"], user["student_id"], cohort_id,
             template["unit_price"], template["discount_amount"], template["payable_amount"],
             template["service_period_days"], now, now),
        )
        item_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO student_cohort_rel (institution_id, user_id, student_id, cohort_id, order_item_id,
                                            enroll_status, enroll_at, created_at, updated_at)
            VALUES (1, %s, %s, %s, %s, 'active', %s, %s, %s)
            """,
            (user["user_id"], user["student_id"], cohort_id, item_id, now, now, now),
        )
    return {"cohortId": cohort_id, "cohortName": "测试班·编程 第5期"}


def chat(client, user_id, message):
    response = client.post("/chat", json={"userId": user_id, "message": message})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_chinese_numeral_matches_arabic_name(client, samples, enrolled):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "五期 学到哪了")
    assert enrolled["cohortName"] in data["reply"], data["reply"]


def test_arabic_numeral_matches_too(client, samples, enrolled):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "第5期进度")
    assert enrolled["cohortName"] in data["reply"]
    assert "剩余" in data["reply"] or "剩" in data["reply"]


def test_same_name_cohorts_ask_which_one(client, samples):
    """库里确实有两期同名的 202611 期，只能按开课日期让用户选。"""
    user = samples.user_with_student
    twins = fetch_one(
        """
        SELECT COUNT(*) AS n FROM student_cohort_rel r
        JOIN series_cohort c ON c.id = r.cohort_id
        WHERE r.student_id = %s AND c.cohort_name = '数据库开发实战班·直播 202611期'
        """,
        (user["student_id"],),
    )
    if int(twins["n"]) < 2:
        pytest.skip("演示学员名下没有同名班次，跳过")

    data = chat(client, user["user_id"], "数据库开发实战班 202611期 学到哪")
    assert data["need"] == "cohort", data
    reply = data["reply"]
    assert reply.count("数据库开发实战班·直播 202611期") >= 2, reply
    assert "哪一期" in reply or "哪一个" in reply, reply


def test_unknown_cohort_lists_what_user_has(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "量子力学三期 学到哪")
    reply = data["reply"]
    assert "没找到" in reply or "没有找到" in reply, reply
    assert "数据库基础班" in reply or "第5期" in reply, "至少要把手上的班次列出来"
    assert data["state"] != "handoff"


def test_progress_survives_no_enrollment(client, samples):
    """名下没有任何班次的人，也要给一句人话，而不是空列表。"""
    orphan = fetch_one(
        "SELECT id FROM sys_user WHERE id NOT IN (SELECT user_id FROM student_cohort_rel) LIMIT 1"
    )
    if orphan is None:
        pytest.skip("所有人都有班次")
    data = chat(client, int(orphan["id"]), "我的学习进度")
    assert "没有" in data["reply"] or "没查到" in data["reply"], data["reply"]
