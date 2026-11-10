# -*- coding: utf-8 -*-
"""短信代发接口（qianwen_auto 人机验证专用）。

链路::

    qianwen_auto  ──POST /api/sms-send/task──►  本服务（任务入库 + 阻塞等待）
                                                     ▲
                              长轮询 /poll          │ 回执 /result
    局域网里的手机（send_sms APP）──────────────────┘

接口一览：

* ``POST /api/sms-send/task``      提交代发任务（等待发送结果）
* ``GET  /api/sms-send/poll``      手机设备长轮询领取任务（兼心跳）
* ``POST /api/sms-send/result``    手机设备回执发送结果
* ``GET  /api/sms-send/devices``   查看在线设备与两张卡号（排障用）
* ``GET  /api/sms-send/tasks``     查看最近任务（排障用）

鉴权：``.env`` 里 ``SMS_SEND_TOKEN`` 非空时，以上接口都要带 ``X-Token``；
留空则与现有验证码接口一样不校验，方便局域网直接使用。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import get_settings
from errors import ApiError
from middleware import client_ip
from sms_send_store import (
    STATUS_FAILED,
    STATUS_SENT,
    claim_task,
    create_task,
    get_task,
    list_devices,
    normalize_phone,
    online_device_count,
    recent_tasks,
    register_device,
    set_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sms-send", tags=["短信代发"])


# =====================================================================
#  鉴权（可选）
# =====================================================================

async def verify_send_token(
    x_token: Optional[str] = Header(
        default=None,
        alias="X-Token",
        description="SMS_SEND_TOKEN，留空时不需要",
    ),
) -> None:
    expected = get_settings().sms_send_token
    if not expected:
        return
    if not x_token:
        raise ApiError(401, "缺少 X-Token")
    try:
        matched = secrets.compare_digest(
            x_token.encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, ValueError):
        matched = False
    if not matched:
        raise ApiError(401, "无效的 X-Token")


# =====================================================================
#  请求模型
# =====================================================================

class SendTaskPayload(BaseModel):
    """qianwen_auto 提交的代发任务。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)

    send_phone: str = Field(
        ..., min_length=5, max_length=32,
        description="用哪张 SIM 卡发（正在登录千问的手机号）",
    )
    recipient: str = Field(
        ..., min_length=5, max_length=32,
        description="收短信的服务号，例如 1069076020201123",
    )
    content: str = Field(
        ..., min_length=1, max_length=200,
        description="短信正文，例如 XZ960572",
    )
    requester: str = Field(
        default="qianwen_auto", max_length=64,
        description="调用方标识，便于日志追溯",
    )
    wait: bool = Field(
        default=True,
        description="true = 阻塞到设备回执；false = 只入队立即返回",
    )
    timeout_s: Optional[float] = Field(
        default=None, ge=5, le=600,
        description="等待发送结果的最长秒数，默认取服务端配置",
    )

    @field_validator("send_phone", "recipient")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        normalized = normalize_phone(value)
        if not normalized:
            raise ValueError("必须是有效号码")
        return normalized


