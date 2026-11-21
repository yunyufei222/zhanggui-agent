"""agent 的配置：复用主应用的 DB 配置，端口单独一个。"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import DB_CONFIG

ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION = "0.1.0"
# agent 与主 API 分端口跑：主 API 是 8000，agent 默认 8100，互不影响。
AGENT_PORT = int(os.getenv("AGENT_PORT", "8100"))

__all__ = ["DB_CONFIG", "ROOT_DIR", "VERSION", "AGENT_PORT"]
