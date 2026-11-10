"""短信表的数据访问层。

所有 SQL 集中在这里，并且**一律使用 ``%s`` 占位符 + 参数元组**，
由驱动负责转义，从根本上杜绝 SQL 注入；任何位置都不做字符串拼接。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from database import Database, get_database
from models import SmsPage, SmsQueryFilter, SmsRecord

logger = logging.getLogger(__name__)

TABLE = "sms_records"

# 查询时显式列出字段，不用 SELECT *：字段顺序稳定，也不怕将来加字段把
# 结果集撑大（TEXT 类型的 message_content 在列表页是不需要的，
# 但列表页要展示正文，所以这里保留）。
_COLUMNS = (
    "id, message_unique_id, device_id, sim_slot, sim_phone_number, "
    "sender, sender_name, message_content, sms_vcode, receive_time, create_time"
)

# ---------------------------------------------------------------------
# 入库：靠 uk_message_unique_id 唯一索引实现去重，upsert 按数据库方言分两份
#
#   MySQL      : ON DUPLICATE KEY UPDATE 字段=它自己（空更新，不覆盖原记录）
#                affected_rows：1 = 新插入；0 = 命中唯一索引（重复消息）
#   PostgreSQL : ON CONFLICT (message_unique_id) DO NOTHING RETURNING id
#                返回 1 行 = 新插入（顺带拿到自增 id）；0 行 = 重复
#
# 与 INSERT IGNORE 的区别：IGNORE 会把「字段超长」这类真正的错误也一起吞掉，
# 这里要的是「只忽略重复」。占位符统一 %s，PostgreSQL 侧由 database.py 自动
# 转成 $1, $2 ...（见 database.py 头部说明）。
# ---------------------------------------------------------------------
_INSERT_COLUMNS = (
    "message_unique_id, device_id, sim_slot, sim_phone_number, "
    "sender, sender_name, message_content, sms_vcode, receive_time"
)
_INSERT_VALUES = "(%s, %s, %s, %s, %s, %s, %s, %s, %s)"

_INSERT_SQL_MYSQL = f"""
INSERT INTO {TABLE} ({_INSERT_COLUMNS})
VALUES {_INSERT_VALUES}
ON DUPLICATE KEY UPDATE message_unique_id = message_unique_id
"""

_INSERT_SQL_POSTGRESQL = f"""
INSERT INTO {TABLE} ({_INSERT_COLUMNS})
VALUES {_INSERT_VALUES}
ON CONFLICT (message_unique_id) DO NOTHING
RETURNING id
"""


class SmsRepository:
    """短信记录的数据访问对象。"""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db or get_database()

    # ------------------------------------------------------------ 写入

    async def insert_sms(
        self,
        *,
        message_unique_id: str,
        device_id: str,
        sim_slot: int,
        sim_phone_number: str,
        sender: str,
        sender_name: str,
        message_content: str,
        sms_vcode: str,
        receive_time: Any,
    ) -> tuple[bool, Optional[int]]:
        """入库一条短信。

        整个动作在事务里完成（见 :meth:`Database.run_in_transaction`），
        异常时自动回滚。``sender_name`` 与 ``sms_vcode`` 由上层从正文里
        解析出来后一并传入。

        :return: ``(是否新插入, 新记录的主键 id)``；
                 命中唯一索引（重复消息）时返回 ``(False, None)``，
                 并且**不会修改已有记录**。
        """

        args = (
            message_unique_id,
            device_id,
            sim_slot,
            sim_phone_number,
            sender,
            sender_name,
            message_content,
            sms_vcode,
            receive_time,
        )

        sql = (
            _INSERT_SQL_POSTGRESQL
            if self._db.dialect == "postgresql"
            else _INSERT_SQL_MYSQL
        )

        async def operation(cursor) -> tuple[bool, Optional[int]]:
            await cursor.execute(sql, args)
            if self._db.dialect == "postgresql":
                # RETURNING id：有返回行 = 新插入；无返回行 = 命中唯一索引
                if cursor.lastrowid is not None:
                    return True, cursor.lastrowid
                return False, None
            affected = cursor.rowcount
            if affected == 1:
                # 新插入，lastrowid 就是自增主键
                return True, cursor.lastrowid
            # 0（重复，无变化）或 2（重复且发生了更新）都按「已存在」处理
            return False, None

        return await self._db.run_in_transaction(operation)

    # ------------------------------------------------------------ 查询

    async def query_page(
        self,
        filters: SmsQueryFilter,
        page: int,
        page_size: int,
    ) -> SmsPage:
        """分页查询，按短信接收时间倒序（最新在最前）。"""
        where_sql, where_args = self._build_where(filters)

        # 总数与设备数一次查出，省一次数据库往返
        count_sql = (
            f"SELECT COUNT(*) AS total, COUNT(DISTINCT device_id) AS devices "
            f"FROM {TABLE} {where_sql}"
        )
        count_row = await self._db.fetch_one(count_sql, where_args) or {}
        total = int(count_row.get("total") or 0)

        offset = max(0, (page - 1) * page_size)

        # ORDER BY receive_time DESC, id DESC：
        #   * receive_time 上有索引，倒序取前 N 条可以直接走索引
        #   * 再按 id 倒序是为了同一秒内的多条短信顺序稳定
        # LIMIT / OFFSET 同样用参数占位符
        list_sql = (
            f"SELECT {_COLUMNS} FROM {TABLE} {where_sql} "
            f"ORDER BY receive_time DESC, id DESC LIMIT %s OFFSET %s"
        )
        rows = await self._db.fetch_all(list_sql, (*where_args, page_size, offset))

        return SmsPage(
            items=[SmsRecord.from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def count_devices(self) -> int:
        """在线设备数（历史出现过的设备数），页面顶部展示用。"""
        value = await self._db.fetch_value(
            f"SELECT COUNT(DISTINCT device_id) AS c FROM {TABLE}"
        )
        return int(value or 0)

    # ------------------------------------------------------------ 删除

    async def delete_by_ids(self, ids: list[int]) -> int:
        """按主键 id 批量删除（单选时传一个 id 即可），返回删除条数。

        ``IN (%s, %s, ...)`` 的占位符个数按列表长度动态生成，
        **值仍全部走参数元组**，不做字符串拼接，杜绝注入。
        """
        if not ids:
            return 0
        placeholders = ", ".join(["%s"] * len(ids))
        sql = f"DELETE FROM {TABLE} WHERE id IN ({placeholders})"
        return await self._db.execute(sql, tuple(ids))

    async def delete_all(self) -> int:
        """清空整张表（不可逆），返回删除条数。

        这里用 ``DELETE`` 而不是 ``TRUNCATE``：DELETE 走事务、能被
        :meth:`Database.run_in_transaction` 的异常回滚保护，语义更安全；
        TRUNCATE 是 DDL，无法回滚且会重置自增 id。
        """
        return await self._db.execute(f"DELETE FROM {TABLE}")

    # ------------------------------------------------------------ 查询验证码（供模拟器自动化用）

    async def get_latest_vcode(
        self,
        sim_phone: str,
        sender: str = "",
        after_ts: Optional[float] = None,
        after_grace: float = 0.0,
        sender_name: str = "",
    ) -> Optional[dict]:
        """查询某手机号最新收到的、带验证码的短信。

        这是给雷电模拟器自动化脚本用的接口后端支撑：脚本轮询调用
        ``GET /api/sms/vcode?phone=138xxxx`` 时，最终走到这里。

        :param sim_phone:   真实手机号（会自动去掉 +86 国家码再查）；
                            传空串表示不按手机号过滤（兜底模式用）
        :param sender:      可选的发送方过滤（例如豆包的通道号），空串表示不限
        :param sender_name: 可选的短信签名**精确匹配**（例如 ``深度求索``）。
                            多平台自动化同时跑时，各客户端带上各自平台的签名，
                            防止「同时收到多条验证码时拿串平台」。
                            空串表示不按签名过滤（兼容旧客户端）。
        :param after_ts:    只返回 receive_time > 此时间戳（秒，UTC）的记录。
                            用于防止轮询拿到历史旧验证码：客户端点「下一步」发新
                            验证码之前先记下当前时间，轮询时带上，保证拿到的是
                            这一轮真正新收到的那条。
        :param after_grace: after 宽限秒数。短信的 receive_time 来自安卓
                            手机时钟，与客户端 PC 时钟可能差几十秒；短信又常在
                            点击后 1~3 秒到达，「手机时钟慢 + 短信太快」会把
                            刚收到的真码误判成历史旧码 → 轮询永远 204。
                            先用严格 after 查；查不到再放宽 grace 秒查一次
                            （命中会记 WARNING 日志）。
        :return: 记录 dict；没有匹配返回 ``None``
        """
        from sms_parse import normalize_phone

        phone = normalize_phone(sim_phone) if sim_phone else ""

        # 严格 after 先查
        row = await self._fetch_vcode_row(phone, sender, after_ts, sender_name)
        if row is not None:
            return row

        # 严格查不到 → 放宽 after 宽限再查一次（吸收双端时钟差）
        if after_ts is not None and after_grace > 0:
            relaxed = after_ts - after_grace
            row = await self._fetch_vcode_row(phone, sender, relaxed, sender_name)
            if row is not None:
                logger.warning(
                    "验证码命中使用了 after 宽限 phone=%s vcode=%s "
                    "receive_time=%s after=%.0f grace=%.0fs"
                    "（接收手机时钟可能慢于客户端，请校准两台设备时间）",
                    phone or "-", row.get("sms_vcode"),
                    row.get("receive_time"), after_ts, after_grace,
                )
                return row
        return None

    async def _fetch_vcode_row(
        self,
        phone: str,
        sender: str,
        after_ts: Optional[float],
        sender_name: str = "",
    ) -> Optional[dict]:
        """按条件取最新一条带验证码的记录（get_latest_vcode 的底层查询）。"""
        sql = f"SELECT {_COLUMNS} FROM {TABLE} WHERE sms_vcode != '' "
        args: list[Any] = []

        if phone:
            # 手机号按「纯数字」包含匹配：上报侧可能存成
            # 「+86 187-9885-3401」这类带分隔符的形式，直接 LIKE 数字串
            # 会永不命中。PostgreSQL 用 regexp_replace 先剥离非数字再比；
            # MySQL 5.7 无 REGEXP_REPLACE，退回普通 LIKE。
            if self._db.dialect == "postgresql":
                sql += "AND regexp_replace(sim_phone_number, '\\D', '', 'g') LIKE %s "
            else:
                sql += "AND sim_phone_number LIKE %s "
            args.append(f"%{phone}%")

        if sender:
            sql += "AND (sender LIKE %s OR sender_name LIKE %s) "
            args.extend([f"%{sender}%", f"%{sender}%"])

        if sender_name:
            # 短信签名精确匹配（=，不是 LIKE）：各平台自动化客户端各带各的签名，
            # 即使同一秒收到多条不同平台的验证码也互不干扰
            sql += "AND sender_name = %s "
            args.append(sender_name)

        if after_ts is not None:
            # 秒级时间戳 → 时间类型，按数据库方言选择函数
            if self._db.dialect == "postgresql":
                sql += "AND receive_time > to_timestamp(%s) "
            else:
                sql += "AND receive_time > FROM_UNIXTIME(%s) "
            args.append(after_ts)

        sql += "ORDER BY receive_time DESC, id DESC LIMIT 1"

        row = await self._db.fetch_one(sql, tuple(args))
        return dict(row) if row else None

    async def get_latest_for_phone(self, sim_phone: str) -> Optional[dict]:
        """取某手机号最新一条短信（不限验证码/发送方/时间）。

        仅用于 /api/sms/vcode 返回 204 时的诊断日志：快速分辨是
        「该号码根本没收到过短信」还是「有短信但被 after/sender/空验证码
        过滤掉了」。
        """
        from sms_parse import normalize_phone

        phone = normalize_phone(sim_phone)
        if not phone:
            return None

        if self._db.dialect == "postgresql":
            sql = (
                f"SELECT {_COLUMNS} FROM {TABLE} "
                f"WHERE regexp_replace(sim_phone_number, '\\D', '', 'g') LIKE %s "
                f"ORDER BY receive_time DESC, id DESC LIMIT 1"
            )
        else:
            sql = (
                f"SELECT {_COLUMNS} FROM {TABLE} "
                f"WHERE sim_phone_number LIKE %s "
                f"ORDER BY receive_time DESC, id DESC LIMIT 1"
            )
        row = await self._db.fetch_one(sql, (f"%{phone}%",))
        return dict(row) if row else None

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _build_where(filters: SmsQueryFilter) -> tuple[str, tuple[Any, ...]]:
        """把筛选条件拼成 ``WHERE`` 子句。

        仍然全部使用占位符：值通过参数元组传入，条件本身只由代码里的
        固定字符串拼成，用户输入永远不会成为 SQL 的一部分。

        注意：这里是「包含匹配」（``%关键字%``），对排障最友好，但前置
        通配符会让索引失效、退化成扫描。数据量大之后如果筛选明显变慢，
        可把下面三处改成 ``%s`` + ``f"{value}%"``（前缀匹配），
        即可命中 idx_device_id / idx_sender / idx_sim_phone。
        """
        conditions: list[str] = []
        args: list[Any] = []

        if filters.device_id:
            conditions.append("device_id LIKE %s")
            args.append(f"%{filters.device_id}%")
        if filters.sim_phone_number:
            conditions.append("sim_phone_number LIKE %s")
            args.append(f"%{filters.sim_phone_number}%")
        if filters.sender:
            conditions.append("sender LIKE %s")
            args.append(f"%{filters.sender}%")

        where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where_sql, tuple(args)


_repository: Optional[SmsRepository] = None


def get_repository() -> SmsRepository:
    """获取进程内唯一的数据访问对象。"""
    global _repository
    if _repository is None:
        _repository = SmsRepository()
    return _repository
