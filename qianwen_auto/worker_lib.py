# -*- coding: utf-8 -*-
"""
worker_lib.py —— qianwen_auto 的公共「工人」运行时。

从 deepseek_auto/worker_lib.py 直接复制。逻辑与平台无关，无需修改。
  - 任务认领 / 心跳续约 / 异常归还 / 结果回调 / 截图上传
  - 通用主循环 run_worker_loop(platform_code, device, handle_unit)
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import defaultdict

import requests

# 优先从 config 读地址（config 本身支持环境变量覆盖）
try:
    from config import RPA_SERVICE_URL, BACKEND_BASE_URL
except Exception:
    RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://localhost:8084")
    BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")

CLAIM_IDLE_SLEEP = int(os.getenv("CLAIM_IDLE_SLEEP", "5"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))

# 连接拒绝等临时性错误时退避等待的时长（秒）——服务起来前就安静等待
RPA_DOWN_SLEEP = int(os.getenv("RPA_DOWN_SLEEP", "30"))
# 同一种错误（URL + 异常类型）限频打日志的间隔（秒）
ERROR_LOG_THROTTLE = int(os.getenv("ERROR_LOG_THROTTLE", "60"))

_logger = logging.getLogger("worker_lib")


def log(msg):
    _logger.info(str(msg))


# ---------------------------------------------------------------------------
#  错误限频：同一 (URL, exception_type) 在 ERROR_LOG_THROTTLE 秒内只打一条
# ---------------------------------------------------------------------------
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


# 模块级标记：RPA 服务是否暂时不可达。主循环据此决定是 claim 间隔还是退避 sleep
_rpa_down_until = 0.0  # 0 表示正常，>0 表示在此时间戳之前判定为不可达


def _mark_rpa_down(duration: float = RPA_DOWN_SLEEP):
    global _rpa_down_until
    _rpa_down_until = time.time() + duration


def _rpa_is_down() -> bool:
    return _rpa_down_until > time.time()


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


def default_worker_id():
    """默认工人标识 = 机器名。"""
    return socket.gethostname()


# =====================================================================
#  截图上传 / 结果回调
# =====================================================================

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
                return result["data"].get("url") or (
                    result["data"].get("data")
                    if isinstance(result["data"].get("data"), str) else None
                )
            if isinstance(result.get("data"), str):
                return result["data"]
            if result.get("success") is True:
                return result.get("data")
        log(f"上传失败: {resp.text[:300]}")
        return None
    except Exception as e:
        log(f"上传异常: {e}")
        return None


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
#  任务认领 / 心跳 / 归还
# =====================================================================

def claim_work(platform_code, worker_id, account_id=None):
    """认领一个待执行单元。
    返回 dict(id/taskNo/aiPlatform/questionText)；无任务时返回 None。
    """
    payload = {"platform": platform_code, "workerId": worker_id}
    if account_id is not None:
        payload["accountId"] = account_id
    obj = _post(RPA_SERVICE_URL + "/internal/rpa/worker/assign", payload, timeout=10)
    if not obj or obj.get("code") != 200:
        return None
    data = obj.get("data")
    if isinstance(data, dict) and data.get("id") is not None:
        return data
    return None


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


# =====================================================================
#  通用主循环（Android 设备直传版）
# =====================================================================

def run_worker_loop(platform_code, device, handle_unit, worker_id=None):
    """工人主循环：认领 → 心跳 → 执行 → （异常归还）。

    参数：
      platform_code  平台 code（必须等于后端 task_result.ai_platform 存储值）
      device         uiautomator2 设备对象（handle_unit 的第一个参数）
      handle_unit    回调签名 handle_unit(device, unit)，执行一个问题并自行 callback；
                     抛出异常时单元会被归还任务池。
      worker_id      工人标识，默认取机器名

    注意：手机号轮换由 run.py 的 PhoneSessionManager 负责。
    """
    worker_id = worker_id or default_worker_id()
    log(f"Worker 启动: platform={platform_code}, workerId={worker_id}")
    log(f"认领地址: {RPA_SERVICE_URL}   回调地址: {BACKEND_BASE_URL}")

    current_unit_id = None
    # 心跳失败限频：同一条错误警告 30s 内只打一次
    _last_hb_err_log = 0.0

    try:
        while True:
            # RPA 服务暂时不可达（连接拒绝）：安静退避，不做无意义请求
            if _rpa_is_down():
                remaining = _rpa_down_until - time.time()
                _logger.debug(f"RPA 服务不可达，还需退避 {remaining:.0f}s")
                time.sleep(max(1.0, min(remaining, RPA_DOWN_SLEEP)))
                continue

            unit = claim_work(platform_code, worker_id)
            if unit is None:
                time.sleep(CLAIM_IDLE_SLEEP)
                continue

            unit_id = unit.get("id")
            current_unit_id = unit_id
            log(f"认领单元: id={unit_id}, taskNo={unit.get('taskNo')}, "
                f"question={str(unit.get('questionText'))[:40]}")

            # 心跳线程
            stop_event = threading.Event()
            failed = [0]

            def heartbeat_loop():
                nonlocal _last_hb_err_log
                while not stop_event.is_set():
                    stop_event.wait(HEARTBEAT_INTERVAL)
                    if stop_event.is_set():
                        break
                    if heartbeat(unit_id, worker_id):
                        failed[0] = 0
                    else:
                        failed[0] += 1
                        now = time.time()
                        # 心跳失败也限频打，不要每秒刷屏
                        if now - _last_hb_err_log >= ERROR_LOG_THROTTLE:
                            _logger.warning(
                                f"心跳失败 {failed[0]} 次，请检查网络/后端状态"
                            )
                            _last_hb_err_log = now

            hb = threading.Thread(target=heartbeat_loop, daemon=True,
                                  name="worker-heartbeat")
            hb.start()

            try:
                handle_unit(device, unit)
            except Exception as e:
                log(f"单元执行异常，归还任务池重试: unit_id={unit_id}, err={e}")
                abort_unit(unit_id, worker_id)
            finally:
                stop_event.set()
                hb.join(timeout=2)
                current_unit_id = None

            time.sleep(CLAIM_IDLE_SLEEP)

    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在优雅退出...")
    finally:
        if current_unit_id is not None:
            try:
                abort_unit(current_unit_id, worker_id)
                log(f"已归还正在跑的单元: {current_unit_id}")
            except Exception as e:
                log(f"归还单元异常: {e}")
        log("Worker 已退出")
