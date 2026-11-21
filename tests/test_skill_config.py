"""T2.15：技能 YAML 配置化。

判据：改配置要能改变行为（不是读进内存摆着），配置坏了要在启动时炸（不是对话里炸）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent import clarify, skill_config
from agent.app import app
from agent.skill_config import ConfigError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "agent" / "skills.yaml"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def temp_config(tmp_path):
    """把运行中的配置指向一份临时副本，测试结束后恢复默认。"""

    def _use(mutator):
        data = yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))
        mutator(data)
        path = tmp_path / "skills.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        skill_config.config.path = path
        skill_config.config.reload()
        return path

    yield _use
    skill_config.config.path = DEFAULT
    skill_config.config.reload()


def chat(client, user_id, message, session_key=None):
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    response = client.post("/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_default_config_is_complete():
    data = skill_config.config
    assert data.get("version") == 1
    assert data.get("ticket.sla_hours") == 4
    assert data.get("clarify.multi_min") == 2.0
    assert "先办哪一件" in data.text("clarify.texts.multi_intent", first="退款", second="订单")


def test_sla_change_reaches_the_reply(client, samples, temp_config):
    """工单只在有订单上下文时才建，所以先查一单再转人工。"""
    from app.database import fetch_one

    def bump(data):
        data["ticket"]["sla_hours"] = 8

    temp_config(bump)
    user = samples.user_with_student
    order = fetch_one(
        "SELECT order_no FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user["user_id"],)
    )
    if order is None:
        pytest.skip("演示学员还没有订单，先跑 e2e.py")

    first = chat(client, user["user_id"], "查订单 " + order["order_no"])
    data = chat(client, user["user_id"], "顺延", first["sessionKey"])
    assert first["intent"] == "order"
    assert "8 小时" in data["reply"], data["reply"]


def test_multi_intent_threshold_is_configurable(client, samples, temp_config):
    def raise_bar(data):
        data["clarify"]["multi_min"] = 9.0

    temp_config(raise_bar)
    user = samples.user_with_student
    data = chat(client, user["user_id"], "我要退款，另外帮我查下订单")
    assert data.get("clarifyKind") != "multi_intent", data


def test_invalid_value_fails_loudly(tmp_path):
    data = yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))
    data["clarify"]["ambiguous_below"] = 3
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        skill_config.SkillConfig(path)
    assert "ambiguous_below" in str(error.value)


def test_broken_yaml_fails_loudly(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("clarify: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        skill_config.SkillConfig(path)
