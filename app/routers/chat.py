"""Chat API — M1 的第一刀：规则版意图 → 库里真值应答。

不依赖 LLM（D1 尚未拍板），不建会话表（D2 尚未拍板）。
每一步都写进 trace 随响应返回；会话持久化留给 M2。
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from datetime import datetime

from ..database import db_cursor, fetch_all, fetch_one
from ..errors import conflict, not_found
from ..dependencies import get_current_user_id
from ..response import ok

router = APIRouter(prefix="/api/v1", tags=["chat"])

SLOT_ORDER = "orderNo"
SLOT_DETAIL = "detail"
REFUND_KEYS = ("退款", "退钱", "退费")
TICKET_KEYS = ("工单", "投诉", "举报")
ORDER_NO_RE = re.compile(r"ORD[0-9A-Za-z]{4,}", re.I)


def load_slots(session_key: str) -> dict[str, str]:
    rows = fetch_all("SELECT slot_key, slot_value FROM dialogue_slot WHERE session_key = %s ORDER BY id DESC", (session_key,))
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(r["slot_key"], r["slot_value"])
    return out


def save_slot(session_key: str, key: str, value: str, source: str) -> None:
    with db_cursor() as (_, cur):
        cur.execute(
            "INSERT INTO dialogue_slot (session_key, slot_key, slot_value, source, created_at) VALUES (%s, %s, %s, %s, %s)",
            (session_key, key, value, source, datetime.now()),
        )


def extract_order_no(text: str) -> str:
    m = ORDER_NO_RE.search(text or "")
    return m.group(0) if m else ""


INTENTS = (
    ("course", ("课程", "课", "班", "多少钱", "价格", "讲什么")),
    ("order", ("订单", "ord", "买了", "付款", "退款")),
    ("progress", ("进度", "学到", "学习", "上到哪", "开了几")),
)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    sessionKey: str | None = Field(default=None, max_length=64)


def detect(text: str) -> str:
    low = text.lower()
    for name, keys in INTENTS:
        if any(k in low for k in keys):
            return name
    return "unknown"


def answer_course(text: str) -> dict[str, Any]:
    like = "%" + re.sub(r"^(我想|我要|帮我|查|看)+", "", text.strip())[:20] + "%"
    rows = fetch_all(
        """
        SELECT s.id, s.series_code, s.series_name, s.sale_status,
               MIN(c.sale_price) AS min_price, MAX(c.sale_price) AS max_price,
               COUNT(c.id) AS cohort_count
        FROM series AS s
        LEFT JOIN series_cohort AS c ON c.series_id = s.id AND c.yn = 1
        WHERE s.series_name LIKE %s
        GROUP BY s.id, s.series_code, s.series_name, s.sale_status
        ORDER BY cohort_count DESC
        LIMIT 3
        """,
        (like,),
    )
    if not rows:
        return {"text": "没找到匹配的课程。可以说得更具体些，比如「数据分析」或「考研英语」。", "items": []}
    lines = []
    for r in rows:
        price = "暂无班次" if r["min_price"] is None else (
            f"{r['min_price']} 元" if r["min_price"] == r["max_price"] else f"{r['min_price']}–{r['max_price']} 元"
        )
        lines.append(f"《{r['series_name']}》（{r['series_code']}）· {r['cohort_count']} 个班次 · {price}")
    return {"text": "查到 " + str(len(rows)) + " 门课：\n" + "\n".join(lines), "items": [dict(r) for r in rows]}


def answer_order(text: str, user_id: int) -> dict[str, Any]:
    m = re.search(r"(ORD[0-9A-Za-z]+)", text, re.I)
    if m:
        row = fetch_one("SELECT * FROM `order` WHERE order_no = %s AND user_id = %s", (m.group(1), user_id))
    else:
        row = fetch_one("SELECT * FROM `order` WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
    if row is None:
        return {"text": "没查到这个订单号名下的订单。", "items": []}
    items = fetch_all("SELECT item_name, order_item_status, payable_amount FROM order_item WHERE order_id = %s", (row["id"],))
    names = "、".join(i["item_name"] for i in items) or "（无明细）"
    return {
        "text": f"订单 {row['order_no']} 状态 {row['order_status']}，应付 {row['payable_amount']} 元，含：{names}",
        "items": [dict(row)] + [dict(i) for i in items],
    }


def answer_progress(text: str, user_id: int) -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT c.cohort_name, c.start_date, c.end_date, sc.series_name,
               DATEDIFF(c.end_date, CURDATE()) AS days_left
        FROM student_cohort_rel AS rel
        JOIN series_cohort AS c ON c.id = rel.cohort_id
        LEFT JOIN series AS sc ON sc.id = c.series_id
        WHERE rel.student_id = (SELECT student_id FROM `order` WHERE user_id = %s AND student_id IS NOT NULL ORDER BY id DESC LIMIT 1)
        ORDER BY c.start_date DESC
        LIMIT 3
        """,
        (user_id,),
    )
    if not rows:
        return {"text": "没有查到报名记录，所以还算不出学习进度。", "items": []}
    lines = []
    for r in rows:
        left = r["days_left"]
        state = "已结束" if left is not None and left < 0 else f"剩 {left} 天"
        lines.append(f"{r['series_name'] or ''} {r['cohort_name']}：{r['start_date']} → {r['end_date']}（{state}）")
    return {"text": "你名下的班次：\n" + "\n".join(lines) + "\n课时级进度还没有数据源，这里只到班次。", "items": [dict(r) for r in rows]}


