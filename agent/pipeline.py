"""一轮对话的主流程（T2.3）。

顺序固定：落 input → 判定意图 → 取/补槽位 → 查（订单 / 知识库）→ 出话 → 落 answer + state。
每一步都写进四表，所以任何一轮都能原样回放。

分流规则（宁可不答）：
- 说「人工」→ 直接转；
- 订单类且给了单号 → 查订单；没给单号且不是政策问句 → 追问单号；
- 订单类但问的是政策（多久/时效/规则…）→ 走知识库；
- 其他 → 知识库：强命中给原文，弱命中/没命中一律转人工。
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

from app.routers.chat import answer_course, answer_progress, extract_order_no, open_handoff
from app.routers.kb import FALLBACK_ANSWER, STRONG_SCORE, search_rows

from .intent import HttpLLM, classify
from .skills import order as order_skill
from .state import Session
from .store import Store, new_session_key


def _build_llm() -> HttpLLM | None:
    """只有显式打开才会真的调模型：测试与离线环境默认走纯规则。"""
    if os.getenv("AGENT_LLM_ENABLED", "0") != "1":
        return None
    key = os.getenv("AGENT_LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None
    return HttpLLM(
        base_url=os.getenv("AGENT_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=key,
        model=os.getenv("AGENT_LLM_MODEL", "deepseek-flash"),
    )


LLM = _build_llm()

POLICY_HINTS = ("多久", "时效", "规则", "政策", "流程", "能不能", "怎么退", "如何", "几天", "什么时候")
MANUAL_HINTS = ("人工", "真人", "客服")
HANDOFF_SUFFIX = "\n已经帮你转人工，客服会按订单实际情况核实后回复。"
# 库里的单号带连字符（ORD-xxxx），router 那个正则只认纯字母数字，这里放宽
ORDER_RE = re.compile(r"ORD[0-9A-Za-z-]{3,}", re.I)


def _terms(text: str) -> Iterator[str]:
    """整句 + 2~4 字切片：中文问句整句命中不了 LIKE，得拆成词。"""
    yield text.strip()
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", " ", text)
    for token in cleaned.split():
        if len(token) <= 1:
            continue
        for size in (4, 3, 2):
            for start in range(0, len(token) - size + 1):
                yield token[start : start + size]


def best_kb(message: str) -> dict[str, Any] | None:
    """在知识库里找最强的强命中：先整句，再按切片；同分取更长的词。"""
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    seen: set[str] = set()
    for term in _terms(message):
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        rows = search_rows(term, None, 1)
        if not rows:
            continue
        row = rows[0]
        score = int(row["score"])
        if score < STRONG_SCORE:
            continue
        key = (score, len(term))
        if best is None or key > best[0]:
            best = (key, row)
    return best[1] if best else None


def _handoff(store: Store, session: Session, user_id: int, message: str, reason: str, order_no: str, reply: str) -> dict[str, Any]:
    store.put_slot(session.session_id, "lastIntent", "handoff", "ask")
    handoff_id = open_handoff(user_id, session.session_id, reason, order_no or "", message)
    store.trace(session.session_id, "handoff", str(handoff_id))
    return {"reply": reply + HANDOFF_SUFFIX, "state": "handoff", "handoffId": handoff_id, "need": None, "matched": False}


def handle_turn(store: Store, user_id: int, message: str, session_key: str | None = None) -> dict[str, Any]:
    key = session_key or new_session_key()
    try:
        session = store.load(key)
    except KeyError:
        store.start_session(key, user_id, None)
        session = store.load(key)

    store.trace(key, "input", message)
    store.append_turn(key, "user", message)

    manual = any(word in message for word in MANUAL_HINTS)
    verdict = classify(message, LLM)
    intent = "human" if manual else verdict.intent
    store.trace(key, "intent", f"{intent} conf={verdict.confidence:.2f} src={verdict.source}")

    order_no = (ORDER_RE.search(message).group(0) if ORDER_RE.search(message) else "") or extract_order_no(message)
    if order_no:
        store.put_slot(key, "orderNo", order_no, "user")
        session.slots["orderNo"] = order_no

    result: dict[str, Any]
    if manual:
        result = _handoff(store, session, user_id, message, "ticket", session.slots.get("orderNo", ""), FALLBACK_ANSWER)

    elif intent in ("order", "refund") and session.slots.get("orderNo"):
        result = {
            "reply": order_skill.answer(user_id, session.slots["orderNo"]),
            "state": "answering",
            "need": None,
            "matched": False,
        }

    elif intent in ("order", "refund"):
        if any(hint in message for hint in POLICY_HINTS):
            hit = best_kb(message)
            if hit is not None:
                store.trace(key, "query", f"kb:{hit['article_no']}")
                result = {
                    "reply": hit["content"],
                    "state": "answering",
                    "need": None,
                    "matched": True,
                    "articleNo": hit["article_no"],
                }
            else:
                result = _handoff(store, session, user_id, message, "refund", "", FALLBACK_ANSWER)
        else:
            store.put_slot(key, "need", "orderNo", "ask")
            result = {
                "reply": order_skill.ask_with_recent(user_id),
                "state": "filling",
                "need": "orderNo",
                "matched": False,
            }

    elif intent == "policy":
        hit = best_kb(message)
        if hit is not None:
            store.trace(key, "query", f"kb:{hit['article_no']}")
            result = {
                "reply": hit["content"],
                "state": "answering",
                "need": None,
                "matched": True,
                "articleNo": hit["article_no"],
            }
        else:
            result = _handoff(store, session, user_id, message, "ticket", session.slots.get("orderNo", ""), FALLBACK_ANSWER)

    elif intent == "course":
        answer = answer_course(message)
        result = {"reply": answer["text"], "state": "answering", "need": None, "matched": False}

    elif intent == "progress":
        answer = answer_progress(message, user_id)
        result = {"reply": answer["text"], "state": "answering", "need": None, "matched": False}

    else:
        hit = best_kb(message)
        if hit is not None:
            store.trace(key, "query", f"kb:{hit['article_no']}")
            result = {
                "reply": hit["content"],
                "state": "answering",
                "need": None,
                "matched": True,
                "articleNo": hit["article_no"],
            }
        else:
            result = _handoff(
                store, session, user_id, message, "ticket", session.slots.get("orderNo", ""), FALLBACK_ANSWER
            )

    store.trace(key, "answer", result["reply"])
    store.save_state(key, result["state"])
    store.append_turn(key, "assistant", result["reply"])
    return {
        "sessionKey": key,
        "intent": intent,
        "intentConfidence": verdict.confidence,
        "intentSource": verdict.source,
        **result,
    }
