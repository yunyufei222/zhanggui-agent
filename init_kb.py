"""初始化客服政策知识库（T1.1）：建表 + 导入 20 条政策种子，幂等。

用法：
    uv run init_kb.py            # 或在 venv 里 python init_kb.py

为什么单独一个脚本、而不是塞进 init_db.py：init_db 会重建整个库（drop/create），
日常补一张表不该把 79 万条测试数据一起带走。这里只做 `CREATE TABLE IF NOT EXISTS`
加 `INSERT ... ON DUPLICATE KEY UPDATE`，跑几次都是同一个结果。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pymysql

from app.config import DB_CONFIG

ROOT_DIR = Path(__file__).parent
CSV_PATH = ROOT_DIR / "seeds" / "4_kb" / "kb_article.csv"
SEED_TIME = "2026-11-21 09:00:00"

DDL = """
CREATE TABLE IF NOT EXISTS kb_article (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    article_no VARCHAR(32) NOT NULL,
    category VARCHAR(32) NOT NULL,
    title VARCHAR(128) NOT NULL,
    keywords VARCHAR(255) NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    priority INT NOT NULL DEFAULT 100,
    yn TINYINT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uk_kb_article_no (article_no),
    KEY idx_kb_category (category),
    KEY idx_kb_yn_priority (yn, priority)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '客服政策知识库'
"""

UPSERT = """
INSERT INTO kb_article (article_no, category, title, keywords, content, priority, yn, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    category = VALUES(category),
    title = VALUES(title),
    keywords = VALUES(keywords),
    content = VALUES(content),
    priority = VALUES(priority),
    yn = VALUES(yn),
    updated_at = VALUES(updated_at)
"""


def main() -> int:
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"种子为空：{CSV_PATH}")
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(DDL)
            for row in rows:
                cursor.execute(
                    UPSERT,
                    (
                        row["article_no"],
                        row["category"],
                        row["title"],
                        row["keywords"],
                        row["content"],
                        int(row["priority"]),
                        int(row["yn"]),
                        SEED_TIME,
                        SEED_TIME,
                    ),
                )
            cursor.execute("SELECT COUNT(*) FROM kb_article WHERE yn = 1")
            total = int(cursor.fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    print(f"kb_article 就绪：种子 {len(rows)} 条，启用 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
