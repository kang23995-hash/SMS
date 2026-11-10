"""中间件：按客户端 IP 限流 + 访问日志。

这里用**纯 ASGI 中间件**而不是 Starlette 的 ``BaseHTTPMiddleware``：
后者会额外包一层 anyio 任务与内存流，在「几百 QPS 的短请求」这个场景下
开销可观；纯 ASGI 中间件是直接透传 ``scope/receive/send``，最省。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Iterable, Optional

from fastapi import Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config import get_settings

logger = logging.getLogger("app.access")

# 只有这些路径前缀会被限流：静态资源和首页是给人看的，不需要限
DEFAULT_LIMIT_PREFIXES: tuple[str, ...] = ("/api/",)


def client_ip_from_scope(scope: Scope, trust_proxy: bool) -> str:
    """从 ASGI scope 里取出客户端 IP。

    默认**不信任** ``X-Forwarded-For``：这个头是客户端可以随便伪造的，
    如果无脑采信，攻击者只要每次都换一个假 IP 就能绕过限流。
    只有当服务确实跑在 Nginx / 反向代理后面时，才把
    ``TRUST_PROXY_HEADERS=true`` 打开。
    """
    if trust_proxy:
        forwarded = Headers(scope=scope).get("x-forwarded-for")
        if forwarded:
            # XFF 可能是一串「客户端, 代理1, 代理2」，第一个才是真实来源
            return forwarded.split(",")[0].strip()

    client = scope.get("client")
    return client[0] if client else "-"


def client_ip(request: Request) -> str:
    """Request 版本，供业务代码 / 异常处理器使用。"""
    trust_proxy = get_settings().trust_proxy_headers
    return client_ip_from_scope(request.scope, trust_proxy)


class AccessLogMiddleware:
    """访问日志：方法、路径、状态码、耗时、来源 IP。

    单条日志就是往内存队列里塞一个对象（见 logger.py），
    不会因为写磁盘而拖慢请求。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._trust_proxy = get_settings().trust_proxy_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code: int = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            cost_ms = (time.perf_counter() - start) * 1000
            ip = client_ip_from_scope(scope, self._trust_proxy)
            # 5xx 用 WARNING，其它用 INFO，方便按级别捞问题
            log = logger.warning if status_code >= 500 else logger.info
            log(
                "%s %s -> %d  %.1fms  client=%s",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                cost_ms,
                ip,
            )


class RateLimitMiddleware:
    """按 IP 的滑动窗口限流。

    目的不是防 DDoS，而是防止「某台设备异常刷接口 / 一堆手机同时上报」
    把数据库打垮 —— 被限流的请求在进入业务逻辑之前就被拦下，
    连数据库连接都不会占用。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        times: int,
        window: float,
        enabled: bool = True,
        trust_proxy: Optional[bool] = None,
        limit_prefixes: Iterable[str] = DEFAULT_LIMIT_PREFIXES,
    ) -> None:
        self.app = app
        self._times = max(1, times)
        self._window = max(0.1, window)
        self._enabled = enabled
        self._trust_proxy = (
            trust_proxy if trust_proxy is not None else get_settings().trust_proxy_headers
        )
        self._prefixes = tuple(limit_prefixes)

        # ip -> 该 IP 在窗口内的请求时间戳（单调时钟）
        self._hits: dict[str, deque[float]] = {}
        self._cleanup_threshold = 10_000

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._prefixes and not path.startswith(self._prefixes):
            await self.app(scope, receive, send)
            return

        ip = client_ip_from_scope(scope, self._trust_proxy)
        allowed, retry_after = self._hit(ip)

        if not allowed:
            logger.warning(
                "触发限流：client=%s path=%s 上限=%d 次/%gs",
                ip,
                path,
                self._times,
                self._window,
            )
            response = JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "msg": f"请求过于频繁，请在 {retry_after:.0f} 秒后重试",
                },
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    # ------------------------------------------------------------ 限流算法

    def _hit(self, ip: str) -> tuple[bool, float]:
        """滑动窗口计数。

        返回 ``(是否放行, 建议等待秒数)``。

        说明：这里的窗口是每个 worker 进程各自维护的，所以实际全局阈值
        约等于「单 worker 阈值 × worker 数」。对局域网场景足够了；
        若要做严格的全局限流，需要把计数放到 Redis 里。

        线程安全：本方法内部没有 await，不会被其它协程打断，
        因此对 ``self._hits`` 的读改写是原子的，无需再加锁。
        """
        now = time.monotonic()
        cutoff = now - self._window

        bucket = self._hits.get(ip)
        if bucket is None:
            bucket = deque()
            self._hits[ip] = bucket

        # 丢掉窗口外的旧记录
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._times:
            # 最早那次请求滑出窗口后就能再放行
            return False, max(0.0, bucket[0] + self._window - now)

        bucket.append(now)

        if len(self._hits) > self._cleanup_threshold:
            self._purge(cutoff)

        return True, 0.0

    def _purge(self, cutoff: float) -> None:
        """清理已经不活跃的 IP，防止字典无限增长导致内存泄漏。"""
        stale = [ip for ip, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for ip in stale:
            self._hits.pop(ip, None)
        logger.debug("限流表已清理，剩余跟踪 IP 数=%d", len(self._hits))
