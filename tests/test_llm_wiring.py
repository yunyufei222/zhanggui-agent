"""接入大模型的接线测试（不需要真 key）。

用本地假 OpenAI 兼容服务验证四件事：
1. 只有 `AGENT_LLM_ENABLED=1` 时才真的调模型（默认关，测试/离线不受影响）；
2. 环境变量名对得上（BASE_URL / API_KEY / MODEL），构造出的客户端超时 ≤3s；
3. 请求形状是 OpenAI 兼容的 `/chat/completions`，返回能解析成意图；
4. 接上之后，一句模糊话（规则只给 0.33）会变成 `intentSource=llm`。
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from agent import pipeline
from agent.app import app as agent_app
from agent.pipeline import _build_llm


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StubLLM(BaseHTTPRequestHandler):
    """可换答案的假模型：content / reasoning 由 REPLY 控制。"""

    calls = 0
    REPLY = {"content": "policy"}

    def do_POST(self):  # noqa: N802 —— http.server 的命名约定
        StubLLM.calls += 1
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        assert "messages" in body, "请求不是 OpenAI 兼容形状"
        payload = json.dumps({"choices": [{"message": dict(StubLLM.REPLY)}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 别刷屏
        return


@pytest.fixture(scope="module")
def stub():
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), StubLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_ENABLED", raising=False)
    assert _build_llm() is None, "默认必须是关的：不能因为机器上有 key 就偷偷联网"


def test_env_wires_the_client(monkeypatch, stub):
    # .env 里可能已设 AGENT_LLM_TIMEOUT（推理模型要 8 秒），这条只验默认档
    monkeypatch.delenv("AGENT_LLM_TIMEOUT", raising=False)
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    monkeypatch.setenv("AGENT_LLM_MODEL", "stub-model")

    client = _build_llm()
    assert client is not None
    assert client.base_url == stub.rstrip("/")
    assert client.model == "stub-model"
    assert client.timeout <= 3.0, "外部调用要压到 3 秒级"


def test_timeout_override(monkeypatch, stub):
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    monkeypatch.setenv("AGENT_LLM_TIMEOUT", "8")
    assert _build_llm().timeout == 8.0


def test_client_parses_openai_shape(monkeypatch, stub):
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    client = _build_llm()
    assert client("这句话该怎么归") == "policy"


def test_english_answer_is_parsed(monkeypatch, stub):
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    StubLLM.REPLY = {"content": "refund"}
    try:
        assert _build_llm()("我要退款") == "refund"
    finally:
        StubLLM.REPLY = {"content": "policy"}


def test_chinese_answer_maps_via_alias(monkeypatch, stub):
    """deepseek-flash 实测会回中文（“支付”），白名单不能把它全拒了。"""
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    StubLLM.REPLY = {"content": "支付"}
    try:
        assert _build_llm()("付款") == "order"
    finally:
        StubLLM.REPLY = {"content": "policy"}


def test_empty_content_does_not_parse_reasoning(monkeypatch, stub):
    """推理模型 content 为空时，不能去扫 reasoning_content——那里有提示词原文，
    列着所有枚举词，扫它必然命中 order，等于每次都猜 order。"""
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    StubLLM.REPLY = {"content": "", "reasoning_content": "order | refund | policy | course ... 需要输出一个词"}
    try:
        assert _build_llm()("付款") is None
    finally:
        StubLLM.REPLY = {"content": "policy"}


def test_chat_reports_llm_source(monkeypatch, stub, samples):
    monkeypatch.setenv("AGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", stub)
    monkeypatch.setenv("AGENT_LLM_MODEL", "stub-model")
    monkeypatch.setattr(pipeline, "LLM", _build_llm())

    user = samples.user_with_student
    with TestClient(agent_app) as client:
        data = client.post("/chat", json={"userId": user["user_id"], "message": "付款"}).json()["data"]

    assert data["intentSource"] == "llm", data
    assert data["intent"] == "policy"
    assert not data.get("degraded"), "模型正常时不该标降级"
