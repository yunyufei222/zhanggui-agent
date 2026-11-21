"""技能配置加载（T2.15）。

为什么要它：阈值、话术、类型映射原来散在五个模块的顶层常量里，
改一句话术要动代码、要重跑全量测试、评审要看 diff。现在它们在一个 YAML 里，
改完重启即生效；配置坏了启动就报错，而不是线上回答突然变成半句话。

用法：`from .skill_config import config`；取 `config.get("ticket.sla_hours", 4)`。
测试可以把环境变量 AGENT_SKILLS_CONFIG 指向临时文件再 `reload()`。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("skills.yaml")


class ConfigError(RuntimeError):
    """配置坏了——宁可在启动时炸，也别在对话里炸。"""


def _check(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or "version" not in data:
        raise ConfigError("skills.yaml 缺少 version 段")

    clarify = data.get("clarify") or {}
    multi_min = clarify.get("multi_min", 2.0)
    if not isinstance(multi_min, (int, float)) or multi_min <= 0:
        raise ConfigError(f"clarify.multi_min 必须是正数，当前 {multi_min!r}")
    ambiguous = clarify.get("ambiguous_below", 0.5)
    if not isinstance(ambiguous, (int, float)) or not (0 < ambiguous <= 1):
        raise ConfigError(f"clarify.ambiguous_below 必须在 (0, 1] 之间，当前 {ambiguous!r}")
    for key in ("negations", "refund_words"):
        value = clarify.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"clarify.{key} 必须是字符串列表")

    external = data.get("external") or {}
    timeout = external.get("timeout_s", 3.0)
    if not isinstance(timeout, (int, float)) or not (0 < timeout <= 30):
        raise ConfigError(f"external.timeout_s 必须在 (0, 30] 之间，当前 {timeout!r}")
    retries = external.get("retries", 1)
    if not isinstance(retries, int) or not (0 <= retries <= 3):
        raise ConfigError(f"external.retries 必须是 0~3 的整数，当前 {retries!r}")
    if not str(external.get("degrade_text", "")).strip():
        raise ConfigError("external.degrade_text 不能为空——降级时总得给人一句话")

    ticket = data.get("ticket") or {}
    sla = ticket.get("sla_hours", 4)
    if not isinstance(sla, int) or sla <= 0:
        raise ConfigError(f"ticket.sla_hours 必须是正整数，当前 {sla!r}")
    if not isinstance(ticket.get("type_cn", {}), dict):
        raise ConfigError("ticket.type_cn 必须是映射")

    refund = data.get("refund") or {}
    if not isinstance(refund.get("hints", {}), dict):
        raise ConfigError("refund.hints 必须是映射")

    course = data.get("course") or {}
    for key in ("browse", "noise"):
        pattern = course.get(key)
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"course.{key} 必须是非空正则字符串")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ConfigError(f"course.{key} 正则写错了：{error}") from error


class SkillConfig:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("AGENT_SKILLS_CONFIG") or DEFAULT_PATH)
        self.data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(f"读不到技能配置 {self.path}：{error}") from error
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise ConfigError(f"技能配置不是合法 YAML：{error}") from error
        _check(data)
        self.data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def text(self, dotted: str, **kwargs: Any) -> str:
        template = str(self.get(dotted, ""))
        try:
            return template.format(**kwargs) if kwargs else template
        except (KeyError, IndexError) as error:
            raise ConfigError(f"话术 {dotted} 的占位符对不上：{error}") from error


config = SkillConfig()
