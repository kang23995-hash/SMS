# -*- coding: utf-8 -*-
"""短信代发任务的持久化存储。

qianwen_auto 遇到「发送短信码」人机验证时，会把「用哪个手机号发、发给谁、
发什么内容」提交到这里；局域网里的安卓设备（send_sms APP）长轮询领取任务，
真正把短信发出去后回执结果。

为什么用 SQLite 而不是内存字典：
    服务默认以 ``--workers 4`` 启动，4 个进程各自有独立内存。设备轮询和
    qianwen 提交任务很可能落在不同进程上，内存队列会「谁都看不见谁」。
    SQLite 是跨进程共享的本地文件，写入量很小（每条任务几行），足够用。

并发控制：
    每条 SQL 都用 ``BEGIN IMMEDIATE`` 抢写锁，配合 WAL 与 busy_timeout，
    多进程同时领取任务时也只会有一个进程成功把某条任务改成 ``sending``。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from config import BASE_DIR

DB_PATH = BASE_DIR / "data" / "sms_send.db"

# 只在同一进程内串行化数据库访问；跨进程靠 SQLite 自己的锁
_LOCK = threading.RLock()

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


def normalize_phone(raw: str | None) -> str:
    """把手机号归一成纯数字。

    只去掉空格、``+``、连字符和 ``0086/86`` 国家码前缀；
    **不做长度截断** —— 收件号码可能是 1069076020201123 这种 16 位服务号。
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0086") and len(digits) > 11:
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _init_schema() -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id   TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL DEFAULT '',
                    sim1        TEXT NOT NULL DEFAULT '',
                    sim2        TEXT NOT NULL DEFAULT '',
                    ip          TEXT NOT NULL DEFAULT '',
                    app_version TEXT NOT NULL DEFAULT '',
                    last_seen   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    send_phone  TEXT NOT NULL,
                    recipient   TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    requester   TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    device_id   TEXT NOT NULL DEFAULT '',
                    detail      TEXT NOT NULL DEFAULT '',
                    created_at  REAL NOT NULL,
                    claimed_at  REAL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_sms_send_task_status
                    ON tasks (status, send_phone, created_at);
                """
            )
            conn.commit()
        finally:
            conn.close()


_init_schema()


def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict]:
    return dict(row) if row is not None else None


# =====================================================================
#  设备心跳
# =====================================================================

def register_device(
    *,
    device_id: str,
    device_name: str,
    sim1: str,
    sim2: str,
    ip: str,
    app_version: str,
) -> None:
    """设备每次轮询都刷新心跳（同时更新它配置的两张卡号）。"""
    now = time.time()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO devices
                    (device_id, device_name, sim1, sim2, ip, app_version, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name = excluded.device_name,
                    sim1        = excluded.sim1,
                    sim2        = excluded.sim2,
                    ip          = excluded.ip,
                    app_version = excluded.app_version,
                    last_seen   = excluded.last_seen
                """,
                (device_id, device_name, normalize_phone(sim1),
                 normalize_phone(sim2), ip, app_version, now),
            )
            conn.commit()
        finally:
            conn.close()


def list_devices(online_window: float) -> list[dict]:
    now = time.time()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY last_seen DESC"
            ).fetchall()
        finally:
            conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item["online"] = (now - float(item["last_seen"])) <= online_window
        item["age_seconds"] = round(now - float(item["last_seen"]), 1)
        items.append(item)
    return items


def online_device_count(phone: str, online_window: float) -> int:
    """指定手机号当前有几台在线设备能发（用于日志提示）。"""
    now = time.time()
    phone = normalize_phone(phone)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM devices "
                "WHERE last_seen >= ? AND (sim1 = ? OR sim2 = ?)",
                (now - online_window, phone, phone),
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"] or 0) if row else 0


# =====================================================================
#  任务
# =====================================================================

def create_task(
    *,
    send_phone: str,
    recipient: str,
    content: str,
    requester: str,
    task_ttl: float,
) -> int:
    """创建一条代发任务，返回任务 id。"""
    now = time.time()
    send_phone = normalize_phone(send_phone)
    recipient = normalize_phone(recipient)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE tasks SET status = ?, detail = ?, finished_at = ? "
                "WHERE status = ? AND created_at < ?",
                (STATUS_FAILED, "任务过期（等待设备超时）", now,
                 STATUS_PENDING, now - task_ttl),
            )
            cur = conn.execute(
                "INSERT INTO tasks "
                "(send_phone, recipient, content, requester, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (send_phone, recipient, content, requester, STATUS_PENDING, now),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def get_task(task_id: int) -> Optional[dict]:
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def claim_task(
    *,
    device_id: str,
    sim1: str,
    sim2: str,
    task_ttl: float,
    stale_claim: float,
) -> Optional[dict]:
    """设备领取一条属于自己卡号的任务。

    在同一个 ``BEGIN IMMEDIATE`` 事务里完成「作废过期任务 → 回收卡死任务 →
    选中最早一条 pending → 标记为 sending」，保证多台设备/多个 worker
    不会重复领取同一条任务。
    """
    now = time.time()
    phones = [p for p in (normalize_phone(sim1), normalize_phone(sim2)) if p]
    if not phones:
        return None

    placeholders = ",".join("?" for _ in phones)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 过期任务直接作废：验证码几分钟就失效，迟到发出去没意义
            conn.execute(
                "UPDATE tasks SET status = ?, detail = ?, finished_at = ? "
                "WHERE status = ? AND created_at < ?",
                (STATUS_FAILED, "任务过期（等待设备超时）", now,
                 STATUS_PENDING, now - task_ttl),
            )
            # 设备领取后崩溃/被杀，超时后放回队列让别的设备接
            conn.execute(
                "UPDATE tasks SET status = ?, device_id = '', claimed_at = NULL "
                "WHERE status = ? AND claimed_at < ?",
                (STATUS_PENDING, STATUS_SENDING, now - stale_claim),
            )

            row = conn.execute(
                f"SELECT * FROM tasks "
                f"WHERE status = ? AND send_phone IN ({placeholders}) "
                f"AND created_at >= ? "
                f"ORDER BY id ASC LIMIT 1",
                (STATUS_PENDING, *phones, now - task_ttl),
            ).fetchone()

            if row is None:
                conn.commit()
                return None

            conn.execute(
                "UPDATE tasks SET status = ?, device_id = ?, claimed_at = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_SENDING, device_id, now, int(row["id"]), STATUS_PENDING),
            )
            conn.commit()

            task = dict(row)
            task["status"] = STATUS_SENDING
            task["device_id"] = device_id
            task["claimed_at"] = now
            return task
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


def set_result(
    *,
    task_id: int,
    device_id: str,
    ok: bool,
    detail: str,
) -> bool:
    """设备回执。只有当前领取者能改，防止重复回执把状态覆盖。"""
    now = time.time()
    status = STATUS_SENT if ok else STATUS_FAILED
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, detail = ?, finished_at = ? "
                "WHERE id = ? AND device_id = ?",
                (status, detail, now, int(task_id), device_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def recent_tasks(limit: int = 20) -> list[dict]:
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]
