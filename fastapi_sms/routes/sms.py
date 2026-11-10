"""短信上报接口。

``POST /api/sms/receive`` —— 安卓 APP 上报短信的唯一入口。

处理链路：

    限流中间件 -> X-Token 鉴权 -> Pydantic 强校验 -> 事务入库(唯一索引去重) -> 返回

任何一步失败都会返回 ``{"code": ..., "msg": ...}``，HTTP 状态码与 code 一致，
APP 侧只要认「2xx = 成功」即可。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from config import get_settings
from database import DatabaseUnavailableError
from errors import ApiError
from middleware import client_ip
from models import ClearPayload, DeletePayload, SmsPayload
from repository import get_repository
from security import verify_token
from sms_parse import extract_sender_name, extract_vcode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sms", tags=["短信上报"])


@router.post(
    "/receive",
    summary="接收 APP 上报的短信",
    description=(
        "请求头需带 `X-Token`。报文为 JSON，字段见 SmsPayload。\n\n"
        "- 新消息：`{\"code\":200,\"msg\":\"success\"}`\n"
        "- 重复消息（命中唯一索引）：`{\"code\":200,\"msg\":\"duplicate ignored\"}`"
    ),
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_token)],
    responses={
        200: {"description": "成功（含重复消息）"},
        401: {"description": "X-Token 缺失或错误"},
        422: {"description": "报文格式错误或字段缺失"},
        429: {"description": "触发限流"},
        503: {"description": "数据库不可用或服务端未配置 Token"},
    },
)
async def receive_sms(payload: SmsPayload, request: Request) -> JSONResponse:
    """接收一条短信并入库。"""
    settings = get_settings()
    source_ip = client_ip(request)

    # 从正文里解析出验证码与发送方名称（短信签名）后再入库，
    # 解析是纯函数，失败最多返回空串，绝不影响这条短信的正常入库。
    sender_name = extract_sender_name(payload.message_content)
    sms_vcode = extract_vcode(payload.message_content)

    # 先落一条 INFO 日志：请求来源、消息 ID、设备、卡槽，便于事后追溯
    logger.info(
        "收到短信上报 unique_id=%s device=%s slot=%s sender=%s "
        "sender_name=%s vcode=%s 内容长度=%d client=%s",
        payload.message_unique_id,
        payload.device_id,
        payload.sim_slot,
        payload.sender,
        sender_name or "-",
        sms_vcode or "-",
        len(payload.message_content),
        source_ip,
    )

    try:
        # 整体超时保护：即使数据库层因为某些原因卡住，也会在
        # APP_REQUEST_TIMEOUT 秒内返回，避免连接被长时间占用
        inserted, record_id = await asyncio.wait_for(
            get_repository().insert_sms(
                message_unique_id=payload.message_unique_id,
                device_id=payload.device_id,
                sim_slot=payload.sim_slot,
                sim_phone_number=payload.sim_phone_number,
                sender=payload.sender,
                sender_name=sender_name,
                message_content=payload.message_content,
                sms_vcode=sms_vcode,
                receive_time=payload.normalized_receive_time(),
            ),
            timeout=settings.app_request_timeout,
        )
    except asyncio.TimeoutError as exc:
        logger.error(
            "处理超时(>%gs) unique_id=%s client=%s",
            settings.app_request_timeout,
            payload.message_unique_id,
            source_ip,
        )
        raise ApiError(503, "服务处理超时，请稍后重试") from exc
    except DatabaseUnavailableError as exc:
        # 返回 503 而不是 500：告诉 APP「是服务端暂时的问题，值得重试」，
        # APP 会在 60 秒窗口内继续重试
        logger.error(
            "入库失败(数据库不可用) unique_id=%s device=%s client=%s err=%s",
            payload.message_unique_id,
            payload.device_id,
            source_ip,
            exc,
            exc_info=True,
        )
        raise ApiError(503, "数据库暂时不可用，请稍后重试") from exc

    if inserted:
        logger.info(
            "入库成功 id=%s unique_id=%s device=%s slot=%s sender=%s client=%s",
            record_id,
            payload.message_unique_id,
            payload.device_id,
            payload.sim_slot,
            payload.sender,
            source_ip,
        )
        return JSONResponse(status_code=200, content={"code": 200, "msg": "success"})

    # 重复消息：唯一索引挡住了，没有重复入库。
    # 依然返回 200，让 APP 停止重试（否则 APP 会白重试 60 秒后丢弃）
    logger.info(
        "重复消息已忽略 unique_id=%s device=%s client=%s",
        payload.message_unique_id,
        payload.device_id,
        source_ip,
    )
    return JSONResponse(
        status_code=200, content={"code": 200, "msg": "duplicate ignored"}
    )


# =====================================================================
#  验证码查询接口（RPA 自动登录轮询用）
#
#  ⚠️ 安全提示：与列表页一样不校验 X-Token（RPA 客户端不带 Token），
#     局域网内可访问本服务的机器都能查验证码。不可信网络请只绑 127.0.0.1
#     或加带认证的反向代理（详见 DEPLOY.md「访问控制」一节）。
# =====================================================================


@router.get(
    "/vcode",
    summary="查询指定手机号的最新验证码",
    description=(
        "供 RPA 登录流程轮询：\n\n"
        "- 命中：`200 {\"code\":200,\"data\":{\"vcode\":\"123456\",...}}`\n"
        "- 暂无：`204 No Content`（客户端继续轮询）\n\n"
        "`after` 传「点击发送验证码」那一刻的 Unix 秒时间戳，可避免拿到历史旧码。\n"
        "`sender_name` 传本平台的短信签名（如 `深度求索`）做精确匹配，"
        "多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。"
    ),
    responses={
        200: {"description": "命中，data.vcode 即验证码"},
        204: {"description": "还没有匹配的短信"},
        422: {"description": "缺少 phone 参数"},
        503: {"description": "数据库不可用"},
    },
)
async def get_latest_vcode(
    request: Request,
    phone: str = Query(..., description="登录用的手机号（SIM 本机号码）"),
    sender: str = Query("", description="可选：按发送方号码/短信签名过滤"),
    sender_name: str = Query(
        "", description="可选：按短信签名【xxx】精确匹配（多平台防串码）"
    ),
    after: float | None = Query(
        None, description="Unix 秒时间戳，只取这之后收到的短信"
    ),
) -> Response:
    """取指定手机号最新一条带验证码的短信。"""
    source_ip = client_ip(request)
    settings = get_settings()
    repo = get_repository()

    try:
        row = await asyncio.wait_for(
            repo.get_latest_vcode(
                phone,
                sender=sender.strip(),
                after_ts=after,
                after_grace=settings.vcode_after_grace_seconds,
                sender_name=sender_name.strip(),
            ),
            timeout=settings.app_request_timeout,
        )

        # ---- 手机号兜底：该号码在库里一条短信都没有（转发 APP 上报的
        #      sim_phone_number 为空或格式对不上）时，回退为「after 窗口内
        #      最新一条验证码」。单接收设备场景下这能把「永远 204」救回来。
        fallback_used = False
        if not row and settings.vcode_phone_fallback:
            latest_for_phone = await asyncio.wait_for(
                repo.get_latest_for_phone(phone),
                timeout=settings.app_request_timeout,
            )
            if latest_for_phone is None:
                row = await asyncio.wait_for(
                    repo.get_latest_vcode(
                        "",
                        sender=sender.strip(),
                        after_ts=after,
                        after_grace=settings.vcode_after_grace_seconds,
                        sender_name=sender_name.strip(),
                    ),
                    timeout=settings.app_request_timeout,
                )
                fallback_used = row is not None
                if fallback_used:
                    logger.warning(
                        "验证码兜底命中：phone=%s 在库中无任何记录，"
                        "返回最新验证码 vcode=%s 入库手机号=%s sender=%s "
                        "sender_name=%s"
                        "（转发 APP 未上报本机号码或格式不符，多接收设备"
                        "场景请关闭 VCODE_PHONE_FALLBACK）",
                        phone, row.get("sms_vcode"),
                        row.get("sim_phone_number"), row.get("sender"),
                        row.get("sender_name") or "-",
                    )
            else:
                # 有该号码的记录但没命中 → 是被 after/sender/空验证码过滤的，
                # 把最新一条的关键信息打出来，一眼看清过滤原因
                logger.info(
                    "验证码暂无 phone=%s after=%s sender=%s sender_name=%s | "
                    "该号最新短信: receive_time=%s sender=%s sender_name=%s vcode=%s",
                    phone, after, sender or "-", sender_name or "-",
                    latest_for_phone.get("receive_time"),
                    latest_for_phone.get("sender"),
                    latest_for_phone.get("sender_name") or "-",
                    latest_for_phone.get("sms_vcode") or "(空)",
                )
    except asyncio.TimeoutError as exc:
        raise ApiError(503, "服务处理超时，请稍后重试") from exc
    except DatabaseUnavailableError as exc:
        logger.error("验证码查询失败(数据库不可用) phone=%s err=%s", phone, exc)
        raise ApiError(503, "数据库暂时不可用，请稍后重试") from exc

    if not row:
        # 204：明确表达「服务正常，只是还没收到」，客户端继续轮询
        return Response(status_code=204)

    logger.info(
        "验证码命中 phone=%s vcode=%s sender=%s client=%s",
        phone, row.get("sms_vcode"), row.get("sender"), source_ip,
    )
    # 只挑客户端需要的字段，并把 datetime 转字符串（JSON 无法直接序列化）
    data = {
        "id": row.get("id"),
        "vcode": row.get("sms_vcode") or "",
        "sim_phone_number": row.get("sim_phone_number") or "",
        "sender": row.get("sender") or "",
        "sender_name": row.get("sender_name") or "",
        "receive_time": str(row.get("receive_time") or ""),
    }
    return JSONResponse(status_code=200, content={"code": 200, "data": data})


# =====================================================================
#  删除接口（供 Web 列表页的「删除选中 / 清空全部」调用）
#
#  ⚠️ 安全提示：列表页本身没有任何登录认证（局域网内谁都能打开），
#     所以这两个接口同样不校验 X-Token —— 否则页面上的按钮没法工作。
#     也就是说，能访问到本服务的人也能删除数据。若部署在不可信网络，
#     请把服务只绑到 127.0.0.1，或在前面加一层带认证的反向代理
#     （详见 DEPLOY.md「访问控制」一节）。
# =====================================================================


@router.post(
    "/delete",
    summary="按 id 删除短信（支持单选/多选）",
    description="请求体 `{\"ids\": [1, 2, 3]}`，返回实际删除的条数。",
    responses={
        200: {"description": "删除完成（可能 0 条）"},
        422: {"description": "ids 缺失或格式错误"},
        503: {"description": "数据库不可用"},
    },
)
async def delete_records(payload: DeletePayload, request: Request) -> JSONResponse:
    """按主键 id 批量删除。单选就是列表里只勾一条。"""
    source_ip = client_ip(request)
    # 去重 + 只保留正整数，避免脏 id 混进来（虽然只是删不到而已）
    ids = sorted({int(i) for i in payload.ids if int(i) > 0})

    try:
        deleted = await asyncio.wait_for(
            get_repository().delete_by_ids(ids),
            timeout=get_settings().app_request_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ApiError(503, "服务处理超时，请稍后重试") from exc
    except DatabaseUnavailableError as exc:
        logger.error("删除失败(数据库不可用) ids=%s client=%s err=%s", ids, source_ip, exc)
        raise ApiError(503, "数据库暂时不可用，请稍后重试") from exc

    logger.info("删除短信 %d 条 ids=%s client=%s", deleted, ids, source_ip)
    return JSONResponse(
        status_code=200,
        content={"code": 200, "msg": f"已删除 {deleted} 条", "deleted": deleted},
    )


@router.post(
    "/clear",
    summary="清空全部短信（需 confirm=true）",
    description="请求体 `{\"confirm\": true}`，清空整张表并返回删除条数。",
    responses={
        200: {"description": "清空完成"},
        400: {"description": "未带 confirm=true"},
        503: {"description": "数据库不可用"},
    },
)
async def clear_records(payload: ClearPayload, request: Request) -> JSONResponse:
    """清空整张表（不可逆）。必须显式 ``confirm=true``，防误触。"""
    source_ip = client_ip(request)

    if not payload.confirm:
        raise ApiError(400, "清空操作需要显式确认（confirm=true）")

    try:
        deleted = await asyncio.wait_for(
            get_repository().delete_all(),
            timeout=get_settings().app_request_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise ApiError(503, "服务处理超时，请稍后重试") from exc
    except DatabaseUnavailableError as exc:
        logger.error("清空失败(数据库不可用) client=%s err=%s", source_ip, exc)
        raise ApiError(503, "数据库暂时不可用，请稍后重试") from exc

    logger.warning("清空短信表，共删除 %d 条 client=%s", deleted, source_ip)
    return JSONResponse(
        status_code=200,
        content={"code": 200, "msg": f"已清空 {deleted} 条", "deleted": deleted},
    )
