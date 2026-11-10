"""数据库连接池与事务封装（同时支持 MySQL 与 PostgreSQL）。

后端选择
--------
通过 ``.env`` 的 ``DB_TYPE`` 决定用哪个异步驱动：

    DB_TYPE=mysql        ->  aiomysql（默认，向后兼容）
    DB_TYPE=postgresql   ->  asyncpg

对外接口完全一致（``fetch_all`` / ``fetch_one`` / ``fetch_value`` /
``execute`` / ``run_in_transaction``），所以上层业务代码（repository 等）
不需要关心底层是哪个库。

SQL 占位符
----------
全项目的 SQL 统一用 ``%s`` 书写（MySQL 原生风格）。查询发往 PostgreSQL 前，
会在这里把 ``%s`` 依次转换成 ``$1, $2 ...``，因此 **repository 里的 SQL
只写一份，两种库都能跑**。

公共设计要点
------------
* **连接池**：连接在进程内复用，绝不每次请求新建/销毁。
* **事务**：所有读写都在显式事务里执行，异常自动回滚。
* **断连自愈**：连接类错误自动丢弃坏连接、重建连接池并重试。
* **超时保护**：每条 SQL 都套了 ``asyncio.wait_for``，慢查询快速失败。
* **参数化查询**：一律用占位符 + 参数元组，由驱动转义，杜绝 SQL 注入。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional, TypeVar

from config import Settings, get_settings

# 两个驱动按需导入：只装其中一个（或只用一个后端）也能正常启动。
# 真正用到但没装时，在建立连接池那一步给出明确报错。
try:  # pragma: no cover - 取决于运行环境装了哪个驱动
    import aiomysql
except ImportError:  # pragma: no cover
    aiomysql = None  # type: ignore[assignment]

try:  # pragma: no cover
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---- MySQL：连接层面的可恢复错误码，出现这些才值得重试 ----
#   2006 MySQL server has gone away
#   2013 Lost connection to MySQL server during query
#   2055 Lost connection to MySQL server at 'reading authorization packet'
#   1927 Connection was killed
#   1047 Unknown command（连接状态错乱）
#   1053 Server shutdown in progress
_RETRYABLE_ERRNOS = frozenset({2006, 2013, 2055, 1927, 1047, 1053})

# ---- PostgreSQL：连接层面的可恢复 SQLSTATE ----
#   08xxx 连接异常族（08006 连接失败、08003 连接不存在 …）
#   57P01 管理端关闭连接 / 57P02 崩溃 / 57P03 暂时无法连接
#   53300 连接数已满
_PG_RETRYABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})

# PostgreSQL：判断一条 SQL 是否会返回结果集（决定用 fetch 还是 execute）。
# 命中「以 select/with/... 开头」或「含 RETURNING 子句」时按查询处理。
_PG_ROW_RETURNING_RE = re.compile(
    r"^\s*(?:select|with|values|show|explain|table)\b|\breturning\b",
    re.IGNORECASE,
)


class DatabaseUnavailableError(RuntimeError):
    """数据库不可用（连不上、连接池未就绪、SQL 超时）。

    上层捕获后应返回 503，让 APP 按重试策略继续重试，
    而不是返回 500 让 APP 以为报文本身有问题。
    """


# =====================================================================
#  PostgreSQL 辅助：占位符转换 + 游标适配
# =====================================================================


def _to_pg_placeholders(sql: str) -> str:
    """把 SQL 里的 ``%s`` 依次替换成 ``$1, $2 ...``（asyncpg 风格）。

    只替换 ``%s`` 这一种模式；SQL 文本里没有其它以 ``%`` 开头的写法
    （LIKE 的通配符在参数里，不在 SQL 文本中），因此转换是安全的。
    """
    out: list[str] = []
    index = 0
    i = 0
    length = len(sql)
    while i < length:
        if sql[i] == "%" and i + 1 < length and sql[i + 1] == "s":
            index += 1
            out.append(f"${index}")
            i += 2
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _pg_returns_rows(sql: str) -> bool:
    """该 SQL 是否会产生结果集（SELECT / RETURNING 等）。"""
    return bool(_PG_ROW_RETURNING_RE.search(sql))


def _pg_affected_rows(status: str) -> int:
    """从 asyncpg 的状态字符串里取出受影响行数。

    例如 ``"INSERT 0 1"`` -> 1，``"UPDATE 3"`` -> 3，``"DELETE 0"`` -> 0。
    """
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


class _PgCursor:
    """把 asyncpg 连接包装成与 aiomysql 游标一致的接口。

    只实现 repository 里用到的那几个成员：``execute`` / ``fetchall`` /
    ``fetchone`` / ``rowcount`` / ``lastrowid``。这样上层的 operation 回调
    （按游标编写）在两种后端下都能原样复用。
    """

    __slots__ = ("_conn", "_rows", "rowcount", "lastrowid")

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0
        self.lastrowid: Optional[int] = None

    async def execute(self, sql: str, args: Optional[tuple[Any, ...]] = None) -> None:
        query = _to_pg_placeholders(sql)
        params = tuple(args or ())

        if _pg_returns_rows(query):
            # 会返回结果集的语句（SELECT、INSERT ... RETURNING）：用 fetch，
            # 行数即 rowcount；若返回行里有 id 列，则当作 lastrowid
            # （配合 INSERT ... RETURNING id 实现“拿到自增主键”）。
            rows = await self._conn.fetch(query, *params)
            self._rows = [dict(row) for row in rows]
            self.rowcount = len(self._rows)
            self.lastrowid = self._rows[0].get("id") if self._rows else None
        else:
            status = await self._conn.execute(query, *params)
            self._rows = []
            self.rowcount = _pg_affected_rows(status)

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    async def fetchone(self) -> Optional[dict[str, Any]]:
        return self._rows[0] if self._rows else None


class Database:
    """连接池 + 事务的封装，进程内单例使用。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        # 方言：'mysql' 或 'postgresql'（供 repository 选择 upsert 语法）
        self.dialect = self._settings.db_type
        self._pool: Any = None
        # 保护连接池的创建/重建，避免并发请求同时重建出一堆池子
        self._pool_lock = asyncio.Lock()
        # 需要进行“重试/自愈”判断的底层异常类型（按后端选择）。
        # 用元组保存，因为 except 子句里不能用函数调用。
        self._operation_errors: tuple[type[BaseException], ...] = self._build_operation_errors()

    @property
    def is_postgresql(self) -> bool:
        return self.dialect == "postgresql"

    def _build_operation_errors(self) -> tuple[type[BaseException], ...]:
        """按后端返回“连接/通信类异常”的元组。驱动缺失时返回空元组。"""
        if self.is_postgresql:
            if asyncpg is None:
                return ()
            # PostgresConnectionError 覆盖所有连接类错误（08xxx、认证等）；
            # OSError 覆盖底层 socket 错误（连接被拒、重置等）
            return (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError)
        if aiomysql is None:
            return ()
        return (aiomysql.OperationalError, aiomysql.InterfaceError)

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> bool:
        """启动时建立连接池。失败只记日志不抛异常。

        这样即使数据库暂时没起来，服务仍然能启动：``GET /health`` 会
        如实报告不可用，页面会给出提示，等数据库恢复后连接池会自动重建。
        """
        try:
            await self._get_pool()
            return True
        except DatabaseUnavailableError as exc:
            logger.critical(
                "启动时无法连接 %s（%s:%s/%s）：%s。服务将继续启动，"
                "数据库恢复后会自动重连。",
                self.dialect,
                self._settings.db_host,
                self._settings.db_port,
                self._settings.db_name,
                exc,
            )
            return False

    async def close(self) -> None:
        """优雅关闭：释放所有连接。"""
        async with self._pool_lock:
            await self._close_pool_locked()

    async def is_healthy(self) -> tuple[bool, str]:
        """健康检查：真正跑一次 SELECT 1，而不是只看池子是否存在。"""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                if self.is_postgresql:
                    await conn.fetchval("SELECT 1")
                else:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")
                        await cursor.fetchone()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 —— 健康检查要吞掉所有异常
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ 对外执行接口

    async def run_in_transaction(
        self,
        operation: Callable[[Any], Awaitable[T]],
        *,
        timeout: Optional[float] = None,
        retry: bool = True,
    ) -> T:
        """在事务中执行 ``operation(cursor)``。

        * 正常结束 -> ``COMMIT``
        * 抛异常   -> ``ROLLBACK`` 后原样抛出
        * 连接类异常 -> 重建连接池并重试（最多 ``DB_RETRY_TIMES`` 次）
        * 超时     -> 直接抛 :class:`DatabaseUnavailableError`（不重试，
          避免雪上加霜）
        """
        query_timeout = timeout or self._settings.db_query_timeout
        max_attempts = self._settings.db_retry_times if retry else 1

        last_error: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._execute_once(operation), timeout=query_timeout
                )
            except asyncio.TimeoutError as exc:
                logger.error(
                    "SQL 执行超时（>%.1fs），本次请求判定为失败", query_timeout
                )
                raise DatabaseUnavailableError(
                    f"数据库响应超时（超过 {query_timeout:g} 秒）"
                ) from exc
            except DatabaseUnavailableError:
                # 池子没建起来，不属于「重试能解决」的范畴，直接上抛
                raise
            except self._operation_errors as exc:  # type: ignore[misc]
                last_error = exc
                if not self._is_retryable(exc) or attempt >= max_attempts:
                    logger.error(
                        "数据库操作失败（第 %d/%d 次尝试，不可重试或已用尽重试）: %s",
                        attempt,
                        max_attempts,
                        exc,
                    )
                    raise DatabaseUnavailableError(str(exc)) from exc

                logger.warning(
                    "数据库连接异常，第 %d/%d 次尝试失败，正在重建连接池后重试: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                await self._recycle_pool()
                # 退避，给数据库一点恢复时间
                await asyncio.sleep(min(0.2 * attempt, 1.0))

        # 理论上到不了这里
        raise DatabaseUnavailableError(str(last_error))

    # ------------------------------------------------------------ 读操作便捷方法

    async def fetch_all(
        self, sql: str, args: Optional[tuple[Any, ...]] = None
    ) -> list[dict[str, Any]]:
        """查询多行，返回 ``[{列名: 值}, ...]``。"""

        async def operation(cursor: Any) -> list[dict[str, Any]]:
            await cursor.execute(sql, args or ())
            return list(await cursor.fetchall())

        return await self.run_in_transaction(operation)

    async def fetch_one(
        self, sql: str, args: Optional[tuple[Any, ...]] = None
    ) -> Optional[dict[str, Any]]:
        """查询单行，无结果返回 ``None``。"""

        async def operation(cursor: Any) -> Optional[dict[str, Any]]:
            await cursor.execute(sql, args or ())
            return await cursor.fetchone()

        return await self.run_in_transaction(operation)

    async def fetch_value(
        self, sql: str, args: Optional[tuple[Any, ...]] = None
    ) -> Any:
        """查询单个标量值（取第一行第一列）。"""

        row = await self.fetch_one(sql, args)
        if not row:
            return None
        return next(iter(row.values()))

    async def execute(
        self, sql: str, args: Optional[tuple[Any, ...]] = None
    ) -> int:
        """执行单条写语句，返回受影响行数。

        需要多条语句组成一个原子操作时，请使用 :meth:`run_in_transaction`，
        把多条语句放进同一个 ``operation`` 回调里。
        """

        async def operation(cursor: Any) -> int:
            await cursor.execute(sql, args or ())
            return cursor.rowcount

        return await self.run_in_transaction(operation)

    # ------------------------------------------------------------ 内部实现

    async def _execute_once(
        self, operation: Callable[[Any], Awaitable[T]]
    ) -> T:
        """一次「取连接 -> 开事务 -> 执行 -> 提交/回滚」的完整动作。"""
        pool = await self._get_pool()

        if self.is_postgresql:
            # asyncpg：事务用连接自带的事务上下文管理器，退出时自动
            # COMMIT；异常时自动 ROLLBACK。
            async with pool.acquire() as conn:
                async with conn.transaction():
                    return await operation(_PgCursor(conn))

        # MySQL：DictCursor 让查询结果直接是 dict，事务手动控制
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await conn.begin()
                try:
                    result = await operation(cursor)
                except Exception:
                    # 任何异常都回滚，保证原子性
                    await conn.rollback()
                    raise
                else:
                    await conn.commit()
                    return result

    async def _get_pool(self) -> Any:
        """取连接池；不存在（或刚被回收）时惰性重建。"""
        if self._pool is not None:
            return self._pool

        async with self._pool_lock:
            # 双重检查：可能已被其它协程建好
            if self._pool is None:
                await self._create_pool_locked()
            assert self._pool is not None
            return self._pool

    async def _create_pool_locked(self) -> None:
        """在持有锁的前提下建立连接池。"""
        if self.is_postgresql:
            await self._create_pg_pool_locked()
        else:
            await self._create_mysql_pool_locked()

    async def _create_mysql_pool_locked(self) -> None:
        s = self._settings
        if aiomysql is None:
            logger.critical(
                "DB_TYPE=mysql 但未安装 aiomysql。请执行：pip install aiomysql"
            )
            raise DatabaseUnavailableError(
                "未安装 aiomysql，无法连接 MySQL（pip install aiomysql）"
            )
        try:
            self._pool = await aiomysql.create_pool(
                host=s.db_host,
                port=s.db_port,
                user=s.db_user,
                password=s.db_password,
                db=s.db_name,
                # utf8mb4 才能正确存取 emoji / 生僻字
                charset="utf8mb4",
                # 关掉自动提交，事务由我们显式控制
                autocommit=False,
                minsize=max(1, s.db_pool_min),
                maxsize=max(1, s.db_pool_max),
                connect_timeout=s.db_connect_timeout,
                echo=False,
            )
            logger.info(
                "MySQL 连接池已建立：%s:%s/%s（连接数 %d~%d）",
                s.db_host,
                s.db_port,
                s.db_name,
                s.db_pool_min,
                s.db_pool_max,
            )
        except Exception as exc:
            self._pool = None
            logger.critical(
                "创建 MySQL 连接池失败：%s（请检查 .env 的 DB_* 配置、MySQL 是否启动、"
                "账号密码是否正确）",
                exc,
                exc_info=True,
            )
            raise DatabaseUnavailableError(f"无法连接数据库：{exc}") from exc

    async def _create_pg_pool_locked(self) -> None:
        s = self._settings
        if asyncpg is None:
            logger.critical(
                "DB_TYPE=postgresql 但未安装 asyncpg。请执行：pip install asyncpg"
            )
            raise DatabaseUnavailableError(
                "未安装 asyncpg，无法连接 PostgreSQL（pip install asyncpg）"
            )
        try:
            self._pool = await asyncpg.create_pool(
                host=s.db_host,
                port=s.db_port,
                user=s.db_user,
                password=s.db_password,
                database=s.db_name,
                min_size=max(1, s.db_pool_min),
                max_size=max(1, s.db_pool_max),
                # 建立连接的超时时间
                timeout=s.db_connect_timeout,
                # 单条命令超时（与上层的 wait_for 双保险）
                command_timeout=s.db_query_timeout,
                # 让库的 CURRENT_TIMESTAMP（create_time 默认值）落在本地时区，
                # 与 receive_time 保持一致；application_name 便于在 pg_stat_activity 里识别
                server_settings={
                    "application_name": "sms_monitor",
                    "timezone": s.app_timezone,
                },
            )
            logger.info(
                "PostgreSQL 连接池已建立：%s:%s/%s（连接数 %d~%d）",
                s.db_host,
                s.db_port,
                s.db_name,
                s.db_pool_min,
                s.db_pool_max,
            )
        except Exception as exc:
            self._pool = None
            logger.critical(
                "创建 PostgreSQL 连接池失败：%s（请检查 .env 的 DB_* 配置、PostgreSQL "
                "是否启动、账号密码是否正确、库是否已创建）",
                exc,
                exc_info=True,
            )
            raise DatabaseUnavailableError(f"无法连接数据库：{exc}") from exc

    async def _recycle_pool(self) -> None:
        """丢弃当前连接池并重建（断连自愈的核心）。"""
        async with self._pool_lock:
            await self._close_pool_locked()
            try:
                await self._create_pool_locked()
            except DatabaseUnavailableError:
                # 重建也失败就留给下一次请求再试，不在这里继续纠缠
                logger.error("重建连接池失败，等待下一个请求触发重连")

    async def _close_pool_locked(self) -> None:
        if self._pool is None:
            return
        pool = self._pool
        self._pool = None
        try:
            if self.is_postgresql:
                # asyncpg 的 close() 本身就是协程，等它关闭所有连接
                await pool.close()
            else:
                pool.close()
                await pool.wait_closed()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭连接池时出现异常（可忽略）: %s", exc)

    def _is_retryable(self, exc: BaseException) -> bool:
        """判断是否属于「连接坏了，重试有意义」的错误（兼容两种后端）。"""
        # PostgreSQL：错误对象带 sqlstate
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate is not None:
            return sqlstate.startswith("08") or sqlstate in _PG_RETRYABLE_SQLSTATES

        # 底层 socket 错误（连接被拒绝 / 重置等）
        if isinstance(exc, OSError):
            return True

        # MySQL：errno 作为第一个参数
        if exc.args and isinstance(exc.args[0], int):
            return exc.args[0] in _RETRYABLE_ERRNOS

        return False


# ---------------------------------------------------------------- 单例
_database: Optional[Database] = None


def get_database() -> Database:
    """获取进程内唯一的 Database 实例。"""
    global _database
    if _database is None:
        _database = Database()
    return _database
