"""T_M1.1：规则版对话入口——三类意图在**不依赖 LLM** 的前提下从库里拿真值。

判据（M1「能答」的最小闭环）：
1. 订单：给单号 → 状态、金额、课程都来自库；
2. 政策：命中知识库原文（KB 编号来自 demo.json）；
3. 课程：价格/授课/班期来自库；
4. 规则拿不准时仍然转人工，不因为「没有模型」就瞎答；
5. LLM 客户端直接坏掉时，auto 模式照样能答（兜底不是靠外网）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import pipeline
from agent.app import app
from app.database import fetch_one
from seed_demo import load_manifest, resolve


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def chat(client, user_id, message, mode="rule", session_key=None):
    body = {"userId": user_id, "message": message, "mode": mode}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def demo_order(user_id: int) -> dict:
    row = fetch_one(
        "SELECT order_no, order_status FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    assert row is not None, "演示学员没有订单，先跑 e2e.py"
    return row


def test_rule_mode_answers_order_from_db(client, samples):
    user = samples.user_with_student
    order = demo_order(user["user_id"])
    data = chat(client, user["user_id"], "查订单 " + order["order_no"])

    assert data["intent"] == "order"
    assert data["intentSource"] == "rule", "纯规则模式不该出现 llm 来源"
    assert order["order_no"] in data["reply"]
    assert "元" in data["reply"] and "门" in data["reply"]


def test_rule_mode_answers_policy_from_kb(client, samples):
    user = samples.user_with_student
    expect = load_manifest()["expect"]
    data = chat(client, user["user_id"], expect["kbQuestion"])

    assert data["intent"] == "policy"
    assert data.get("matched") is True
    assert data.get("articleNo") == expect["kbArticleNo"]


def test_rule_mode_answers_course_from_db(client, samples):
    user = samples.user_with_student
    demo = resolve()
    data = chat(client, user["user_id"], "通用编程入门班·直播 多少钱")

    assert data["intent"] == "course"
    cheapest = fetch_one(
        "SELECT MIN(c.sale_price) AS price FROM series_cohort c JOIN series s ON s.id = c.series_id "
        "WHERE s.series_code = %s AND c.yn = 1",
        (demo["seriesCode"],),
    )
    assert f"{float(cheapest['price']):.0f}" in data["reply"]
    assert "授课" in data["reply"]


def test_rule_mode_still_hands_off_when_unsure(client, samples):
    user = samples.user_with_student
    data = chat(client, user["user_id"], "顺延")

    assert data["state"] == "handoff", data
    assert "转人工" in data["reply"]


def test_auto_mode_survives_broken_llm(client, samples, monkeypatch):
    """LLM 挂掉时 auto 模式必须还能用规则答——兜底不能以「联网可用」为前提。"""

    def boom(_text: str):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(pipeline, "LLM", boom)
    user = samples.user_with_student
    # 「付款」规则只给 0.33，一定会去问 LLM——正好验证“问了但挂了，仍能好好说话”
    data = chat(client, user["user_id"], "付款", mode="auto")

    assert data["intentSource"] == "rule"
    assert data["action"] == "clarify" and data["clarifyKind"] == "ambiguous", data


def test_health_reports_llm_state(client):
    data = client.get("/health").json()
    assert data["llm"] in {"on", "off"}
