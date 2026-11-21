#!/usr/bin/env python
"""演示数据固定（T4.2）：把「演示用谁」从代码里搬到一个可读的文件里。

问题：e2e.py 之前用 `ORDER BY id LIMIT 1` 挑学员和班期——那等于「谁先建库谁就是演示主」，
换台机器、重灌一次种子，学历、班期、价格全变，十条断言的期望值就跟着漂。

现在只认业务键（手机号 / series_code / cohort_code），由本脚本解析成 id：
    python seed_demo.py            # 检查：解析并打印，缺任何一项就退出 1
    python seed_demo.py --write    # 把解析结果写回 demo.json 的 resolved 段
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DB_PASSWORD", "123456")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import fetch_one  # noqa: E402

MANIFEST = ROOT / "demo.json"


class DemoDataMissing(RuntimeError):
    """演示数据找不到——通常是没有灌种子，或者换了库。"""


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def resolve(manifest: dict | None = None) -> dict[str, int | str]:
    data = manifest or load_manifest()
    mobile = data["user"]["mobile"]
    series_code = data["series"]["code"]
    cohort_code = data["cohort"]["code"]

    user = fetch_one(
        "SELECT u.id AS user_id, sp.id AS student_id "
        "FROM sys_user AS u JOIN student_profile AS sp ON sp.user_id = u.id "
        "WHERE u.mobile = %s AND sp.yn = 1 LIMIT 1",
        (mobile,),
    )
    if user is None:
        raise DemoDataMissing(f"找不到手机号 {mobile} 对应的学员档案（先跑 init_db.py 灌种子）")

    series = fetch_one(
        "SELECT id, series_code, sale_status FROM series WHERE series_code = %s LIMIT 1",
        (series_code,),
    )
    if series is None:
        raise DemoDataMissing(f"找不到 series_code={series_code}")

    cohort = fetch_one(
        "SELECT id, institution_id, cohort_code, sale_price FROM series_cohort "
        "WHERE cohort_code = %s AND series_id = %s LIMIT 1",
        (cohort_code, int(series["id"])),
    )
    if cohort is None:
        raise DemoDataMissing(f"找不到 cohort_code={cohort_code}（series_id={series['id']}）")

    return {
        "userId": int(user["user_id"]),
        "studentId": int(user["student_id"]),
        "seriesId": int(series["id"]),
        "cohortId": int(cohort["id"]),
        "institutionId": int(cohort["institution_id"]),
        "seriesCode": str(series["series_code"]),
        "cohortCode": str(cohort["cohort_code"]),
        "salePrice": float(cohort["sale_price"]),
    }


def main() -> int:
    data = load_manifest()
    try:
        demo = resolve(data)
    except DemoDataMissing as error:
        print(f"✗ {error}")
        return 1

    print("演示数据解析成功：")
    for key in ("userId", "studentId", "seriesId", "cohortId", "institutionId", "salePrice"):
        print(f"  {key:<14} {demo[key]}")
    print(f"  {'seriesCode':<14} {demo['seriesCode']}")
    print(f"  {'cohortCode':<14} {demo['cohortCode']}")

    expected = float(data["expect"]["paidAmount"])
    if abs(expected - demo["salePrice"]) > 0.01:
        print(f"! demo.json 的 paidAmount={expected} 与班期售价 {demo['salePrice']} 不一致")

    if "--write" in sys.argv:
        data["resolved"] = {
            key: demo[key] for key in ("userId", "studentId", "seriesId", "cohortId", "institutionId")
        }
        MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✓ 已写回 {MANIFEST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
