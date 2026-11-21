#!/usr/bin/env python
"""T4.1 端到端十条断言（P30：让「看起来行」变得可以否证）。

跑法：
    .venv/Scripts/python.exe e2e.py

它会自己起两个真服务（main:8111 / agent:8112），按一条真实业务链打十枪，
打印 PASS/FAIL，最后给退出码（0 全过 / 1 有失败）。

为什么不用 TestClient：它会绕过中间件、header 解析和序列化——装配错了照样绿。
这里每一枪都是真 HTTP，跟前端打开页面做的是同一件事。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

os.environ.setdefault("DB_PASSWORD", "123456")
# 验收要确定性：不管 .env 里接没接模型，这里一律走规则（模型链路由 tests/test_llm_wiring.py 覆盖）
os.environ["AGENT_LLM_ENABLED"] = "0"

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app.database import fetch_one  # noqa: E402
from seed_demo import load_manifest, resolve as resolve_demo  # noqa: E402

def _free_port(start: int) -> int:
    """从 start 往后找一个没人占的端口。

    不能让 e2e 撞上正在跑的演示服务：绑不上时它不会失败，只会悄悄去连那台
    老进程，结果是「10/10 通过」但测的不是当前代码。宁可换端口，也不冒名顶替。
    """
    import socket

    for port in range(start, start + 40):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"从 {start} 起找不到空闲端口")


MAIN_PORT = int(os.getenv("E2E_MAIN_PORT") or _free_port(8111))
AGENT_PORT = int(os.getenv("E2E_AGENT_PORT") or _free_port(MAIN_PORT + 1))
MAIN = f"http://127.0.0.1:{MAIN_PORT}/api/v1"
AGENT = f"http://127.0.0.1:{AGENT_PORT}"

PASSED: list[int] = []
FAILED: list[int] = []


def http(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 25,
):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"raw": raw}


def user_headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def chat(session_key: str | None, user_id: int, message: str) -> dict:
    body = {"userId": user_id, "message": message}
    if session_key:
        body["sessionKey"] = session_key
    status, payload = http(f"{AGENT}/chat", "POST", body)
    assert status == 200, f"/chat {status} {payload}"
    return payload["data"]


def check(number: int, title: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(number)
        print(f"  PASS {number:>2}. {title}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(number)
        print(f"  FAIL {number:>2}. {title}" + (f" —— {detail}" if detail else ""))


def wait_ready(url: str) -> None:
    for _ in range(120):
        try:
            status, _ = http(url + "/health", timeout=3)
            if status == 200:
                return
        except Exception:  # noqa: BLE001 —— 起服务期间连不上是正常的
            pass
        time.sleep(0.5)
    raise SystemExit(f"服务没起来：{url}/health")


def serve(target: str, port: int) -> None:
    try:
        uvicorn.run(target, host="127.0.0.1", port=port, log_level="warning")
    except BaseException:  # noqa: BLE001 —— 线程里挂了必须喊出来，否则只会卡在等健康检查
        print(f"\n[服务启动失败 {target}:{port}]\n{traceback.format_exc()}", flush=True)


def main() -> int:
    for target, port in (("app.main:app", MAIN_PORT), ("agent.app:app", AGENT_PORT)):
        threading.Thread(target=serve, args=(target, port), daemon=True).start()
    wait_ready(f"http://127.0.0.1:{MAIN_PORT}")
    wait_ready(f"http://127.0.0.1:{AGENT_PORT}")
    print("\n服务就绪：main %d / agent %d\n" % (MAIN_PORT, AGENT_PORT))

    # T4.2：演示对象来自 demo.json（业务键），不是「库里第一条」
    manifest = load_manifest()
    demo = resolve_demo(manifest)
    expect = manifest["expect"]
    user_id = int(demo["userId"])
    student_id = int(demo["studentId"])
    cohort_id = int(demo["cohortId"])
    headers = user_headers(user_id)
    print(
        f"演示对象：user={user_id} student={student_id} cohort={demo['cohortCode']} "
        f"售价={demo['salePrice']}\n"
    )

    # 1 健康
    _, agent_health = http(f"{AGENT}/health")
    _, main_health = http(f"http://127.0.0.1:{MAIN_PORT}/health")
    health = agent_health.get("data", agent_health)
    check(
        1,
        "两个服务健康，知识库就绪",
        bool(agent_health)
        and bool(main_health)
        and int(health.get("kbArticles", 0)) >= 20
        and len(health.get("states", [])) == 6,
        f"kb={health.get('kbArticles')} db={health.get('db')}",
    )

    # 2 下单
    status, order_payload = http(
        f"{MAIN}/orders",
        "POST",
        {
            "studentId": student_id,
            "cohortId": cohort_id,
            "couponReceiveRecordId": None,
            "orderSourceChannelId": None,
            "remark": "demo 演示单",
        },
        headers,
    )
    if status != 200:
        check(2, "下单成功（pending）", False, f"{status} {order_payload}")
        print("\n前置失败，后续断言跳过。")
        return 1
    order_id = int(order_payload["data"]["orderId"])
    order_no = order_payload["data"]["orderNo"]
    item = fetch_one("SELECT id, payable_amount FROM order_item WHERE order_id = %s LIMIT 1", (order_id,))
    created = fetch_one("SELECT order_status FROM `order` WHERE id = %s", (order_id,))
    check(
        2,
        "下单成功（pending）",
        created is not None and created["order_status"] == "pending" and item is not None,
        f"{order_no} 应付 {item['payable_amount']}",
    )

    # 3 支付
    status, pay_payload = http(
        f"{MAIN}/orders/{order_id}/payments", "POST", {"paymentChannelCode": "wechat_pay"}, headers
    )
    payment = fetch_one(
        "SELECT payment_no, order_id, payment_channel, amount FROM payment_record WHERE id = %s",
        (int(pay_payload["data"]["paymentId"]),),
    )
    http(
        f"{MAIN}/payment-notifications/mock",
        "POST",
        {
            "paymentNo": payment["payment_no"],
            "orderId": payment["order_id"],
            "paymentChannelCode": payment["payment_channel"],
            "amount": str(payment["amount"]),
            "tradeStatus": "paid",
            "thirdPartyTradeNo": "TP_E2E",
        },
        {"X-Demo-Payment-Signature": "mock-payment-signature"},
    )
    order_row = fetch_one("SELECT order_status, paid_amount FROM `order` WHERE id = %s", (order_id,))
    item_row = fetch_one("SELECT order_item_status FROM order_item WHERE id = %s", (item["id"],))
    check(
        3,
        "支付回调后订单与明细都是 paid",
        order_row["order_status"] == "paid" and item_row["order_item_status"] == "paid",
        f"{order_row['order_status']} / {item_row['order_item_status']}",
    )

    # 4 报名关系
    rel = fetch_one(
        "SELECT enroll_status FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (student_id, cohort_id),
    )
    check(4, "付款即建立报名关系（active）", rel is not None and rel["enroll_status"] == "active")

    # 5 对话查单
    first = chat(None, user_id, "查订单 " + order_no)
    session_key = first["sessionKey"]
    check(
        5,
        "对话查单：意图 order 且回复带单号",
        first["intent"] == "order" and order_no in first["reply"] and "已支付" in first["reply"],
        first["reply"][:40],
    )

    # 6 政策问答
    policy = chat(session_key, user_id, expect["kbQuestion"])
    check(
        6,
        "政策问答命中知识库原文（KB 编号来自 demo.json）",
        policy.get("matched") is True and policy.get("articleNo") == expect["kbArticleNo"],
        f"matched={policy.get('matched')} article={policy.get('articleNo')}",
    )

    # 7 弱命中转人工 → 建工单
    weak = chat(session_key, user_id, expect["handoffMessage"])
    ticket = fetch_one(
        "SELECT id, ticket_no, ticket_status FROM service_ticket WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    check(
        7,
        "弱命中转人工并落工单（4 小时 SLA）",
        weak["state"] == "handoff"
        and ticket is not None
        and ticket["ticket_no"] in weak["reply"]
        and f"{expect['slaHours']} 小时" in weak["reply"],
        f"ticket={ticket['ticket_no'] if ticket else None}",
    )

    # 8 工单流转
    ticket_id = int(ticket["id"])
    accept = http(f"{MAIN}/service-tickets/{ticket_id}/accept", "POST", {"remark": "e2e 受理"}, headers)
    close = http(f"{MAIN}/service-tickets/{ticket_id}/close", "POST", {"remark": "e2e 完结"}, headers)
    flows = fetch_one(
        "SELECT COUNT(*) AS n FROM service_ticket_follow_record WHERE ticket_id = %s", (ticket_id,)
    )
    check(
        8,
        "工单 pending→in_progress→closed 且留痕（受理+完结各一条）",
        accept[0] == 200
        and close[0] == 200
        and close[1]["data"]["ticketStatus"] == "closed"
        and int(flows["n"]) >= 2,
        f"跟进 {flows['n']} 条",
    )

    # 9 退款申请
    before = fetch_one("SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user_id,))["n"]
    refund = chat(None, user_id, "我要退款 " + order_no)
    after = fetch_one("SELECT COUNT(*) AS n FROM refund_request WHERE user_id = %s", (user_id,))["n"]
    refund_row = fetch_one(
        "SELECT id, refund_no, refund_status FROM refund_request WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    check(
        9,
        "退款申请落库 pending 并回 RF 单号",
        int(after) == int(before) + 1
        and refund_row is not None
        and refund_row["refund_status"] == "pending"
        and refund_row["refund_no"] in refund["reply"],
        f"{refund_row['refund_no'] if refund_row else None}",
    )

    # 10 审批 → 四表 + 报名关系
    approve = http(
        f"{MAIN}/refund-requests/{int(refund_row['id'])}/approve",
        "POST",
        {"approvedAmount": float(item["payable_amount"]), "remark": "e2e 全额批"},
        headers,
    )
    final_refund = fetch_one("SELECT refund_status FROM refund_request WHERE id = %s", (refund_row["id"],))
    final_payment = fetch_one("SELECT payment_status FROM payment_record WHERE order_id = %s", (order_id,))
    final_order = fetch_one("SELECT order_status FROM `order` WHERE id = %s", (order_id,))
    final_item = fetch_one("SELECT order_item_status FROM order_item WHERE id = %s", (item["id"],))
    final_rel = fetch_one(
        "SELECT enroll_status FROM student_cohort_rel WHERE student_id = %s AND cohort_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (student_id, cohort_id),
    )
    check(
        10,
        "审批后四表 + 报名关系全部 refunded",
        approve[0] == 200
        and final_refund["refund_status"] == "refunded"
        and final_payment["payment_status"] == "refunded"
        and final_order["order_status"] == "refunded"
        and final_item["order_item_status"] == "refunded"
        and final_rel["enroll_status"] == "refunded",
        f"退款/支付/订单/明细/报名 = {final_refund['refund_status']}/{final_payment['payment_status']}"
        f"/{final_order['order_status']}/{final_item['order_item_status']}/{final_rel['enroll_status']}",
    )

    print(f"\n{len(PASSED)}/10 通过" + ("" if not FAILED else f"，失败：{FAILED}"))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
