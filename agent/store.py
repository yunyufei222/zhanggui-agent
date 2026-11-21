"""会话四表的读写层（T2.2）。

表已经在库里（M1/M2 建的），这里不重造轮子，只做一件事：
**让 agent 的每一步都落进这四张表，并且能原样读回来。**

- `dialogue_session` 一次会话一行：谁、意图、开/关；
- `dialogue_turn`    每一轮一行：role + content，turn_no 由这里分配；
- `dialogue_slot`    槽位 append-only，读的时候按 id 倒序取每个 key 的最新值；
- `dialogue_trace`   每一步一行：input / intent / query / answer / state。

`state` 这一行是 agent 状态机的落点——`load()` 靠它还原到上次那一站，
所以「回放」不依赖任何进程内内存。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.database import db_cursor, fetch_all, fetch_one

from .state import STATES, Session


def new_session_key() -> str:
    """S + 16 位：和库里已有的 S… 键同形，肉眼可分。"""
    return "S" + uuid.uuid4().hex[:16]


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Store:
    """四张表的最小读写面。所有时间由 Python 给，不靠数据库默认值。"""

    # ---------------------------------------------------------------- session
    def start_session(self, session_key: str, user_id: int, intention: str | None = None) -> str:
        now = _now()
        with db_cursor() as (_, cursor):
            cursor.execute(
                """
                INSERT INTO dialogue_session (session_key, user_id, intention, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'open', %s, %s)
                ON DUPLICATE KEY UPDATE updated_at = VALUES(updated_at)
                """,
                (session_key, user_id, intention, now, now),
            )
        return session_key

    def close(self, session_key: str) -> None:
        with db_cursor() as (_, cursor):
            cursor.execute(
                "UPDATE dialogue_session SET status = 'closed', updated_at = %s WHERE session_key = %s",
                (_now(), session_key),
            )

    # ------------------------------------------------------------------- turn
    def append_turn(self, session_key: str, role: str, content: str) -> int:
        """分配 turn_no 并写入；同一会话内严格 +1（放在同一事务里取 MAX）。"""
        now = _now()
        with db_cursor() as (_, cursor):
            cursor.execute(
                "SELECT COALESCE(MAX(turn_no), 0) AS n FROM dialogue_turn WHERE session_key = %s",
                (session_key,),
            )
            turn_no = int(cursor.fetchone()["n"]) + 1
            cursor.execute(
                "INSERT INTO dialogue_turn (session_key, turn_no, role, content, created_at) VALUES (%s, %s, %s, %s, %s)",
                (session_key, turn_no, role, content, now),
            )
            cursor.execute(
                "UPDATE dialogue_session SET updated_at = %s WHERE session_key = %s",
                (now, session_key),
            )
        return turn_no

    # ------------------------------------------------------------------- slot
    def put_slot(self, session_key: str, slot_key: str, slot_value: str, source: str = "user") -> None:
        with db_cursor() as (_, cursor):
            cursor.execute(
                "INSERT INTO dialogue_slot (session_key, slot_key, slot_value, source, created_at) VALUES (%s, %s, %s, %s, %s)",
                (session_key, slot_key, str(slot_value), source, _now()),
            )

    def slots(self, session_key: str) -> dict[str, str]:
        """每个 key 取最新一次写入（append-only 的好处：历史还在，读的时候只要最新）。"""
        rows = fetch_all(
            "SELECT slot_key, slot_value FROM dialogue_slot WHERE session_key = %s ORDER BY id DESC",
            (session_key,),
        )
        out: dict[str, str] = {}
        for row in rows:
            out.setdefault(row["slot_key"], row["slot_value"])
        return out

    # ------------------------------------------------------------------ trace
    def trace(self, session_key: str, step: str, value: Any) -> None:
        with db_cursor() as (_, cursor):
            cursor.execute(
                "INSERT INTO dialogue_trace (session_key, step, value, created_at) VALUES (%s, %s, %s, %s)",
                (session_key, step, str(value), _now()),
            )

    def save_state(self, session_key: str, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"没有这个状态：{state}（允许：{', '.join(STATES)}）")
        self.trace(session_key, "state", state)

    # ------------------------------------------------------------------- load
    def load(self, session_key: str) -> Session:
        """从四张表还原一个会话：状态取最后一条 state trace，槽位取每个 key 的最新值。"""
        row = fetch_one("SELECT user_id FROM dialogue_session WHERE session_key = %s", (session_key,))
        if row is None:
            raise KeyError(f"没有这个会话：{session_key}")
        last_state = fetch_one(
            "SELECT value FROM dialogue_trace WHERE session_key = %s AND step = 'state' ORDER BY id DESC LIMIT 1",
            (session_key,),
        )
        state = str(last_state["value"]) if last_state else "idle"
        if state not in STATES:
            state = "idle"
        return Session(session_id=session_key, user_id=int(row["user_id"]), state=state, slots=self.slots(session_key))
