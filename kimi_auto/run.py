# -*- coding: utf-8 -*-
"""
kimi_auto 主入口 —— Kimi Android Worker
================================================================

主流程（与 deepseek_auto 骨架一致，但按 Kimi 的实际情况改造）：

    手机号池轮询（phone_db，次数最少优先）
      → 强制登出 + 登录新手机号（kimi_login，①~⑩ 步骤）
      → 从 rpa-service 认领后台任务，kimi_worker.capture_one_answer 执行
        「新建对话 → 提问 → 等思考与回答完毕 → 采集问题/回答/思考 → 长截图」
      → 上传截图 + 结果回调（worker_lib）
      → **每跑满 10 题，该手机号使用次数 +1，然后登出换号**

关于「每 10 个问题才 +1」：
    用户明确要求「每提问 10 个问题，手机号的次数 +1」。
    因此计数不是每题 +1，而是跑到批次末尾一次性 +1，
    具体实现见 PhoneSessionManager.mark_used()。

用法：
  python run.py --add 13800138000      # 添加手机号
  python run.py --list                 # 查看手机号池
  python run.py --reset                # 清零使用次数
  python run.py --delete 13800138000   # 删除手机号
  python run.py --check                # 自检（FastAPI / 手机号池 / adb / RPA）
  python run.py --once                 # 本地单轮冒烟：登录 + 提问 1 题（不回调）
  python run.py                        # 正式跑：后台任务循环
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
_logger = logging.getLogger("kimi_run")


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
from vcode_client import poll_vcode, health_check
from kimi_login import (
    connect_phone, start_kimi, login_with_phone,
    logout, force_reset, get_device_serial,
    is_logged_in, is_on_login_entry,
)
from worker_lib import (
    claim_work, heartbeat, abort_unit, default_worker_id,
)
import kimi_worker

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
        """强制登出 + 登录新手机号。

        核心改进：先启动 App 判状态，**再决定要不要走登出流程**。

        Kimi 登出确认后有时会闪退，App 自动重启后直接处于未登录主页
        （显示「去登录」按钮）。此时如果还强行走「开抽屉 → 点头像 →
        进设置 → 下滑 → 点退出登录」整套流程，只会在开抽屉阶段反复失败
        （因为抽屉根本不是打开状态）。

        所以这里先判断启动后的实际状态：
          - 有「去登录」按钮 → 未登录主页 → 直接进入登录流程（跳过登出！）
          - 已登录           → 正常登出 → force_reset → 再启动 → 登录
          - 其他异常态       → force_reset 复位 → 再启动 → 登录
        """
        d = self.d
        log(f"===== 强制登出 + 登录新手机号（原因: {reason}）=====")

        # --- 先启动 App 等稳定 ---
        start_kimi(d, wait=6)

        # --- 检测当前状态，决定是否需要走登出流程 ---
        if is_on_login_entry(d):
            # ★ 关键场景：App 闪退重启后停在未登录主页，直接跳登录！
            log("启动后检测到「去登录」按钮 → 未登录主页，跳过登出流程，直接进入登录")
        elif is_logged_in(d):
            log("启动后检测到已登录 → 走正常登出流程")
            try:
                logout(d)
            except Exception as e:
                log(f"logout 异常（忽略）: {e}")
            try:
                force_reset(d)
            except Exception as e:
                log(f"force_reset 异常（忽略）: {e}")
            time.sleep(3)
            start_kimi(d, wait=5)
        else:
            log(f"启动后既非已登录也非未登录主页（可能在设置/验证码等中间态），force_reset 复位")
            try:
                force_reset(d)
            except Exception as e:
                log(f"force_reset 异常（忽略）: {e}")
            time.sleep(3)
            start_kimi(d, wait=5)

        # --- 选手机号 ---
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
        """完成一题后调用：批次计数 +1；跑到批次末尾才给手机号使用次数 +1。"""
        if not self._phone:
            return
        self._count_in_batch += 1
        log(f"批次进度 {self._count_in_batch}/{self.BATCH_SIZE}")

        if self._count_in_batch >= self.BATCH_SIZE:
            # ⚠️ 用户要求：每提问 10 个问题，手机号次数 +1（而不是每题 +1）
            self.db.increment(self._phone)
            log(f"手机号 {self._phone} 使用次数 +1（本批 {self.BATCH_SIZE} 题已完成）")

    def should_relogin(self) -> bool:
        return self._count_in_batch >= self.BATCH_SIZE


# =====================================================================
#  make_handle_unit —— 闭包：问答采集 + 使用计数
# =====================================================================
def make_handle_unit(session_mgr: PhoneSessionManager):
    """包装 kimi_worker.handle_unit：单元完成后累加批次计数。"""

    def handle_unit(d, unit):
        unit_id = unit.get("id")
        task_no = unit.get("taskNo")
        question = str(unit.get("questionText") or "").strip()
        if not question:
            raise RuntimeError("任务问题为空")

        task_dir = SCREENSHOT_DIR / str(task_no or unit_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        log(f"===== 单元: id={unit_id} taskNo={task_no} 问题={question[:60]} =====")

        # handle_unit 内部完成：新建对话 → 输入 → 发送 → 等完毕 → 采集 → 长截图
        #                        → 上传截图 → 回调结果
        kimi_worker.handle_unit(d, unit)

        session_mgr.mark_used()
        log(f"✅ 单元完成: {task_no}")

    return handle_unit


# =====================================================================
#  run_kimi_loop —— 主循环（认领 → 心跳 → 执行 → 换号）
# =====================================================================
def run_kimi_loop(d, worker_id=None):
    smgr = None
    consecutive_failures = 0
    current_unit_id = None
    worker_id = worker_id or default_worker_id()

    log(f"Kimi Worker START platform={PLATFORM_NAME} workerId={worker_id}")
    log(f"RPA  = {RPA_SERVICE_URL}")
    log(f"BACK = {BACKEND_BASE_URL}")
    log(f"每手机号连跑 {PhoneSessionManager.BATCH_SIZE} 题后换号（换号时才 +1 次）")

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
                # 只有连续 3 次都失败才强制登出换号恢复
                # 单次 / 双次失败只是跳过这题继续认领 —— 避免一次验证误判就把整套登录态打崩
                if consecutive_failures >= 3:
                    log("连续失败 3 次，强制登出恢复登录态")
                    try:
                        smgr.force_relogin(reason="连续失败 3 次")
                    except Exception:
                        pass
                    if consecutive_failures >= 3:
                        log("连续失败 3 次，退出")
                        break
                else:
                    log(f"单题失败，继续认领下一题（已失败 {consecutive_failures} 次）")
                time.sleep(10)

    except KeyboardInterrupt:
        log("Ctrl+C")
    finally:
        if current_unit_id:
            try:
                abort_unit(current_unit_id, worker_id)
            except Exception:
                pass
        log("Kimi Worker 退出")


# =====================================================================
#  命令行 + 自检 + 主入口
# =====================================================================
def _setup_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kimi Android Worker")
    p.add_argument("--check", action="store_true", help="只自检不跑任务")
    p.add_argument("--list", action="store_true", help="列出手机号池")
    p.add_argument("--add", type=str, metavar="PHONE", help="添加手机号")
    p.add_argument("--reset", action="store_true", help="重置所有手机号使用次数")
    p.add_argument("--delete", type=str, metavar="PHONE", help="删除一个手机号")
    p.add_argument("--once", action="store_true", help="本地跑 1 题（登录 + 采集，不回调）")
    return p.parse_args()


def _check() -> bool:
    ok = True

    if health_check():
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

    # Kimi 登录无图形人机验证，因此这里不再检查 QWEN_API_KEY（与 deepseek 不同）
    log("ℹ️  Kimi 登录无人机验证，无需千问 Key")

    try:
        r = requests.get(RPA_SERVICE_URL + "/actuator/health", timeout=3)
        if r.ok:
            log("✅ RPA OK")
        else:
            log(f"⚠️  RPA {r.status_code}（不影响 --once）")
    except Exception:
        log("⚠️  RPA 不可达（不影响 --once）")

    return ok


def _run_once(d) -> int:
    """本地单轮冒烟：登录 → 新建对话 → 提问 1 题 → 采集 → 长截图（不上传不回调）。"""
    smgr = PhoneSessionManager(d)
    smgr.force_relogin(reason="本地 --once")

    from config import QUESTIONS
    question = QUESTIONS[0] if QUESTIONS else "你好，做个自我介绍"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    task_dir = SCREENSHOT_DIR / f"once_{stamp}"
    task_dir.mkdir(parents=True, exist_ok=True)

    log(f"[本地] 手机号={smgr._phone} 问题={question}")
    answer_text, thinking_text = kimi_worker.capture_one_answer(d, question, task_dir)
    log(f"[本地] 回答 {len(answer_text)} 字符：{answer_text[:100]}...")
    log(f"[本地] 思考 {len(thinking_text)} 字符")
    log(f"[本地] 产物目录：{task_dir}")
    smgr.mark_used()
    return 0


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
    start_kimi(d)

    if args.once:
        sys.exit(_run_once(d))

    log("=" * 60)
    log("Kimi Android Worker 启动")
    log(f"RPA: {RPA_SERVICE_URL}")
    log(f"回调: {BACKEND_BASE_URL}")
    log(f"验证码: {SERVER_BASE_URL}")
    log(f"批次: 每 {BATCH_SIZE} 题自动登出换手机号（换号时该号次数 +1）")
    log("=" * 60)

    worker_id = f"kimi_app_{socket.gethostname()}"
    try:
        run_kimi_loop(d, worker_id)
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
    finally:
        try:
            force_reset(d)
        except Exception:
            pass


if __name__ == "__main__":
    main()
