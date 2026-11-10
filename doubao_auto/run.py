# -*- coding: utf-8 -*-
"""
豆包 Android Worker —— 单文件版。

完整内嵌 worker_lib（任务池 + 账号池 + 心跳 + callback），
新增 PhoneSessionManager（本地手机号池 10 题自动登出换号）。

🔧 启动前改这两个 URL（或用环境变量）：
    RPA_SERVICE_URL      认领/心跳/异常归还接口
    BACKEND_BASE_URL     回调结果/上传截图接口
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

# === 环境变量 / 配置 ===
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://172.28.108.138:8084")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://172.28.108.138:8080")
CLAIM_IDLE_SLEEP = int(os.getenv("CLAIM_IDLE_SLEEP", "5"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
ACQUIRE_ACCOUNT_SLEEP = int(os.getenv("ACQUIRE_ACCOUNT_SLEEP", "15"))
PLATFORM_NAME = "doubao_app"

# FastAPI 验证码服务端
try:
    from config import SERVER_BASE_URL, SCREENSHOT_DIR
except ImportError:
    SERVER_BASE_URL = "http://localhost:8000"
    SCREENSHOT_DIR = BASE_DIR / "screenshots"
    SCREENSHOT_DIR.mkdir(exist_ok=True)


# -*- coding: utf-8 -*-
"""
worker_lib.py —— 各平台 RPA 脚本的公共「工人」运行时。

v2 更新：完整集成账号池调度！
  工人主循环现在是：
    借一个 AI 账号 → 连跑 BATCH_SIZE(默认10) 个问题 → 释放账号 → 重新借下一个 → ...
  每个账号有独立的浏览器 profile（按 account_id 命名目录），
  第一次使用时会让用户手动登录一次（cookie 持久化在 profile 里），
  之后自动从账号池轮换，再也不用手动开脚本登录了。

环境变量：
  RPA_SERVICE_URL      认领/心跳/账号池接口的地址（rpa-service，默认 http://localhost:8084）
  BACKEND_BASE_URL     回调/上传截图接口的地址（网关，默认 http://localhost:8080）
  CLAIM_IDLE_SLEEP     认领不到单元时的空转间隔秒数（默认 5）
  HEARTBEAT_INTERVAL   心跳续约间隔秒数（默认 60，需远小于服务端租约时长）
  BATCH_SIZE           每个账号连跑多少个问题后自动换号（默认 10）
  ACQUIRE_ACCOUNT_SLEEP 借不到账号时的等待间隔秒数（默认 15，因为要等账号冷却）
