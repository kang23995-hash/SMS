# -*- coding: utf-8 -*-
"""
deepseek_auto 主入口 —— DeepSeek Android Worker

与 doubao_auto/run.py 相同的骨架：
  手机号池轮询（phone_db）→ 强制登出 → 登录（含人机验证破解）
  → 从 rpa-service 认领后台任务 → deepseek_worker.handle_unit 执行问答采集
  → 截图上传 + 结果回调 → 每号跑满 10 题自动登出换号

用法：
  python run.py --add 13800138000   # 添加手机号
  python run.py --list              # 查看手机号池
  python run.py --reset             # 清零使用次数
  python run.py --delete 13800138000
  python run.py --check             # 自检（FastAPI/手机号池/adb/RPA）
  python run.py --once              # 本地单轮测试（登录 + 问 1 题）
  python run.py                     # 正式跑：后台任务循环
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("deepseek_run")


def log(msg):
    _logger.info(str(msg))


from config import (
    SERVER_BASE_URL,
    RPA_SERVICE_URL,
    BACKEND_BASE_URL,
    PLATFORM_NAME,
    QUESTIONS_PER_PHONE,
    SCREENSHOT_DIR,
)
from phone_db import PhoneDB
from vcode_client import poll_vcode, health_check, VcodeTimeoutError
from deepseek_login import (
    connect_phone, start_deepseek, login_with_phone,
    logout, is_logged_in, force_reset, get_device_serial,
)
from worker_lib import (
    claim_work, heartbeat, abort_unit, default_worker_id,
)
import deepseek_worker

# 每手机号连跑多少题后换号（跟随 config.QUESTIONS_PER_PHONE）
BATCH_SIZE = QUESTIONS_PER_PHONE
# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 60


# =====================================================================
#  PhoneSessionManager —— 本地手机号池（10 题自动登出换号）
# =====================================================================
class PhoneSessionManager:
    """本地手机号池：次数最少优先。每轮都强制登出再登录。"""

    BATCH_SIZE = BATCH_SIZE

    def __init__(self, d):
        self.d = d
        self.db = PhoneDB()
        self._phone: str | None = None
        self._count_in_batch = 0

    def force_relogin(self, reason="进入新一轮") -> str:
        """强制登出 → force_reset → 从手机号池选号 → 登录（含人机验证）。
        不管当前状态，每轮都走完整流程。"""
        d = self.d
        log(f"===== 强制登出 + 登录新手机号（原因: {reason}）=====")

        try:
            logout(d)
        except Exception as e:
            log(f"logout 异常（忽略）: {e}")

        try:
            force_reset(d)
        except Exception as e:
            log(f"force_reset 异常（忽略）: {e}")

        time.sleep(3)
        start_deepseek(d)

        phone = self.db.pick()
        if not phone:
            raise RuntimeError("手机号池空！先 python run.py --add 加几个号")
        log(f"选中手机号: {phone}")

        def poller(after_ts=None):
            v, _ = poll_vcode(phone, after_ts=after_ts)
            return v

        login_with_phone(d, phone, vcode=None, poller=poller)
        self._phone = phone
        self._count_in_batch = 0
        log(f"===== 手机号 {phone} 登录成功，开始问问题 =====")
        return phone

    def mark_used(self):
        if not self._phone:
            return
        self.db.increment(self._phone)
        self._count_in_batch += 1
        log(f"手机号 {self._phone} 使用次数 +1"
            f"（批次 {self._count_in_batch}/{self.BATCH_SIZE}）")

    def should_relogin(self) -> bool:
        return self._count_in_batch >= self.BATCH_SIZE


# =====================================================================
#  make_handle_unit —— 闭包：问答采集 + 使用计数
#  （上传截图 / callback 已封装在 deepseek_worker.handle_unit 内部）
# =====================================================================
def make_handle_unit(session_mgr: PhoneSessionManager):
    def handle_unit(d, unit):
        unit_id = unit.get("id")
        task_no = unit.get("taskNo")
        question = str(unit.get("questionText") or "").strip()
        if not question:
            raise RuntimeError("任务问题为空")

        task_dir = SCREENSHOT_DIR / str(task_no or unit_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        log(f"===== 单元: id={unit_id} taskNo={task_no} =====")
        log(f"问题: {question[:80]}")

        # deepseek_worker.handle_unit 内部完成：
        #   新对话 → 深度思考 → 输入 → 发送 → 等回答 → 思考采集
        #   → 长截图 → HTML 生成 → 上传截图 → callback 回调
        deepseek_worker.handle_unit(d, unit)

        session_mgr.mark_used()
        log(f"✅ 单元完成: {task_no}")

    return handle_unit


# =====================================================================
#  run_deepseek_loop —— 主循环（认领 → 心跳 → 执行 → 换号）
# =====================================================================
def run_deepseek_loop(d, worker_id=None):
    smgr = None
    consecutive_failures = 0
    current_unit_id = None
    worker_id = worker_id or default_worker_id()

    log(f"DeepSeek Worker START platform={PLATFORM_NAME} workerId={worker_id}")
    log(f"RPA  = {RPA_SERVICE_URL}")
    log(f"BACK = {BACKEND_BASE_URL}")
    log(f"每手机号连跑 {PhoneSessionManager.BATCH_SIZE} 题后换号")

    # 启动必须先登录成功一次（失败持续重试，直到登上）
    while smgr is None:
        try:
            smgr = PhoneSessionManager(d)
            smgr.force_relogin(reason="程序启动")
        except Exception as e:
            log(f"启动登录失败，10s 后重试: {e}")
            time.sleep(10)

    handle_unit = make_handle_unit(smgr)
    last_heartbeat = 0.0

    try:
        while True:
            unit = claim_work(PLATFORM_NAME, worker_id)
            if not unit:
                time.sleep(15)
                continue

            unit_id = unit.get("id")
            current_unit_id = unit_id
            task_no = unit.get("taskNo") or str(unit_id)
            q = (unit.get("questionText") or unit.get("question") or "")[:50]
            log(f"CLAIM id={unit_id} taskNo={task_no} q={q}")

            # 心跳续约
            now = time.time()
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                try:
                    heartbeat(unit_id, worker_id)
                    last_heartbeat = now
                except Exception:
                    pass

            try:
                # 批次到量 → 登出换号
                if smgr.should_relogin():
                    log("BATCH DONE -> force_relogin")
                    smgr.force_relogin(reason="批次完成")

                handle_unit(d, unit)
                consecutive_failures = 0

            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_failures += 1
                log(f"FAIL {consecutive_failures}/3: {e}")
                # 异常单元归还任务池
                try:
                    abort_unit(unit_id, worker_id)
                except Exception:
                    pass
                # 登录态可能损坏 → 强制重登恢复
                try:
                    smgr.force_relogin(reason="异常恢复")
                except Exception:
                    pass
                if consecutive_failures >= 3:
                    log("连续失败 3 次，退出")
                    break
                time.sleep(10)

    except KeyboardInterrupt:
        log("Ctrl+C")
    finally:
        if current_unit_id:
            try:
                abort_unit(current_unit_id, worker_id)
            except Exception:
                pass
        log("DeepSeek Worker 退出")


# =====================================================================
#  命令行 + 自检 + 主入口
# =====================================================================
def _setup_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DeepSeek Android Worker")
    p.add_argument("--check", action="store_true", help="只自检不跑任务")
    p.add_argument("--list", action="store_true", help="列出手机号池")
    p.add_argument("--add", type=str, metavar="PHONE", help="添加手机号")
    p.add_argument("--reset", action="store_true", help="重置所有手机号使用次数")
    p.add_argument("--delete", type=str, metavar="PHONE", help="删除一个手机号")
    p.add_argument("--once", action="store_true", help="本地跑 1 轮（测试）")
    return p.parse_args()


def _check() -> bool:
    ok = True
    h = health_check()
    if h:
        log(f"✅ FastAPI OK: {SERVER_BASE_URL}/health")
    else:
        log(f"❌ FastAPI 挂了: {SERVER_BASE_URL}")
        ok = False

    db = PhoneDB()
    cnt = db.count()
    if cnt == 0:
        log("❌ 手机号池空！python run.py --add 13800138000")
        ok = False
    else:
        log(f"✅ 手机号池: {cnt} 个号，下一个 {db.pick()}")

    try:
        log(f"✅ adb 设备: {get_device_serial()}")
    except Exception as e:
        log(f"❌ adb 没连上: {e}")
        ok = False

    # 千问 key 检查（人机验证的命脉）
    from config import QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL
    if QWEN_API_KEY:
        log(f"✅ 千问 VL 已配置: model={QWEN_MODEL} base={QWEN_BASE_URL}")
    else:
        log("❌ 未配置 QWEN_API_KEY（.env 或环境变量），人机验证无法破解")
        ok = False

    try:
        r = requests.get(RPA_SERVICE_URL + "/actuator/health", timeout=3)
        if r.ok:
            log("✅ RPA OK")
        else:
            log(f"⚠️  RPA {r.status_code}（不影响 --once）")
    except Exception:
        log("⚠️  RPA 不可达（不影响 --once）")

    return ok


def main():
    args = _setup_args()

    if args.list:
        print("\n".join(
            f"{r['id']:>4} {r['phone']:^14} {r['use_count']:>8}"
            for r in PhoneDB().list_all()
        ))
        return
    if args.add:
        PhoneDB().add(args.add.strip())
        print(f"已添加 {args.add}")
        return
    if args.reset:
        PhoneDB().reset_all()
        print("已清零")
        return
    if args.delete:
        ok = PhoneDB().remove(args.delete.strip())
        print(f"已删除 {args.delete}" if ok else f"找不到 {args.delete}")
        return

    if not _check():
        log("自检未通过")
        sys.exit(1)
    if args.check:
        log("自检通过")
        return

    d = connect_phone()
    start_deepseek(d)

    if args.once:
        # once 模式也要先 force_relogin（完整走一遍人机验证登录）
        smgr = PhoneSessionManager(d)
        smgr.force_relogin(reason="程序启动")

        from config import QUESTIONS
        q = QUESTIONS[0] if QUESTIONS else "你好，做个自我介绍"
        log(f"[本地] phone={smgr._phone} 问题={q}")
        # 本地测试：构造一个假 unit 走完整采集流程（不上传不回调会失败，
        # 所以本地模式直接调底层能力做冒烟）
        deepseek_worker.new_chat(d)
        deepseek_worker.input_text(d, q)
        deepseek_worker.send_message(d)
        answer = deepseek_worker.wait_answer_finish(d)
        log(f"[本地] 回答 {len(answer)} 字符: {answer[:100]}...")
        return

    log("=" * 60)
    log("DeepSeek Android Worker 启动")
    log(f"RPA: {RPA_SERVICE_URL}")
    log(f"回调: {BACKEND_BASE_URL}")
    log(f"验证码: {SERVER_BASE_URL}")
    log(f"批次: 每 {BATCH_SIZE} 题自动登出换手机号")
    log("=" * 60)

    worker_id = f"deepseek_app_{socket.gethostname()}"
    try:
        run_deepseek_loop(d, worker_id)
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
    finally:
        try:
            force_reset(d)
        except Exception:
            pass


if __name__ == "__main__":
    main()
