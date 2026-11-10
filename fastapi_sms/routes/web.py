"""页面路由：短信列表页 + 健康检查。

页面是给人看的，所以这里不做 X-Token 校验（浏览器直接打开 URL 即可），
**但这也意味着局域网内任何人都能看短信内容**。如果不希望如此，
可以把服务只绑定到 127.0.0.1 并用本机浏览器看，或者架一层带认证的反向代理
（详见 DEPLOY.md「访问控制」一节）。
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config import TEMPLATES_DIR, get_settings
from database import DatabaseUnavailableError, get_database
from models import SmsPage, SmsQueryFilter
from repository import get_repository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["页面"])

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse, summary="短信列表页")
async def index(
    request: Request,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(0, ge=0, description="每页条数，0 表示用默认值"),
    device_id: str = Query("", description="按设备 ID 筛选（包含匹配）"),
    phone: str = Query("", description="按本机 SIM 号码筛选（包含匹配）"),
    sender: str = Query("", description="按发送方号码筛选（包含匹配）"),
) -> HTMLResponse:
    """渲染短信列表：默认按接收时间倒序，最新在最上方。"""
    settings = get_settings()

    # 每页条数做了上下限约束，避免有人手动改 URL 把 page_size 调成 100000
    size = page_size or settings.page_size_default
    size = max(1, min(size, settings.page_size_max))

    filters = SmsQueryFilter(
        device_id=device_id.strip(),
        sim_phone_number=phone.strip(),
        sender=sender.strip(),
    )

    result = SmsPage(items=[], total=0, page=page, page_size=size)
    error_message: str | None = None
    device_count = 0

    try:
        repository = get_repository()
        result = await repository.query_page(filters, page, size)
        device_count = await repository.count_devices()
    except DatabaseUnavailableError as exc:
        # 数据库挂了也要能打开页面：显示一条明确的错误提示，而不是 500 白屏
        error_message = (
            f"数据库暂时不可用：{exc}。请检查 MySQL 是否启动、.env 中的 DB_* 配置是否正确。"
        )
        logger.error("列表页查询失败：%s", exc)

    # 构造分页链接时复用的筛选条件串（不含 page / page_size）
    query_params = {
        key: value
        for key, value in (
            ("device_id", filters.device_id),
            ("phone", filters.sim_phone_number),
            ("sender", filters.sender),
        )
        if value
    }
    base_query = urlencode(query_params)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_name": "短信监测系统",
            "result": result,
            "filters": filters,
            "page": result.page,
            "page_size": size,
            "total": result.total,
            "total_pages": result.total_pages,
            "device_count": device_count,
            "page_numbers": _page_window(result.page, result.total_pages),
            "base_query": base_query,
            "error_message": error_message,
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """浏览器打开页面时会自动请求它。

    不处理的话会走 404，每次刷新都在日志里刷一条 WARNING。
    这里直接返回 204（无内容），让日志保持干净。
    """
    return Response(status_code=204)


@router.get("/health", summary="健康检查（含数据库连通性）")
async def health() -> JSONResponse:
    """给部署自检与监控用：会真正查一次数据库，而不是只回 200。

    同时返回当前鉴权开关状态，方便测试脚本与排障时自动判断要不要带 X-Token。
    """
    settings = get_settings()
    healthy, detail = await get_database().is_healthy()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "code": 200 if healthy else 503,
            "msg": "ok" if healthy else "database unavailable",
            "database": "up" if healthy else "down",
            "auth": "enabled" if settings.auth_enabled else "disabled",
            "detail": detail,
        },
    )


def _page_window(current: int, total: int, span: int = 2) -> list[int]:
    """生成页码按钮的窗口（当前页前后各 span 页），避免页码多到铺满屏幕。"""
    if total <= 1:
        return [1] if total == 1 else []
    start = max(1, current - span)
    end = min(total, current + span)
    return list(range(start, end + 1))
