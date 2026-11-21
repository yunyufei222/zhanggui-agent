"""客服 agent 的 HTTP 入口（T2.1）。

现在只有 /health —— 它是后面所有任务的体检口：数据库通不通、政策库读不读得到、
状态机版本是什么。没有它，后面每一步都要靠人猜服务是不是活着。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import VERSION
from .pipeline import handle_turn
from .state import STATES, TRANSITIONS
from .store import Store
from app.database import fetch_one
from app.response import ok

app = FastAPI(title="Edu Agent", version=VERSION)


@app.get("/health")
def health() -> dict[str, object]:
    db_up = False
    kb_articles = 0
    open_sessions = 0
    try:
        row = fetch_one("SELECT COUNT(*) AS total FROM kb_article WHERE yn = 1")
        kb_articles = int(row["total"]) if row else 0
        live = fetch_one("SELECT COUNT(*) AS total FROM dialogue_session WHERE status = 'open'")
        open_sessions = int(live["total"]) if live else 0
        fetch_one("SELECT 1 AS one")
        db_up = True
    except Exception:  # 数据库挂了要让 /health 说真话，不是 500
        db_up = False
    return {
        "ok": db_up,
        "version": VERSION,
        "db": "up" if db_up else "down",
        "kbArticles": kb_articles,
        "openSessions": open_sessions,
        "states": list(STATES),
        "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
    }


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    sessionKey: str | None = Field(default=None, max_length=64)
    userId: int | None = None


def _user_id(body: ChatBody, header: str | None) -> int:
    if body.userId is not None:
        return int(body.userId)
    if header and header.isdigit():
        return int(header)
    raise HTTPException(status_code=401, detail="缺少 userId（body 或 X-User-Id 头）")


@app.post("/chat")
def chat(
    body: ChatBody,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> dict[str, Any]:
    """一轮对话：判读 → 补槽/检索 → 作答或转人工；四表全落。"""
    store = Store()
    result = handle_turn(store, _user_id(body, x_user_id), body.message, body.sessionKey)
    return ok(result)


def _chunks(text: str, size: int = 12) -> Iterator[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


@app.post("/chat/stream")
def chat_stream(
    body: ChatBody,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> StreamingResponse:
    """同样的流程，按 SSE 吐：meta → 若干 token → done。"""
    store = Store()
    result = handle_turn(store, _user_id(body, x_user_id), body.message, body.sessionKey)

    def events() -> Iterator[str]:
        meta = {"sessionKey": result["sessionKey"], "intent": result["intent"]}
        yield "event: meta\ndata: " + json.dumps(meta, ensure_ascii=False) + "\n\n"
        for piece in _chunks(result["reply"]):
            yield "data: " + json.dumps({"text": piece}, ensure_ascii=False) + "\n\n"
        done = {"state": result["state"], "matched": result.get("matched", False), "need": result.get("need")}
        yield "event: done\ndata: " + json.dumps(done, ensure_ascii=False) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-store"})


@app.get("/sessions/{session_key}/messages")
def session_messages(session_key: str, limit: int = 50) -> dict[str, Any]:
    """按轮次返回对话；limit 取最近 N 轮。"""
    store = Store()
    if store.session_row(session_key) is None:
        raise HTTPException(status_code=404, detail="会话不存在：" + session_key)
    rows = store.messages(session_key, limit=max(1, min(limit, 200)))
    return ok(
        {
            "sessionKey": session_key,
            "count": len(rows),
            "messages": [
                {
                    "turnNo": int(row["turn_no"]),
                    "role": row["role"],
                    "content": row["content"],
                    "at": row["created_at"],
                }
                for row in rows
            ],
        }
    )


@app.get("/sessions/{session_key}/state")
def session_state(session_key: str) -> dict[str, Any]:
    """状态 / 槽位 / 任务栈 / 最近意图，一次给全。"""
    snapshot = Store().snapshot(session_key)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="会话不存在：" + session_key)
    return ok(snapshot)


@app.post("/sessions/{session_key}/resume")
def session_resume(session_key: str, limit: int = 20) -> dict[str, Any]:
    """把关闭的会话重新打开，并把最近几轮一起带回来。"""
    store = Store()
    if store.session_row(session_key) is None:
        raise HTTPException(status_code=404, detail="会话不存在：" + session_key)
    store.reopen(session_key)
    snapshot = store.snapshot(session_key) or {}
    rows = store.messages(session_key, limit=max(1, min(limit, 200)))
    return ok(
        {
            **snapshot,
            "messages": [
                {"turnNo": int(row["turn_no"]), "role": row["role"], "content": row["content"]}
                for row in rows
            ],
        }
    )
