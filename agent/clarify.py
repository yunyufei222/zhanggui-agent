"""四类澄清（T2.5）：宁可多问一句，也别猜错一件（P26）。

四类触发，各有明确阈值，全部可解释、可测试：
1. conflict     —— 同一句里「要退款」和「不退了」同时出现；
2. multi_intent —— 两个意图都拿到 ≥2.0 的强证据（一句话两件事）；
3. ambiguous    —— 有意图但置信度 < 0.5（规则没把握，LLM 也没把握）；
4. missing_param—— 意图清楚、缺关键参数（单号 / 哪一门），由各分支追问时标注。

澄清不是转人工：用户补一句就能继续，所以 state 仍是 filling，不落 handoff。
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_CN = {
    "order": "订单",
    "refund": "退款",
    "policy": "政策",
    "course": "课程",
    "progress": "进度",
    "human": "人工",
    "greeting": "打招呼",
    "unknown": "其他",
}

MULTI_MIN = 2.0
AMBIGUOUS_BELOW = 0.5
NEGATIONS = ("不退", "别退", "不想退", "不用退", "先不退")


@dataclass
class Clarify:
    kind: str
    question: str
    need: str | None = None


def decide(
    message: str,
    intent: str,
    confidence: float,
    ranked: list[tuple[str, float, list[str]]],
    has_order_no: bool = False,
) -> Clarify | None:
    # 1. 冲突：想退又不想退
    mentions_refund = any(word in message for word in ("退款", "退钱", "退课"))
    if mentions_refund and any(word in message for word in NEGATIONS):
        return Clarify("conflict", "你是想申请退款，还是先不退了？我按你说的办。")

    # 2. 多意图：两个意图都够强；带单号时「订单」只是指路，不算第二件事
    strong = [(name, score) for name, score, _ in ranked if score >= MULTI_MIN]
    if has_order_no:
        strong = [(name, score) for name, score in strong if name != "order"]
    if len(strong) >= 2:
        first, second = strong[0][0], strong[1][0]
        return Clarify(
            "multi_intent",
            f"这句话里有两件事：{KIND_CN.get(first, first)}和{KIND_CN.get(second, second)}。先办哪一件？",
        )

    # 3. 模糊：规则只蹭到一个弱词，硬答就是猜
    if intent not in ("unknown", "greeting") and 0 < confidence < AMBIGUOUS_BELOW:
        return Clarify(
            "ambiguous",
            f"你是指{KIND_CN.get(intent, intent)}相关的事吗？能再说具体一点吗——比如哪门课、哪个单号。",
        )
    return None
