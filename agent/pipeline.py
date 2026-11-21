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

from . import clarify
from .intent import HttpLLM, classify
from .intent import scores as intent_scores
from .skills import order as order_skill
from .skills import refund as refund_skill
from .skills import ticket as ticket_skill
from .state import Session
from .tasks import KIND_CN, TaskStack
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
CANCEL_HINTS = ("算了", "不查了", "不办了", "取消", "退出", "先不看", "不买了")
RESUME_HINTS = ("继续刚才", "回到刚才", "刚才那个", "接着刚才", "继续上一个")
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
    """按「命中了几种说法」投票，而不是被某一种说法的第一条带走。

    旧实现每个切片只取排名第一的那条，再比单个分数——
    「退款多久到账」里「退款」把 KB-001（七天无理由，priority 高）顶上来，
    「到账」命中的 KB-003（退款到账时效）因为不是那一条的榜首就被丢掉了，
    于是回答了错误的政策。现在按文章累计命中的切片种类，命中面更宽的胜。
    """
    votes: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for term in _terms(message):
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        for row in search_rows(term, None, 3):
            score = int(row["score"])
            if score < STRONG_SCORE:
                continue
            slot = votes.setdefault(
                str(row["article_no"]), {"row": row, "terms": set(), "score": 0}
            )
            slot["terms"].add(term)
            slot["score"] += score
            if score > int(slot["row"]["score"]):
                slot["row"] = row

    if not votes:
        return None
    winner = max(votes.values(), key=lambda item: (len(item["terms"]), item["score"]))
    return winner["row"]


def _finish(
    store: Store,
    key: str,
    intent: str,
    verdict,
    result: dict[str, Any],
    stack: TaskStack,
    prev_state: str | None = None,
) -> dict[str, Any]:
    """一轮的收尾：栈落盘 → answer → state → 助手那轮。"""
    if result.get("need") and not result.get("action"):
        result["action"] = "clarify"
        result.setdefault("clarifyKind", "missing_param")
    result.setdefault("action", "answer")

    store.put_slot(key, "taskStack", stack.to_json(), "ask")
    store.trace(key, "answer", result["reply"])
    store.save_state(key, result["state"])
    store.trace_transition(key, prev_state, result["state"], result.get("action") or intent)
    store.append_turn(key, "assistant", result["reply"])
    return {
        "sessionKey": key,
        "intent": intent,
        "intentConfidence": verdict.confidence,
        "intentSource": verdict.source,
        **result,
    }


def _handoff(store: Store, session: Session, user_id: int, message: str, reason: str, order_no: str, reply: str) -> dict[str, Any]:
    store.put_slot(session.session_id, "lastIntent", "handoff", "ask")
    handoff_id = open_handoff(user_id, session.session_id, reason, order_no or "", message)
    store.trace(session.session_id, "handoff", str(handoff_id))

    # T2.10：有单就落一张 service_ticket，把单号给它；没单不硬造。
    ticket = None
    try:
        ticket = ticket_skill.open_ticket(
            user_id,
            order_no or "",
            title=(ticket_skill.TYPE_CN.get(reason, reason) + "｜" + message[:40]),
            content=(message + ("（订单 " + order_no + "）" if order_no else "")),
            ticket_type="after_sales" if reason != "refund" else "refund",
        )
    except Exception:
        ticket = None
    if ticket is not None:
        suffix = f"\n已为你建工单 {ticket['ticketNo']}，客服会在 {ticket_skill.SLA_HOURS} 小时内回复。"
        store.trace(session.session_id, "ticket", ticket["ticketNo"])
    else:
        suffix = HANDOFF_SUFFIX

    return {
        "reply": reply + suffix,
        "state": "handoff",
        "action": "handoff",
        "handoffId": handoff_id,
        "ticketNo": ticket["ticketNo"] if ticket else None,
        "need": None,
        "matched": False,
    }


def handle_turn(store: Store, user_id: int, message: str, session_key: str | None = None) -> dict[str, Any]:
    key = session_key or new_session_key()
    try:
        session = store.load(key)
    except KeyError:
        store.start_session(key, user_id, None)
        session = store.load(key)

    prev_state = session.state
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

    stack = TaskStack.from_json(session.slots.get("taskStack"))
    control = None
    if any(word in message for word in CANCEL_HINTS):
        control = "cancel"
    elif any(word in message for word in RESUME_HINTS):
        control = "resume"

    if control == "cancel":
        cancelled = stack.cancel()
        reply = (
            f"好，「{KIND_CN.get(cancelled.kind, cancelled.kind)}」先不办了。" if cancelled else "好，那就不办了。"
        )
        return _finish(
            store, key, intent, verdict,
            {"reply": reply, "state": "idle", "need": None, "matched": False},
            stack,            prev_state,
        )

    if control == "resume":
        task = stack.top()
        if task is None:
            return _finish(
                store, key, intent, verdict,
                {"reply": "现在没有正在办的事。", "state": "idle", "need": None, "matched": False},
                stack,                prev_state,
            )
        if task.kind in ("order", "refund"):
            return _finish(
                store, key, intent, verdict,
                {"reply": order_skill.ask_with_recent(user_id), "state": "filling", "need": "orderNo", "matched": False},
                stack,                prev_state,
            )
        return _finish(
            store, key, intent, verdict,
            {"reply": f"我们接着说「{KIND_CN.get(task.kind, task.kind)}」。", "state": "answering", "need": None, "matched": False},
            stack,            prev_state,
        )

    ask = clarify.decide(
        message,
        intent,
        verdict.confidence,
        intent_scores(message),
        has_order_no=bool(order_no),
    )
    if ask is not None:
        store.trace(key, "clarify", ask.kind)
        return _finish(
            store, key, intent, verdict,
            {
                "reply": ask.question,
                "state": "filling",
                "need": ask.need,
                "matched": False,
                "action": "clarify",
                "clarifyKind": ask.kind,
            },
            stack,            prev_state,
        )

    if intent in ("order", "refund", "policy", "course", "progress"):
        stack.push(intent)

    result: dict[str, Any]
    if manual:
        result = _handoff(store, session, user_id, message, "ticket", session.slots.get("orderNo", ""), FALLBACK_ANSWER)

    elif intent == "refund":
        if not session.slots.get("orderNo"):
            store.put_slot(key, "need", "orderNo", "ask")
            result = {
                "reply": order_skill.ask_with_recent(user_id),
                "state": "filling",
                "need": "orderNo",
                "matched": False,
            }
        else:
            outcome = refund_skill.apply(user_id, session.slots["orderNo"], message)
            result = {
                "reply": outcome["reply"],
                "state": "filling" if outcome.get("need") else "answering",
                "need": outcome.get("need"),
                "matched": False,
                "refundNo": outcome.get("refundNo"),
            }

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

    if result["state"] == "answering" and stack.top() is not None and stack.top().kind == intent:
        stack.pop()
    return _finish(store, key, intent, verdict, result, stack, prev_state)
