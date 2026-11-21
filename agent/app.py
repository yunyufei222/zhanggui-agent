"""客服 agent 的 HTTP 入口（T2.1）。

现在只有 /health —— 它是后面所有任务的体检口：数据库通不通、政策库读不读得到、
状态机版本是什么。没有它，后面每一步都要靠人猜服务是不是活着。
"""

from __future__ import annotations

from fastapi import FastAPI

from .config import VERSION
from .state import STATES, TRANSITIONS
from app.database import fetch_one

app = FastAPI(title="Edu Agent", version=VERSION)


@app.get("/health")
def health() -> dict[str, object]:
    db_up = False
    kb_articles = 0
    try:
        row = fetch_one("SELECT COUNT(*) AS total FROM kb_article WHERE yn = 1")
        kb_articles = int(row["total"]) if row else 0
        fetch_one("SELECT 1 AS one")
        db_up = True
    except Exception:  # 数据库挂了要让 /health 说真话，不是 500
        db_up = False
    return {
        "ok": db_up,
        "version": VERSION,
        "db": "up" if db_up else "down",
        "kbArticles": kb_articles,
        "states": list(STATES),
        "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
    }
