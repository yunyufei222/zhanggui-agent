"""课程咨询技能（T2.6）：回复里的每个数字都必须来自库。

老实现把整句「通用编程入门班多少钱」当 LIKE 模式去匹配 series_name，必然落空；
就算命中，也只有名字和价格区间——没有授课方式、班期、余位，更没有
「你已经报过这门课、订单是什么状态」。P⑲ 说的「上游信息残缺拖低回复质量」就是这个。
"""

from __future__ import annotations

import re
from typing import Any

from app.database import fetch_all, fetch_one

DELIVERY_CN = {
    "online_live": "直播",
    "online_recorded": "录播",
    "offline": "线下面授",
    "mixed": "直播+录播",
    "online": "在线",
}

STATUS_CN = {
    "pending": "待支付",
    "paid": "已支付",
    "completed": "已完成",
    "cancelled": "已取消",
    "partial_refunded": "部分退款",
    "refunded": "已退款",
}

BROWSE = re.compile(r"有什么|有哪些|哪些课|都有|啥课|什么课|推荐")
NOISE = re.compile(r"多少钱|价格|学费|费用|怎么样|怎么报|介绍|讲讲|我想|我要|帮我|查一下|查询|请问|课程|这课|一门|的课")
SPLIT = re.compile(r"[\s，。,.!?？!、]+")


def _terms(message: str) -> list[str]:
    """把问句切成 2~4 字的片段，用来和课程名做重叠打分（不要求整句命中）。"""
    cleaned = NOISE.sub(" ", message)
    terms: list[str] = []
    for chunk in SPLIT.split(cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        for size in (4, 3, 2):
            for start in range(0, max(1, len(chunk) - size + 1)):
                piece = chunk[start : start + size]
                if len(piece) == size:
                    terms.append(piece)
    return list(dict.fromkeys(terms))


def _load_series() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT MIN(s.id) AS id, s.series_code, s.series_name, s.delivery_mode,
               MIN(c.sale_price) AS min_price, MAX(c.sale_price) AS max_price,
               COUNT(c.id) AS cohort_count
        FROM series AS s
        LEFT JOIN series_cohort AS c ON c.series_id = s.id AND c.yn = 1
        WHERE s.sale_status = 'on_sale'
        GROUP BY s.series_code, s.series_name, s.delivery_mode
        ORDER BY MIN(s.id)
        """
    )


def _price_text(row: dict[str, Any]) -> str:
    low, high = row["min_price"], row["max_price"]
    if low is None:
        return "暂无在售班期"
    if low == high:
        return f"{float(low):.0f} 元"
    return f"{float(low):.0f}–{float(high):.0f} 元"


def _nearest_cohort(series_code: str) -> dict[str, Any] | None:
    """同一个 series_code 在种子里按机构有多份，班期要跨这些 id 一起看。"""
    return fetch_one(
        """
        SELECT c.cohort_code, c.cohort_name, c.start_date, c.end_date, c.sale_price,
               c.max_student_count, c.current_student_count
        FROM series_cohort AS c
        JOIN series AS s ON s.id = c.series_id
        WHERE s.series_code = %s AND c.yn = 1 AND (c.end_date IS NULL OR c.end_date >= CURDATE())
        ORDER BY c.start_date, c.sale_price
        LIMIT 1
        """,
        (series_code,),
    )


def _owned(user_id: int, series_code: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT o.order_no, o.order_status, i.payable_amount, i.item_name
        FROM `order` AS o
        JOIN order_item AS i ON i.order_id = o.id
        JOIN series_cohort AS c ON c.id = i.cohort_id
        JOIN series AS s ON s.id = c.series_id
        WHERE o.user_id = %s AND s.series_code = %s
        ORDER BY o.id DESC
        LIMIT 1
        """,
        (user_id, series_code),
    )


def _describe(row: dict[str, Any], user_id: int) -> str:
    delivery = DELIVERY_CN.get(str(row["delivery_mode"]), str(row["delivery_mode"]))
    lines = [f"《{row['series_name']}》", f"授课：{delivery}｜价格：{_price_text(row)}（{int(row['cohort_count'])} 个班期）"]

    cohort = _nearest_cohort(str(row["series_code"]))
    if cohort is None:
        lines.append("开班：暂无在售班期，可以先留个意向。")
    else:
        seats = int(cohort["max_student_count"]) - int(cohort["current_student_count"])
        when = str(cohort["start_date"])
        if when < str(__import__("datetime").date.today()):
            lines.append(f"开班：{when} 已开班（{cohort['cohort_code']}），余位 {seats}")
        else:
            lines.append(f"开班：{when} 起（{cohort['cohort_code']}），余位 {seats}")

    owned = _owned(user_id, str(row["series_code"]))
    if owned is not None:
        status = STATUS_CN.get(str(owned["order_status"]), str(owned["order_status"]))
        tail = "可以重新报" if owned["order_status"] == "refunded" else "教务会联系你安排上课"
        lines.append(f"已报名：订单 {owned['order_no']}（{status}，{float(owned['payable_amount']):.0f} 元）——{tail}")
    return "\n".join(lines)


def consult(user_id: int, message: str) -> dict[str, Any]:
    rows = _load_series()
    terms = _terms(message)

    if terms:
        scored = []
        for row in rows:
            name = str(row["series_name"])
            hits = sum(1 for term in terms if term in name)
            if hits:
                scored.append((hits, len(name), row))
        if scored:
            scored.sort(key=lambda item: (-item[0], item[1]))
            best = [row for _hits, _len, row in scored[:3]]
            if len(best) > 1 and scored[1][0] == scored[0][0]:
                lines = [f"找到 {len(best)} 门相近的课："]
                for row in best:
                    lines.append(f"· 《{row['series_name']}》{_price_text(row)}")
                lines.append("想看哪一门？说课程名就行。")
                return {"text": "\n".join(lines), "items": best}
            return {"text": _describe(best[0], user_id), "items": [best[0]]}

        if terms and not BROWSE.search(message):
            return {
                "text": "没找到匹配的课程。可以说得更具体些，比如「通用编程入门班」；或者问「有什么课程」看全部。",
                "items": [],
            }

    # 没给具体课程：列在售的几门（真实的，不凑数）
    if not rows:
        return {"text": "现在没有在售课程。", "items": []}
    lines = [f"在售课程 {len(rows)} 门，先看这几门："]
    for row in rows[:3]:
        lines.append(f"· 《{row['series_name']}》{_price_text(row)}")
    lines.append("想了解哪一门？说课程名就行。")
    return {"text": "\n".join(lines), "items": rows[:3]}
