"""统一的错误定义与异常处理。

对外承诺：**任何**情况下接口都返回 ``{"code": <int>, "msg": "<提示>"}``，
客户端只需要认这两个字段。内部异常（栈）只进日志，不外泄给调用方。

业务码与 HTTP 状态码保持一致，便于抓包时一眼看懂：

    ======  ==============================================
    code    含义
    ======  ==============================================
    200     成功（含「重复消息已忽略」，对 APP 而言同为成功）
    401     X-Token 缺失或错误
    422     报文格式错误 / 必填字段缺失
    429     IP 触发限流
    500     服务内部异常
    503     数据库不可用 / 服务端未配置 Token
    ======  ==============================================
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """业务异常。抛出后会被下面的处理器统一转成标准响应。"""

    def __init__(self, code: int, msg: str, *, http_status: int | None = None) -> None:
        self.code = code
        self.msg = msg
        self.http_status = http_status if http_status is not None else code
        super().__init__(msg)


def _envelope(code: int, msg: str) -> dict[str, object]:
    return {"code": code, "msg": msg}


def _format_validation_errors(exc: RequestValidationError) -> str:
    """把 Pydantic 的校验错误整理成一行看得懂的中文提示。"""
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(
            str(item) for item in error.get("loc", ()) if item not in ("body", "query", "path")
        )
        message = error.get("msg", "校验失败")
        parts.append(f"{location or 'body'} -> {message}")
    detail = "；".join(parts)
    return f"请求数据校验失败：{detail}" if detail else "请求数据校验失败"


def register_exception_handlers(app: FastAPI) -> None:
    """把各类异常注册到统一的响应格式上。"""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        # 业务异常属于「预期内」的情况，用 WARNING 而不是 ERROR
        logger.warning(
            "业务异常 code=%s msg=%s path=%s client=%s",
            exc.code,
            exc.msg,
            request.url.path,
            _client_of(request),
        )
        return JSONResponse(status_code=exc.http_status, content=_envelope(exc.code, exc.msg))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        msg = _format_validation_errors(exc)
        logger.warning(
            "报文校验失败 path=%s client=%s 详情=%s",
            request.url.path,
            _client_of(request),
            exc.errors()[:5],
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(422, msg),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 例如 404 / 405，转成同样的信封格式
        code = exc.status_code
        msg = exc.detail if isinstance(exc.detail, str) else "请求错误"
        logger.warning(
            "HTTP 异常 code=%s path=%s client=%s", code, request.url.path, _client_of(request)
        )
        return JSONResponse(status_code=code, content=_envelope(code, msg))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：任何没被捕获的异常都在这里收口，记录完整堆栈但只返回笼统提示
        logger.error(
            "未处理异常 path=%s client=%s error=%s",
            request.url.path,
            _client_of(request),
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(500, "服务内部错误，请查看服务端日志"),
        )


def _client_of(request: Request) -> str:
    from middleware import client_ip  # 局部导入，避免循环依赖

    return client_ip(request)
