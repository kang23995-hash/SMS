"""应用配置。

所有可变参数统一从 .env 读取，代码中不出现任何硬编码的账号、密码或 Token。
这样同一份代码可以在不同机器上通过替换 .env 直接部署。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------- 路径常量
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
LOGS_DIR = BASE_DIR / "logs"

# 加载 .env；文件不存在时静默跳过，方便用系统环境变量部署（如 Docker / 服务管理器）
load_dotenv(BASE_DIR / ".env")

# .env 模板里的占位符，用于提示用户还没改配置
_PLACEHOLDER_DB_PASSWORD = "请填写你的MySQL密码"
_PLACEHOLDER_API_TOKEN = "请填写一个足够随机的长字符串"


def _get_str(key: str, default: str) -> str:
    """读取字符串配置；未设置或为空时返回默认值。"""
    value = os.getenv(key)
    return default if value is None or value.strip() == "" else value.strip()


def _get_raw_str(key: str, default: str = "") -> str:
    """读取字符串配置但保留空串语义（例如允许数据库空密码）。"""
    value = os.getenv(key)
    return default if value is None else value


def _get_int(key: str, default: int) -> int:
    """读取整型配置；非法值回退到默认值，避免因配置笔误导致服务起不来。"""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """一次性解析好的运行时配置。"""

    # ---------- 服务 ----------
    app_host: str = field(default="0.0.0.0")
    app_port: int = field(default=8000)
    app_workers: int = field(default=4)
    app_request_timeout: float = field(default=10.0)

    # ---------- 数据库 ----------
    # 后端类型：mysql / postgresql。决定用哪个异步驱动（aiomysql / asyncpg），
    # 以及 repository 里 upsert 语句的写法。默认 mysql，保持向后兼容。
    db_type: str = field(default="mysql")
    db_host: str = field(default="127.0.0.1")
    db_port: int = field(default=3306)
    db_user: str = field(default="root")
    db_password: str = field(default="")
    db_name: str = field(default="sms_monitor")
    db_pool_min: int = field(default=1)
    db_pool_max: int = field(default=10)
    db_connect_timeout: int = field(default=10)
    db_query_timeout: float = field(default=8.0)
    db_retry_times: int = field(default=3)

    # ---------- 安全 ----------
    api_token: str = field(default="")
    # 是否启用 X-Token 校验。
    #   true  -> 请求必须带正确的 X-Token，否则 401（默认，更安全）
    #   false -> 上报接口不做任何鉴权，直接收单
    # 注意：当前安卓端（SMSUploader）不会发送 X-Token 请求头，
    # 因此若要用 APP 直接上报，必须置为 false，或先给 APP 加上该请求头。
    api_token_enabled: bool = field(default=True)

    # ---------- 限流 ----------
    rate_limit_enabled: bool = field(default=True)
    rate_limit_times: int = field(default=300)
    rate_limit_window: float = field(default=60.0)
    # 默认不信任 X-Forwarded-For：客户端可以伪造它来绕过按 IP 的限流。
    # 只有确实部署在 Nginx 等反向代理后面时才置为 True。
    trust_proxy_headers: bool = field(default=False)

    # ---------- 其它 ----------
    app_timezone: str = field(default="Asia/Shanghai")
    log_level: str = field(default="INFO")
    page_size_default: int = field(default=20)
    page_size_max: int = field(default=100)

    # ---- 短信代发（qianwen_auto 人机验证 → 手机设备发短信）----
    # 设备在线判定窗口（秒）：设备每隔几秒长轮询一次，超过该窗口没心跳视为离线
    sms_send_online_window: float = field(default=90.0)
    # 任务在队列里最多存活多久（秒），过期作废，避免验证码失效后还被发出去
    sms_send_task_ttl: float = field(default=300.0)
    # 设备领取任务后多久没回执就重新放回队列（秒）
    sms_send_stale_claim: float = field(default=90.0)
    # 调用方（qianwen_auto）等待发送结果的最长时间（秒）
    sms_send_wait_timeout: float = field(default=180.0)
    # 设备长轮询单次最长挂起时间（秒）
    sms_send_poll_wait: int = field(default=25)
    # 代发接口的 X-Token（可选）。留空表示不校验，方便局域网直接使用
    sms_send_token: str = field(default="")

    # ---- 验证码轮询接口（/api/sms/vcode）----
    # after 宽限秒数：客户端传「点发送验证码」的 PC 时间戳，但短信的
    # receive_time 来自安卓手机时钟；两台设备的时钟可能差几十秒，
    # 短信又常在点击后 1~3 秒就到达，于是「手机时钟慢 + 短信太快」
    # 会把刚收到的真码误判成「历史旧码」滤掉 → 轮询永远 204。
    # 严格 after 查不到时，自动放宽 after_grace 秒再查一次（记日志）。
    vcode_after_grace_seconds: int = field(default=120)
    # 手机号兜底：转发 APP 上报的 sim_phone_number 可能带空格/连字符/
    # 国家码甚至读不到（空串），导致 LIKE 永不匹配 → 永远 204。
    # 开启后：该手机号在库里一条记录都没有时，回退为「after 窗口内
    # 最新一条验证码」（记警告日志）。单接收设备场景建议开启。
    vcode_phone_fallback: bool = field(default=True)

    # ------------------------------------------------------------ 派生属性
    @property
    def is_postgresql(self) -> bool:
        """当前是否使用 PostgreSQL 后端。"""
        return self.db_type == "postgresql"

    @property
    def total_db_connections(self) -> int:
        """所有 worker 加起来会向 MySQL 建立的最大连接数。

        必须小于 MySQL 的 max_connections，否则并发上报时会报
        "Too many connections"。部署文档中会提醒核对。
        """
        return max(1, self.app_workers) * max(1, self.db_pool_max)

    @property
    def db_password_is_placeholder(self) -> bool:
        return self.db_password == _PLACEHOLDER_DB_PASSWORD

    @property
    def api_token_is_placeholder(self) -> bool:
        return self.api_token == _PLACEHOLDER_API_TOKEN or not self.api_token

    @property
    def auth_enabled(self) -> bool:
        """X-Token 校验是否真正生效。"""
        return self.api_token_enabled

    def safe_summary(self) -> dict:
        """用于启动时打印配置概览 —— 绝不包含密码与 Token 明文。"""
        return {
            "listen": f"{self.app_host}:{self.app_port}",
            "workers": self.app_workers,
            "database": f"{self.db_type} @ {self.db_host}:{self.db_port}/{self.db_name}",
            "db_user": self.db_user,
            "pool": f"{self.db_pool_min}~{self.db_pool_max} x {self.app_workers} workers"
                    f" = 最多 {self.total_db_connections} 条连接",
            "token_configured": not self.api_token_is_placeholder,
            "x_token_auth": (
                "已启用（请求必须带 X-Token）"
                if self.auth_enabled
                else "已关闭（上报接口不做鉴权）"
            ),
            "rate_limit": (
                f"{self.rate_limit_times} 次 / {self.rate_limit_window:g} 秒 / IP"
                if self.rate_limit_enabled
                else "已关闭"
            ),
            "trust_proxy_headers": self.trust_proxy_headers,
            "timezone": self.app_timezone,
        }


def _build_settings() -> Settings:
    # 先解析数据库类型，再据此决定默认端口（PG 默认 5432，MySQL 默认 3306）。
    # 用户显式设置了 DB_PORT 时以用户为准。
    raw_db_type = _get_str("DB_TYPE", "mysql").lower()
    db_type = "postgresql" if raw_db_type in {"postgresql", "postgres", "pg"} else "mysql"
    default_db_port = 5432 if db_type == "postgresql" else 3306

    return Settings(
        app_host=_get_str("APP_HOST", "0.0.0.0"),
        app_port=_get_int("APP_PORT", 8000),
        app_workers=_get_int("APP_WORKERS", 4),
        app_request_timeout=float(_get_int("APP_REQUEST_TIMEOUT", 10)),
        db_type=db_type,
        db_host=_get_str("DB_HOST", "127.0.0.1"),
        db_port=_get_int("DB_PORT", default_db_port),
        db_user=_get_str("DB_USER", "root"),
        db_password=_get_raw_str("DB_PASSWORD", ""),
        db_name=_get_str("DB_NAME", "sms_monitor"),
        db_pool_min=_get_int("DB_POOL_MIN", 1),
        db_pool_max=_get_int("DB_POOL_MAX", 10),
        db_connect_timeout=_get_int("DB_CONNECT_TIMEOUT", 10),
        db_query_timeout=float(_get_int("DB_QUERY_TIMEOUT", 8)),
        db_retry_times=_get_int("DB_RETRY_TIMES", 3),
        api_token=_get_str("API_TOKEN", ""),
        api_token_enabled=_get_bool("API_TOKEN_ENABLED", True),
        rate_limit_enabled=_get_bool("RATE_LIMIT_ENABLED", True),
        rate_limit_times=_get_int("RATE_LIMIT_TIMES", 300),
        rate_limit_window=float(_get_int("RATE_LIMIT_WINDOW", 60)),
        trust_proxy_headers=_get_bool("TRUST_PROXY_HEADERS", False),
        app_timezone=_get_str("APP_TIMEZONE", "Asia/Shanghai"),
        log_level=_get_str("LOG_LEVEL", "INFO").upper(),
        page_size_default=_get_int("PAGE_SIZE_DEFAULT", 20),
        page_size_max=_get_int("PAGE_SIZE_MAX", 100),
        vcode_after_grace_seconds=_get_int("VCODE_AFTER_GRACE_SECONDS", 120),
        vcode_phone_fallback=_get_bool("VCODE_PHONE_FALLBACK", True),
        sms_send_online_window=float(_get_int("SMS_SEND_ONLINE_WINDOW", 90)),
        sms_send_task_ttl=float(_get_int("SMS_SEND_TASK_TTL", 300)),
        sms_send_stale_claim=float(_get_int("SMS_SEND_STALE_CLAIM", 90)),
        sms_send_wait_timeout=float(_get_int("SMS_SEND_WAIT_TIMEOUT", 180)),
        sms_send_poll_wait=_get_int("SMS_SEND_POLL_WAIT", 25),
        sms_send_token=_get_raw_str("SMS_SEND_TOKEN", "").strip(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内缓存配置，避免每次请求都读环境变量。"""
    return _build_settings()
