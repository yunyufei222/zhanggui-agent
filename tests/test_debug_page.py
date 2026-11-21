"""T3.1：前端调试页（对话 + 槽位 + 轨迹）。

判据：这个页面不是设计稿，是能直接用的调试台——
1. 能打开（200 / text/html），三块都在：对话、槽位、轨迹；
2. 它调用的是真接口：`/chat`、`/sessions/{key}/state|messages|trace`；
3. 不是空壳：有 userId 输入、有发送动作、没有 TODO 占位。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def page(client) -> str:
    response = client.get("/debug")
    assert response.status_code == 200, response.text
    return response.text


def test_page_has_three_panels(client):
    response = client.get("/debug")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for word in ("对话", "槽位", "轨迹"):
        assert word in response.text, f"页面缺少「{word}」"


def test_page_calls_real_endpoints(page):
    for path in ('"/chat"', "/state", "/messages", "/trace"):
        assert path in page, f"页面没有调用 {path}"


def test_page_is_not_an_empty_shell(page):
    assert 'id="userId"' in page
    assert 'id="send"' in page
    assert "TODO" not in page
    assert "localhost" not in page, "同源页面不该硬编码 localhost（换端口就废）"
