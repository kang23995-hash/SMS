"""数据模型。

分两类，不要混用：

1. **Pydantic 模型**（``SmsPayload`` / ``ApiResponse``）
   负责接口层的强校验与序列化。请求进来先过这一层，
   格式不对、字段缺失、类型错误都会在进入业务逻辑之前被拦下（422）。

2. **数据库行模型**（``SmsRecord`` / ``SmsPage``）
   表里一行数据的 Python 表示，供业务层与模板层使用，
   避免到处传递裸 dict、把字段名拼错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from config import get_settings
from sms_parse import normalize_phone

# =====================================================================
#  时区处理
# =====================================================================


def normalize_datetime(value: datetime, tz_name: str) -> datetime:
    """把上报时间归一到配置时区，并去掉时区信息。

    原因：安卓端上报的是带时区的 ISO-8601（例如 ``2026-10-12T21:30:05+08:00``），
    而 MySQL 的 ``DATETIME`` 字段本身不存储时区。为避免同一批数据里
    混入不同时区的时间戳，统一转换到 ``APP_TIMEZONE`` 后再入库。
    """
    if value.tzinfo is None:
        # 上报方没带时区，视为已经是本地时间，原样使用
        return value
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        # 时区库缺失（Windows 上缺 tzdata 包时会出现）时降级：
        # 去掉时区信息直接入库，不因为一个时区配置让整条链路失败
        return value.replace(tzinfo=None)
    return value.astimezone(tz).replace(tzinfo=None)


# =====================================================================
#  接口层：Pydantic 请求 / 响应模型
# =====================================================================


class SmsPayload(BaseModel):
    """APP 上报的单条短信。

    字段名与安卓端 JSON 完全一致（camelCase），内部用 snake_case，
    通过 ``alias_generator`` 自动映射，两边都不用迁就对方。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        # 多余字段直接忽略：将来 APP 加了新字段，老服务也不会因此报错
        extra="ignore",
        str_strip_whitespace=True,
    )

    device_id: str = Field(
        ..., min_length=1, max_length=128, description="设备唯一标识"
    )
    sim_slot: int = Field(
        ..., ge=0, le=8, description="SIM 卡槽编号：1/2，0 表示未知"
    )
    sim_phone_number: str = Field(
        default="",
        max_length=32,
        description="本机 SIM 号码，读不到时为空串。入库前会自动去掉 +86 国家码",
    )
    sender: str = Field(..., min_length=1, max_length=64, description="发送方号码")
    message_content: str = Field(
        ...,
        max_length=10000,
        description="短信正文。上限 10000 字符，远超实际长短信长度，"
        "同时保证 utf8mb4 下不会撑爆 TEXT 字段",
    )
    receive_time: datetime = Field(..., description="短信接收时间，ISO-8601")
    message_unique_id: str = Field(
        ..., min_length=1, max_length=64, description="消息唯一 ID（UUID），用于去重"
    )

    @field_validator("sim_phone_number")
    @classmethod
    def _normalize_sim_phone(cls, value: str) -> str:
        """把 ``+8618798853401`` 这类带国家码的号码归一成纯手机号。

        在进入业务逻辑之前就统一格式，表里存的永远是「11 位本地号码」，
        页面筛选、去重对账都不会因为前缀不一致而出错。
        """
        return normalize_phone(value)

    def normalized_receive_time(self) -> datetime:
        """转换到配置时区、去掉 tzinfo 后用于入库。"""
        return normalize_datetime(self.receive_time, get_settings().app_timezone)


class ApiResponse(BaseModel):
    """统一响应信封：所有接口都返回 ``{"code": ..., "msg": ...}``。"""

    code: int = Field(..., description="业务状态码，与 HTTP 状态码保持一致")
    msg: str = Field(..., description="提示信息")


class DeletePayload(BaseModel):
    """删除请求体：按主键 id 批量删除（支持单选/多选）。"""

    ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="要删除的记录主键 id 列表，最多一次 1000 条",
    )


class ClearPayload(BaseModel):
    """清空请求体：必须显式带上 ``confirm=true`` 才执行。"""

    confirm: bool = Field(
        False,
        description="清空是不可逆操作，必须显式传 true 才会真正执行",
    )


# =====================================================================
#  数据层：数据库行模型
# =====================================================================


@dataclass(slots=True)
class SmsRecord:
    """``sms_records`` 表的一行。

    这是「数据库 ORM 模型」的角色：把表结构固定成 Python 对象，
    查询结果统一经 :meth:`from_row` 转换，字段名只在模型里出现一次。
    """

    id: int
    message_unique_id: str
    device_id: str
    sim_slot: int
    sim_phone_number: str
    sender: str
    sender_name: str
    message_content: str
    sms_vcode: str
    receive_time: datetime
    create_time: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SmsRecord":
        """把数据库查询结果（dict）转成模型。"""
        return cls(
            id=int(row["id"]),
            message_unique_id=row["message_unique_id"] or "",
            device_id=row["device_id"] or "",
            sim_slot=int(row["sim_slot"] or 0),
            sim_phone_number=row["sim_phone_number"] or "",
            sender=row["sender"] or "",
            sender_name=row["sender_name"] or "",
            message_content=row["message_content"] or "",
            sms_vcode=row["sms_vcode"] or "",
            receive_time=row["receive_time"],
            create_time=row["create_time"],
        )

    @property
    def sim_slot_label(self) -> str:
        """页面展示用：卡槽编号，0 显示为「未知」。"""
        return f"卡槽 {self.sim_slot}" if self.sim_slot else "未知"

    @property
    def sim_phone_label(self) -> str:
        return self.sim_phone_number or "-"

    @property
    def sender_name_label(self) -> str:
        """页面展示用：没提取到签名时显示「-」。"""
        return self.sender_name or "-"

    @property
    def sms_vcode_label(self) -> str:
        """页面展示用：没提取到验证码时显示「-」。"""
        return self.sms_vcode or "-"


@dataclass(slots=True)
class SmsQueryFilter:
    """列表页的筛选条件（都是「包含匹配」，空串表示不筛选）。"""

    device_id: str = ""
    sim_phone_number: str = ""
    sender: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.device_id or self.sim_phone_number or self.sender)


@dataclass(slots=True)
class SmsPage:
    """一页查询结果，附带分页元信息。"""

    items: list[SmsRecord] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))  # 向上取整

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages
