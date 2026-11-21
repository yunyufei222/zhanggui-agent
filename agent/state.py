"""对话状态机骨架（D5=A：自写，不引 LangGraph）。

只做一件事：把「现在能做什么、下一步去哪」写成一张可核对的表。
非法转移直接抛错——宁可让调用方炸，也不要静默走错分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 六个站：进线 → 判读 → 补槽 → 作答 → 转人工 → 收口
STATES = ("idle", "reading", "filling", "answering", "handoff", "closed")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "idle": ("reading",),
    "reading": ("filling", "answering", "handoff", "closed"),
    "filling": ("filling", "answering", "handoff", "closed"),
    "answering": ("filling", "closed"),
    "handoff": ("closed",),
    "closed": (),
}

EVENTS = ("message", "slot_filled", "policy_matched", "policy_weak", "user_asks_human", "resolved")


class IllegalTransition(RuntimeError):
    """状态机拒绝的转移：带上起点、终点与事件，方便回放。"""


@dataclass
class Session:
    """一次会话的最小状态：谁、在哪一站、攒了什么。"""

    session_id: str
    user_id: int | None = None
    state: str = "idle"
    slots: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def advance(self, event: str, to: str | None = None) -> str:
        if event not in EVENTS:
            raise IllegalTransition(f"未注册的事件：{event}")
        target = to or next_state(self.state, event)
        if target not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state} 不能因为 {event} 走到 {target}（允许：{TRANSITIONS[self.state]}）")
        self.history.append({"from": self.state, "to": target, "event": event})
        self.state = target
        return target


def next_state(state: str, event: str) -> str:
    """事件 → 下一站。表就是契约，改行为先改表。"""
    if state not in TRANSITIONS:
        raise IllegalTransition(f"没有这个状态：{state}")
    if event not in EVENTS:
        raise IllegalTransition(f"未注册的事件：{event}")
    rules = {
        "message": "reading",
        "slot_filled": "answering",
        "policy_matched": "answering",
        "policy_weak": "handoff",
        "user_asks_human": "handoff",
        "resolved": "closed",
    }
    return rules.get(event, state)
