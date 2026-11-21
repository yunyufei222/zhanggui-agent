"""任务栈（T2.11）：用户在一句一句里切换的事，得有个东西记住。

规矩：
- 同一类任务连推两次不叠层（把新槽位并进栈顶），否则「再问一次订单」会堆成十层；
- 取消 = 弹出栈顶，并让上层重新露出来（不静默清空）；
- 落盘用 dialogue_slot 的 taskStack（append-only，读最新），换进程也能接上。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class Task:
    kind: str
    slots: dict[str, str] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


class TaskStack:
    def __init__(self, items: list[Task] | None = None) -> None:
        self.items: list[Task] = list(items or [])

    def push(self, kind: str, slots: dict[str, str] | None = None) -> Task:
        if self.items and self.items[-1].kind == kind:
            self.items[-1].slots.update(slots or {})
            return self.items[-1]
        task = Task(kind=kind, slots=dict(slots or {}))
        self.items.append(task)
        return task

    def top(self) -> Task | None:
        return self.items[-1] if self.items else None

    def pop(self) -> Task | None:
        return self.items.pop() if self.items else None

    def cancel(self) -> Task | None:
        return self.pop()

    def to_json(self) -> str:
        return json.dumps(
            [{"kind": task.kind, "slots": task.slots, "at": task.at} for task in self.items],
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "TaskStack":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, list):
            return cls()
        items: list[Task] = []
        for entry in data:
            if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
                continue
            slots = entry.get("slots")
            items.append(
                Task(
                    kind=entry["kind"],
                    slots={str(k): str(v) for k, v in slots.items()} if isinstance(slots, dict) else {},
                    at=float(entry.get("at") or 0),
                )
            )
        return cls(items)


KIND_CN = {
    "order": "查订单",
    "refund": "退款",
    "policy": "查政策",
    "course": "问课程",
    "progress": "查进度",
}
