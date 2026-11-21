"""`.env` 解析的容错（2026-11-21 真踩过的坑）。

用户给 .env 每行加了中文行尾注释：`DB_PASSWORD=*** # 数据库密码`。
python-dotenv 不会剥掉行尾的 `# 数据库密码`，于是口令变成 `***# 数据库密码`，
pymysql 用 latin-1 编码口令时报 UnicodeEncodeError，服务启动就 db=down。
"""

from __future__ import annotations

from app.config import clean_env_value


def test_inline_comment_is_stripped():
    assert clean_env_value("*** # 数据库密码") == "***"
    assert clean_env_value("127.0.0.1 # 数据库地址") == "127.0.0.1"
    assert clean_env_value("3306\t# 端口") == "3306"


def test_quoted_value_keeps_hash():
    assert clean_env_value("'p#ss word'") == "p#ss word"
    assert clean_env_value('"a # b"') == "a # b"


def test_plain_and_empty():
    assert clean_env_value("123456") == "123456"
    assert clean_env_value("") == ""
    assert clean_env_value(None) == ""


def test_hash_without_space_is_part_of_value():
    """`abc#def` 这种没空格的 # 是口令的一部分，不能切。"""
    assert clean_env_value("abc#def") == "abc#def"
