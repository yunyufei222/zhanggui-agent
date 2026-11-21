"""T2.14：外部调用要有超时、重试一次、降级话术（P27「一次慢调用拖死整轮对话」）。

判据：
1. 超时与重试次数来自配置（默认 3s / 1 次），不写死在代码里；
2. 超时一次后能重试成功；两次都失败就**安静地**退化成规则（不抛、不 500）；
3. 主应用连不上时，模拟接口重试后给 503 和一句能照着做的话；
4. LLM 挂着不返回时，一轮对话仍然能在超时窗口内答完，并标出 `degraded`。
"""

from __future__ import annotations

import socket
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from agent import pipeline, sim
from agent.app import app as agent_app
from agent.intent import HttpLLM
from agent.skill_config import config


@pytest.fixture(scope="module")
def client():
    with TestClient(agent_app) as test_client:
        yield test_client


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FlakyOpener:
    """前 N 次超时，之后返回正常响应；同时记录调用次数。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise socket.timeout("timed out")
        payload = b'{"choices": [{"message": {"content": "policy"}}]}'
        return FakeResponse(payload)


def make_llm() -> HttpLLM:
    llm = HttpLLM("http://127.0.0.1:9/v1", "test-key", "test-model")
    return llm


def test_timeout_and_retries_come_from_config():
    llm = make_llm()
    assert llm.timeout == float(config.get("external.timeout_s"))
    assert llm.retries == int(config.get("external.retries"))
    assert llm.timeout <= 3.0, "默认超时要压到 3 秒级，不能让慢调用拖住一轮"


def test_retry_after_one_timeout_succeeds():
    llm = make_llm()
    llm.opener = FlakyOpener(failures=1)
    assert llm("这句话该怎么归") == "policy"
    assert llm.opener.calls == 2, "超时后必须重试一次"


def test_gives_up_quietly_after_retries():
    llm = make_llm()
    llm.opener = FlakyOpener(failures=99)
    assert llm("这句话该怎么归") is None, "重试用尽要返回 None，交给规则兜底"
    assert llm.opener.calls == 1 + llm.retries


def test_sim_retries_then_degrades_with_actionable_message(monkeypatch, samples):
    # 先建一张真订单：号不存在会先 404，轮不到测「外部调用降级」
    from app.main import app as main_app

    user = samples.user_with_student
    cohort = samples.on_sale_series_with_cohort
    with TestClient(main_app) as main_client:
        order = main_client.post(
            "/api/v1/orders",
            headers={"X-User-Id": str(user["user_id"])},
            json={
                "studentId": user["student_id"],
                "cohortId": cohort["cohort_id"],
                "couponReceiveRecordId": None,
                "orderSourceChannelId": None,
                "remark": "pytest degrade",
            },
        ).json()["data"]

    calls = {"n": 0}

    class BoomOpener:
        def open(self, request, timeout=None):
            calls["n"] += 1
            raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sim, "_OPENER", BoomOpener())
    with TestClient(agent_app) as client:
        response = client.post("/sim/payment", json={"orderNo": order["orderNo"]})

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "start-all.bat" in detail, detail
    assert calls["n"] == 1 + int(config.get("external.retries"))


def test_slow_llm_does_not_stall_the_turn(client, samples, monkeypatch):
    def hanging(_text):
        time.sleep(float(config.get("external.timeout_s")) + 0.5)
        raise TimeoutError("llm timeout")

    monkeypatch.setattr(pipeline, "LLM", hanging)
    user = samples.user_with_student

    started = time.perf_counter()
    response = client.post("/chat", json={"userId": user["user_id"], "message": "付款", "mode": "auto"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert elapsed < float(config.get("external.timeout_s")) * 2, f"一轮花了 {elapsed:.1f}s，太久了"
    data = response.json()["data"]
    assert data["intentSource"] == "rule"
    assert data.get("degraded") is True, "外部失败要在响应里标出来，别装作一切正常"
