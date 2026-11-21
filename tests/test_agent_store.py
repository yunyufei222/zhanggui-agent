"""T2.2：会话四表（session / turn / slot / trace）的读写层。

判据是「回放」：同一个 session_key 重新载入，必须还原到上次的状态和槽位；
两个会话不许串槽；每走一站都要在 trace 里留下可查的一行。
"""

from __future__ import annotations

import uuid

import pytest

from app.database import fetch_all, fetch_one
from agent.state import Session
from agent.store import Store, new_session_key


@pytest.fixture()
def store() -> Store:
    return Store()


@pytest.fixture()
def session_key(store: Store) -> str:
    key = new_session_key()
    store.start_session(key, user_id=1, intention="refund")
    return key


def test_start_creates_open_session(store, session_key):
    row = fetch_one("SELECT user_id, intention, status FROM dialogue_session WHERE session_key = %s", (session_key,))
    assert row is not None
    assert row["user_id"] == 1
    assert row["intention"] == "refund"
    assert row["status"] == "open"


def test_turns_number_from_one(store, session_key):
    assert store.append_turn(session_key, "user", "我的订单到哪了") == 1
    assert store.append_turn(session_key, "assistant", "帮您查一下") == 2
    rows = fetch_all("SELECT turn_no, role FROM dialogue_turn WHERE session_key = %s ORDER BY turn_no", (session_key,))
    assert [(row["turn_no"], row["role"]) for row in rows] == [(1, "user"), (2, "assistant")]


def test_slots_latest_wins(store, session_key):
    store.put_slot(session_key, "orderNo", "ORD-A", "user")
    store.put_slot(session_key, "orderNo", "ORD-B", "user")
    store.put_slot(session_key, "reason", "refund", "ask")
    slots = store.slots(session_key)
    assert slots["orderNo"] == "ORD-B"
    assert slots["reason"] == "refund"


def test_state_and_slots_survive_resume(store, session_key):
    store.put_slot(session_key, "orderNo", "ORD-C", "user")
    store.save_state(session_key, "filling")
    resumed = store.load(session_key)
    assert isinstance(resumed, Session)
    assert resumed.session_id == session_key
    assert resumed.state == "filling"
    assert resumed.slots == {"orderNo": "ORD-C"}


def test_trace_is_append_only_and_queryable(store, session_key):
    store.trace(session_key, "input", "查订单")
    store.trace(session_key, "intent", "order_query")
    rows = fetch_all("SELECT step FROM dialogue_trace WHERE session_key = %s ORDER BY id", (session_key,))
    assert [row["step"] for row in rows] == ["input", "intent"]


def test_close_marks_session_closed(store, session_key):
    store.close(session_key)
    row = fetch_one("SELECT status FROM dialogue_session WHERE session_key = %s", (session_key,))
    assert row["status"] == "closed"


def test_two_sessions_do_not_leak_slots(store):
    a = new_session_key()
    b = new_session_key()
    store.start_session(a, user_id=1)
    store.start_session(b, user_id=1)
    store.put_slot(a, "orderNo", "ORD-A", "user")
    assert store.slots(b) == {}
    assert store.slots(a) == {"orderNo": "ORD-A"}
