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



# 兜底话术：拿不准的时候必须说这句，不把「最像的那条」当答案（T1.3）。
FALLBACK_ANSWER = "这条我不确定，怕给您说错。已经帮您转人工，客服会按订单实际情况核实后回复。"
STRONG_SCORE = 2  # 关键词命中(3) 或 标题命中(2) 才算强；只有正文蹭到(1) 不算


def _where(q: str, category: str | None) -> tuple[str, list[Any]]:
    pattern = like_pattern(q.strip())
    conditions = ["yn = 1", "(title LIKE %s OR keywords LIKE %s OR content LIKE %s)"]
    params: list[Any] = [pattern, pattern, pattern]
    if category:
        conditions.append("category = %s")
        params.append(category)
    return " AND ".join(conditions), params


def search_rows(q: str, category: str | None, limit: int) -> list[dict[str, Any]]:
    """按关键词查政策：关键词 > 标题 > 正文，同分按 priority。"""
    where, params = _where(q, category)
    pattern = like_pattern(q.strip())
    return fetch_all(
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


def count_rows(q: str, category: str | None) -> int:
    where, params = _where(q, category)
    return count_total(f"SELECT COUNT(*) AS total FROM kb_article WHERE {where}", tuple(params))


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
    total = count_rows(q, category)
    rows = search_rows(q, category, limit)
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

@router.get("/kb/answer")
def answer_policy(
    current_user_id: Annotated[int | None, Depends(get_optional_current_user_id)],
    q: Annotated[str, Query(min_length=1, max_length=50, description="用户问法/关键词。")],
    category: Annotated[str | None, Query(max_length=32)] = None,
):
    """宁可不答：只给强命中，弱命中/无命中一律兜底转人工。"""
    rows = search_rows(q, category, 3)
    if not rows:
        return ok({
            "q": q, "matched": False, "confidence": "none",
            "articleNo": None, "content": None, "answer": FALLBACK_ANSWER, "candidates": [],
        })
    top = rows[0]
    if int(top["score"]) < STRONG_SCORE:
        return ok({
            "q": q, "matched": False, "confidence": "weak",
            "articleNo": None, "content": None, "answer": FALLBACK_ANSWER,
            "candidates": [row["article_no"] for row in rows],
        })
    return ok({
        "q": q, "matched": True, "confidence": "strong",
        "articleNo": top["article_no"], "category": top["category"],
        "title": top["title"], "content": top["content"], "answer": top["content"],
    })
