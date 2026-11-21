#!/usr/bin/env python
"""清掉测试制造的脏数据（只删带测试标记的行，不碰演示数据）。

    python purge_demo.py            # 只看：每张表会删多少
    python purge_demo.py --yes      # 真删（同一事务）

判据是「能证明是测试写的」：
- 订单：remark 以 pytest / e2e T4.1 / probe / browser demo / 契约测试 开头
- 班次：cohort_name 以「测试班」开头（测试里造的）
其余一律不动——演示用的那张单、那几个班次都留着。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DB_PASSWORD", "123456")

import pymysql  # noqa: E402

from app.config import DB_CONFIG  # noqa: E402

ORDER_MARKERS = (
    "pytest",
    "e2e T4.1",
    "probe",
    "browser demo",
    "契约测试",
)

ORDER_WHERE = " OR ".join(["remark LIKE %s"] * len(ORDER_MARKERS))
ORDER_PARAMS = tuple(marker + "%" for marker in ORDER_MARKERS)

COHORT_WHERE = "cohort_name LIKE '测试班%'"


def counts(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT id FROM `order` WHERE {ORDER_WHERE}", ORDER_PARAMS)
        orders = [row[0] for row in cursor.fetchall()]
        cursor.execute(f"SELECT id FROM series_cohort WHERE {COHORT_WHERE}")
        cohorts = [row[0] for row in cursor.fetchall()]

        def count(sql: str, params: tuple) -> int:
            cursor.execute(sql, params)
            return int(cursor.fetchone()[0])

        if not orders and not cohorts:
            return out

        order_ids = orders or [0]
        cohort_ids = cohorts or [0]
        item_sql = (
            f"SELECT id FROM order_item WHERE order_id IN ({','.join(['%s'] * len(order_ids))}) "
            f"OR cohort_id IN ({','.join(['%s'] * len(cohort_ids))})"
        )
        item_params = tuple(order_ids) + tuple(cohort_ids)
        cursor.execute(item_sql, item_params)
        items = [row[0] for row in cursor.fetchall()]
        item_ids = items or [0]

        out["service_ticket"] = count(
            f"SELECT COUNT(*) FROM service_ticket WHERE order_item_id IN ({','.join(['%s'] * len(item_ids))})",
            tuple(item_ids),
        )
        out["refund_request"] = count(
            f"SELECT COUNT(*) FROM refund_request WHERE order_item_id IN ({','.join(['%s'] * len(item_ids))})",
            tuple(item_ids),
        )
        out["student_cohort_rel"] = count(
            f"SELECT COUNT(*) FROM student_cohort_rel WHERE order_item_id IN ({','.join(['%s'] * len(item_ids))})",
            tuple(item_ids),
        )
        out["payment_record"] = count(
            f"SELECT COUNT(*) FROM payment_record WHERE order_id IN ({','.join(['%s'] * len(order_ids))})",
            tuple(order_ids),
        )
        out["order_item"] = len(items)
        out["order"] = len(orders)
        out["series_cohort"] = len(cohorts)
    return out


def purge(conn) -> dict[str, int]:
    done: dict[str, int] = {}
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT id FROM `order` WHERE {ORDER_WHERE}", ORDER_PARAMS)
        order_ids = [row[0] for row in cursor.fetchall()] or [0]
        cursor.execute(f"SELECT id FROM series_cohort WHERE {COHORT_WHERE}")
        cohort_ids = [row[0] for row in cursor.fetchall()] or [0]
        cursor.execute(
            f"SELECT id FROM order_item WHERE order_id IN ({','.join(['%s'] * len(order_ids))}) "
            f"OR cohort_id IN ({','.join(['%s'] * len(cohort_ids))})",
            tuple(order_ids) + tuple(cohort_ids),
        )
        item_ids = [row[0] for row in cursor.fetchall()] or [0]

        def run(table: str, where: str, params: tuple) -> None:
            target = "`order`" if table == "order" else table   # order 是保留字，必须加反引号
            cursor.execute(f"DELETE FROM {target} WHERE {where}", params)
            done[table] = done.get(table, 0) + cursor.rowcount

        marks = ",".join(["%s"] * len(item_ids))
        run("service_ticket_satisfaction_survey",
            f"ticket_id IN (SELECT id FROM service_ticket WHERE order_item_id IN ({marks}))", tuple(item_ids))
        run("service_ticket_follow_record",
            f"ticket_id IN (SELECT id FROM service_ticket WHERE order_item_id IN ({marks}))", tuple(item_ids))
        run("service_ticket", f"order_item_id IN ({marks})", tuple(item_ids))
        run("refund_request", f"order_item_id IN ({marks})", tuple(item_ids))
        run("student_cohort_rel", f"order_item_id IN ({marks})", tuple(item_ids))
        run("payment_record", f"order_id IN ({','.join(['%s'] * len(order_ids))})", tuple(order_ids))
        run("order_item", f"order_id IN ({','.join(['%s'] * len(order_ids))}) "
                          f"OR cohort_id IN ({','.join(['%s'] * len(cohort_ids))})",
            tuple(order_ids) + tuple(cohort_ids))
        run("order", f"id IN ({','.join(['%s'] * len(order_ids))})", tuple(order_ids))
        run("series_cohort", f"id IN ({','.join(['%s'] * len(cohort_ids))})", tuple(cohort_ids))
    return done


def main() -> int:
    apply = "--yes" in sys.argv
    conf = dict(DB_CONFIG)
    conf.pop("autocommit", None)
    conn = pymysql.connect(**conf, autocommit=False)
    try:
        if apply:
            result = purge(conn)
            conn.commit()
            print("已删除：")
        else:
            result = counts(conn)
            print("将删除（dry-run，加 --yes 才真删）：")
        for table, total in result.items():
            print(f"  {table:<32} {total}")
        if not apply:
            print("\n判据：订单 remark 带 pytest/e2e/probe/契约测试 标记；班次名以「测试班」开头。")
        return 0
    except Exception as error:  # noqa: BLE001
        conn.rollback()
        print(f"✗ 回滚了：{error}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
