"""FastAPI 应用入口。

启动方式见 README.md，最常用的一条：

    python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

注意：**日志必须在导入其它模块之前初始化**，这样 uvicorn 自带日志、
业务日志才会全部汇入同一条非阻塞队列。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import STATIC_DIR, get_settings
from database import get_database
from errors import register_exception_handlers
from logger import setup_logging, shutdown_logging
from middleware import AccessLogMiddleware, RateLimitMiddleware
from routes import sms as sms_routes
from routes import sms_send as sms_send_routes
from routes import web as web_routes

# ---- 第一件事：把日志系统架起来（之后所有 logger 都走非阻塞队列）----
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时建连接池，关闭时释放。"""
    settings = get_settings()

    logger.info("=" * 68)
    logger.info("短信监测系统启动中……")
    for key, value in settings.safe_summary().items():
        logger.info("  %-22s : %s", key, value)
    logger.info("=" * 68)

    # 两个最容易踩的配置问题，启动时就大声提醒
    if settings.db_password_is_placeholder:
        logger.critical(
            "配置检查：.env 里的 DB_PASSWORD 仍是模板占位符，请改成真实的 MySQL 密码！"
        )
    if not settings.api_token_enabled:
        logger.warning(
            "鉴权已关闭（API_TOKEN_ENABLED=false）：上报接口 /api/sms/receive 不校验 "
            "X-Token，局域网内任何设备都能写入数据。当前安卓端不发送 X-Token 请求头，"
            "需要此设置才能正常上报；如需加固，请给 APP 加上该请求头后把开关改回 true。"
        )
    elif settings.api_token_is_placeholder:
        logger.critical(
            "配置检查：已启用 X-Token 校验，但 .env 里的 API_TOKEN 未设置，"
            "上报接口会一律返回 503。"
            "生成办法：python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    if settings.total_db_connections > 151:
        logger.warning(
            "配置提醒：worker 数 × 连接池上限 = %d，可能超过 MySQL 默认的 "
            "max_connections(151)。请调小 APP_WORKERS 或 DB_POOL_MAX。",
            settings.total_db_connections,
        )

    # 建连接池。失败不会让服务起不来，只是 /health 会报 down，
    # 等 MySQL 恢复后请求会自动触发重建
    await get_database().connect()

    try:
        yield
    finally:
        logger.info("服务正在关闭，释放数据库连接……")
        await get_database().close()
        logger.info("服务已关闭")
        # flush 日志队列，保证退出前日志不丢
        shutdown_logging()


def create_app() -> FastAPI:
    """组装 FastAPI 应用（工厂函数，方便测试时另建实例）。"""
    settings = get_settings()

    app = FastAPI(
        title="短信监测系统",
        description=(
            "接收安卓 APP 上报的短信并入库，提供 Web 列表页查看。"
            "接口文档见 /docs"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # ---- 中间件 ----
    # 注意顺序：**后添加的在外层**。
    # 所以先加限流、再加访问日志，让访问日志能记录到被限流的 429 请求。
    app.add_middleware(
        RateLimitMiddleware,
        times=settings.rate_limit_times,
        window=settings.rate_limit_window,
        enabled=settings.rate_limit_enabled,
    )
    app.add_middleware(AccessLogMiddleware)

    # ---- 统一异常处理（保证任何情况都返回 {"code","msg"}）----
    register_exception_handlers(app)

    # ---- 路由 ----
    app.include_router(sms_routes.router)
    app.include_router(sms_send_routes.router)
    app.include_router(web_routes.router)

    # ---- 静态资源 ----
    # 直接托管本地 Bootstrap，不依赖 CDN：
    # 局域网部署时这台机器/手机未必能上外网，用 CDN 会导致页面样式丢失
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    # 直接用 `python main.py` 启动的便捷入口。
    # 生产环境建议用 README 里的 uvicorn 命令行方式，参数更直观。
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        workers=settings.app_workers,
        # 我们自己记访问日志，关掉 uvicorn 的以免重复
        access_log=False,
        # 不让 uvicorn 覆盖我们的日志配置
        log_config=None,
        timeout_keep_alive=30,
        limit_concurrency=1000,
        backlog=2048,
    )
