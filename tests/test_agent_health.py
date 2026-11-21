"""T2.1：agent 工程骨架 + /health。

判据：服务活着能自证（数据库通、政策库读得到、状态机表在），
状态机的非法转移必须当场炸，不许静默走错。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import app
from agent.state import TRANSITIONS, IllegalTransition, Session, next_state


def test_health_reports_db_and_kb():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["db"] == "up"
    assert data["kbArticles"] == 20
    assert data["version"]


def test_state_machine_table_is_consistent():
    """表里的每个目标态都必须是一个注册过的状态，否则图会指向不存在的地方。"""
    from agent.state import STATES

    assert set(TRANSITIONS) == set(STATES)
    for source, targets in TRANSITIONS.items():
        for target in targets:
            assert target in STATES, f"{source} → {target} 指向未注册的状态"


def test_session_walks_the_happy_path():
    session = Session(session_id="s1", user_id=1)
    assert session.state == "idle"
    session.advance("message")            # idle → reading
    assert session.state == "reading"
    session.advance("slot_filled")        # reading → answering
    assert session.state == "answering"
    session.advance("resolved")           # answering → closed
    assert session.state == "closed"
    assert [step["to"] for step in session.history] == ["reading", "answering", "closed"]


def test_weak_policy_goes_to_handoff():
    session = Session(session_id="s2")
    session.advance("message")
    session.advance("policy_weak")
    assert session.state == "handoff"


def test_illegal_transition_raises():
    session = Session(session_id="s3")
    with pytest.raises(IllegalTransition):
        session.advance("resolved")  # idle 不能直接 closed


def test_unknown_event_raises():
    with pytest.raises(IllegalTransition):
        next_state("idle", "launch_missiles")
