"""异步非阻塞日志。

设计要点
--------
* 业务代码调用 ``logger.info(...)`` 时，只是把日志记录丢进一个内存队列，
  真正的磁盘写入由后台监听线程完成（``QueueHandler`` + ``QueueListener``）。
  这样即使磁盘 IO 抖动，也不会阻塞 asyncio 事件循环 —— 满足
  「大量上报时日志不要拖慢接口响应」的要求。
* 日志分级落到不同文件：
    - ``logs/app_<pid>.log``    全部日志（级别由 LOG_LEVEL 控制）
    - ``logs/error_<pid>.log``  仅 WARNING 及以上，排障时只看这个
* 文件名带进程 PID：uvicorn 多 worker 时每个进程是独立的 OS 进程，
  共用同一个日志文件会在轮转时互相冲突（Windows 上尤其明显），
  因此每个 worker 写自己的文件。
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import sys
from typing import Optional

from config import LOGS_DIR, get_settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(process)d | %(name)-22s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024  # 单文件上限 10MB
_BACKUP_COUNT = 5

_listener: Optional[logging.handlers.QueueListener] = None
_configured = False


def _build_handlers(level: int) -> list[logging.Handler]:
    """构造真正的落盘/输出处理器（运行在监听线程里）。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 全量日志文件
    app_handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"app_{pid}.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(level)
    app_handler.setFormatter(formatter)

    # 仅告警及以上的错误日志文件
    error_handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"error_{pid}.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    # 控制台输出（便于前台运行时直接观察）
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    return [app_handler, error_handler, console_handler]


def setup_logging() -> None:
    """初始化日志系统。重复调用安全（只生效一次）。"""
    global _listener, _configured
    if _configured:
        return
    _configured = True

    settings = get_settings()
    level = getattr(logging, settings.log_level, logging.INFO)

    root = logging.getLogger()
    # 清掉可能已存在的处理器，避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    # 关键：业务线程只写队列，不碰磁盘
    log_queue: queue.Queue = queue.Queue(maxsize=10000)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.setLevel(level)
    root.addHandler(queue_handler)

    _listener = logging.handlers.QueueListener(
        log_queue, *_build_handlers(level), respect_handler_level=True
    )
    _listener.start()

    # 让 uvicorn 自带的日志也走同一条队列（否则它仍会同步写自己的 handler）
    # uvicorn.access 由我们自己的访问日志中间件替代，这里关掉以免重复记录
    for name, propagate in (
        ("uvicorn", True),
        ("uvicorn.error", True),
        ("uvicorn.access", False),
        ("uvicorn.asgi", True),
    ):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = propagate
        uv_logger.setLevel(level)

    atexit.register(shutdown_logging)
    logging.getLogger(__name__).info("日志系统已启动，级别=%s，输出目录=%s", settings.log_level, LOGS_DIR)


def shutdown_logging() -> None:
    """停止监听线程并 flush 队列，保证进程退出前日志不丢。"""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
