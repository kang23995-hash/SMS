"""``X-Token`` 鉴权。

短信内容属于敏感数据，而且这个服务监听在局域网上——局域网里任何一台
设备都能访问它。带上密钥能挡住「同一 WiFi 下的其它设备乱塞假短信」。

不过鉴权是**可开关**的（见 :func:`verify_token`）：当前安卓端并不会发送
``X-Token`` 请求头，因此用 APP 直接上报时需要把 ``API_TOKEN_ENABLED`` 关掉。

校验用 :func:`secrets.compare_digest` 做**常量时间**比较，
避免通过测量响应时间逐字节猜出 Token（普通的 ``==`` 会短路比较，
存在时序侧信道）。
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Request
from fastapi import Header

from config import get_settings
from errors import ApiError

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Token"


async def verify_token(
    request: Request,
    x_token: str | None = Header(
        default=None,
        alias=TOKEN_HEADER,
        description="调用凭证，需与服务端 .env 中的 API_TOKEN 一致",
    ),
) -> None:
    """FastAPI 依赖：校验请求头 ``X-Token``，不通过则抛 401。

    受 ``API_TOKEN_ENABLED`` 开关控制：

    * ``true`` （默认）—— 必须带正确的 Token；
    * ``false`` —— 直接放行，不做任何鉴权。

    之所以要有这个开关：当前安卓端（SMSUploader）并不会发送自定义请求头，
    如果服务端强制校验，APP 上报会被一律拒绝（表现为一直重试直到超时丢弃）。
    用 APP 直接上报时把开关关掉即可；等服务端只对可信来源开放时再打开。
    """
    settings = get_settings()

    if not settings.api_token_enabled:
        # 关闭鉴权：直接放行。启动时日志里会有一条醒目提示。
        return

    expected = settings.api_token

    if settings.api_token_is_placeholder:
        # 与其「没配 Token 就放行」（等于裸奔），不如明确拒绝并提示怎么修
        logger.error(
            "服务端未配置 API_TOKEN，已拒绝请求。请修改 .env 中的 API_TOKEN 后重启服务。"
        )
        raise ApiError(
            503,
            "服务端未配置 API_TOKEN，请在 .env 中设置一个随机字符串后重启服务",
        )

    if not _token_matches(x_token, expected):
        logger.warning(
            "鉴权失败：提供的 Token=%s，来源 IP=%s",
            _mask(x_token),
            _client_ip(request),
        )
        raise ApiError(401, "无效的 X-Token")


def _token_matches(provided: str | None, expected: str) -> bool:
    """常量时间比较。任何异常（例如含非 ASCII 字符）都视为不匹配。"""
    if not provided:
        return False
    try:
        return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _mask(token: str | None) -> str:
    """日志里只留 Token 的前 4 位，避免密钥被完整写进日志文件。"""
    if not token:
        return "<缺失>"
    return f"{token[:4]}***（长度 {len(token)}）"


def _client_ip(request: Request) -> str:
    from middleware import client_ip  # 局部导入，避免循环依赖

    return client_ip(request)
