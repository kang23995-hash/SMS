# -*- coding: utf-8 -*-
"""
手机号池 —— SQLite 管理「轮询使用、次数最少优先」。

用法（命令行也能管）:

    # 添加手机号
    python phone_db.py add 13800138000
    python phone_db.py add 13900139001 --note 副号

    # 列出所有号
    python phone_db.py list

    # 重置所有使用次数
    python phone_db.py reset

    # 删掉一个号
    python phone_db.py remove 13800138000

代码里调用:

    from phone_db import PhoneDB
    db = PhoneDB()
    phone = db.pick()                  # 选使用次数最少的那个
    db.increment(phone)                # 用完次数 +1
    db.add("13800138000")              # 添加
    db.remove("13800138000")           # 移除
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATA_DIR

DB_PATH = DATA_DIR / "phones.db"


class PhoneDB:
    """手机号池。线程不安全 —— 整个脚本单进程用，够了。"""

    def __init__(self, db_path: Path = DB_PATH):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        """第一次启动时建表。"""
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS phones (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone       TEXT    UNIQUE NOT NULL,
                    use_count   INTEGER NOT NULL DEFAULT 0,
                    last_used   TEXT,
                    created     TEXT    DEFAULT CURRENT_TIMESTAMP,
                    note        TEXT    DEFAULT ''
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 增删查改 ----

    def add(self, phone: str, note: str = "") -> bool:
        """添加一个手机号。已存在就返回 False。"""
        phone = phone.strip()
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO phones (phone, note) VALUES (?, ?)",
                (phone, note),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def remove(self, phone: str) -> bool:
        """移除一个手机号。"""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM phones WHERE phone = ?", (phone.strip(),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_all(self) -> list[dict]:
        """列出所有号，按 use_count ASC 排。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, phone, use_count, last_used, created, note "
                "FROM phones ORDER BY use_count ASC, phone ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def pick(self) -> Optional[str]:
        """选使用次数最少的那个。次数相同则随机挑一个。
        没有任何号时返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT phone FROM phones "
                "ORDER BY use_count ASC, RANDOM() LIMIT 1"
            ).fetchone()
            return row["phone"] if row else None
        finally:
            conn.close()

    def increment(self, phone: str) -> None:
        """用完 +1，顺便更新 last_used。"""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE phones SET use_count = use_count + 1, last_used = ? "
                "WHERE phone = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), phone),
            )
            conn.commit()
        finally:
            conn.close()

    def reset_all(self) -> None:
        """把所有号的 use_count 清零。"""
        conn = self._connect()
        try:
            conn.execute("UPDATE phones SET use_count = 0, last_used = NULL")
            conn.commit()
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) AS c FROM phones").fetchone()["c"]
        finally:
            conn.close()


# =====================================================================
#  命令行入口 —— 方便直接 python phone_db.py 管理
# =====================================================================

def _cmd_add(args):
    phone = args[0]
    note = args[2] if len(args) > 2 and args[1] == "--note" else ""
    ok = PhoneDB().add(phone, note)
    print(f"已添加 {phone}" if ok else f"{phone} 已存在，跳过")


def _cmd_list(_args):
    db = PhoneDB()
    items = db.list_all()
    if not items:
        print("（空）还没有任何手机号")
        print("  python phone_db.py add 13800138000   ← 添加第一个")
        return
    print(f"{'ID':>4} {'手机号':^14} {'使用次数':>8} {'最后使用':<20} 备注")
    print("-" * 80)
    for r in items:
        print(
            f"{r['id']:>4} {r['phone']:^14} {r['use_count']:>8} "
            f"{r['last_used'] or '-':<20} {r['note'] or ''}"
        )
    print(f"\n共 {len(items)} 个号，下一个会选次数最少的。")


def _cmd_remove(args):
    ok = PhoneDB().remove(args[0])
    print(f"已删除 {args[0]}" if ok else f"{args[0]} 不存在")


def _cmd_reset(_args):
    PhoneDB().reset_all()
    print("所有手机号的使用次数已清零。")


def _cmd_pick(_args):
    phone = PhoneDB().pick()
    print(phone or "（空）没有可用手机号")


HELP = """手机号池管理工具
用法：
  python phone_db.py add    13800138000 [--note 备注]
  python phone_db.py list
  python phone_db.py remove 13800138000
  python phone_db.py reset
  python phone_db.py pick
"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(HELP)
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    {"add": _cmd_add, "list": _cmd_list, "ls": _cmd_list,
     "remove": _cmd_remove, "rm": _cmd_remove,
     "reset": _cmd_reset, "pick": _cmd_pick,
     "-h": lambda a: print(HELP), "--help": lambda a: print(HELP),
     "help": lambda a: print(HELP),
     }.get(cmd, lambda a: print(f"未知命令: {cmd}\n{HELP}"))(args)