class SendResultPayload(BaseModel):
    """手机设备回执。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)

    task_id: int = Field(..., ge=1, description="任务 id")
    device_id: str = Field(..., min_length=1, max_length=128)
    ok: bool = Field(..., description="短信是否发送成功")
    detail: str = Field(default="", max_length=500, description="失败原因/结果说明")


# =====================================================================
#  提交任务
# =====================================================================

@router.post(
    "/task",
    summary="提交短信代发任务并等待结果",
    dependencies=[Depends(verify_send_token)],
)
async def create_send_task(
    payload: SendTaskPayload,
    request: Request,
) -> JSONResponse:
    settings = get_settings()
    source_ip = client_ip(request)

    timeout = payload.timeout_s or settings.sms_send_wait_timeout
    task_id = await asyncio.to_thread(
        create_task,
        send_phone=payload.send_phone,
        recipient=payload.recipient,
        content=payload.content,
        requester=payload.requester or "qianwen_auto",
        task_ttl=settings.sms_send_task_ttl,
    )

    online = await asyncio.to_thread(
        online_device_count,
        payload.send_phone,
        settings.sms_send_online_window,
    )
    logger.info(
        "代发任务已创建 id=%s 发送号=%s 收件号=%s 内容=%s 在线设备=%d client=%s",
        task_id, payload.send_phone, payload.recipient, payload.content,
        online, source_ip,
    )

    if not payload.wait:
        return JSONResponse(
            status_code=200,
            content={"code": 200, "ok": True, "task_id": task_id,
                     "msg": "任务已入队"},
        )

    if online == 0:
        logger.warning(
            "代发任务 id=%s：当前没有持有 %s 的在线设备，等待设备上线（最多 %gs）",
            task_id, payload.send_phone, timeout,
        )

    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        row = await asyncio.to_thread(get_task, task_id)
        if row is None:
            raise ApiError(404, f"任务 {task_id} 不存在")

        current = str(row.get("status") or "")
        if current != last_status:
            last_status = current
            logger.info("代发任务 id=%s 状态=%s device=%s",
                        task_id, current, row.get("device_id") or "-")

        if current == STATUS_SENT:
            logger.info(
                "代发任务完成 id=%s 发送号=%s device=%s 耗时=%.1fs",
                task_id, payload.send_phone, row.get("device_id"),
                time.time() - (row.get("created_at") or time.time()),
            )
            return JSONResponse(
                status_code=200,
                content={
                    "code": 200,
                    "ok": True,
                    "task_id": task_id,
                    "device_id": row.get("device_id") or "",
                    "msg": row.get("detail") or "短信已发送",
                },
            )

        if current == STATUS_FAILED:
            detail = row.get("detail") or "设备发送失败"
            logger.warning("代发任务失败 id=%s 原因=%s", task_id, detail)
            raise ApiError(502, f"短信发送失败：{detail}")

        await asyncio.sleep(0.5)

    logger.warning(
        "代发任务等待超时 id=%s 发送号=%s 已等待 %gs（任务会保留到 %gs 过期）",
        task_id, payload.send_phone, timeout, settings.sms_send_task_ttl,
    )
    raise ApiError(
        504,
        f"等待手机设备发送短信超时（{timeout:g}s）：请确认持有 {payload.send_phone} "
        f"的手机已安装 send_sms 并开启监收",
    )


# =====================================================================
#  设备长轮询
# =====================================================================

@router.get(
    "/poll",
    summary="手机设备长轮询领取代发任务",
    dependencies=[Depends(verify_send_token)],
)
async def poll_task(
    request: Request,
    device_id: str = Query(..., min_length=1, max_length=128),
    sim1: str = Query("", max_length=32, description="卡1手机号"),
    sim2: str = Query("", max_length=32, description="卡2手机号"),
    device_name: str = Query("", max_length=64),
    app_version: str = Query("", max_length=32),
    wait: int = Query(0, ge=0, le=30, description="长轮询挂起秒数，0=立即返回"),
) -> Response:
    settings = get_settings()
    source_ip = client_ip(request)
    wait_seconds = min(wait or settings.sms_send_poll_wait, settings.sms_send_poll_wait)

    await asyncio.to_thread(
        register_device,
        device_id=device_id,
        device_name=device_name,
        sim1=sim1,
        sim2=sim2,
        ip=source_ip,
        app_version=app_version,
    )

    deadline = time.time() + wait_seconds
    while True:
        task = await asyncio.to_thread(
            claim_task,
            device_id=device_id,
            sim1=sim1,
            sim2=sim2,
            task_ttl=settings.sms_send_task_ttl,
            stale_claim=settings.sms_send_stale_claim,
        )
        if task is not None:
            logger.info(
                "任务已派发 id=%s → device=%s(%s) 发送号=%s 收件号=%s",
                task["id"], device_id, device_name or "-",
                task["send_phone"], task["recipient"],
            )
            return JSONResponse(
                status_code=200,
                content={
                    "code": 200,
                    "data": {
                        "task_id": int(task["id"]),
                        "send_phone": task["send_phone"],
                        "recipient": task["recipient"],
                        "content": task["content"],
                    },
                },
            )

        if time.time() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(1.0)


# =====================================================================
#  设备回执
# =====================================================================

@router.post(
    "/result",
    summary="手机设备回执短信发送结果",
    dependencies=[Depends(verify_send_token)],
)
async def report_result(payload: SendResultPayload) -> JSONResponse:
    updated = await asyncio.to_thread(
        set_result,
        task_id=payload.task_id,
        device_id=payload.device_id,
        ok=payload.ok,
        detail=payload.detail,
    )
    if not updated:
        logger.warning(
            "回执未命中（任务不存在或不是该设备领取） id=%s device=%s ok=%s",
            payload.task_id, payload.device_id, payload.ok,
        )
        return JSONResponse(
            status_code=200,
            content={"code": 200, "msg": "ignored（任务不存在或已结束）"},
        )

    logger.info(
        "设备回执 id=%s device=%s ok=%s detail=%s",
        payload.task_id, payload.device_id, payload.ok, payload.detail or "-",
    )
    return JSONResponse(status_code=200, content={"code": 200, "msg": "success"})


# =====================================================================
#  排障接口
# =====================================================================

@router.get(
    "/devices",
    summary="查看在线设备（排障）",
    dependencies=[Depends(verify_send_token)],
)
async def devices() -> JSONResponse:
    settings = get_settings()
    items = await asyncio.to_thread(list_devices, settings.sms_send_online_window)
    return JSONResponse(
        status_code=200,
        content={"code": 200, "data": items},
    )


@router.get(
    "/tasks",
    summary="查看最近代发任务（排障）",
    dependencies=[Depends(verify_send_token)],
)
async def tasks(limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    items = await asyncio.to_thread(recent_tasks, limit)
    return JSONResponse(
        status_code=200,
        content={"code": 200, "data": items},
    )
