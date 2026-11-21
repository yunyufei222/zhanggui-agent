"""Knowledge-base APIs: policy search and one article (T1.2).

D4=A 的选择是「kb_article 表 + SQL 检索」，不引向量库。所以这里就三条查询：
按关键词在 标题/关键词/正文 上做 LIKE，命中优先级 关键词 > 标题 > 正文，
同分按 priority 排；分类可以收窄；单篇按编号取。

检索是公开读接口（客服面板与 agent 都要用），用户头可选：
带了就校验，不带动不了这条只读查询。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from ..database import fetch_all, fetch_one
from ..dependencies import get_optional_current_user_id
from ..errors import not_found
from ..response import ok
from ..utils import count_total

router = APIRouter(prefix="/api/v1", tags=["kb"])

MAX_LIMIT = 20


def like_pattern(keyword: str) -> str:
    """把用户输入转成安全的 LIKE 模式：% _ \\ 先转义，再包 %。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def article_payload(row: dict[str, Any], score: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "articleNo": row["article_no"],
        "category": row["category"],
        "title": row["title"],
        "keywords": row["keywords"],
        "content": row["content"],
    }
    if score is not None:
        payload["score"] = int(score)
    return payload


@router.get("/kb/articles")
def search_articles(
    current_user_id: Annotated[int | None, Depends(get_optional_current_user_id)],
    q: Annotated[
        str,
        Query(min_length=1, max_length=50, description="关键词，匹配标题、关键词与正文。"),
    ],
    category: Annotated[
        str | None,
        Query(max_length=32, description="分类过滤：退款 / 订单 / 支付 / 发票 / 优惠券 / 课程 / 账号 / 服务。"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="返回条数上限。")] = 5,
):
    pattern = like_pattern(q.strip())
    conditions = ["yn = 1", "(title LIKE %s OR keywords LIKE %s OR content LIKE %s)"]
    params: list[Any] = [pattern, pattern, pattern]
    if category:
        conditions.append("category = %s")
        params.append(category)
    where = " AND ".join(conditions)

    total = count_total(f"SELECT COUNT(*) AS total FROM kb_article WHERE {where}", tuple(params))
    rows = fetch_all(
        f"""
        SELECT article_no, category, title, keywords, content,
               CASE
                   WHEN keywords LIKE %s THEN 3
                   WHEN title LIKE %s THEN 2
                   ELSE 1
               END AS score
        FROM kb_article
        WHERE {where}
        ORDER BY score DESC, priority ASC, id ASC
        LIMIT %s
        """,
        tuple([pattern, pattern] + params + [limit]),
    )
    return ok(
        {
            "q": q,
            "category": category,
            "list": [article_payload(row, row["score"]) for row in rows],
            "total": total,
        }
    )


@router.get("/kb/articles/{article_no}")
def get_article(
    current_user_id: Annotated[int | None, Depends(get_optional_current_user_id)],
    article_no: Annotated[str, Path(max_length=32, description="政策编号，如 KB-001。")],
):
    row = fetch_one(
        """
        SELECT article_no, category, title, keywords, content, priority
        FROM kb_article
        WHERE article_no = %s AND yn = 1
        """,
        (article_no,),
    )
    if row is None:
        raise not_found("KB_ARTICLE_NOT_FOUND", f"政策条目不存在：{article_no}")
    payload = article_payload(row)
    payload["priority"] = int(row["priority"])
    return ok(payload)