def open_handoff(user_id: int, session_key: str, reason: str, order_no: str, detail: str) -> int:
    with db_cursor() as (_, cur):
        cur.execute(
            "INSERT INTO dialogue_handoff (session_key, user_id, reason, order_no, detail, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
            (session_key, user_id, reason, order_no, detail[:500], datetime.now()),
        )
        return int(cur.lastrowid)


def take_answer(session_key: str) -> dict[str, Any] | None:
    """该会话最早一条「人工已回复但还没播报」的工单。"""
    row = fetch_one(
        "SELECT id, reason, answer, answered_by FROM dialogue_handoff "
        "WHERE session_key = %s AND status = 'answered' ORDER BY id LIMIT 1",
        (session_key,),
    )
    return dict(row) if row else None


def mark_delivered(handoff_id: int) -> None:
    with db_cursor() as (_, cur):
        cur.execute("UPDATE dialogue_handoff SET status = 'delivered' WHERE id = %s", (handoff_id,))


def persist(user_id: int, message: str, intent: str, reply: str, trace: list[dict[str, Any]], session_key: str = "") -> str:
    """把这一轮的会话/轮次/链路写进四表。失败不拦回答 —— 但要能从库里查回来。"""
    now = datetime.now()
    key = session_key or f"u{user_id}-{now:%Y%m%d}"
    with db_cursor() as (_, cur):
        cur.execute(
            "INSERT INTO dialogue_session (session_key, user_id, intention, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'open', %s, %s) "
            "ON DUPLICATE KEY UPDATE intention = VALUES(intention), updated_at = VALUES(updated_at)",
            (key, user_id, intent, now, now),
        )
        cur.execute("SELECT COALESCE(MAX(turn_no), 0) AS n FROM dialogue_turn WHERE session_key = %s", (key,))
        n = int(cur.fetchone()["n"]) + 1
        cur.execute(
            "INSERT INTO dialogue_turn (session_key, turn_no, role, content, created_at) VALUES (%s, %s, 'user', %s, %s)",
            (key, n, message, now),
        )
        cur.execute(
            "INSERT INTO dialogue_turn (session_key, turn_no, role, content, created_at) VALUES (%s, %s, 'assistant', %s, %s)",
            (key, n, reply, now),
        )
        for t in trace:
            cur.execute(
                "INSERT INTO dialogue_trace (session_key, step, value, created_at) VALUES (%s, %s, %s, %s)",
                (key, str(t.get("step", ""))[:20], str(t.get("value", ""))[:2000], now),
            )
    return key


@router.get("/chat/handoffs")
def list_handoffs(status: str = "pending"):
    rows = fetch_all(
        "SELECT id, session_key, user_id, reason, order_no, detail, status, created_at "
        "FROM dialogue_handoff WHERE status = %s ORDER BY id LIMIT 50",
        (status,),
    )
    return ok({"status": status, "count": len(rows), "items": [dict(r) for r in rows]})


class AnswerBody(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)
    answeredBy: str = Field(default="human", max_length=40)


