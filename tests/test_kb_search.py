"""T1.2：知识检索接口（SQL LIKE，D4=A 的落地）。

判据是「拿真政策问，答得回、答得准、答不了要空」：
- 命中：按标题/关键词/正文任一处匹配，结果里每一条都真的提到那个词；
- 空：不存在的词必须返回 0 条，不许硬凑；
- 过滤：分类能把范围收窄；
- 边界：q 必填（缺了就 422），limit 封顶，单篇可按编号取、取不到 404。
"""

from __future__ import annotations


def test_kb_search_hits(client):
    response = client.get("/api/v1/kb/articles", params={"q": "退款"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] >= 3
    numbers = [item["articleNo"] for item in data["list"]]
    assert "KB-001" in numbers
    for item in data["list"]:
        haystack = item["title"] + item["keywords"] + item["content"]
        assert "退款" in haystack


def test_kb_search_unknown_returns_empty(client):
    response = client.get("/api/v1/kb/articles", params={"q": "量子纠缠"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 0
    assert data["list"] == []


def test_kb_search_requires_q(client):
    response = client.get("/api/v1/kb/articles")
    assert response.status_code == 422


def test_kb_search_category_filter(client):
    response = client.get("/api/v1/kb/articles", params={"q": "退款", "category": "发票"})
    assert response.status_code == 200
    items = response.json()["data"]["list"]
    assert items != []
    assert {item["category"] for item in items} == {"发票"}


def test_kb_search_limit(client):
    response = client.get("/api/v1/kb/articles", params={"q": "退款", "limit": 2})
    assert response.status_code == 200
    assert len(response.json()["data"]["list"]) <= 2


def test_kb_article_detail(client):
    response = client.get("/api/v1/kb/articles/KB-002")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["articleNo"] == "KB-002"
    assert data["category"] == "退款"
    assert "课时" in data["content"]


def test_kb_article_detail_missing(client):
    response = client.get("/api/v1/kb/articles/KB-999")
    assert response.status_code == 404
