"""T4.2：演示数据固定（demo.json + seed_demo.py）。

判据：演示对象必须靠业务键找到，而且清单里的期望值要和库里真实数据对得上——
否则 e2e 的十条断言就是拿一把会自己变长的尺子在量。
"""

from __future__ import annotations

import pytest

from app.database import fetch_one
from seed_demo import DemoDataMissing, load_manifest, resolve


def test_manifest_resolves_by_business_keys():
    demo = resolve()
    assert demo["userId"] > 0
    assert demo["studentId"] > 0
    assert demo["cohortId"] > 0
    assert demo["seriesCode"] and demo["cohortCode"]


def test_expectations_match_database():
    manifest = load_manifest()
    demo = resolve(manifest)

    assert abs(demo["salePrice"] - float(manifest["expect"]["paidAmount"])) < 0.01, "清单售价和班期售价不一致"

    article = fetch_one(
        "SELECT article_no FROM kb_article WHERE article_no = %s", (manifest["expect"]["kbArticleNo"],)
    )
    assert article is not None, f"清单里的知识条目 {manifest['expect']['kbArticleNo']} 不存在"


def test_missing_mobile_fails_loudly():
    manifest = load_manifest()
    manifest["user"]["mobile"] = "10000000000"
    with pytest.raises(DemoDataMissing):
        resolve(manifest)
