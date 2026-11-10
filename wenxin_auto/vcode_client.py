# -*- coding: utf-8 -*-
"""
向 FastAPI 服务端轮询某个手机号的最新验证码。

调用方式：

    from vcode_client import poll_vcode
    vcode, data = poll_vcode("13800138000")
    if vcode:
        print("拿到了:", vcode)

内部逻辑：
  每隔 VCODE_POLL_INTERVAL 秒调一次 GET {SERVER}/api/sms/vcode?phone=xxx
    200 + vcode 不为空 → 返回
    204               → 继续等
    其它（500/503）    → 记日志继续尝试（直到超时）
  超时 → 抛 VcodeTimeoutError
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from config import (
    SERVER_BASE_URL,
    VCODE_POLL_INTERVAL,
    VCODE_POLL_TIMEOUT,
    VCODE_SENDER_NAME,
)

logger = logging.getLogger(__name__)


class VcodeTimeoutError(RuntimeError):
    """轮询超时还没拿到验证码。"""


def poll_vcode(
    phone: str,
    *,
    sender: str = "",
    sender_name: Optional[str] = None,
    after_ts: Optional[float] = None,
    timeout: float = VCODE_POLL_TIMEOUT,
    interval: float = VCODE_POLL_INTERVAL,
) -> tuple[str, dict]:
    """轮询获取指定手机号的最新验证码。

    :param sender_name: 本平台短信签名（短信【xxx】里的名字），传给服务端做
                     精确匹配，拿到结果后客户端还会再核对一次签名，
                     双重保险防止多平台同时收码时拿串。
                     默认 None = 使用 config.VCODE_SENDER_NAME；
                     显式传 "" 可关闭签名过滤（回退旧行为）。
    :param after_ts: Unix 秒级时间戳；只返回 receive_time > 此时间的记录。
                     客户端应该在「点验证码登录、短信真正发出」之前先记下当前时间，
                     把这个值传进来，防止轮询拿到历史旧码。
    :return: (vcode, 完整响应 data dict)
    :raises VcodeTimeoutError: 超时
    """
    # None → 用 config 里本平台的签名；显式传 "" 才关闭过滤
    want_sender_name = (
        VCODE_SENDER_NAME if sender_name is None else sender_name.strip()
    )

    url = f"{SERVER_BASE_URL}/api/sms/vcode"
    params: dict = {"phone": phone}
    if sender:
        params["sender"] = sender
    if want_sender_name:
        params["sender_name"] = want_sender_name
    if after_ts is not None:
        params["after"] = after_ts

    start = time.time()
    attempt = 0

    while time.time() - start < timeout:
        attempt += 1
        try:
            resp = requests.get(url, params=params, timeout=5)
        except requests.RequestException as exc:
            logger.warning(f"[vcode] HTTP 请求失败（第 {attempt} 次）：{exc}")
            time.sleep(interval)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json().get("data") or {}
            except ValueError:
                data = {}
            vcode = (data.get("vcode") or "").strip()
            if vcode:
                # 客户端二次校验签名：服务端版本过旧不识别 sender_name 参数时，
                # 会把别家平台的验证码也返回过来，这里挡住，宁可继续等也不拿错
                got_name = (data.get("sender_name") or "").strip()
                if want_sender_name and got_name != want_sender_name:
                    logger.warning(
                        f"[vcode] 第 {attempt} 次: 签名不符 "
                        f"(期望={want_sender_name} 实际={got_name or '空'})，"
                        f"丢弃该验证码继续等"
                    )
                else:
                    elapsed = time.time() - start
                    logger.info(
                        f"[vcode] 第 {attempt} 次轮询命中: phone={phone} "
                        f"vcode={vcode} sender_name={got_name or '-'} "
                        f"耗时={elapsed:.1f}s"
                    )
                    return vcode, data
            else:
                # vcode 为空，可能那条短信不是验证码，继续等
                logger.debug(f"[vcode] 第 {attempt} 次: 有记录但 vcode 为空，继续等")

        elif resp.status_code == 204:
            logger.debug(f"[vcode] 第 {attempt} 次: 204 还没收到，继续等")

        elif resp.status_code in (500, 503):
            logger.warning(
                f"[vcode] 第 {attempt} 次: 服务端异常 HTTP {resp.status_code}，继续尝试"
            )

        else:
            logger.warning(
                f"[vcode] 第 {attempt} 次: 意想不到的状态码 {resp.status_code}"
            )

        time.sleep(interval)

    raise VcodeTimeoutError(
        f"轮询超时（{timeout}s × {attempt} 次）仍未拿到 phone={phone} 的验证码"
    )


def health_check() -> Optional[dict]:
    """启动时先探测一下服务端活着没，避免跑半小时才发现服务端挂了。"""
    try:
        resp = requests.get(f"{SERVER_BASE_URL}/health", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None
