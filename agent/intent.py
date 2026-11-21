"""意图识别（T2.4）：规则打底，LLM 只在拿不准时兜底，结论一律白名单校验。

为什么不直接上 LLM（D1=A 允许兜底，但不等于凡事都问）：
- 五类常见问句用关键词权重就能到 0.8+，本地、零延迟、可解释；
- 真正模糊的（「帮我看看那个」）才值得一次网络往返；
- 模型返回什么都要过 `INTENTS` 白名单——它说「发射导弹」也不能变成意图。

置信度口径：top 权重 / 3，封顶 1.0；前两名咬得紧（≥80%）再打六折。
这个数只用于「要不要问 LLM」和留痕，不当概率用。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

INTENTS = ("order", "refund", "policy", "course", "progress", "human", "greeting", "unknown")

# 关键词 → 权重。宁可少写，也别把「退款」单独塞进 refund：
# 「退款多久到账」问的是政策，不是要发起退款。
RULES: dict[str, tuple[tuple[str, float], ...]] = {
    "order": (("订单", 2.0), ("单号", 1.5), ("买了", 1.5), ("付款", 1.0), ("支付", 1.0), ("到哪", 0.5)),
    "refund": (("退款申请", 2.0), ("我要退款", 2.0), ("申请退款", 2.0), ("退钱", 1.5), ("退课", 1.5)),
    "policy": (
        ("多久", 1.5), ("时效", 1.5), ("规则", 1.5), ("政策", 1.5),
        ("几天", 1.5), ("能不能退", 1.5), ("怎么退", 1.5), ("到账", 1.5), ("流程", 1.2),
    ),
    "course": (("课程", 2.0), ("多少钱", 1.5), ("价格", 1.5), ("讲什么", 1.5), ("什么课", 1.5)),
    "progress": (("进度", 2.0), ("学到", 2.0), ("上到哪", 1.5), ("第几章", 1.5)),
    "human": (("人工", 3.0), ("真人", 3.0), ("客服", 2.0)),
    "greeting": (("你好", 1.0), ("在吗", 1.0), ("谢谢", 1.0)),
}

ClassifyFn = Callable[[str], "str | None"]
PROMPT = (
    "判断用户这句话属于哪类意图，只回一个词，不要解释。"
    "可选：order（查订单）/ refund（要退款）/ policy（问政策规则）/ course（问课程）/ "
    "progress（问学习进度）/ human（要人工）/ greeting（打招呼）/ unknown。句子："
)


@dataclass
class Intent:
    intent: str
    confidence: float
    source: str
    hits: list[str] = field(default_factory=list)
    fallback: bool = False   # 问过 LLM 但没拿到可用结论 → 降级用规则

    def __str__(self) -> str:  # 方便写 trace
        return f"{self.intent}/{self.confidence:.2f}/{self.source}"


def _rule_score(text: str) -> tuple[str, float, list[str]]:
    low = text.lower()
    scores: list[tuple[float, str, list[str]]] = []
    for intent, words in RULES.items():
        hits = [word for word, _ in words if word in low]
        score = sum(weight for word, weight in words if word in low)
        if hits:
            scores.append((score, intent, hits))
    if not scores:
        return "unknown", 0.0, []
    scores.sort(reverse=True)
    top_score, top_intent, hits = scores[0]
    confidence = min(1.0, top_score / 3.0)
    if len(scores) > 1 and scores[1][0] >= top_score * 0.8:
        confidence *= 0.6
    return top_intent, round(confidence, 3), hits


def scores(text: str) -> list[tuple[str, float, list[str]]]:
    """完整打分表（高到低），澄清要判断「一句话里几件事」就得看全表。"""
    low = text.lower()
    ranked: list[tuple[float, str, list[str]]] = []
    for intent, words in RULES.items():
        hits = [word for word, _ in words if word in low]
        if hits:
            ranked.append((sum(weight for word, weight in words if word in low), intent, hits))
    ranked.sort(reverse=True)
    return [(intent, score, hits) for score, intent, hits in ranked]


def classify(text: str, llm: ClassifyFn | None = None, threshold: float = 0.5) -> Intent:
    """规则优先；只有规则拿不准、并且给了 llm 时，才问一次。"""
    intent, confidence, hits = _rule_score(text)
    base = Intent(intent, confidence, "rule", hits)
    if llm is None or confidence >= threshold:
        return base
    try:
        guess = llm(text)
    except Exception:  # LLM 挂了不该影响对话
        base.fallback = True
        return base
    if isinstance(guess, str) and guess.strip().lower() in INTENTS:
        return Intent(guess.strip().lower(), 0.6, "llm", hits)
    base.fallback = True
    return base


class HttpLLM:
    """OpenAI 兼容的意图分类器（DeepSeek / 任何 /chat/completions 都行）。

    只发一个最小请求；超时、网络错误、返回不合格式，一律返回 None——上层退回规则。
    走本机直连（ProxyHandler({})），不吃系统代理：代理进程不在时 urllib 会直接 10061。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> None:
        from .skill_config import config

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout if timeout is not None else config.get("external.timeout_s", 3.0))
        self.retries = int(retries if retries is not None else config.get("external.retries", 1))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(self, text: str) -> str | None:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": PROMPT + text}],
                "temperature": 0,
                "max_tokens": 8,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={"content-type": "application/json", "authorization": "Bearer " + self.api_key},
            method="POST",
        )
        # 超时/断网只重试一次：再多也没用，反而把一轮对话拖长
        for _attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                content = payload["choices"][0]["message"]["content"]
                break
            except Exception:  # noqa: BLE001 —— 归类失败不该变成对话失败
                continue
        else:
            return None
        word = str(content or "").strip().strip("。.!！`\"'").split()
        return word[0].lower() if word else None
