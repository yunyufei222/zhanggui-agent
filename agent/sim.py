"""业务对象消息模拟（T3.2）。

调试台只能看对话，看不到「订单付款了、退款批了、工单关了」这些业务动作——
演示时得切换 Swagger 手动打，一条链就走散了。这里把三个最常用的动作收成接口，
由 agent 进程**以真实 HTTP 调用主应用**完成（同 start-all.bat 起的两个服务），
既不是直接改库，也不用给浏览器开跨域。

约定：
- 只认业务号（订单号 / 退款单号 / 工单号），不认自增 id——和 demo.json 一个口径；
- 找不到就 404，不猜；
- 付款用主应用的「创建支付 + 模拟回调」两步，和 e2e 走同一条路。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import fetch_one
from app.response import ok
from .skill_config import config

router = APIRouter(prefix="/sim", tags=["sim"])

MAIN_BASE = os.getenv("SIM_MAIN_BASE", "http://127.0.0.1:8111").rstrip("/")
# 直连本机：系统代理一挂，urllib 走代理会 10061（和 HttpLLM 同一个坑）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PAYMENT_SIGNATURE = os.getenv("DEMO_PAYMENT_SIGNATURE", "mock-payment-signature")


def _call(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    user_id: int | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(MAIN_BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if user_id is not None:
        request.add_header("X-User-Id", str(user_id))
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    timeout = float(config.get("external.timeout_s", 3.0))
    retries = int(config.get("external.retries", 1))
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload).get("data") if payload else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8")
            if error.code < 500:   # 4xx 重试没意义，直接告诉调用方
                raise HTTPException(
                    status_code=502,
                    detail=f"主应用返回 {error.code}：{detail[:200]}",
                ) from error
            last_error = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error

    if isinstance(last_error, urllib.error.HTTPError):
        detail = last_error.read().decode("utf-8")
        raise HTTPException(status_code=502, detail=f"主应用返回 {last_error.code}：{detail[:200]}")
    raise HTTPException(
        status_code=503,
        detail=f"{config.get('external.degrade_text')}（{MAIN_BASE}：{getattr(last_error, 'reason', last_error)}）",
    )


class PaymentSim(BaseModel):
    orderNo: str = Field(description="订单号。")
    tradeStatus: str = Field(default="paid", description="paid / failed / closed。")


class TicketSim(BaseModel):
    ticketNo: str = Field(description="工单号。")
    action: str = Field(default="accept", description="accept / close / reopen。")


class RefundSim(BaseModel):
    refundNo: str = Field(description="退款单号。")
    approvedAmount: float | None = Field(default=None, description="不传则按申请金额全额批。")


@router.post("/payment")
def simulate_payment(body: PaymentSim) -> dict[str, Any]:
    order = fetch_one(
        "SELECT id, user_id, order_status FROM `order` WHERE order_no = %s", (body.orderNo,)
    )
    if order is None:
        raise HTTPException(status_code=404, detail=f"没有订单 {body.orderNo}")
    if body.tradeStatus not in {"paid", "failed", "closed"}:
        raise HTTPException(status_code=422, detail="tradeStatus 只能是 paid / failed / closed")

    payment = fetch_one(
        "SELECT id, payment_no, amount, payment_channel, payment_status FROM payment_record "
        "WHERE order_id = %s ORDER BY id DESC LIMIT 1",
        (order["id"],),
    )
    if payment is None or payment["payment_status"] in {"failed", "closed"}:
        created = _call(
            f"/api/v1/orders/{order['id']}/payments", "POST", {"paymentChannelCode": "wechat_pay"}, order["user_id"]
        )
        payment = fetch_one(
            "SELECT id, payment_no, amount, payment_channel, payment_status FROM payment_record WHERE id = %s",
            (created["paymentId"],),
        )

    _call(
        "/api/v1/payment-notifications/mock",
        "POST",
        {
            "paymentNo": payment["payment_no"],
            "orderId": order["id"],
            "paymentChannelCode": payment["payment_channel"],
            "amount": str(payment["amount"]),
            "tradeStatus": body.tradeStatus,
            "thirdPartyTradeNo": "TP_SIM",
        },
        headers={"X-Demo-Payment-Signature": PAYMENT_SIGNATURE},
    )
    after = fetch_one(
        "SELECT o.order_status, p.payment_status FROM `order` o "
        "JOIN payment_record p ON p.order_id = o.id WHERE o.id = %s ORDER BY p.id DESC LIMIT 1",
        (order["id"],),
    )
    return ok(
        {
            "orderNo": body.orderNo,
            "tradeStatus": body.tradeStatus,
            "orderStatus": after["order_status"],
            "paymentStatus": after["payment_status"],
            "paymentNo": payment["payment_no"],
        }
    )


@router.post("/ticket")
def simulate_ticket(body: TicketSim) -> dict[str, Any]:
    ticket = fetch_one(
        "SELECT id, ticket_no, user_id, ticket_status FROM service_ticket WHERE ticket_no = %s", (body.ticketNo,)
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"没有工单 {body.ticketNo}")
    if body.action not in {"accept", "close", "reopen"}:
        raise HTTPException(status_code=422, detail="action 只能是 accept / close / reopen")

    path = {"accept": "accept", "close": "close", "reopen": "reopen"}[body.action]
    _call(f"/api/v1/service-tickets/{ticket['id']}/{path}", "POST", {"remark": "调试台模拟"}, ticket["user_id"])
    after = fetch_one("SELECT ticket_status FROM service_ticket WHERE id = %s", (ticket["id"],))
    return ok({"ticketNo": body.ticketNo, "action": body.action, "ticketStatus": after["ticket_status"]})


@router.post("/refund")
def simulate_refund(body: RefundSim) -> dict[str, Any]:
    refund = fetch_one(
        "SELECT id, refund_no, user_id, refund_status, apply_amount FROM refund_request WHERE refund_no = %s",
        (body.refundNo,),
    )
    if refund is None:
        raise HTTPException(status_code=404, detail=f"没有退款单 {body.refundNo}")

    amount = body.approvedAmount if body.approvedAmount is not None else float(refund["apply_amount"])
    _call(
        f"/api/v1/refund-requests/{refund['id']}/approve",
        "POST",
        {"approvedAmount": amount, "remark": "调试台模拟审批"},
        refund["user_id"],
    )
    after = fetch_one(
        "SELECT r.refund_status, p.payment_status, o.order_status "
        "FROM refund_request r JOIN payment_record p ON p.id = r.payment_id "
        "JOIN `order` o ON o.id = r.order_id WHERE r.id = %s",
        (refund["id"],),
    )
    return ok(
        {
            "refundNo": body.refundNo,
            "refundStatus": after["refund_status"],
            "paymentStatus": after["payment_status"],
            "orderStatus": after["order_status"],
            "approvedAmount": amount,
        }
    )
