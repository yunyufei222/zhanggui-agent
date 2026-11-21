"""Application configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent

load_dotenv(ROOT_DIR / ".env")


def clean_env_value(text: str | None) -> str:
    """容忍 `.env` 里的行尾注释：`KEY=123456 # 数据库密码` → `123456`。

    人写配置喜欢加中文注释，但 python-dotenv 会把 `# 数据库密码` 当成值的一部分——
    结果口令变成 `123456# 数据库密码`，连库时报 latin-1 编码错。引号里的 `#` 不动。
    """
    if text is None:
        return ""
    value = str(text).strip()
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        return value[1:-1]
    for marker in (" #", "\t#"):
        if marker in value:
            value = value.split(marker, 1)[0]
    return value.strip()


# dotenv 的宽容层：环境变量优先，缺的才从 .env 补，并且补之前先剥掉行尾注释
for _key, _raw in (dotenv_values(ROOT_DIR / ".env") or {}).items():
    if _key and _key not in os.environ:
        os.environ[_key] = clean_env_value(_raw)

APP_PORT = int(os.getenv("APP_PORT", "8000"))
DEMO_PAYMENT_SIGNATURE = os.getenv("DEMO_PAYMENT_SIGNATURE", "mock-payment-signature")

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ["DB_PORT"]),
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
    "charset": "utf8mb4",
    "autocommit": False,
}