"""
import json
import os
import socket
import threading
import time

import requests
from datetime import datetime

# ========== 账号池相关的新配置 ==========
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))          # 一个账号连跑多少题后换号
ACQUIRE_ACCOUNT_SLEEP = int(os.getenv("ACQUIRE_ACCOUNT_SLEEP", "15"))  # 借不到账号时等多久再试


# --- 我们自己的 log（替代原 worker_lib._log）---
import logging as _logging
_logging.basicConfig(
    level=_logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
_logger = _logging.getLogger("doubao_worker")
def log(msg): _logger.info(str(msg))


def _post(url, payload, timeout=30):
    """统一 POST 封装。

    连接拒绝/超时：限频打一条 WARNING + 标记 RPA 服务暂时不可达（触发主循环退避）；
    HTTP 非 200：按原逻辑打日志；
    其它异常：限频打日志。
    所有失败统一返回 None。
    """
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
        log(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    except Exception as e:
        # 连接拒绝 / 超时 → 标记服务不可达，主循环会安静等待
        if isinstance(e, (requests.ConnectionError, requests.Timeout)) or "10061" in str(e):
            _mark_rpa_down()
        _throttled_log_error(url, e)
        return None


# ---------------------------------------------------------------------------
#  错误限频：同一 (URL, exception_type) 在 ERROR_LOG_THROTTLE 秒内只打一条
# ---------------------------------------------------------------------------
RPA_DOWN_SLEEP = int(os.getenv("RPA_DOWN_SLEEP", "30"))
ERROR_LOG_THROTTLE = int(os.getenv("ERROR_LOG_THROTTLE", "60"))

_last_error_log: dict[tuple[str, str], float] = {}


def _throttled_log_error(url: str, exc: Exception):
    """请求失败后限频打一条提示，其余静默。

    连接拒绝 / 超时：服务暂时无响应，属正常等待场景，用 INFO 级别提示；
    其他异常：可能是真错误，保留 WARNING。
    """
    now = time.time()
    key = (url, type(exc).__name__)
    last = _last_error_log.get(key, 0)
    if now - last >= ERROR_LOG_THROTTLE:
        exc_name = type(exc).__name__
        exc_str = str(exc)
        # 连接拒绝 / 超时：服务暂时无响应，静默退避，不打"报错"字样
        if (isinstance(exc, (requests.ConnectionError, requests.Timeout))
                or "ConnectionError" in exc_name or "10061" in exc_str
                or "Timeout" in exc_name or "timed out" in exc_str):
            _logger.info(f"RPA 服务暂时无响应，静默等待中（{RPA_DOWN_SLEEP}s 后自动重试）")
        else:
            _logger.warning(f"请求异常 {url}: {exc}")
        _last_error_log[key] = now


_rpa_down_until = 0.0


def _mark_rpa_down(duration: float = RPA_DOWN_SLEEP):
    global _rpa_down_until
    _rpa_down_until = time.time() + duration


def _rpa_is_down() -> bool:
    return _rpa_down_until > time.time()


def default_worker_id():
    return socket.gethostname()


def upload_screenshot(local_path, task_id=None):
    """上传截图到后端，返回 URL；失败时返回 None。"""
    try:
        with open(local_path, "rb") as f:
            files = {"file": (os.path.basename(local_path), f, "image/png")}
            data = {}
            if task_id is not None:
                data["taskId"] = str(task_id)
            resp = requests.post(
                BACKEND_BASE_URL + "/api/rpa/upload",
                files=files,
                data=data,
                timeout=60,
            )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 200 and isinstance(result.get("data"), dict):
                return result["data"].get("url") or (result["data"].get("data") if isinstance(result["data"].get("data"), str) else None)
            if isinstance(result.get("data"), str):
                return result["data"]
            if result.get("success") is True:
                return result.get("data")
        log(f"上传失败: {resp.text[:300]}")
        return None
    except Exception as e:
        log(f"上传异常: {e}")
        return None
# =====================================================================
#  完整登录态导出/导入（跨机器复用 cookie + localStorage + sessionStorage）
# =====================================================================

STATE_FILE_NAME = "state.json"

def export_state(context, profile_dir):
    """
    把浏览器上下文的完整登录态（cookie + localStorage + sessionStorage）以 JSON 明文导出到 profile 目录。
    使用 Playwright 的 context.storage_state() 一次性拿全，避免"只有 cookie 没有 localStorage"导致登录失效。
    """
    state_file = os.path.join(profile_dir, STATE_FILE_NAME)
    try:
        state = context.storage_state()
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        cookie_count = len(state.get("cookies", []))
        origin_count = len(state.get("origins", []))
        ls_total = sum(len(o.get("localStorage", [])) for o in state.get("origins", []))
        log(f"已导出 state: {cookie_count} cookies, {ls_total} localStorage entries → {state_file}")
        return state_file
    except Exception as e:
        log(f"导出 state 失败（不影响本次工作）: {e}")
        return None

def import_state(context, profile_dir):
    """
    从 profile 目录的 state.json 导入完整登录态（cookie + localStorage + sessionStorage）到浏览器上下文。
    """
    state_file = os.path.join(profile_dir, STATE_FILE_NAME)
    if not os.path.exists(state_file):
        return 0, 0
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        cookie_count = len(state.get("cookies", []))
        ls_total = sum(len(o.get("localStorage", [])) for o in state.get("origins", []))
        context.add_cookies(state.get("cookies", []))
        for origin_entry in state.get("origins", []):
            origin = origin_entry.get("origin", "")
            for item in origin_entry.get("localStorage", []):
                try:
                    context.add_init_script(f"localStorage.setItem({json.dumps(item['name'])}, {json.dumps(item['value'])});", )
                except Exception:
                    pass
        log(f"已从 state.json 导入 {cookie_count} cookies + {ls_total} localStorage entries")
        return cookie_count, ls_total
    except Exception as e:
        log(f"导入 state 失败（可忽略，后续会手动登录重新导出）: {e}")
        return 0, 0
# =====================================================================
#  账号池接口（新增！worker 不再硬编码浏览器 profile，每次启动先从账号池借）
# =====================================================================

def acquire_account(platform_code, worker_id):
    """
    从账号池借一个空闲账号。
    返回 dict：{id, platform, accountName, cookie, ...}；借不到返回 None。
    """
    obj = _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/acquire-account",
        {"platform": platform_code, "workerId": worker_id},
        timeout=30,
    )
    if obj and obj.get("code") == 200:
        return obj.get("data")
    log(f"借账号失败: {obj.get('message') if obj else '网络错误'}")
    return None


def release_account(account_id, worker_id, reason="BATCH_DONE"):
    """
    释放账号（用完后主动归还）。

    reason 控制后端行为：
      - "BATCH_DONE"（默认）：正常批次完成 → 后端自动冷却账号 10 分钟
      - "MANUAL_EXIT"：用户手动 Ctrl+C 退出 → 只清 worker 绑定，不冷却（账号立即可被其他 worker 借）
      - "ERROR_CHANGE"：跑崩了/遇到风控主动换号 → 后端自动冷却（等同 BATCH_DONE）
    """
    _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/release-account",
        {"accountId": account_id, "workerId": worker_id, "reason": reason},
        timeout=15,
    )


def cooldown_account(account_id, worker_id, minutes=10):
    """标记账号冷却（遇到验证码/风控时主动换号）。"""
    _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/cooldown-account",
        {"accountId": account_id, "workerId": worker_id, "minutes": minutes},
        timeout=15,
    )


def upload_account_cookies(account_id, worker_id, cookies_json):
    """
    把当前浏览器的 cookies 上传到后端账号记录。
    这样其他 worker 借到同一个账号时，后端会把 cookie 随 account dict 下发，
    免手动登录。
    """
    if not cookies_json:
        return False
    obj = _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/upload-cookie",
        {"accountId": account_id, "workerId": worker_id, "cookie": cookies_json},
        timeout=15,
    )
    ok = bool(obj and obj.get("code") == 200)
    if ok:
        log(f"已把账号 {account_id} 的 cookies 上传到后端")
    else:
        log(f"上传 cookies 到后端失败（不影响本次工作）: {obj}")
    return ok


# =====================================================================
#  任务认领 / 心跳 / 回调
# =====================================================================

def claim_work(platform_code, worker_id, account_id=None):
    """
    认领一个待执行单元。
    返回 dict(id/taskNo/aiPlatform/questionText)；无任务时返回 None。

    新增 account_id 参数：如果 worker 已经借到了账号，
    就把 accountId 传给后端，后端会记录到 task_result.account_id，
    回调时就知道该用哪个账号更新状态。
    """
    payload = {"platform": platform_code, "workerId": worker_id}
    if account_id is not None:
        payload["accountId"] = account_id
    obj = _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/assign",
        payload,
        timeout=10,
    )
    # 调试日志：看看服务端到底返回了什么
    if obj is None:
        log(f"[claim] {platform_code} 无任务或服务不可达")
        return None
    code = obj.get("code")
    if code != 200:
        log(f"[claim] {platform_code} 服务端返回 code={code}, msg={obj.get('message', '')}")
        return None
    data = obj.get("data")
    if not isinstance(data, dict):
        log(f"[claim] {platform_code} 服务端返回 data 类型异常: {type(data)}")
        return None
    if data.get("id") is None:
        log(f"[claim] {platform_code} 服务端返回 data 无 id 字段: {data}")
        return None
    return data


def heartbeat(unit_id, worker_id):
    """心跳续约，返回是否续约成功。"""
    obj = _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/heartbeat",
        {"unitId": unit_id, "workerId": worker_id},
        timeout=15,
    )
    return bool(obj and obj.get("code") == 200)


def abort_unit(unit_id, worker_id):
    """异常中止：把单元归还任务池，让别的工人重试。"""
    _post(
        RPA_SERVICE_URL + "/internal/rpa/worker/abort",
        {"unitId": unit_id, "workerId": worker_id},
        timeout=15,
    )


def callback(unit, status, answer_text=None, thinking_content=None,
             source_info=None, image_url=None, error_msg=None):
    """把单个单元的结果回调到后端（携带 taskResultId 精确定位对应记录）。"""
    payload = {
        "taskNo": unit.get("taskNo"),
        "taskResultId": unit.get("id"),
        "aiPlatform": unit.get("aiPlatform"),
        "questionText": unit.get("questionText"),
        "thinkingContent": thinking_content or "",
        "answerText": answer_text or "",
        "sourceInfo": source_info or "",
        "screenshotUrls": [image_url] if image_url else [],
        "status": status,
        "errorMsg": error_msg,
        "durationMs": 0,
    }
    obj = _post(BACKEND_BASE_URL + "/api/rpa/callback", payload, timeout=60)
    return bool(obj and obj.get("code") == 200)


# =====================================================================
#  主循环（重写：集成账号池轮换）
# =====================================================================

def run_worker_loop(platform_code, worker_context, handle_unit, worker_id=None):
    """
    工人主循环：借账号 → 连跑 BATCH_SIZE 个单元 → 释放 → 重新借。

    参数：
      platform_code  平台 code（必须等于后端 task_result.ai_platform 存储值，
                     如 deepseek/doubao/kimi/wenxin/qianwen/tencent）
      worker_context  一个 dict，包含当前 worker 的所有状态：
                     {
                       'get_page': lambda: page,          # 获取当前浏览器 page 对象
                       'open_profile': lambda(profile_dir):  # 打开指定 profile 目录的浏览器
                       'close_browser': lambda:              # 关闭当前浏览器
                       'profile_base_dir': str,             # profile 根目录
                     }
      handle_unit    回调签名 handle_unit(page, unit)，负责执行一个问题并调用 callback；
                     抛出异常时单元会被归还任务池。
      worker_id      工人标识，默认取机器名

    账号池策略：
      1. 启动时调 acquire_account 借一个账号
      2. 用这个账号连跑 BATCH_SIZE（默认10）个问题
      3. 用完后调 release_account 释放，重新借下一个
      4. 遇到验证码/风控 → 调 cooldown_account(分钟) 标记冷却，然后借新号
      5. 借不到账号时等待 ACQUIRE_ACCOUNT_SLEEP 秒后重试
    """
    worker_id = worker_id or default_worker_id()
    log(f"Worker 启动: platform={platform_code}, workerId={worker_id}")
    log(f"认领地址: {RPA_SERVICE_URL}   回调地址: {BACKEND_BASE_URL}")
    log(f"账号池模式: BATCH_SIZE={BATCH_SIZE}（每 {BATCH_SIZE} 个问题自动换号）")

    current_account = None          # 当前借到的账号 dict
    questions_done_in_batch = 0     # 当前批次已完成的问题数
    current_profile_dir = None      # 当前账号用的浏览器 profile 目录

    def _try_acquire_account():
        """尝试借一个账号，同时打开对应的浏览器 profile。"""
        nonlocal current_account, current_profile_dir, questions_done_in_batch
        while True:
            account = acquire_account(platform_code, worker_id)
            if account is not None:
                current_account = account
                account_id = account["id"]
                questions_done_in_batch = 0
                # 构造这个账号专属的 profile 目录（按 account_id 隔离）
                current_profile_dir = os.path.join(
                    worker_context.get("profile_base_dir", "worker_profiles"),
                    f"{platform_code}_account_{account_id}"
                )
                os.makedirs(current_profile_dir, exist_ok=True)
                log(f"成功借到账号: id={account_id}, name={account.get('accountName', '')}, profile={current_profile_dir}")

                # ===== 诊断：看看后端到底有没有下发 storage_state =====
                _diag_state = account.get("cookie")
                if _diag_state:
                    _diag_cookie_count = _diag_state.count('"name"') if isinstance(_diag_state, str) else "?"
                    _diag_len = len(str(_diag_state))
                    log(f"[诊断] 后端 storage_state 长度={_diag_len}, 约 {_diag_cookie_count} 条 cookie, 前80字符={str(_diag_state)[:80]}")
                else:
                    log(f"[诊断] 后端 cookie 为 {repr(_diag_state)}（空/None）—— 该账号还没上传过登录态")

                # ===== 跨机器登录态复用：后端下发的 storage_state 无条件写本地 state.json =====
                # 后端存的是 Playwright storage_state 格式（cookie + localStorage + sessionStorage），
                # 直接写 state.json，doubao.py new_context(storage_state=state.json) 自动全注入。
                remote_state_str = account.get("cookie") or ""
                if remote_state_str:
                    local_state_path = os.path.join(current_profile_dir, STATE_FILE_NAME)
                    try:
                        state_obj = json.loads(remote_state_str) if isinstance(remote_state_str, str) else remote_state_str
                        with open(local_state_path, "w", encoding="utf-8") as f:
                            json.dump(state_obj, f, ensure_ascii=False, indent=2)
                        _cookie_n = len(state_obj.get("cookies", []))
                        _ls_n = sum(len(o.get("localStorage", [])) for o in state_obj.get("origins", []))
                        log(f"已从后端拉取账号 {account_id} 的 state（{_cookie_n} cookies + {_ls_n} localStorage），写入本地 state.json（免登录！）")
                    except Exception as e:
                        log(f"后端 state 解析失败，回退到手动登录: {e}")

                # 让 worker 打开这个 profile 的浏览器
                if "open_profile" in worker_context:
                    try:
                        worker_context["open_profile"](current_profile_dir, account_id=account_id, worker_id=worker_id)
                        log(f"浏览器已打开账号 {account_id} 的 profile")
                    except Exception as e:
                        log(f"打开浏览器 profile 失败: {e}")
                return True
            log(f"暂时没有可用账号，等待 {ACQUIRE_ACCOUNT_SLEEP} 秒后重试...")
            time.sleep(ACQUIRE_ACCOUNT_SLEEP)

    def _release_current_account(reason="正常用完"):
        """
        释放当前账号。

        reason 到后端 reason 的映射：
          - "worker 手动退出" → MANUAL_EXIT（不冷却）
          - 其他（"正常用完" / "批次完成" / "run_unit 异常换号"）→ BATCH_DONE（冷却）
        """
        nonlocal current_account, current_profile_dir
        if current_account is not None:
            # 把中文 reason 映射成后端能识别的英文 reason
            backend_reason = "MANUAL_EXIT" if reason == "worker 手动退出" else "BATCH_DONE"
            try:
                release_account(current_account["id"], worker_id, reason=backend_reason)
                log(f"已释放账号: id={current_account['id']}, 原因={reason} (后端reason={backend_reason})")
            except Exception as e:
                log(f"释放账号异常（不影响主流程）: {e}")
            current_account = None
            current_profile_dir = None

    # ============ 主循环 ============
    # _current_unit_id 用于 finally 块：如果退出时还有单元在跑，主动归还
    _current_unit_id = None

    try:
        while True:
            # RPA 服务暂时不可达（连接拒绝）：安静退避，不做无意义请求
            if _rpa_is_down():
                remaining = _rpa_down_until - time.time()
                _logger.debug(f"RPA 服务不可达，还需退避 {remaining:.0f}s")
                time.sleep(max(1.0, min(remaining, RPA_DOWN_SLEEP)))
                continue

            # 第一步：确保有账号用（刚启动或上一个用完了）
            if current_account is None:
                if not _try_acquire_account():
                    continue

            # 第二步：认领一个问题单元
            account_id = current_account["id"] if current_account else None
            unit = claim_work(platform_code, worker_id, account_id)
            if unit is None:
                time.sleep(CLAIM_IDLE_SLEEP)
                # 如果认领不到任务，但我们已经跑够了 BATCH_SIZE 个，就释放账号换一个
                if questions_done_in_batch >= BATCH_SIZE:
                    log(f"已跑够 {BATCH_SIZE} 个问题，释放当前账号换号")
                    _release_current_account("批次完成")
                continue

            unit_id = unit.get("id")
            _current_unit_id = unit_id  # 记录当前在跑的单元，退出时归还用
            task_no = unit.get("taskNo")
            log(f"认领单元: id={unit_id}, taskNo={task_no}, question={str(unit.get('questionText'))[:40]}, accountId={account_id}")

            # 心跳线程
            stop_event = threading.Event()
            failed = [0]
            _last_hb_err_log = [0.0]

            def heartbeat_loop():
                while not stop_event.is_set():
                    stop_event.wait(HEARTBEAT_INTERVAL)
                    if stop_event.is_set():
                        break
                    if heartbeat(unit_id, worker_id):
                        failed[0] = 0
                    else:
                        failed[0] += 1
                        now = time.time()
                        if now - _last_hb_err_log[0] >= ERROR_LOG_THROTTLE:
                            _logger.warning(
                                f"心跳失败 {failed[0]} 次，请检查网络/后端状态"
                            )
                            _last_hb_err_log[0] = now

            hb = threading.Thread(target=heartbeat_loop, daemon=True, name="worker-heartbeat")
            hb.start()

            page = worker_context["get_page"]() if "get_page" in worker_context else worker_context.get("page")

            try:
                handle_unit(page, unit)
                questions_done_in_batch += 1
                log(f"批次进度: {questions_done_in_batch}/{BATCH_SIZE}")
            except Exception as e:
                log(f"单元执行异常，归还任务池重试: unit_id={unit_id}, err={e}")
                abort_unit(unit_id, worker_id)
                # 执行失败时也递增计数，但如果连续失败可能会触发账号自动冷却
                questions_done_in_batch += 1

                # 如果遇到验证/风控类异常，主动把账号标记冷却，然后换号
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("验证", "验证码", "captcha", "风控", "verified")):
                    log(f"检测到风控/验证码，主动冷却账号 {account_id} 并换号")
                    if current_account is not None:
                        try:
                            cooldown_account(current_account["id"], worker_id, minutes=10)
                        except Exception:
                            pass
                    questions_done_in_batch = BATCH_SIZE  # 强制换号
            finally:
                stop_event.set()
                hb.join(timeout=2)
                _current_unit_id = None  # 单元处理完了，清掉记录

            # 第三步：检查是否需要换号
            if questions_done_in_batch >= BATCH_SIZE:
                log(f"一个账号已完成 {BATCH_SIZE} 个问题，释放并重新借号")
                _release_current_account("批次完成")
                # 让 worker 关闭当前浏览器
                if "close_browser" in worker_context:
                    try:
                        worker_context["close_browser"]()
                    except Exception as e:
                        log(f"关闭浏览器异常: {e}")
                # 等一下让上一个账号进入冷却，同时给下一个账号准备
                time.sleep(2)

            time.sleep(CLAIM_IDLE_SLEEP)

    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在优雅退出...")
    except Exception as e:
        log(f"主循环异常退出: {e}")
    finally:
        # ===== 优雅清理：无论 Ctrl+C 还是主循环异常，都执行 =====

        # 1. 归还正在跑的单元（如果有）
        if _current_unit_id is not None:
            try:
                abort_unit(_current_unit_id, worker_id)
                log(f"已归还正在跑的单元: {_current_unit_id}")
            except Exception as e:
                log(f"归还单元异常: {e}")

        # 2. 释放账号（手动退出≠账号有问题，只清 worker_id，不标记冷却）
        _release_current_account("worker 手动退出")

        # 3. 关浏览器
        if "close_browser" in worker_context:
            try:
                worker_context["close_browser"]()
                log("浏览器已关闭")
            except Exception as e:
                log(f"关闭浏览器异常: {e}")

        log("Worker 已优雅退出，所有资源已清理")


# ============================================================
#  豆包本地模块（同目录）
# ============================================================
from phone_db import PhoneDB
from vcode_client import poll_vcode, health_check, VcodeTimeoutError
from doubao_login import (
    connect_phone, start_doubao, login_with_phone,
    logout, is_logged_in, force_reset, current_is_doubao, get_device_serial,
)
from doubao_worker import (
    ask_one,
    current_is_doubao as _c_idoubao,
    ensure_input_mode as _ensure_input_mode,
)

# ============================================================
#  PhoneSessionManager —— 本地手机号池（10 题自动登出换号）
#  原 worker_lib 的账号池 acquire/release 保留，
#  豆包 Android 平台用这个本地管理器覆盖。
# ============================================================
class PhoneSessionManager:
    """本地手机号池：次数最少优先。每轮都强制登出再登录。"""

    BATCH_SIZE = 10

    def __init__(self, d):
        self.d = d
        self.db = PhoneDB()
        self._phone: str | None = None
        self._count_in_batch = 0

    def force_relogin(self, reason="进入新一轮") -> str:
        """强制登出 → force_reset → 从手机号池选号 → 登录。
        不管当前状态，每轮都走完整流程。

        优化：先检测主页是否有「立即登录，解锁更多功能」，有则直接点击登录，
        避免不必要的登出+重置流程。
        """
        d = self.d
        log(f"===== 强制登出 + 登录新手机号（原因: {reason}）=====")

        # ---- 新流程：先检测主页「立即登录」字样 ----
        # 如果主页有「立即登录，解锁更多功能」，说明当前未登录，
        # 直接点击去登录，跳过登出+重置流程
        start_doubao(d)
        time.sleep(1.5)
        try:
            xml = d.dump_hierarchy()
        except Exception:
            xml = ""

        if "立即登录" in xml and "解锁更多功能" in xml:
            log("🔔 检测到主页「立即登录，解锁更多功能」，直接点击去登录")
            # 点击主页的「立即登录，解锁更多功能」
            # 用 textContains 匹配（文字含箭头 →，精确匹配容易失败）
            clicked = False
            for text in ("立即登录，解锁更多功能", "立即登录", "解锁更多功能"):
                try:
                    obj = d(textContains=text)
                    if obj.exists(timeout=2):
                        obj.click()
                        clicked = True
                        log(f"✅ 已点击「{text}」")
                        break
                except Exception:
                    pass
            if not clicked:
                # 坐标兜底：「立即登录」在主页顶部偏右
                w, h = d.info["displayWidth"], d.info["displayHeight"]
                d.click(int(w * 0.50), int(h * 0.25))
                log("✅ 已按坐标点击「立即登录」")
            time.sleep(2.0)  # 等登录弹窗加载

            # 点击弹窗中的「手机号登录」
            try:
                obj = d(text="手机号登录")
                if obj.exists(timeout=3):
                    obj.click()
                    log("✅ 已点击「手机号登录」")
                else:
                    log("未检测到「手机号登录」弹窗按钮，可能已直接进入登录页")
            except Exception:
                pass

            phone = self.db.pick()
            if not phone:
                raise RuntimeError("手机号池空！先 python run.py --add 加几个号")
            log(f"选中手机号: {phone}")

            def poller(after_ts=None):
                v, _ = poll_vcode(phone, after_ts=after_ts)
                return v

            login_with_phone(d, phone, vcode=None, poller=poller)
        else:
            # ---- 老流程：登出 → 重置 → 登录 ----
            try: logout(d)
            except Exception as e: log(f"logout 异常（忽略）: {e}")

            try: force_reset(d)
            except Exception as e: log(f"force_reset 异常（忽略）: {e}")

            time.sleep(3)
            start_doubao(d)

            phone = self.db.pick()
            if not phone:
                raise RuntimeError("手机号池空！先 python run.py --add 加几个号")
            log(f"选中手机号: {phone}")

            def poller(after_ts=None):
                v, _ = poll_vcode(phone, after_ts=after_ts)
                return v

            login_with_phone(d, phone, vcode=None, poller=poller)

        # 刚登录进入主页：先检测一次底部输入模式，防止豆包默认停在
        # 「按住说话」语音模式导致后续问题无法输入
        try:
            _ensure_input_mode(d)
        except Exception as e:
            log(f"登录后输入模式检测异常（忽略）: {e}")

        self._phone = phone
        self._count_in_batch = 0
        log(f"===== 手机号 {phone} 登录成功，开始问问题 =====")
        return phone

    def mark_used(self):
        if not self._phone: return
        self.db.increment(self._phone)
        self._count_in_batch += 1
        log(f"手机号 {self._phone} 使用次数 +1（批次 {self._count_in_batch}/{self.BATCH_SIZE}）")

    def should_relogin(self) -> bool:
        return self._count_in_batch >= self.BATCH_SIZE

# ============================================================
#  make_handle_unit —— 闭包：登录态管理 + ask_one + 上传 + callback
# ============================================================
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

        # 登录由 run_doubao_loop 批次控制
        # 执行提问 → 回答 → 截图 → 提取（完整复用 doubao_worker 原脚本逻辑）
        result = ask_one(d, question, tag=f"task_{task_no or unit_id}")

        answer = result.get("answer", "") or ""
        thinking = result.get("thinking", "") or ""
        screenshot_path = result.get("screenshot", "") or ""

        log(f"answer={len(answer)}字符 thinking={len(thinking)}字符 shot={screenshot_path or '(无)'}")
        if not answer or len(answer.strip()) < 10:
            raise RuntimeError("没有提取到有效回答")

        # 上传截图（用原 worker_lib.upload_screenshot）
        image_url = None
        if screenshot_path:
            log("上传截图...")
            image_url = upload_screenshot(screenshot_path, task_id=unit_id)
            if not image_url:
                log("截图上传失败")

        # callback 结果（用原 worker_lib.callback）
        log("回调结果...")
        ok = callback(unit, status="SUCCESS",
                      answer_text=answer, thinking_content=thinking,
                      source_info="", image_url=image_url)
        if not ok:
            raise RuntimeError("callback 失败")

        session_mgr.mark_used()
        log(f"✅ 单元完成: {task_no}")

    return handle_unit


# ============================================================
#  run_doubao_loop —— 豆包特有的主循环（复用原 worker_lib.run_worker_loop
#  的 claim/heartbeat/abort/callback 逻辑，但用 PhoneSessionManager 替代账号池）
# ============================================================
def run_doubao_loop(d, worker_id=None):
    smgr = None
    consecutive_failures = 0
    last_heartbeat = 0.0
    hb_interval = 60
    current_unit_id = None
    worker_id = worker_id or default_worker_id()

    log(f"Doubao Worker START platform={PLATFORM_NAME} workerId={worker_id}")
    log(f"RPA  = {RPA_SERVICE_URL}")
    log(f"BACK = {BACKEND_BASE_URL}")
    log(f"每手机号连跑 {PhoneSessionManager.BATCH_SIZE} 题后换号")

    while smgr is None:
        try:
            smgr = PhoneSessionManager(d)
            smgr.force_relogin(reason="程序启动")
        except Exception as e:
            log(f"启动登录失败，10s 后重试: {e}")
            time.sleep(10)

    handle_unit = make_handle_unit(smgr)

    try:
        while True:
            # RPA 服务暂时不可达（连接拒绝）：安静退避，不做无意义请求
            if _rpa_is_down():
                remaining = _rpa_down_until - time.time()
                _logger.debug(f"RPA 服务不可达，还需退避 {remaining:.0f}s")
                time.sleep(max(1.0, min(remaining, RPA_DOWN_SLEEP)))
                continue

            unit = claim_work(PLATFORM_NAME, worker_id)
            if not unit:
                time.sleep(15)
                continue

            unit_id = unit.get("id")
            current_unit_id = unit_id
            task_no = unit.get("taskNo") or str(unit_id)
            q = (unit.get("questionText") or unit.get("question") or "")[:50]
            log(f"CLAIM id={unit_id} taskNo={task_no} q={q}")

            now = time.time()
            if now - last_heartbeat > hb_interval:
                try:
                    heartbeat(PLATFORM_NAME, str(unit_id))
                    last_heartbeat = now
                except Exception:
                    pass

            try:
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
                try:
                    abort_unit(PLATFORM_NAME, str(unit_id) if unit_id else "unknown", str(e))
                except Exception:
                    pass
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
            try: abort_unit(current_unit_id, worker_id)
            except Exception: pass
        log("Doubao Worker 退出")



# ============================================================
#  命令行 + 主入口
# ============================================================
def _setup_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="豆包 Android Worker（完整内嵌 worker_lib）")
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
    if h: log(f"✅ FastAPI OK: {SERVER_BASE_URL}/health")
    else: log(f"❌ FastAPI 挂了: {SERVER_BASE_URL}"); ok = False

    db = PhoneDB()
    cnt = db.count()
    if cnt == 0:
        log("❌ 手机号池空！python run.py --add 13800138000"); ok = False
    else:
        log(f"✅ 手机号池: {cnt} 个号，下一个 {db.pick()}")

    try:
        log(f"✅ adb 设备: {get_device_serial()}")
    except Exception as e:
        log(f"❌ adb 没连上: {e}"); ok = False

    try:
        r = requests.get(RPA_SERVICE_URL + "/actuator/health", timeout=3)
        if r.ok: log(f"✅ RPA OK")
        else: log(f"⚠️  RPA {r.status_code}（不影响 --once）")
    except Exception:
        log(f"⚠️  RPA 不可达（不影响 --once）")

    return ok


def main():
    args = _setup_args()

    if args.list:   print("\n".join(f"{r['id']:>4} {r['phone']:^14} {r['use_count']:>8}" for r in PhoneDB().list_all())); return
    if args.add:    PhoneDB().add(args.add.strip()); print(f"已添加 {args.add}"); return
    if args.reset:  PhoneDB().reset_all(); print("已清零"); return
    if args.delete:
        ok = PhoneDB().remove(args.delete.strip())
        print(f"已删除 {args.delete}" if ok else f"找不到 {args.delete}")
        return

    if not _check(): log("自检未通过"); sys.exit(1)
    if args.check: log("自检通过"); return

    d = connect_phone()
    start_doubao(d)

    if args.once:
        # once 模式也要先 force_relogin
        smgr = PhoneSessionManager(d)
        smgr.force_relogin(reason="程序启动")
        handle_unit = make_handle_unit(smgr)

        phone = smgr._phone  # 已经 force_relogin 登录好了
        from config import QUESTIONS
        q = QUESTIONS[0] if QUESTIONS else "你好，做个自我介绍"
        log(f"[本地] phone={phone} 问题={q}")
        result = ask_one(d, q, tag="local_test")
        log(f"  answer={len(result.get('answer',''))}字符")
        log(f"  shot={result.get('screenshot','(无)')}")
        return

    log("=" * 60)
    log("豆包 Android Worker 启动（完整 worker_lib 内嵌版）")
    log(f"RPA: {RPA_SERVICE_URL}")
    log(f"回调: {BACKEND_BASE_URL}")
    log(f"验证码: {SERVER_BASE_URL}")
    log(f"批次: 每 {BATCH_SIZE} 题自动登出换手机号")
    log("=" * 60)

    worker_id = f"doubao_app_{socket.gethostname()}"
    try:
        run_doubao_loop(d, worker_id)
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
    finally:
        try: force_reset(d)
        except Exception: pass


if __name__ == "__main__":
    main()