@router.post("/chat/handoffs/{handoff_id}/answer")
def answer_handoff(handoff_id: int, body: AnswerBody):
    row = fetch_one("SELECT id, status FROM dialogue_handoff WHERE id = %s", (handoff_id,))
    if row is None:
        raise not_found("HANDOFF_NOT_FOUND", "工单不存在")
    if row["status"] != "pending":
        raise conflict("HANDOFF_ALREADY_ANSWERED", "这条工单已经被回复过了")
    with db_cursor() as (_, cur):
        cur.execute(
            "UPDATE dialogue_handoff SET answer = %s, answered_by = %s, answered_at = %s, status = 'answered' WHERE id = %s",
            (body.answer, body.answeredBy, datetime.now(), handoff_id),
        )
    return ok({"id": handoff_id, "status": "answered"})


@router.post("/chat")
def chat(
    body: ChatBody,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
):
    session_key_hint = (body.sessionKey or "").strip()
    # 人工已回复的工单优先播报，然后把它标记为已送达
    if session_key_hint:
        ans = take_answer(session_key_hint)
        if ans:
            mark_delivered(int(ans["id"]))
            text = "【人工回复 · " + str(ans["reason"]) + "】" + str(ans["answer"])
            trace0 = [{"step": "input", "value": body.message},
                      {"step": "handoff", "value": "播报人工回复 #" + str(ans["id"]) + " by " + str(ans["answered_by"] or "?" )},
                      {"step": "answer", "value": text[:60]}]
            key = ""
            try:
                key = persist(current_user_id, body.message, "handoff_reply", text, trace0, session_key_hint)
            except Exception:
                pass
            return ok({"reply": text, "intent": "handoff_reply", "data": [], "trace": trace0, "sessionKey": key})
    
    trace: list[dict[str, Any]] = [{"step": "input", "value": body.message}]
    intent = detect(body.message)
    trace.append({"step": "intent", "value": intent})
    slots = load_slots(session_key_hint) if session_key_hint else {}
    # 「这笔要办什么」也是槽位：第二句里往往不再出现「退款/投诉」字样。
    deal_saved = slots.get("deal", "")
    deal_kind = ("ticket" if any(k in body.message for k in TICKET_KEYS) else
                 "refund" if any(k in body.message for k in REFUND_KEYS) else deal_saved)
    want_deal = deal_kind != ""
    if want_deal or intent == "order":
        got = extract_order_no(body.message) or slots.get(SLOT_ORDER, "")
        if extract_order_no(body.message):
            trace.append({"step": "slot", "value": SLOT_ORDER + "=" + extract_order_no(body.message) + "（来源 user）"})
        if got == "":
            trace.append({"step": "slot", "value": SLOT_ORDER + " 缺失 → 追问（来源 ask）"})
            trace.append({"step": "answer", "value": "请提供订单号"})
            session_key = ""
            try:
                session_key = persist(current_user_id, body.message, intent, "请提供订单号，我好帮你办。", trace, session_key_hint)
                save_slot(session_key, SLOT_ORDER, "", "ask")
                if deal_kind:
                    save_slot(session_key, "deal", deal_kind, "user")
            except Exception:
                pass
            return ok({
                "reply": "办这件事需要订单号 —— 麻烦把「ORD」开头的那串发我，我直接查。",
                "intent": intent,
                "need": SLOT_ORDER,
                "data": [],
                "trace": trace,
                "sessionKey": session_key,
            })
        is_ticket = deal_kind == "ticket"
        result = answer_order(got + " 的订单" if not got.startswith("ORD") else got, current_user_id)
        if want_deal and is_ticket:
            detail_now = re.sub(r"ORD[0-9A-Za-z]{4,}", "", body.message).replace("订单号是", "").replace("订单号", "")
            for kw in TICKET_KEYS + REFUND_KEYS + ("我要", "我想", "帮我", "一下"):
                detail_now = detail_now.replace(kw, "")
            detail_now = detail_now.strip()
            detail_old = slots.get(SLOT_DETAIL, "")
            if len(detail_now) < 4 and not detail_old:
                trace.append({"step": "slot", "value": SLOT_DETAIL + " 缺失 → 追问（来源 ask）"})
                session_key = ""
                try:
                    session_key = persist(current_user_id, body.message, intent, "请说明遇到的问题", trace, session_key_hint)
                    save_slot(session_key, SLOT_ORDER, got, "user")
                    save_slot(session_key, SLOT_DETAIL, "", "ask")
                    save_slot(session_key, "deal", deal_kind, "user")
                except Exception:
                    pass
                return ok({
                    "reply": "订单确认了。再说一句**具体遇到什么问题**（例如「课程看不了」「老师没回」），我一并转给人工。",
                    "intent": intent, "need": SLOT_DETAIL, "data": [], "trace": trace, "sessionKey": session_key,
                })
            detail = detail_now or detail_old
            trace.append({"step": "slot", "value": SLOT_DETAIL + "=" + detail[:30] + "（来源 user）"})
            handoff = {"reason": "ticket", "orderNo": got, "detail": detail, "why": "D3 = A（只读 + 转人工）：不自动提交工单，转人工处理"}
            try:
                handoff["id"] = open_handoff(current_user_id, session_key_hint or "", "ticket", got, detail)
            except Exception:
                handoff["id"] = None
            trace.append({"step": "handoff", "value": "转人工：工单 · 订单 " + got})
            session_key = ""
            try:
                session_key = persist(current_user_id, body.message, intent, "已转人工（工单）", trace, session_key_hint)
                save_slot(session_key, SLOT_ORDER, got, "user")
                save_slot(session_key, SLOT_DETAIL, detail, "user")
                save_slot(session_key, "deal", deal_kind, "user")
                save_slot(session_key, "handoff", "ticket", "object")
            except Exception as exc:
                trace.append({"step": "persist", "value": "失败：" + str(exc)[:120]})
            return ok({
                "reply": "已转人工 —— 我把订单 " + got + " 和「" + detail + "」一起交过去了。人工会在这个工单里回复你。",
                "intent": intent, "data": result.get("items", []), "trace": trace, "sessionKey": session_key, "handoff": handoff,
            })
        if want_deal:
            result["text"] = ("订单已确认。\n" + result["text"]
                + "\n已按 D3 = A（只读 + 转人工）转给人工处理退款 —— 系统不会自动提交，避免误退。")
            handoff = {"reason": "refund", "orderNo": got, "detail": "", "why": "D3 = A：退款不自动提交，转人工"}
            try:
                handoff["id"] = open_handoff(current_user_id, session_key_hint or "", "refund", got, "")
            except Exception:
                handoff["id"] = None
            trace.append({"step": "handoff", "value": "转人工：退款 · 订单 " + got})
        trace.append({"step": "query", "value": "order:" + str(len(result.get("items", []))) + " 行"})
        trace.append({"step": "answer", "value": result["text"][:60]})
        session_key = ""
        try:
            session_key = persist(current_user_id, body.message, intent, result["text"], trace, session_key_hint)
            if extract_order_no(body.message):
                save_slot(session_key, SLOT_ORDER, extract_order_no(body.message), "user")
        except Exception as exc:
            trace.append({"step": "persist", "value": "失败：" + str(exc)[:120]})
        out_handoff = locals().get("handoff")
        try:
            if session_key and out_handoff:
                save_slot(session_key, "handoff", str(out_handoff.get("reason") or ""), "object")
        except Exception:
            pass
        return ok({"reply": result["text"], "intent": intent, "data": result.get("items", []), "trace": trace,
                   "sessionKey": session_key, "handoff": out_handoff})

    if intent == "course":
        result = answer_course(body.message)
    elif intent == "order":
        result = answer_order(body.message, current_user_id)
    elif intent == "progress":
        result = answer_progress(body.message, current_user_id)
    else:
        result = {"text": "这个问题我还答不了。目前能答：课程咨询、订单查询、学习进度。", "items": []}
    trace.append({"step": "query", "value": intent + ":" + str(len(result.get("items", []))) + " 行"})
    trace.append({"step": "answer", "value": result["text"][:60]})
    session_key = ""
    try:
        session_key = persist(current_user_id, body.message, intent, result["text"], trace, session_key_hint)
    except Exception as exc:  # 落库失败不拦回答，但把原因带回去
        trace.append({"step": "persist", "value": "失败：" + str(exc)[:120]})
    return ok({
        "reply": result["text"],
        "intent": intent,
        "data": result.get("items", []),
        "trace": trace,
        "sessionKey": session_key,
    })
