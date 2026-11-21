"""学习进度技能（T2.8）：班次名模糊匹配，同名班次不瞎猜。

P⑯ 的两个错配：
1. 简称与中文数字——用户说「五期」，库里是「第5期」，直接 LIKE 必然找不到；
2. **同名班次**——库里真有两期都叫「数据库开发实战班·直播 202611期」（开课日期不同），
   挑一个就答等于瞎猜，必须把日期摆出来让用户选。
"""

from __future__ import annotations

import re
from typing import Any

from app.database import fetch_all

NOISE = re.compile(r"进度|学到|学哪|上到|学到哪|哪了|第几章|我的|查一下|查询|看看|课程|班次|怎么样了|情况")
CN_DIGITS = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
STATUS_CN = {"active": "在读", "completed": "已结课", "refunded": "已退款", "cancelled": "已取消"}


def cn_to_digits(text: str) -> str:
    """「五期」→「5期」「二十期」→「20期」；只处理 0~99，够用且可解释。"""
    text = re.sub(f"[{''.join(CN_DIGITS)}]十[{''.join(CN_DIGITS)}]", lambda m: str(
        int(CN_DIGITS[m.group(0)[0]]) * 10 + int(CN_DIGITS[m.group(0)[2]])
    ), text)
    text = re.sub(f"[{''.join(CN_DIGITS)}]十", lambda m: str(int(CN_DIGITS[m.group(0)[0]]) * 10), text)
    text = re.sub("十", "10", text)
    for word, digit in CN_DIGITS.items():
        text = text.replace(word, digit)
    return text


def normalize(text: str) -> str:
    return re.sub(r"[\s·•\-—_（）()【】\[\]]", "", cn_to_digits(text or "").lower())


def enrollments(user_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT c.id AS cohort_id, c.cohort_name, c.start_date, c.end_date,
               s.series_name, r.enroll_status,
               DATEDIFF(c.end_date, CURDATE()) AS days_left
        FROM student_cohort_rel AS r
        JOIN series_cohort AS c ON c.id = r.cohort_id
        LEFT JOIN series AS s ON s.id = c.series_id
        WHERE r.user_id = %s
        ORDER BY c.start_date DESC, c.id DESC
        """,
        (user_id,),
    )


def _longest_common(query: str, name: str) -> int:
    """query 与 name 的最长公共子串长度（>=2 才算命中，避免「班」这种单字乱配）。"""
    if not query or not name:
        return 0
    best = 0
    for size in range(min(len(query), 8), 1, -1):
        for start in range(0, len(query) - size + 1):
            if query[start : start + size] in name:
                best = size
                break
        if best:
            break
    return best


def _score(query: str, row: dict[str, Any]) -> int:
    return max(
        _longest_common(query, normalize(str(row["cohort_name"]))),
        _longest_common(query, normalize(str(row["series_name"] or ""))),
    )


def _when(row: dict[str, Any]) -> str:
    left = row["days_left"]
    if left is None:
        state = "未设结课日"
    elif int(left) < 0:
        state = f"已结束 {abs(int(left))} 天"
    elif int(left) == 0:
        state = "今天结课"
    else:
        state = f"剩余 {int(left)} 天"
    end = row["end_date"] or "未设结课日"
    return f"{row['start_date']} → {end}（{state}）"


def _describe(row: dict[str, Any]) -> str:
    status = STATUS_CN.get(str(row["enroll_status"]), str(row["enroll_status"]))
    return "\n".join(
        [
            f"{row['series_name'] or row['cohort_name']} · {row['cohort_name']}",
            f"时间：{_when(row)}",
            f"报名：{status}",
            "课时级进度还没有数据源，这里能看到的是班次层面。",
        ]
    )


def _list(rows: list[dict[str, Any]], header: str) -> str:
    lines = [header]
    for index, row in enumerate(rows[:5], start=1):
        lines.append(f"{index}. {row['cohort_name']}｜{row['start_date']} 开课")
    lines.append("说期数或开课日期，我再看具体那一期。")
    return "\n".join(lines)


def consult(user_id: int, message: str) -> dict[str, Any]:
    rows = enrollments(user_id)
    if not rows:
        return {"reply": "我这边查到你还没有任何班次记录，所以算不出学习进度。先报名一门课再来看。", "need": None}

    query = normalize(NOISE.sub(" ", message))
    scored = [(_score(query, row), row) for row in rows]
    hits = [(score, row) for score, row in scored if score >= 2]

    if not hits:
        return {"reply": _list(rows, "没找到对应的班次。你名下现在有这些："), "need": "cohort"}

    best_score = max(score for score, _row in hits)
    top = [row for score, row in hits if score == best_score]

    if len(top) > 1:
        names = {str(row["cohort_name"]) for row in top}
        if len(names) == 1:
            head = f"查到 {len(top)} 个都叫「{top[0]['cohort_name']}」的班次，开课日期不一样，你要看哪一期？"
        else:
            head = f"找到 {len(top)} 个相关班次（含直播/录播等不同形式），你要看哪一期？"
        lines = [head]
        for index, row in enumerate(top, start=1):
            lines.append(f"{index}. {row['cohort_name']}｜{_when(row)}")
        lines.append("说日期或第几个就行。")
        return {"reply": "\n".join(lines), "need": "cohort"}

    return {"reply": _describe(top[0]), "need": None}
