"""T1.1：客服政策知识库（kb_article 表 + 20 条政策种子）。

验收只看两件可核对的事：
1. 表在、种子齐（20 条启用、编号唯一、必填不为空、覆盖八个分类）；
2. 每条政策都带可执行的口径（天数/小时/工作日/次/元/%），不是空话。
"""

from __future__ import annotations

EXPECTED_ARTICLES = 20
REQUIRED_CATEGORIES = {"退款", "订单", "支付", "发票", "优惠券", "课程", "账号", "服务"}
CONCRETE_UNITS = ("天", "小时", "工作日", "次", "元", "%")


def test_kb_article_seeded(db):
    row = db["fetch_one"]("SELECT COUNT(*) AS total FROM kb_article WHERE yn = 1")
    assert row["total"] == EXPECTED_ARTICLES

    rows = db["fetch_all"](
        "SELECT article_no, category, title, keywords, content FROM kb_article WHERE yn = 1"
    )
    numbers = [item["article_no"] for item in rows]
    assert len(set(numbers)) == EXPECTED_ARTICLES
    for item in rows:
        assert item["title"].strip()
        assert item["keywords"].strip()
        assert item["content"].strip()
    assert REQUIRED_CATEGORIES <= {item["category"] for item in rows}


def test_kb_policies_are_concrete(db):
    rows = db["fetch_all"]("SELECT article_no, content FROM kb_article WHERE yn = 1")
    vague = [
        item["article_no"]
        for item in rows
        if not any(unit in item["content"] for unit in CONCRETE_UNITS)
    ]
    assert vague == [], f"这些条目没有可核对的口径：{vague}"
