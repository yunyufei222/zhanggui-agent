"""T1.3：政策兜底话术（宁可不答）。

判据只有一条：**拿不准的时候，必须说"不确定 + 转人工"，而不是把最像的那条当答案。**

- 强命中（关键词或标题命中）→ 直接给政策原文；
- 弱命中（只有正文里蹭到一个词）→ 不给，走兜底；
- 没有命中 → 走兜底，且不许编。
"""

from __future__ import annotations

FALLBACK_MARK = "转人工"


def test_answer_strong_hit(client):
    response = client.get("/api/v1/kb/answer", params={"q": "退款到账时效"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["matched"] is True
    assert data["articleNo"] == "KB-003"
    assert data["confidence"] == "strong"
    assert data["answer"] == data["content"]


def test_answer_unknown_goes_fallback(client):
    response = client.get("/api/v1/kb/answer", params={"q": "量子纠缠"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["matched"] is False
    assert data["articleNo"] is None
    assert data["confidence"] == "none"
    assert FALLBACK_MARK in data["answer"]


def test_answer_weak_hit_refuses(client):
    """「顺延」只出现在 KB-003 的正文里，标题和关键词都没有——这种只能兜底。"""
    response = client.get("/api/v1/kb/answer", params={"q": "顺延"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["matched"] is False
    assert data["confidence"] == "weak"
    assert FALLBACK_MARK in data["answer"]
    assert "KB-003" not in data["answer"]


def test_answer_requires_q(client):
    response = client.get("/api/v1/kb/answer")
    assert response.status_code == 422
