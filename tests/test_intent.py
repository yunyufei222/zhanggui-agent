"""T2.4：意图识别（规则 + LLM 兜底 + 置信度）。

判据：
1. 五类典型问句，规则层要能给出正确意图，且置信度够高（≥0.7）；
2. 模棱两可的话，规则置信度低 → 必须去问 LLM；LLM 说人话之外的东西 → 退回规则；
3. LLM 适配器只发一个 OpenAI 兼容请求，返回的意图必须白名单校验（不许把模型的自由发挥当意图）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.intent import INTENTS, HttpLLM, classify


def test_rules_cover_typical_questions():
    cases = {
        "我的订单到哪了": "order",
        "退款多久到账": "policy",
        "课程多少钱": "course",
        "我学到第几章了": "progress",
        "我要找人工客服": "human",
    }
    for text, expected in cases.items():
        result = classify(text)
        assert result.intent == expected, (text, result)
        assert result.confidence >= 0.7, (text, result)


def test_vague_input_has_low_confidence():
    result = classify("帮我看看那个")
    assert result.intent in INTENTS
    assert result.confidence < 0.5, result


def test_llm_fallback_is_used_only_when_unsure():
    calls = []

    def fake_llm(text: str) -> str | None:
        calls.append(text)
        return "order"

    sure = classify("我的订单到哪了", llm=fake_llm)
    assert sure.source == "rule"
    assert calls == [], "规则够确定时不该调 LLM"

    vague = classify("帮我看看那个", llm=fake_llm)
    assert vague.source == "llm"
    assert vague.intent == "order"
    assert calls == ["帮我看看那个"]


def test_llm_garbage_falls_back_to_rules():
    result = classify("帮我看看那个", llm=lambda text: "发射导弹")
    assert result.source == "rule"
    assert result.intent in INTENTS


def test_llm_error_does_not_break():
    def broken(text: str) -> str | None:
        raise RuntimeError("网络炸了")

    result = classify("帮我看看那个", llm=broken)
    assert result.source == "rule"


class _FakeLLMHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _FakeLLMHandler.seen.append({"path": self.path, "body": body, "auth": self.headers.get("authorization")})
        payload = {"choices": [{"message": {"content": "order"}}]}
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 静音
        return


@pytest.fixture()
def fake_llm_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _FakeLLMHandler.seen = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_llm_sends_openai_compatible_request(fake_llm_server):
    llm = HttpLLM(base_url=fake_llm_server, api_key="sk-test", model="deepseek-flash", timeout=5)
    assert llm("帮我看看那个") == "order"
    assert _FakeLLMHandler.seen, "没有发出请求"
    seen = _FakeLLMHandler.seen[0]
    assert seen["path"].endswith("/chat/completions")
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["auth"] == "Bearer sk-test"
    assert "messages" in seen["body"]


def test_classify_with_real_http_llm(fake_llm_server):
    llm = HttpLLM(base_url=fake_llm_server, api_key="sk-test", model="deepseek-flash", timeout=5)
    result = classify("帮我看看那个", llm=llm)
    assert result.source == "llm"
    assert result.intent == "order"
