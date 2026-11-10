# -*- coding: utf-8 -*-
"""
deepseek_worker.py —— v6（deepseek_auto 项目版）
============================================================
由 deepseekapp.py v6 改造接入 deepseek_auto 工程：
  - worker_lib 使用本项目内置的 worker_lib.py（任务池/截图上传/回调）
  - 截图输出目录优先取 config.SCREENSHOT_DIR，与整个工程保持一致

原 v6 说明：
本次新增（相对 v5）：
  1. 【状态检测】新增 is_deep_thinking_enabled()，通过"XML 属性 +
     截图颜色分析"综合判断深度思考按钮当前是否已开启。
  2. 【防误关】重写 enable_deep_thinking()：
       - 点击前先检测，已开启则跳过点击；
       - 点击后再次检测，未成功也不再重试，避免把已开的关掉。
  3. 【稳健性】新增 _find_deep_thinking_button() 统一查找入口，
     优先 resource-id，其次文本/desc（限定屏幕下半区）。

截图模块、正文/思考采集模块保持不变。
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import subprocess
import time
import traceback
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence, Set

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None

from PIL import Image

try:
    import numpy as np
except ImportError:
    np = None


# ============================================================
# worker_lib（可选）
# ============================================================

try:
    from worker_lib import (
        upload_screenshot as worker_upload,
        callback as worker_callback,
        run_worker_loop,
    )
    _HAS_WORKER_LIB = True
except ImportError:
    _HAS_WORKER_LIB = False
    worker_upload = None
    worker_callback = None
    run_worker_loop = None


# ============================================================
# 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# 优先使用工程级 config 的截图目录，保持与 run.py / 登录模块一致
try:
    from config import SCREENSHOT_DIR as OUTPUT_DIR  # type: ignore
except Exception:
    OUTPUT_DIR = BASE_DIR / "screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PLATFORM_NAME = "deepseek_app"
DEEPSEEK_PACKAGE = "com.deepseek.chat"

ANSWER_HARD_TIMEOUT = 600
ANSWER_MIN_WAIT = 6.0
ANSWER_MIN_COMPLETE_LENGTH = 40  # 回答最低有效字符数（deepseek 短回答可能只有 50-60 字，门槛 60 会卡死）
ANSWER_STABLE_SECONDS = 4.0
ANSWER_STABLE_MIN_ROUNDS = 3
POLL_INTERVAL = 1.0
POLL_INTERVAL_SLOW = 2.0

MAX_TOP_SWIPES = 25
MAX_BOTTOM_SWIPES = 25
SCROLL_STABLE_ROUNDS = 3

MAX_CAPTURE_FRAMES = 200
CAPTURE_SWIPE_DURATION = 1.2
CAPTURE_SETTLE_WAIT = 2.0
CAPTURE_FRACTION = 0.42

# ---------- 长截图：右下角「转至底部」悬浮按钮裁切 ----------
# DeepSeek 聊天页右下角有一个圆形「转至底部」悬浮按钮（content-desc 为「转至底部」），
# 只要聊天没有滚到底部，它就固定悬在输入框正上方、消息区右下角。它的存在会带来两个问题：
#   1. 每一屏都会把按钮（以及被按钮盖住的那块内容）印进长图，同一按钮重复出现多次；
#   2. 按钮出现 / 消失会让相邻两帧画面突然变化，拼接时算不准重叠位置，
#      导致整段内容被重复贴两遍（长图末尾内容重复、混乱）。
# doubao_auto 是用「裁掉底部 6% 屏高」解决的；这里优先按 XML 里按钮的真实
# 上沿坐标裁，取不到坐标时再退回按屏高比例裁切。
FLOAT_BTN_FALLBACK_RATIO = 0.07   # 拿不到按钮坐标时的兜底裁切比例（屏高占比）
FLOAT_BTN_MARGIN = 10             # 在按钮上沿之上再留出的安全余量（px）

GENERATING_WORDS = (
    "停止生成", "停止回答", "生成中", "正在生成", "正在回答", "回答中",
    "Stop generating", "Stop response", "Generating response",
)
COMPLETION_WORDS = (
    "复制", "重新生成", "再试一次", "分享", "点赞", "点踩",
    "Copy", "Regenerate", "Share", "Like", "Dislike",
)
INPUT_HINTS = ("发消息", "输入", "Ask anything", "Message", "Send a message")
NEW_CHAT_WORDS = ("新对话", "新聊天", "New chat")
SEND_WORDS = ("发送", "Send")
BOTTOM_WORDS = ("转至底部", "回到底部", "返回底部", "到底部",
                "Jump to bottom", "Scroll to bottom")
THINKING_WORDS = ("深度思考", "DeepThink", "Thinking", "思考过程")

# ---------- 深度思考按钮 / 思考面板 相关常量 ----------

DEEP_THINKING_IDS = (
    "com.deepseek.chat:id/deep_think",
    "com.deepseek.chat:id/deep_thinking",
    "com.deepseek.chat:id/thinking_btn",
    "com.deepseek.chat:id/thinking_button",
    "com.deepseek.chat:id/r1_button",
    "com.deepseek.chat:id/deepthink",
    "com.deepseek.chat:id/btn_deep_think",
)

DEEP_THINKING_WORDS = ("深度思考", "DeepThink")

THINKING_TITLE_PATTERN = re.compile(
    r"已思考|思考中|已深度思考|DeepThink|Thought for|Thinking"
)

THINKING_EXPAND_DESCS = (
    "展开", "展开思考", "展开思考过程",
    "Expand", "Show thinking", "Show more",
)

# 蓝色像素判定阈值（用于判断按钮是否处于激活/高亮状态）
# 蓝色：B 明显大于 R，且 B 也大于 G（排除黄、绿、青误判）
BLUE_R_MIN_DELTA = 25      # B - R >= 25
BLUE_G_MIN_DELTA = 10      # B - G >= 10
BLUE_MIN_VALUE = 110       # B >= 110

# 蓝色像素占比阈值（按钮区域）
BLUE_RATIO_THRESHOLD = 0.01       # >= 1%
# 高饱和像素占比阈值（兜底：非纯蓝但有色）
COLORED_RATIO_THRESHOLD = 0.03    # >= 3%
# 饱和度阈值（max - min）
SATURATION_THRESHOLD = 45

IGNORE_EXACT = {
    "DeepSeek", "DeepSeek AI", "新对话", "新聊天", "New chat",
    "分享", "Share", "复制", "Copy", "重新生成", "Regenerate",
    "点赞", "点踩", "Like", "Dislike", "发送", "Send",
    "来源", "Sources", "关闭", "取消", "返回", "退出",
    "深度思考", "智能搜索", "联网搜索", "快速模式", "专家模式",
    "搜索结果", "回到顶部", "回到底部",
}

IGNORE_SUBSTR = (
    "回答由 AI 生成", "仅供参考", "内容由 AI 生成",
    "以上内容由 AI 生成",
)

KNOWN_INPUT_IDS = (
    "com.deepseek.chat:id/input",
    "com.deepseek.chat:id/input_text",
    "com.deepseek.chat:id/et_input",
    "com.deepseek.chat:id/edit_text",
    "com.deepseek.chat:id/message_input",
    "com.deepseek.chat:id/chat_input",
)
KNOWN_SEND_IDS = (
    "com.deepseek.chat:id/send",
    "com.deepseek.chat:id/send_button",
    "com.deepseek.chat:id/input_send",
    "com.deepseek.chat:id/action_send",
)
KNOWN_NEW_CHAT_IDS = (
    "com.deepseek.chat:id/new_chat",
    "com.deepseek.chat:id/create_new_chat",
    "com.deepseek.chat:id/new_conversation",
)


# ============================================================
# 日志
# ============================================================

LOG = logging.getLogger("deepseek_mobile")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log(msg: str) -> None:
    LOG.info(msg)


# ============================================================
# ADB
# ============================================================

def run_adb(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """调用 adb 命令。如果已知设备序列号且命令不是 devices/connect，自动加 -s。"""
    # 某些 adb 命令（如 devices/connect）不需要 -s，加了反而可能出错
    no_device_cmds = {"devices", "connect", "disconnect", "start-server", "kill-server"}
    first_arg = str(args[0]) if args else ""

    if first_arg not in no_device_cmds and _device_serial:
        cmd = ["adb", "-s", _device_serial, *map(str, args)]
    else:
        cmd = ["adb", *map(str, args)]

    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


_device_serial: str | None = None  # 进程级缓存，只取一次


def get_device_serial() -> str:
    """获取设备序列号。
    
    支持两种模拟器格式：
      - 雷电/MuMu/MEmu（TCP 直连）：serial = 127.0.0.1:PORT
      - Google AVD / 夜神：serial = emulator-PORT
    配置端口后自动匹配 adb devices 里对应的 serial，找不到回退到自动取第一台。
    """
    global _device_serial
    if _device_serial:
        return _device_serial

    # 从 config 读取端口（config.py 已处理 os.getenv 环境变量覆盖）
    port = None
    try:
        from config import EMULATOR_ADB_PORT
        port = EMULATOR_ADB_PORT
    except Exception:
        pass

    # 先尝试 TCP 连接（雷电/MuMu 等模拟器需要 adb connect）
    if port and port > 0:
        try:
            run_adb("connect", f"127.0.0.1:{port}", timeout=15)
        except Exception:
            pass

    # 查所有在线设备
    result = run_adb("devices", timeout=15)
    out = result.stdout.decode("utf-8", errors="ignore")
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if not devices:
        raise RuntimeError("没有检测到 adb 设备。请确认雷电模拟器已启动并 adb devices。")

    # 优先匹配配置的端口（"5556" in serial 同时命中 emulator-5556 和 127.0.0.1:5556）
    if port and port > 0:
        target = str(port)
        for serial in devices:
            if target in serial:
                log(f"匹配到端口 {port} 的设备: {serial}")
                _device_serial = serial
                return serial
        log(f"没找到端口 {port} 对应的设备，回退自动取第一台")

    if len(devices) > 1:
        log(f"检测到多个 adb 设备，使用第一个：{devices[0]}")
    serial = devices[0]
    _device_serial = serial
    return serial


def adb_screenshot_bytes() -> bytes:
    # 确保设备序列号已获取（run.py 可能没调用 connect_device）
    if not _device_serial:
        get_device_serial()
    result = run_adb("exec-out", "screencap", "-p", timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            "adb screencap 失败：" + result.stderr.decode("utf-8", errors="ignore")
        )
    return result.stdout


def save_screen(path: Path) -> Image.Image:
    data = adb_screenshot_bytes()
    path.write_bytes(data)
    return Image.open(BytesIO(data)).convert("RGB")


# ============================================================
# UIAutomator
# ============================================================

def connect_device():
    if u2 is None:
        raise RuntimeError("缺少 uiautomator2：pip install -U uiautomator2")
    serial = get_device_serial()
    log(f"连接 UIAutomator2：{serial}")
    d = u2.connect(serial)
    try:
        info = d.info
        log("设备：%s %sx%s" % (
            info.get("productName") or info.get("brand") or "",
            info.get("displayWidth"), info.get("displayHeight"),
        ))
    except Exception as exc:
        raise RuntimeError(f"uiautomator2 已连接但读设备信息失败：{exc}")
    return d


def dump_xml(d, path: Optional[Path] = None) -> str:
    xml = d.dump_hierarchy(compressed=False)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(xml, encoding="utf-8")
        except Exception as exc:
            LOG.debug("写 xml 失败 %s: %s", path, exc)
    return xml


def clean_text(text) -> str:
    if not text:
        return ""
    text = str(text).replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_nodes(xml: str) -> list:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    result = []
    for node in root.iter("node"):
        result.append({
            "text": clean_text(node.attrib.get("text", "")),
            "desc": clean_text(node.attrib.get("content-desc", "")),
            "rid": node.attrib.get("resource-id", ""),
            "class": node.attrib.get("class", ""),
            "bounds": node.attrib.get("bounds", ""),
            "clickable": node.attrib.get("clickable") == "true",
            "enabled": node.attrib.get("enabled", "true") != "false",
            "visible": node.attrib.get("visible-to-user", "true") != "false",
            "scrollable": node.attrib.get("scrollable") == "true",
            "focusable": node.attrib.get("focusable") == "true",
            "selected": node.attrib.get("selected") == "true",
            "checked": node.attrib.get("checked") == "true",
            "activated": node.attrib.get("activated") == "true",
        })
    return result


def get_screen_size(d) -> tuple:
    try:
        info = d.info
        return int(info["displayWidth"]), int(info["displayHeight"])
    except Exception:
        result = run_adb("shell", "wm", "size", timeout=15)
        text = result.stdout.decode("utf-8", errors="ignore")
        match = re.search(r"(\d+)\s*x\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1080, 1920


def bounds_to_tuple(bounds: str):
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    return tuple(map(int, match.groups()))


def click_node(d, node: dict) -> bool:
    b = bounds_to_tuple(node.get("bounds", ""))
    if not b:
        return False
    x1, y1, x2, y2 = b
    try:
        d.click((x1 + x2) // 2, (y1 + y2) // 2)
        return True
    except Exception:
        return False


def click_first_by_ids(d, ids: Sequence[str]) -> bool:
    for rid in ids:
        try:
            obj = d(resourceId=rid)
            if obj.exists(timeout=0.5):
                obj.click()
                return True
        except Exception:
            continue
    return False


def click_first_by_words(d, words: Sequence[str], *, require_clickable: bool = True) -> bool:
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    candidates = []
    for node in nodes:
        value = node["text"] or node["desc"]
        if not value:
            continue
        if not any(w.lower() in value.lower() for w in words):
            continue
        if require_clickable and not node["clickable"]:
            continue
        if not node["enabled"] or not node["visible"]:
            continue
        b = bounds_to_tuple(node["bounds"])
        area = (b[2] - b[0]) * (b[3] - b[1]) if b else 10 ** 12
        candidates.append((area, node))
    candidates.sort(key=lambda x: x[0])
    for _, node in candidates:
        if click_node(d, node):
            return True
    return False


# ============================================================
# App 生命周期
# ============================================================

def start_deepseek(d) -> None:
    log("启动 DeepSeek Android App")
    d.app_start(DEEPSEEK_PACKAGE, stop=False)
    time.sleep(3)
    try:
        if d.app_current().get("package") != DEEPSEEK_PACKAGE:
            d.app_start(DEEPSEEK_PACKAGE, stop=False)
            time.sleep(3)
        if d.app_current().get("package") != DEEPSEEK_PACKAGE:
            raise RuntimeError("DeepSeek 没有进入前台")
    except Exception as exc:
        raise RuntimeError(f"无法确认 DeepSeek 前台状态：{exc}")


# ============================================================
# 新对话 / 输入 / 发送
# ============================================================

def new_chat(d) -> bool:
    log("创建新对话")
    if click_first_by_ids(d, KNOWN_NEW_CHAT_IDS):
        time.sleep(1.5); return True
    for selector in ({"text": "新对话"}, {"text": "新聊天"}, {"text": "New chat"}):
        try:
            obj = d(**selector)
            if obj.exists(timeout=1):
                obj.click(); time.sleep(1.5); return True
        except Exception:
            pass
    if click_first_by_words(d, NEW_CHAT_WORDS):
        time.sleep(1.5); return True
    try:
        for word in NEW_CHAT_WORDS:
            obj = d(descriptionContains=word)
            if obj.exists(timeout=1):
                obj.click(); time.sleep(1.5); return True
    except Exception:
        pass
    try:
        d.press("back"); time.sleep(1)
        if click_first_by_words(d, NEW_CHAT_WORDS):
            time.sleep(1.5); return True
    except Exception:
        pass
    return False


def find_input(d):
    for rid in KNOWN_INPUT_IDS:
        try:
            obj = d(resourceId=rid)
            if obj.exists(timeout=0.5):
                return obj
        except Exception:
            pass
    for kwargs in (
        {"className": "android.widget.EditText"},
        {"textContains": "发消息"},
        {"descriptionContains": "发消息"},
        {"textContains": "Ask anything"},
        {"textContains": "Message"},
    ):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=0.5):
                return obj
        except Exception:
            pass
    return None


def input_text(d, question: str) -> None:
    edit = find_input(d)
    if edit is None:
        raise RuntimeError("找不到 DeepSeek 输入框")
    log(f"输入问题：{question[:100]}")
    try:
        edit.click()
    except Exception:
        pass
    try:
        edit.set_text(str(question))
    except Exception as first_exc:
        log(f"set_text 失败，尝试 fast input：{first_exc}")
        try:
            d.set_fastinput_ime(True)
            d.send_keys(str(question), clear=True)
        finally:
            try:
                d.set_fastinput_ime(False)
            except Exception:
                pass
    time.sleep(0.8)


def send_message(d) -> bool:
    log("发送问题")
    if click_first_by_ids(d, KNOWN_SEND_IDS):
        time.sleep(1); return True
    for word in SEND_WORDS:
        try:
            obj = d(text=word)
            if obj.exists(timeout=0.5):
                obj.click(); time.sleep(1); return True
        except Exception:
            pass
        try:
            obj = d(description=word)
            if obj.exists(timeout=0.5):
                obj.click(); time.sleep(1); return True
        except Exception:
            pass
    if click_first_by_words(d, SEND_WORDS):
        time.sleep(1); return True
    try:
        nodes = parse_nodes(dump_xml(d))
        candidates = []
        for node in nodes:
            rid = node["rid"].lower()
            value = (node["text"] or node["desc"]).lower()
            if not node["clickable"] or not node["enabled"] or not node["visible"]:
                continue
            if "send" in rid or value in {x.lower() for x in SEND_WORDS}:
                b = bounds_to_tuple(node["bounds"])
                if b:
                    area = (b[2] - b[0]) * (b[3] - b[1])
                    candidates.append((area, node))
        for _, node in sorted(candidates, key=lambda x: x[0]):
            if click_node(d, node):
                time.sleep(1); return True
    except Exception:
        pass
    try:
        d.press("enter"); time.sleep(1)
        log("使用 Enter 兜底发送")
        return True
    except Exception:
        return False


# ============================================================
# 【v6 重写】深度思考按钮：查找 / 状态检测 / 开启
# ============================================================

def _find_deep_thinking_button(d) -> Optional[dict]:
    """
    查找"深度思考"按钮节点（限定在屏幕下半部，避免命中历史消息里的同名文字）。

    返回：节点 dict（含 bounds / rid / text / desc 等），或 None
    """
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("查找深度思考按钮失败：%s", exc)
        return None

    # ---------- 策略 1：resource-id 精确匹配 ----------
    # 把已知 id 都转成"后缀"形式，容忍 App 换包名前缀
    known_suffixes = [rid.split("/")[-1] for rid in DEEP_THINKING_IDS]
    rid_hits = []
    for n in nodes:
        rid = n["rid"]
        if not rid:
            continue
        if rid in DEEP_THINKING_IDS or rid.split("/")[-1] in known_suffixes:
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            y_ratio = b[1] / float(h)
            if y_ratio < 0.55 or y_ratio > 0.98:
                continue
            rid_hits.append((b[1], n))
    if rid_hits:
        # 取最靠下的一个（输入区内的按钮）
        rid_hits.sort(key=lambda x: x[0], reverse=True)
        return rid_hits[0][1]

    # ---------- 策略 2：文本 / content-desc ----------
    candidates = []
    for n in nodes:
        text = (n["text"] or "").strip()
        desc = (n["desc"] or "").strip()
        combined = (text + " " + desc).strip()
        if not combined:
            continue

        # 文本必须"近似等于"深度思考（避免命中长句）
        matched = False
        for word in DEEP_THINKING_WORDS:
            if word in combined and len(combined) <= len(word) + 12:
                matched = True
                break
        if not matched:
            continue

        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue

        y_ratio = b[1] / float(h)
        if y_ratio < 0.55 or y_ratio > 0.98:
            continue

        # 排序权重：clickable 优先，然后 y 越大越优先（更靠近输入框）
        clickable_score = 0 if n["clickable"] else 1
        candidates.append((clickable_score, -b[1], n))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def _region_has_blue_tint(
    d,
    bounds: tuple,
    expand: int = 6,
    blue_ratio_threshold: float = BLUE_RATIO_THRESHOLD,
    colored_ratio_threshold: float = COLORED_RATIO_THRESHOLD,
) -> bool:
    """
    通过截图分析指定区域是否有"蓝色调"（即按钮处于激活状态）。

    DeepSeek 深度思考按钮：
      - 未开启：灰色（R≈G≈B，无彩色）
      - 已开启：蓝色边框 + 蓝色文字（B 明显大于 R）

    参数：
      expand：以中心为准向外扩张像素，覆盖按钮边缘/边框。
              深度思考文字与相邻的"智能搜索"按钮有间距，expand 不宜过大。

    返回：True 表示检测到蓝色/彩色（已开启），False 表示灰色（未开启）
    """
    try:
        data = adb_screenshot_bytes()
        img = Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        LOG.debug("截图失败：%s", exc)
        return False

    x1, y1, x2, y2 = bounds
    x1 = max(0, x1 - expand)
    y1 = max(0, y1 - expand)
    x2 = min(img.width, x2 + expand)
    y2 = min(img.height, y2 + expand)
    if x2 <= x1 or y2 <= y1:
        return False

    crop = img.crop((x1, y1, x2, y2))

    # ---------- numpy 加速路径 ----------
    if np is not None:
        arr = np.asarray(crop, dtype=np.int16)
        if arr.size == 0:
            return False
        r = arr[:, :, 0]
        g = arr[:, :, 1]
        b = arr[:, :, 2]

        blue_mask = (
            (b - r >= BLUE_R_MIN_DELTA)
            & (b - g >= BLUE_G_MIN_DELTA)
            & (b >= BLUE_MIN_VALUE)
        )
        blue_ratio = float(blue_mask.mean())

        maxc = np.maximum(np.maximum(r, g), b)
        minc = np.minimum(np.minimum(r, g), b)
        sat = maxc - minc
        colored_ratio = float((sat > SATURATION_THRESHOLD).mean())

        LOG.debug(
            "按钮颜色分析 [%d,%d,%d,%d]: blue_ratio=%.4f, colored_ratio=%.4f",
            x1, y1, x2, y2, blue_ratio, colored_ratio,
        )
        return (
            blue_ratio >= blue_ratio_threshold
            or colored_ratio >= colored_ratio_threshold
        )

    # ---------- PIL 兜底路径 ----------
    w, h = crop.size
    total = 0
    blue_count = 0
    colored_count = 0
    # 隔点采样，兼顾速度与精度
    step = 2
    for yy in range(0, h, step):
        for xx in range(0, w, step):
            try:
                r, g, b = crop.getpixel((xx, yy))
            except Exception:
                continue
            total += 1
            if (b - r >= BLUE_R_MIN_DELTA and b - g >= BLUE_G_MIN_DELTA
                    and b >= BLUE_MIN_VALUE):
                blue_count += 1
            if (max(r, g, b) - min(r, g, b)) > SATURATION_THRESHOLD:
                colored_count += 1
    if total == 0:
        return False
    blue_ratio = blue_count / total
    colored_ratio = colored_count / total
    LOG.debug(
        "按钮颜色分析(PIL) [%d,%d,%d,%d]: blue_ratio=%.4f, colored_ratio=%.4f",
        x1, y1, x2, y2, blue_ratio, colored_ratio,
    )
    return (
        blue_ratio >= blue_ratio_threshold
        or colored_ratio >= colored_ratio_threshold
    )


def is_deep_thinking_enabled(d, node: dict) -> Optional[bool]:
    """
    判断深度思考按钮当前是否已开启。

    综合两种信号：
      ① XML 属性：selected / checked / activated 任一为 true → 已开启
      ② 截图颜色：按钮区域有蓝色调（彩色）→ 已开启

    返回：True 表示已开启，False 表示未开启，None 表示无法判定（截图失败等）
    """
    # ---------- ① XML 属性快速路径 ----------
    if node.get("selected") or node.get("checked") or node.get("activated"):
        LOG.debug("深度思考状态：XML 属性命中（已开启）")
        return True

    # ---------- ② 像素颜色分析 ----------
    b = bounds_to_tuple(node.get("bounds", ""))
    if not b:
        return None

    try:
        return _region_has_blue_tint(d, b, expand=6)
    except Exception as exc:
        LOG.debug("深度思考颜色检测异常（无法判定）：%s", exc)
        return None


def enable_deep_thinking(d) -> bool:
    """
    检查并按需开启 DeepSeek 深度思考模式。

    核心防呆逻辑（v6 关键）：
      1) 找到按钮节点；
      2) 点击前检测状态：
           - 已开启  → 跳过点击，直接返回 True
           - 未开启  → 点击
           - 无法判定 → 保守不点击（避免误关），返回 True
      3) 点击后再次检测：
           - 已开启  → 返回 True
           - 未开启  → 不再重试（避免把已开的又关掉），保守返回 True

    返回：True 表示最终处于"已开启"状态（或点击动作已执行）
          False 表示找不到按钮
    """
    log("→ 检查 / 开启深度思考模式")

    button = _find_deep_thinking_button(d)
    if not button:
        log("⚠ 未找到深度思考按钮，跳过（不影响主流程）")
        return False

    preview = (button["text"] or button["desc"])[:24]
    LOG.debug("深度思考按钮节点：text=%r, desc=%r, rid=%r, bounds=%r",
              button["text"], button["desc"], button["rid"], button["bounds"])

    # ---------- 点击前状态检测 ----------
    try:
        already_on = is_deep_thinking_enabled(d, button)
    except Exception as exc:
        LOG.debug("状态检测异常，按无法判定处理：%s", exc)
        already_on = None

    if already_on is True:
        log(f"✓ 深度思考已处于开启状态（{preview}），跳过点击")
        return True

    if already_on is None:
        log(f"⚠ 深度思考状态无法判定（{preview}），保守不点击（避免误关）")
        return True

    log(f"深度思考当前为未开启，点击按钮：{preview}")

    # ---------- 点击 ----------
    b = bounds_to_tuple(button["bounds"])
    if not b:
        log("⚠ 按钮 bounds 无效，无法点击")
        return False
    cx = (b[0] + b[2]) // 2
    cy = (b[1] + b[3]) // 2
    try:
        d.click(cx, cy)
        time.sleep(1.2)
    except Exception as exc:
        log(f"⚠ 点击深度思考按钮失败：{exc}")
        return False

    # ---------- 点击后二次校验 ----------
    button_after = _find_deep_thinking_button(d)
    if button_after is None:
        # 找不到节点了（可能界面刷新），保守认为已开启
        log("✓ 点击完成，但未能再次找到按钮节点（保守认为已开启）")
        return True

    try:
        now_on = is_deep_thinking_enabled(d, button_after)
    except Exception:
        now_on = None

    if now_on is True:
        log("✓ 深度思考模式已成功开启")
        return True

    if now_on is None:
        log("⚠ 点击后状态仍无法判定，保守认为已开启（避免误关）")
        return True

    log("⚠ 点击后未检测到开启状态，但不再重试（防止误关）")
    return True


# ============================================================
# Composer 状态 / 回答完成检测
# ============================================================

def inspect_composer_state(d) -> dict:
    result = {
        "ready": False, "score": 0,
        "input_found": False, "input_enabled": False,
        "input_bottom": False, "hint_found": False,
        "voice_or_plus": False, "stop_control": False,
        "send_control": False, "input_text": "",
    }
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return result

    for n in nodes:
        value = clean_text(n["text"] or n["desc"])
        low = value.lower()
        rid = n["rid"].lower()
        cls = n["class"].lower()
        b = bounds_to_tuple(n["bounds"])

        hint_hit = any(h.lower() in low for h in INPUT_HINTS)
        is_edit = "edittext" in cls
        rid_input = any(k in rid for k in ("input", "message", "composer", "prompt"))
        if is_edit or hint_hit or rid_input:
            if b and b[3] >= int(h * 0.70):
                result["input_found"] = True
                result["input_bottom"] = True
                result["input_enabled"] = result["input_enabled"] or n["enabled"]
                if hint_hit:
                    result["hint_found"] = True
                if value:
                    result["input_text"] = value

        if any(
            word.lower() in low
            for word in ("停止生成", "停止回答", "stop generating", "stop response")
        ):
            if n["enabled"] and (n["clickable"] or b):
                result["stop_control"] = True

        if b and b[1] >= int(h * 0.78):
            if (value in {"+", "添加", "语音", "按住说话", "voice", "Voice"}
                    or "voice" in low or "record" in low or "microphone" in low):
                result["voice_or_plus"] = True

        if value in SEND_WORDS or "send" in rid:
            if b and b[1] >= int(h * 0.70):
                result["send_control"] = True

    if result["input_found"]: result["score"] += 2
    if result["input_bottom"]: result["score"] += 1
    if result["input_enabled"]: result["score"] += 2
    if result["hint_found"]: result["score"] += 2
    if result["voice_or_plus"]: result["score"] += 1
    if not result["stop_control"]: result["score"] += 1

    result["ready"] = (
        result["input_found"] and result["input_bottom"]
        and result["input_enabled"] and not result["stop_control"]
        and result["score"] >= 6
    )
    return result


def xml_contains_generating(xml: str) -> bool:
    try:
        nodes = parse_nodes(xml)
    except Exception:
        return False
    for node in nodes:
        value = clean_text(node["text"] or node["desc"])
        low = value.lower()
        if not value:
            continue
        if any(w.lower() in low for w in GENERATING_WORDS):
            if node["enabled"] and (node["clickable"] or "stop" in node["rid"].lower()):
                return True
    return False


def _extract_textview_texts(xml: str) -> list:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter("node"):
        cls = node.attrib.get("class", "")
        if "TextView" not in cls:
            continue
        text = clean_text(node.attrib.get("text", ""))
        if text:
            out.append(text)
    return out


def best_answer_text(xml: str) -> str:
    """粗略取最长一段，用于完成判定。"""
    if not xml:
        return ""
    all_texts = _extract_textview_texts(xml)
    if not all_texts:
        return ""
    valid = []
    for t in all_texts:
        if t in IGNORE_EXACT:
            continue
        if any(sub in t for sub in IGNORE_SUBSTR):
            continue
        if len(t) < 6:
            continue
        valid.append(t)
    if not valid:
        return ""
    return max(valid, key=len)


def _screen_message_signature(d) -> bytes:
    try:
        data = adb_screenshot_bytes()
        img = Image.open(BytesIO(data)).convert("L")
        w, h = img.size
        crop = img.crop((int(w * 0.10), int(h * 0.13), int(w * 0.92), int(h * 0.80)))
        crop.thumbnail((240, 420))
        if np is not None:
            arr = np.asarray(crop, dtype=np.uint8)
            arr = (arr // 16).astype(np.uint8)
            return arr.tobytes()
        return crop.tobytes()
    except Exception:
        return b""


def _answer_state_snapshot(d, xml: str = "") -> dict:
    if not xml:
        try:
            xml = dump_xml(d)
        except Exception:
            xml = ""
    answer = best_answer_text(xml) if xml else ""
    composer = inspect_composer_state(d)
    return {
        "xml": xml,
        "answer": answer,
        "answer_len": len(clean_text(answer)),
        "generating": xml_contains_generating(xml) if xml else False,
        "composer": composer,
        "screen_sig": _screen_message_signature(d),
    }


def _save_answer_debug(d, reason: str) -> None:
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dump_xml(d, OUTPUT_DIR / f"answer_state_{stamp}_{reason}.xml")
        save_screen(OUTPUT_DIR / f"answer_state_{stamp}_{reason}.png")
    except Exception:
        pass


def wait_answer_finish(d) -> str:
    log("等待 DeepSeek 回答完成（三步硬校验）")
    start = time.time()
    deadline = start + ANSWER_HARD_TIMEOUT

    last_answer = ""
    last_growth_time = time.time()
    stable_rounds = 0
    saw_answer = False
    last_log = 0.0
    last_snapshot = 0.0
    last_state = None

    log(f"发送后强制等待 {ANSWER_MIN_WAIT:.0f} 秒……")
    time.sleep(ANSWER_MIN_WAIT)

    while time.time() < deadline:
        try:
            if time.time() - last_snapshot < 1.5:
                time.sleep(POLL_INTERVAL)
                continue

            last_snapshot = time.time()
            state = _answer_state_snapshot(d)
            last_state = state

            answer = clean_text(state["answer"])
            answer_len = len(answer)
            composer = state["composer"]

            if answer and answer != last_answer:
                last_growth_time = time.time()
                stable_rounds = 0
                last_answer = answer
            else:
                if answer:
                    stable_rounds += 1

            if answer_len >= ANSWER_MIN_COMPLETE_LENGTH:
                saw_answer = True

            now = time.time()
            no_growth = now - last_growth_time

            if now - last_log >= 2.0:
                log(
                    f"回答状态：len={answer_len}, stable={stable_rounds}, "
                    f"no_growth={no_growth:.1f}s, composer={composer['ready']}"
                    f"(score={composer['score']}), stop={composer['stop_control']}, "
                    f"generating={state['generating']}"
                )
                last_log = now

            if (saw_answer and not composer["stop_control"]
                    and not state["generating"] and composer["ready"]
                    and no_growth >= ANSWER_STABLE_SECONDS
                    and stable_rounds >= ANSWER_STABLE_MIN_ROUNDS):
                log(f"✓ 回答完成确认：len={answer_len}, no_growth={no_growth:.1f}s")
                _save_answer_debug(d, "completed_v6")
                return answer

            time.sleep(POLL_INTERVAL)
        except Exception as exc:
            LOG.debug("回答状态检测异常：%s", exc)
            time.sleep(POLL_INTERVAL_SLOW)

    log("达到回答主等待时间，进入超时兜底。")
    if last_state is not None:
        answer = clean_text(last_state["answer"])
        if len(answer) >= ANSWER_MIN_COMPLETE_LENGTH and not last_state["composer"]["stop_control"]:
            log(f"✓ 超时兜底接受回答：len={len(answer)}")
            _save_answer_debug(d, "completed_timeout_v6")
            return answer
    if saw_answer and len(last_answer) >= ANSWER_MIN_COMPLETE_LENGTH:
        log(f"✓ 超时兜底接受 last_answer：len={len(last_answer)}")
        return last_answer
    raise RuntimeError(
        f"等待回答超时：最长 {len(last_answer)} 字符，"
        f"未达 {ANSWER_MIN_COMPLETE_LENGTH} 门槛"
    )


# ============================================================
# 消息视窗 / 截图
# ============================================================

def get_message_viewport(d) -> tuple:
    w, h = get_screen_size(d)
    left, top = 0, int(h * 0.07)
    right, bottom = w, int(h * 0.78)

    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return (left, top, right, bottom)

    for node in nodes:
        if node["scrollable"]:
            b = bounds_to_tuple(node["bounds"])
            if not b:
                continue
            x1, y1, x2, y2 = b
            area = (x2 - x1) * (y2 - y1)
            if area >= w * h * 0.25:
                left = max(left, x1)
                top = max(top, y1)
                right = min(right, x2)
                bottom = min(bottom, y2)

    candidate_tops = []
    for node in nodes:
        cls = node["class"]
        value = (node["text"] or node["desc"]) or ""
        b = bounds_to_tuple(node["bounds"])
        if not b:
            continue
        if b[3] < h * 0.55:
            continue
        is_edit = "EditText" in cls
        is_hint = any(hint.lower() in value.lower() for hint in INPUT_HINTS)
        is_composer_btn = value in {"深度思考", "智能搜索", "联网搜索",
                                     "DeepThink", "Web Search"}
        if is_edit or is_hint or is_composer_btn:
            candidate_tops.append(b[1])

    if candidate_tops:
        input_top = min(candidate_tops)
        bottom = min(bottom, input_top - 5)

    bottom = max(bottom, top + 100)
    return (int(left), int(top), int(right), int(bottom))


def _float_button_top(d) -> Optional[int]:
    """返回右下角「转至底部」悬浮按钮的上沿 y 坐标；设备上不存在时返回 None。

    该按钮是 App 的悬浮控件（content-desc 形如「转至底部」），固定悬在输入框
    正上方、消息区右下角：只要聊天没滚到底部它就一直在。长截图必须把它裁掉，
    否则每一屏都会拍到按钮，拼接后长图里会重复出现同一个按钮，还可能因为
    按钮遮挡 / 出现消失导致相邻帧错位、内容重复。

    注意：XML 里带 content-desc 的节点只是按钮内部的小箭头图标，真正的圆形
    按钮是它的外层容器（比图标大一圈）。所以这里再回查一层「恰好包住图标」
    的父容器，用容器上沿作为裁切基准，保证整个按钮都不入画。
    """
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    w, h = get_screen_size(d)

    icons = []
    for n in nodes:
        value = (n["text"] or n["desc"] or "").strip()
        if not value or not any(word in value for word in BOTTOM_WORDS):
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        # 只认屏幕右下角的悬浮按钮，避免命中答案正文里出现的「底部」字样
        if b[0] < w * 0.60 or b[1] < h * 0.50:
            continue
        icons.append(b)

    if not icons:
        return None

    best_top = None
    for ib in icons:
        iw = max(1, ib[2] - ib[0])
        ih = max(1, ib[3] - ib[1])
        top = ib[1]
        # 回查恰好包住图标、且与图标同量级（不超过 3 倍）的外层容器
        for n in nodes:
            ob = bounds_to_tuple(n["bounds"])
            if not ob:
                continue
            ow = ob[2] - ob[0]
            oh = ob[3] - ob[1]
            if not (ob[0] <= ib[0] and ob[1] <= ib[1]
                    and ob[2] >= ib[2] and ob[3] >= ib[3]):
                continue
            if ow > iw * 3 or oh > ih * 3:
                continue
            if ow * oh <= iw * ih:
                continue
            top = min(top, ob[1])
        if best_top is None or top < best_top:
            best_top = top
    return best_top


def get_capture_viewport(d) -> tuple:
    """截图专用的消息视窗：在 get_message_viewport 的基础上，把右下角
    「转至底部」悬浮按钮整块裁掉，保证每一帧都不会拍到按钮。

    文本采集仍然用 get_message_viewport（完整消息区），只有取图用本函数，
    因此既不会拍到按钮，也不会漏采任何正文。
    """
    viewport = get_message_viewport(d)
    _, h = get_screen_size(d)

    btn_top = _float_button_top(d)
    if btn_top is not None:
        bottom = min(viewport[3], btn_top - FLOAT_BTN_MARGIN)
    else:
        # 兜底：按屏高比例裁掉底部一条（该悬浮按钮本就压在这一条里）
        bottom = viewport[3] - int(h * FLOAT_BTN_FALLBACK_RATIO)

    bottom = max(bottom, viewport[1] + 100)
    return (viewport[0], viewport[1], viewport[2], bottom)


def _capture_tail_frame(d, viewport: tuple) -> Optional[Image.Image]:
    """滚到底部后，按「完整消息视窗」补截一帧尾部画面。

    取图视窗为了避开右下角悬浮按钮裁掉了屏幕底部一条，而回答末尾的
    「N 个网页」来源条 + 复制/点赞工具栏恰好压在这一条里。滚到底部后
    悬浮按钮会自动消失，此时可以安全地按完整视窗补截，既不拍到按钮，
    也不会让长图结尾缺失内容。
    """
    if _float_button_top(d) is not None:
        log("尾帧补截跳过：右下角悬浮按钮仍在屏幕上，保持裁切尾帧")
        return None
    try:
        return save_message_area(d, OUTPUT_DIR / "_tail_frame.png", viewport)
    except Exception as exc:
        LOG.debug("尾帧补截失败：%s", exc)
        return None


def save_message_area(d, path: Path, viewport: Optional[tuple] = None,
                      full: Optional[Image.Image] = None) -> Image.Image:
    """截取消息视窗区域并落盘。

    :param full: 可复用的全屏截图（调用方若已截屏就传入，避免重复 adb 截屏）
    """
    if full is None:
        full = _grab_full_screen()

    if viewport is None:
        viewport = get_message_viewport(d)

    left, top, right, bottom = viewport
    left = max(0, min(left, full.width - 1))
    top = max(0, min(top, full.height - 1))
    right = max(left + 1, min(right, full.width))
    bottom = max(top + 1, min(bottom, full.height))

    crop = full.crop((left, top, right, bottom))
    crop.save(path, format="PNG")
    return crop


def image_signature(img: Image.Image) -> bytes:
    small = img.copy()
    small.thumbnail((240, 600))
    return small.convert("RGB").tobytes()


# 两帧画面「视为相同」的平均像素差阈值（0~255）。
# 取 3.0：滚动 42% 屏宽的内容位移会让大量文字边缘像素变化，均值远超 3；
# 而到底回弹/呼吸灯/抗锯齿抖动只影响零星像素，均值远小于 3。
SIG_SIMILAR_THRESHOLD = 3.0


def _sig_distance(a: bytes, b: bytes) -> float:
    """两个图像签名的平均像素绝对差。numpy 缺失时退化为精确比较。"""
    if len(a) != len(b):
        return float("inf")
    if np is None:
        return 0.0 if a == b else float("inf")
    x = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
    y = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
    return float(np.mean(np.abs(x - y)))


def _sig_similar(a: Optional[bytes], b: Optional[bytes]) -> bool:
    """容差判同：先精确相等快路径，再按平均像素差阈值判断。"""
    if a is None or b is None:
        return False
    if a == b:
        return True
    return _sig_distance(a, b) <= SIG_SIMILAR_THRESHOLD


# ============================================================
#  字体颜色判定：区分「思考过程」与「AI 回答正文」
# ------------------------------------------------------------
#  DeepSeek App 的实际配色（用户确认）：
#    * 思考过程内容 —— 灰色 #7d7f85
#    * AI 回答正文   —— 黑色 #191b23（加粗标题同为黑色）
#
#  uiautomator 的 XML 只给文本和坐标，不携带字体颜色，
#  因此只能在文本节点的矩形范围内直接采样截图像素来判定：
#    区域内绝大多数像素是白色背景（高亮度），字形笔画是深色，
#    取「亮度最低的一小撮像素」的中位色 = 笔画核心色（避开抗锯齿过渡）。
# ============================================================

THINKING_TEXT_RGB = (0x7D, 0x7F, 0x85)   # 灰色 —— 思考过程
ANSWER_TEXT_RGB = (0x19, 0x1B, 0x23)     # 黑色 —— AI 回答正文
TEXT_COLOR_MAX_DIST = 80.0               # 与参考色距离超过此值视为 unknown

# 判定为「文字笔画」的最低像素数；太少说明该节点几乎无字（悬空/透明）
TEXT_MIN_CORE_PIXELS = 6
# 行高经验值（px），用于高节点分行投票；超过 2 倍行高就用 strip 逻辑
TEXT_STRIP_HEIGHT = 36


def _grab_full_screen() -> Image.Image:
    """adb 截整屏，返回 PIL.Image（RGB）。用于颜色采样与裁剪。"""
    data = adb_screenshot_bytes()
    return Image.open(BytesIO(data)).convert("RGB")


def _text_core_rgb_region(screen_np, x1: int, y1: int, x2: int, y2: int
                         ) -> Optional[Tuple[int, int, int]]:
    """对单个矩形区域采样「字形核心色」（与参考色最近的中位色）。"""
    if np is None or screen_np is None:
        return None

    h, w = screen_np.shape[:2]
    x1 = max(0, min(x1, w - 1))
    x2 = max(x1 + 1, min(x2, w))
    y1 = max(0, min(y1, h - 1))
    y2 = max(y1 + 1, min(y2, h))

    region = screen_np[y1:y2, x1:x2]
    if region.size == 0:
        return None

    flat = region.reshape(-1, 3).astype(np.int32)
    luma = (flat[:, 0] * 299 + flat[:, 1] * 587 + flat[:, 2] * 114) / 1000.0

    text_mask = luma < 200
    count = int(text_mask.sum())
    if count < TEXT_MIN_CORE_PIXELS:
        return None

    text_px = flat[text_mask]
    text_luma = luma[text_mask]

    # 取最暗的 20% 作为核心笔画
    k = max(1, int(count * 0.2))
    idx = np.argsort(text_luma)[:k]
    core = text_px[idx]
    median = np.median(core, axis=0)
    return (int(median[0]), int(median[1]), int(median[2]))


def _text_core_rgb(
    screen_np, bounds: tuple
) -> Optional[Tuple[int, int, int]]:
    """采样文本节点矩形内的「字形核心色」。

    对于超高节点（> 2 行），按行拆成 strip 分别采样，然后取 strip 颜色的中位色，
    防止一大段混合文字被某个局部极暗像素拉偏。
    """
    if np is None or screen_np is None:
        return None

    x1, y1, x2, y2 = bounds
    node_h = y2 - y1
    strip_h = TEXT_STRIP_HEIGHT

    if node_h <= strip_h * 2:
        # 小节点：整区域采样
        return _text_core_rgb_region(screen_np, x1, y1, x2, y2)

    # 大节点：横向 strips 投票，再取 strips 的中位色
    strip_colors = []
    for sy in range(y1, y2, strip_h):
        ey = min(sy + strip_h, y2)
        c = _text_core_rgb_region(screen_np, x1, sy, x2, ey)
        if c is not None:
            strip_colors.append(c)
    if not strip_colors:
        return None

    # 取 strips 中位色（避免被单条 strip 的干扰像素支配）
    r = int(np.median([c[0] for c in strip_colors]))
    g = int(np.median([c[1] for c in strip_colors]))
    b = int(np.median([c[2] for c in strip_colors]))
    return (r, g, b)


def _rgb_dist(a: Tuple[int, ...], b: Tuple[int, ...]) -> float:
    """RGB 欧氏距离。"""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _classify_text_node(screen_np, bounds: tuple) -> str:
    """把文本节点分成 'answer'（黑）/'thinking'（灰）/'unknown'。

    与上一版区别：
      * 改亮度阈值为「与参考色的欧氏距离就近分类」，鲁棒性更高；
      * 增加对超高节点的 strip 投票，防止局部像素拉偏；
      * 距离两个参考色都很远时判为 unknown，由调用方宽松处理。
    """
    rgb = _text_core_rgb(screen_np, bounds)
    if rgb is None:
        return "unknown"

    d_gray = _rgb_dist(rgb, THINKING_TEXT_RGB)
    d_black = _rgb_dist(rgb, ANSWER_TEXT_RGB)
    best = min(d_gray, d_black)

    if best > TEXT_COLOR_MAX_DIST:
        return "unknown"

    return "thinking" if d_gray <= d_black else "answer"


def _swipe_in_viewport(d, direction: str,
                       fraction: float = CAPTURE_FRACTION,
                       duration: float = CAPTURE_SWIPE_DURATION,
                       viewport: Optional[tuple] = None):
    if viewport is None:
        viewport = get_message_viewport(d)
    left, top, right, bottom = viewport
    cx = (left + right) // 2
    area_h = max(200, bottom - top)

    if direction == "up":
        y1 = int(top + area_h * 0.80)
        y2 = int(top + area_h * (0.80 - fraction))
    else:
        y1 = int(top + area_h * 0.20)
        y2 = int(top + area_h * (0.20 + fraction))

    try:
        d.swipe(cx, y1, cx, y2, duration=duration)
    except Exception as exc:
        LOG.debug("滑动失败：%s", exc)


def click_bottom_button(d) -> bool:
    for word in BOTTOM_WORDS:
        try:
            obj = d(descriptionContains=word)
            if obj.exists(timeout=0.5):
                obj.click(); time.sleep(0.6)
                return True
        except Exception:
            pass
    return False


def scroll_to_top(d) -> bool:
    log("滚到聊天顶部")
    viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_TOP_SWIPES):
        _swipe_in_viewport(d, "down", fraction=0.55, duration=0.5, viewport=viewport)
        time.sleep(0.6)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_top_probe.png", viewport)
            sig = image_signature(img)
            if _sig_similar(sig, last_sig):
                stable += 1
                log(f"[TOP] 采样 {i+1}: 未移动 ({stable}/{SCROLL_STABLE_ROUNDS})")
                if stable >= SCROLL_STABLE_ROUNDS:
                    log("✓ 已到达聊天顶部")
                    return True
            else:
                stable = 0
                last_sig = sig
        except Exception as exc:
            LOG.debug("顶部探测失败：%s", exc)
    log("顶部定位达到最大次数（可能未真正到顶）")
    return True


def scroll_to_bottom(d) -> bool:
    log("滚回聊天底部")
    for _ in range(3):
        if click_bottom_button(d):
            time.sleep(0.6)
            return True

    viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_BOTTOM_SWIPES):
        _swipe_in_viewport(d, "up", fraction=0.55, duration=0.5, viewport=viewport)
        time.sleep(0.6)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_bottom_probe.png", viewport)
            sig = image_signature(img)
            if _sig_similar(sig, last_sig):
                stable += 1
                log(f"[BOTTOM] 采样 {i+1}: 未移动 ({stable}/{SCROLL_STABLE_ROUNDS})")
                if stable >= SCROLL_STABLE_ROUNDS:
                    log("✓ 已到达聊天底部")
                    return True
            else:
                stable = 0
                last_sig = sig
        except Exception as exc:
            LOG.debug("底部探测失败：%s", exc)
    log("底部定位达到最大次数")
    return True


def _row_features(img: Image.Image, samples: int = 32) -> "np.ndarray":
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = arr.shape
    if w <= samples:
        return arr
    step = w // samples
    cols = np.arange(0, w, step)[:samples]
    return arr[:, cols]


def find_overlap(upper: Image.Image, lower: Image.Image) -> int:
    if np is None:
        raise RuntimeError("长截图需要 numpy：pip install numpy")
    up_feat = _row_features(upper)
    lo_feat = _row_features(lower)
    min_ov = max(15, int(min(len(up_feat), len(lo_feat)) * 0.15))
    # 上限从 0.70 放宽到 0.95：滚到聊天底部前的最后一次滑动位移往往很小
    # （剩余可滚内容不多），相邻两帧重叠会高达 85% 以上。旧上限 0.70 会让
    # 这种帧在搜索范围里找不到真正的重叠位置，退而选中一个错误的「小重叠」，
    # 拼接时就把同一段内容（末尾段落、来源条、工具栏）重复贴两遍。
    max_ov = int(min(len(up_feat), len(lo_feat)) * 0.95)
    if max_ov <= min_ov:
        return min_ov
    best_ov = min_ov
    best_score = float("inf")
    for ov in range(min_ov, max_ov + 1, 2):
        a = up_feat[-ov:]
        b = lo_feat[:ov]
        if a.shape != b.shape:
            continue
        score = float(np.mean(np.abs(a - b)))
        if score < best_score:
            best_score = score
            best_ov = ov
    return best_ov


def stitch_frames(frames: list, output: Path) -> Image.Image:
    if not frames:
        raise RuntimeError("没有可拼接的截图帧")
    canvas = frames[0].copy()
    for idx in range(1, len(frames)):
        upper = frames[idx - 1]
        lower = frames[idx]
        ov = find_overlap(upper, lower)
        ov = max(5, min(ov, lower.height - 1))
        new_part = lower.crop((0, ov, lower.width, lower.height))
        if new_part.height <= 0:
            continue
        merged = Image.new("RGB", (canvas.width, canvas.height + new_part.height))
        merged.paste(canvas, (0, 0))
        merged.paste(new_part, (0, canvas.height))
        canvas = merged
    canvas.save(output, format="PNG", optimize=False)
    return canvas


# ============================================================
# 特殊字符清理 / 噪声过滤 / 文本采集
# ============================================================

def _clean_special_chars(text: str) -> str:
    if not text:
        return ""

    keep = []
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:      # surrogate
            continue
        if 0xE000 <= cp <= 0xF8FF:      # 私用区
            continue
        if 0x1F000 <= cp <= 0x1FFFF:    # emoji 主要区
            continue
        if 0x2600 <= cp <= 0x27BF:      # 杂项符号 / dingbats
            continue
        if 0x2B00 <= cp <= 0x2BFF:      # 补充符号
            continue
        if 0x2460 <= cp <= 0x24FF:      # 带圈数字
            continue
        if cp == 0xFE0F or cp == 0x200D:  # 变体 / 零宽连接
            continue
        keep.append(ch)

    result = "".join(keep)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{2,}", "\n", result)
    return result.strip()


def _is_noise_text(text: str, question: str) -> bool:
    if not text:
        return True

    t = text.strip()
    if not t:
        return True

    cleaned = _clean_special_chars(t)
    if not cleaned:
        return True

    if len(t) < 20 and t in IGNORE_EXACT:
        return True

    for sub in IGNORE_SUBSTR:
        if sub in t and len(t) < 80:
            return True

    if question:
        q = question.strip()
        if t == q:
            return True
        if len(t) <= len(q) + 15 and q in t:
            return True

    if re.match(r"^搜索到\s*\d+\s*个网页\s*>?\s*$", t):
        return True

    if len(cleaned) < 2:
        return True
    if re.match(r"^[\s\d\W]+$", cleaned) and len(cleaned) < 10:
        return True

    return False


def _filter_substring_dupes(texts: list) -> list:
    if not texts:
        return []

    seen = set()
    unique = []
    for t in texts:
        key = t.strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)

    result = []
    for i, t in enumerate(unique):
        ts = t.strip()
        is_sub = False
        for j, other in enumerate(unique):
            if i == j:
                continue
            os = other.strip()
            if os == ts:
                continue
            if len(os) > len(ts) + 8 and ts in os:
                is_sub = True
                break
        if not is_sub:
            result.append(t)
    return result


def _join_answer_texts(texts: list) -> str:
    if not texts:
        return ""
    parts = []
    for t in texts:
        t = t.strip()
        if not t:
            continue
        parts.append(t)
    return "\n\n".join(parts)


def _collect_screen_texts(
    d,
    viewport: tuple,
    question: str,
    seen_hashes: set,
    exclude_texts: Optional[Set[str]] = None,
    screen_np=None,
) -> list:
    """
    采集当前屏的「AI 回答正文」，返回 [(y, text), ...]（按 y 排序）。

    双重过滤，确保不会把思考过程混进正文：
      1. 颜色过滤（主）：字体核心色为灰色(#7d7f85)的节点直接丢弃，
         只保留黑色(#191b23)与无法判色的节点。
      2. 排除集（辅）：与已采集思考过程段落高度重合的文本再排除一次。

    :param screen_np: 全屏 numpy 数组，用于字体颜色采样；为 None 时退化为纯文本过滤
    """
    items = []
    try:
        xml = dump_xml(d)
        nodes = parse_nodes(xml)
    except Exception as exc:
        LOG.debug("采集文本失败：%s", exc)
        return items

    exclude_texts = exclude_texts or set()

    for n in nodes:
        text = n["text"]
        if not text:
            continue

        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue

        if b[1] < viewport[1] or b[3] > viewport[3]:
            continue

        if _is_noise_text(text, question):
            continue

        # 结构化排除：思考标题行 / 来源药丸，任何情况下都不属于回答正文
        _probe = _clean_special_chars(text)
        if THINKING_TITLE_PATTERN.search(_probe):
            continue
        if SOURCE_ENTRY_PATTERN.search(_probe) and len(_probe) <= 20:
            continue

        # ---- 颜色过滤：灰色=思考过程，绝不并入回答正文 ----
        if screen_np is not None and _classify_text_node(screen_np, b) == "thinking":
            LOG.debug("跳过灰色(思考过程)文本：%s", text[:30])
            continue

        cleaned = _clean_special_chars(text)
        if not cleaned or len(cleaned) < 2:
            continue

        if cleaned in exclude_texts:
            continue
        skipped = False
        for ex in exclude_texts:
            if len(ex) >= 10 and (cleaned in ex or ex in cleaned) \
                    and abs(len(ex) - len(cleaned)) < 30:
                skipped = True
                break
        if skipped:
            continue

        h = hash(cleaned)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        items.append((b[1], cleaned))

    items.sort(key=lambda x: x[0])
    return items


# ============================================================
# 思考过程：展开 + 采集
# ============================================================

def _snapshot_texts(d, viewport: Optional[tuple] = None) -> Set[str]:
    """
    取当前消息视窗内所有可见文本的快照（用于前后差异对比）。
    """
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return set()

    if viewport is None:
        try:
            viewport = get_message_viewport(d)
        except Exception:
            w, h = get_screen_size(d)
            viewport = (0, int(h * 0.07), w, int(h * 0.78))

    texts = set()
    for n in nodes:
        t = (n["text"] or "").strip()
        if not t or len(t) < 2:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] < viewport[1] or b[3] > viewport[3]:
            continue
        texts.add(t)
    return texts


def _find_thinking_title_node(d) -> Optional[dict]:
    """
    查找"已思考"标题行节点。
    """
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    candidates = []
    for n in nodes:
        text = (n["text"] or "").strip()
        desc = (n["desc"] or "").strip()
        combined = (text + " " + desc).strip()
        if not combined:
            continue
        if not THINKING_TITLE_PATTERN.search(combined):
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        h_ratio = (b[3] - b[1]) / 1920.0
        if h_ratio > 0.15:
            continue
        candidates.append((b[1], b, n, combined))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    _, b, n, text = candidates[0]
    return {"bounds": b, "node": n, "text": text}


def expand_thinking_panel(d) -> bool:
    """
    点击"已思考"标题行，展开思考过程。
    """
    log("→ 尝试展开思考过程")

    title = _find_thinking_title_node(d)
    if not title:
        log("未找到'已思考'标题行")
        return False

    b = title["bounds"]
    text_preview = title["text"][:40]

    w_screen, _ = get_screen_size(d)
    arrow_x = min(b[2] + 30, w_screen - 10)
    center_y = (b[1] + b[3]) // 2

    # 策略 A：点击行末箭头
    try:
        d.click(arrow_x, center_y)
        time.sleep(1.5)
        log(f"✓ 已点击'已思考'行末箭头：{text_preview}")
        return True
    except Exception as exc:
        LOG.debug("点击行末箭头失败：%s", exc)

    # 策略 B：点击整行中心
    try:
        cx = (b[0] + b[2]) // 2
        d.click(cx, center_y)
        time.sleep(1.5)
        log(f"✓ 已点击'已思考'整行：{text_preview}")
        return True
    except Exception as exc:
        LOG.debug("点击整行失败：%s", exc)

    # 策略 C：通过 content-desc 查找展开按钮
    for desc_word in THINKING_EXPAND_DESCS:
        try:
            obj = d(descriptionContains=desc_word)
            if obj.exists(timeout=0.5):
                obj.click()
                time.sleep(1.5)
                log(f"✓ 通过 content-desc='{desc_word}' 展开成功")
                return True
        except Exception:
            pass

    log("无法展开思考过程")
    return False


def collect_thinking_content(
    d,
    before_texts: Optional[Set[str]] = None,
    viewport: Optional[tuple] = None,
    max_swipes: int = 40,
) -> list:
    """采集展开后的「思考过程」文本，并尽量采全。

    判据：
      * 字体颜色 —— 只收灰色(#7d7f85)文本节点；
      * 结构收尾 —— 布局顺序固定为「灰思考 → 黑正文」，收到灰色文本后
        一旦出现黑色正文节点即判定思考区走完，立即停（不会漏尾部，
        也不会把正文卷进来）。

    :param before_texts: 已废弃参数，仅为兼容旧调用保留（不再使用）
    :return: 段落列表（按屏幕自上而下顺序）
    """
    if viewport is None:
        try:
            viewport = get_message_viewport(d)
        except Exception:
            w, h = get_screen_size(d)
            viewport = (0, int(h * 0.07), w, int(h * 0.78))

    # 注意：不再使用 before_texts 做差集。回答生成完毕时思考面板常已自动
    # 展开，可见思考段会进快照被误当「旧内容」跳过 —— 这正是之前思考过程
    # 从中间才开始、采不全的根因。现在只靠颜色判据全量收集灰色文本。
    seen: Set[str] = set()
    collected: list = []
    idle_rounds = 0

    for step in range(max_swipes):
        # 截整屏用于字体颜色采样
        screen_np = None
        if np is not None:
            try:
                screen_np = np.asarray(_grab_full_screen())
            except Exception as exc:
                LOG.debug("思考过程截屏失败：%s", exc)

        try:
            nodes = parse_nodes(dump_xml(d))
        except Exception:
            nodes = []

        round_items = []
        answer_seen_after_thinking = False
        for n in nodes:
            text = n["text"]
            if not text:
                continue

            cleaned = _clean_special_chars(text)
            if not cleaned or len(cleaned) < 2:
                continue

            if cleaned in IGNORE_EXACT:
                continue

            # 排除标题行：已思考（用时 X 秒）/ 思考中 / DeepThink …
            if THINKING_TITLE_PATTERN.search(cleaned):
                continue

            # 排除来源入口药丸（「31 个网页」等小灰字，不是思考内容）
            if SOURCE_ENTRY_PATTERN.search(cleaned) and len(cleaned) <= 20:
                continue

            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue

            if b[1] < viewport[1] or b[3] > viewport[3]:
                continue

            # 颜色分类
            cls = _classify_text_node(screen_np, b) if screen_np is not None else "thinking"

            # 结构信号：已采集到思考内容后，一旦出现黑色正文节点，
            # 说明思考区域已经走完（布局顺序：灰思考 → 黑正文）
            if cls == "answer" and collected:
                answer_seen_after_thinking = True
                continue

            if cls != "thinking":
                continue

            if cleaned in seen:
                continue

            seen.add(cleaned)
            round_items.append((b[1], cleaned))

        if round_items:
            round_items.sort(key=lambda x: x[0])
            collected.extend(t for _, t in round_items)
            idle_rounds = 0
        elif step > 0:
            # 允许第 1 轮为空（可能刚展开还没渲染完）
            idle_rounds += 1

        # 收尾判定：见到黑色正文 = 思考区结束，立即停；
        # 或连续 3 轮无新增（容错大节点跨屏导致的空轮）
        if answer_seen_after_thinking:
            LOG.debug("检测到黑色正文，思考过程采集结束（第 %d 轮）", step + 1)
            break
        if idle_rounds >= 3:
            break

        _swipe_in_viewport(d, "up", fraction=0.35, duration=0.8, viewport=viewport)
        time.sleep(1.2)

    return _filter_substring_dupes(collected)


def expand_and_collect_thinking(d) -> str:
    """
    展开思考过程并采集内容，返回纯文本。
    """
    log("=" * 50)
    log("开始采集思考过程内容")

    if not expand_thinking_panel(d):
        log("✗ 无法展开思考过程，返回空")
        return ""

    time.sleep(2.0)

    new_texts = collect_thinking_content(d)

    if not new_texts:
        log("未采集到灰色思考文本，重试展开后再采一次…")
        expand_thinking_panel(d)
        time.sleep(2.0)
        new_texts = collect_thinking_content(d)

    if not new_texts:
        log("✗ 未采集到思考过程内容")
        return ""

    thinking_text = clean_text("\n\n".join(new_texts))

    log(f"✓ 思考过程采集完成：{len(thinking_text)} 字符，{len(new_texts)} 段落")
    log("=" * 50)
    return thinking_text


# ============================================================
# 主问答长截图 + 同步文本采集
# ============================================================

def screenshot_main_with_text(
    d,
    output: Path,
    question: str,
    exclude_texts: Optional[Set[str]] = None,
) -> tuple:
    log("开始主问答长截图（慢速小步 + 同步收集文本 v6）")

    exclude_texts = exclude_texts or set()

    if not scroll_to_top(d):
        raise RuntimeError("无法确认聊天顶部")

    time.sleep(1.0)
    viewport = get_message_viewport(d)       # 文本采集视窗（完整消息区）
    capture_vp = get_capture_viewport(d)     # 取图视窗（已裁掉右下角悬浮按钮）
    log(f"消息视窗：{viewport}；取图视窗：{capture_vp}")

    frames = []
    prev_sig = None
    stable_bottom = 0

    seen_hashes = set()
    screens_texts = []
    screen_idx = 0

    idle_rounds = 0    # 保险丝：连续无新画面且无新文本的轮数
    at_bottom = False  # 是否已确认滚到底部（用于最后补截完整尾帧）

    for i in range(MAX_CAPTURE_FRAMES):
        # 一次截屏复用：既用于帧拼接，也用于字体颜色判定（省一次 adb 截屏）
        full_img = _grab_full_screen()
        current = save_message_area(d, OUTPUT_DIR / f"_frame_{i:03d}.png",
                                    capture_vp, full=full_img)
        screen_np = np.asarray(full_img) if np is not None else None
        sig = image_signature(current)
        # 容差判同：防止到底回弹/像素噪声产生“假新屏”混入拼接
        if not frames:
            frames.append(current.copy())
            log("已采集第 1 屏")
            new_frame = True
        elif not _sig_similar(sig, prev_sig):
            frames.append(current.copy())
            log(f"已采集第 {len(frames)} 屏")
            new_frame = True
        else:
            log("当前屏与上一屏相同，跳过")
            new_frame = False
        prev_sig = sig

        # 文本采集仍用完整视窗，保证不漏采任何正文
        items = _collect_screen_texts(
            d, viewport, question, seen_hashes,
            exclude_texts=exclude_texts, screen_np=screen_np,
        )

        if items:
            screens_texts.append((screen_idx, items))
            screen_idx += 1
            log(f"  屏 {screen_idx} 新增正文 {len(items)} 条")

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=capture_vp)
        time.sleep(CAPTURE_SETTLE_WAIT)

        try:
            after = save_message_area(d, OUTPUT_DIR / "_probe_after.png",
                                      capture_vp, full=_grab_full_screen())
            after_sig = image_signature(after)
            # 关键修正：与「本次滑动前」的画面比较（而不是 frames[-1]），
            # 滑动没有带来有效位移 = 到底；容差吸收回弹微动
            if _sig_similar(after_sig, sig):
                stable_bottom += 1
                log(f"[BOTTOM] 未移动 {stable_bottom}/{SCROLL_STABLE_ROUNDS}")
                if stable_bottom >= SCROLL_STABLE_ROUNDS:
                    log("✓ 主问答长截图到达底部")
                    at_bottom = True
                    break
            else:
                stable_bottom = 0
        except Exception as exc:
            LOG.debug("底部探测异常：%s", exc)

        # 保险丝：连续 4 轮既无新画面也无新文本，强制判定到底，
        # 即使上述探测全部失效也保证循环一定退出
        if new_frame or items:
            idle_rounds = 0
        else:
            idle_rounds += 1
            if idle_rounds >= 4:
                log("连续 4 轮无新内容，判定已到底部")
                at_bottom = True
                break

    if not frames:
        raise RuntimeError("没有采集到聊天截图")

    if at_bottom:
        # 取图视窗为了避开右下角悬浮按钮，把屏幕最底部一条裁掉了，
        # 而回答末尾的「N 个网页」来源条 + 复制/点赞工具栏恰好压在这一条里。
        # 滚到底部后悬浮按钮会自动消失，此时按完整视窗再截一帧，
        # 用它替换最后一屏：既没有按钮，长图结尾也不会缺内容。
        tail = _capture_tail_frame(d, viewport)
        if tail is not None:
            frames[-1] = tail
            log("已用完整尾帧替换最后一屏（含来源条 / 复制点赞工具栏）")
    elif len(frames) > 1 and _sig_similar(image_signature(frames[-1]),
                                          image_signature(frames[-2])):
        # 非正常到底（帧数上限 / 保险丝兜底）时，才需要丢掉重复的尾屏
        frames.pop()

    log(f"共采集 {len(frames)} 屏，开始拼接")
    stitch_frames(frames, output)
    log(f"主问答长截图完成：{output}")

    ordered_texts = []
    for _, items in screens_texts:
        for _, t in items:
            ordered_texts.append(t)

    log(f"原始正文条目：{len(ordered_texts)}")

    filtered_texts = _filter_substring_dupes(ordered_texts)
    log(f"过滤后正文条目：{len(filtered_texts)}")

    answer_text = _join_answer_texts(filtered_texts)

    return answer_text, ""


# 来源入口文案。仅用于从正文 / 思考文本中过滤掉「N 个网页」这类药丸文案，
# 不再用于定位或截图搜索结果面板（该功能已移除）。
SOURCE_ENTRY_PATTERN = re.compile(
    r"搜索到\s*\d+\s*个?网页|\d+\s*个?网页|\d+\s*篇网页|参考\s*\d+\s*篇"
)


# ============================================================
# HTML 转换
# ============================================================

def html_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


def text_to_html(text: str) -> str:
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = re.split(r"\n\s*\n", text)
    result = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        lines = para.split("\n")
        if all((not l.strip()) or re.match(r"^[-*]\s+", l.strip()) for l in lines):
            items = [re.sub(r"^[-*]\s+", "", l.strip()) for l in lines if l.strip()]
            if items:
                result.append("<ul>")
                result.extend(f"<li>{html_inline(x)}</li>" for x in items)
                result.append("</ul>")
                continue
        if all((not l.strip()) or re.match(r"^\d+[.、]\s+", l.strip()) for l in lines):
            items = [re.sub(r"^\d+[.、]\s+", "", l.strip()) for l in lines if l.strip()]
            if items:
                result.append("<ol>")
                result.extend(f"<li>{html_inline(x)}</li>" for x in items)
                result.append("</ol>")
                continue
        result.append(f"<p>{html_inline(para).replace(chr(10), '<br>')}</p>")
    return "\n".join(result)


# ============================================================
# 清理
# ============================================================

def cleanup_temp_files() -> None:
    for pattern in (
        "_top_probe.png", "_bottom_probe.png", "_probe_after.png",
    ):
        try:
            (OUTPUT_DIR / pattern).unlink(missing_ok=True)
        except Exception:
            pass
    for path in OUTPUT_DIR.glob("_frame_*.png"):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Worker 任务处理
# ============================================================

def handle_unit(d, unit: dict) -> None:
    unit_id = unit.get("id")
    task_no = unit.get("taskNo")
    question = str(unit.get("questionText") or unit.get("question") or "").strip()
    if not question:
        raise RuntimeError("任务问题为空")

    task_dir = OUTPUT_DIR / str(task_no or unit_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 70)
    log(f"开始任务：unit_id={unit_id}, taskNo={task_no}")
    log(f"问题：{question[:120]}")

    # 1. 新对话
    if not new_chat(d):
        raise RuntimeError("未能确认新建对话。")
    time.sleep(1.5)

    # ---------- 开启深度思考模式（v6 带状态检测，防误关） ----------
    try:
        enable_deep_thinking(d)
    except Exception as exc:
        LOG.warning("深度思考模式处理失败（继续执行）：%s", exc)
    time.sleep(0.8)
    # ----------------------------------------------------------

    # 2. 输入
    input_text(d, question)

    # 3. 发送
    if not send_message(d):
        raise RuntimeError("问题发送失败")

    # 4. 等待回答完成
    coarse_answer = wait_answer_finish(d)
    log(f"粗判回答完成，粗文本 {len(coarse_answer)} 字符")

    # ---------- 展开思考过程 + 采集思考内容 ----------
    thinking_text = ""
    try:
        scroll_to_top(d)
        time.sleep(1.0)
        thinking_text = expand_and_collect_thinking(d)
    except Exception as exc:
        LOG.warning("展开/采集思考过程失败（继续执行）：%s", exc)
    # ----------------------------------------------------------

    # 5. 回到底部 → 主截图 + 正文采集
    scroll_to_bottom(d)
    time.sleep(0.8)

    exclude_texts: Set[str] = set()
    if thinking_text:
        for para in thinking_text.split("\n\n"):
            p = para.strip()
            if p and len(p) >= 4:
                exclude_texts.add(p)

    main_path = task_dir / "answer_main.png"
    answer_text, _ = screenshot_main_with_text(
        d, main_path, question, exclude_texts=exclude_texts,
    )

    # 颜色过滤后 answer_text 已能准确区分正文与思考过程，
    # 「比粗判文本短」不再代表漏采（粗判文本里本就混有灰色思考内容/标题），
    # 因此只在真正没采到正文时才回退到粗判文本，避免把思考过程混回正文
    if len(clean_text(answer_text)) < 20:
        log(f"未采集到有效正文，回退粗判文本（{len(coarse_answer)} 字符）")
        answer_text = coarse_answer

    answer_text = clean_text(answer_text)
    thinking_text = clean_text(thinking_text)

    # ---- 最终防线：正文与思考过程段落级去重 ----
    # 即使颜色/结构判据全部失效，也保证报告里两块内容绝不重叠：
    # 凡正文中出现与思考段落相同（或互为子串）的段落，从正文剔除。
    if thinking_text and answer_text:
        think_paras = [p.strip() for p in thinking_text.split("\n\n")
                       if len(p.strip()) >= 6]
        if think_paras:
            kept, dropped = [], 0
            for para in answer_text.split("\n\n"):
                ps = para.strip()
                contaminated = any(
                    ps == tp or (len(ps) >= 6 and ps in tp)
                    or (len(tp) >= 10 and tp in ps)
                    for tp in think_paras
                )
                if contaminated:
                    dropped += 1
                    continue
                kept.append(para)
            if dropped:
                log(f"最终防线：从正文剔除 {dropped} 段与思考过程重复的内容")
                answer_text = "\n\n".join(kept)

    if len(answer_text) < ANSWER_MIN_COMPLETE_LENGTH // 2:
        raise RuntimeError(f"回答文本过短（{len(answer_text)} 字符）")

    log(f"✓ 完整正文回答：{len(answer_text)} 字符")
    log(f"✓ 思考过程：{len(thinking_text)} 字符")

    # 6. 最终截图：仅使用主问答长截图（已移除网页来源截图功能）
    final_image = main_path

    # 7. 生成 HTML
    answer_html = text_to_html(answer_text)
    thinking_html = text_to_html(thinking_text)
    source_info = ""

    (task_dir / "answer.txt").write_text(answer_html, encoding="utf-8")
    (task_dir / "answer_plain.txt").write_text(answer_text, encoding="utf-8")
    (task_dir / "thinking.txt").write_text(thinking_html, encoding="utf-8")
    (task_dir / "thinking_plain.txt").write_text(thinking_text, encoding="utf-8")
    (task_dir / "sources.txt").write_text("", encoding="utf-8")

    if not answer_html:
        raise RuntimeError("没有生成有效 HTML")

    # 9. 上传
    if not _HAS_WORKER_LIB:
        raise RuntimeError("任务池模式需要 worker_lib.py。")

    log("上传截图")
    image_url = worker_upload(str(final_image), task_id=unit_id)
    if not image_url:
        raise RuntimeError("截图上传失败")

    # 10. 回调
    log("回调任务结果")
    ok = worker_callback(
        unit,
        status="SUCCESS",
        answer_text=answer_html,
        thinking_content=thinking_html,
        source_info=source_info,
        image_url=image_url,
        error_msg=None,
    )
    if not ok:
        raise RuntimeError("结果 callback 失败")

    log(f"✓ 任务完成：{task_no or unit_id}")


def run_worker(d) -> None:
    if not _HAS_WORKER_LIB or run_worker_loop is None:
        raise RuntimeError("没有找到 worker_lib.py。")
    log(f"启动任务池 Worker：platform={PLATFORM_NAME}")
    run_worker_loop(PLATFORM_NAME, d, handle_unit)


# ============================================================
# 诊断 / CLI
# ============================================================

def diagnose(d) -> None:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    diag_dir = BASE_DIR / "deepseek_diagnose" / timestamp
    diag_dir.mkdir(parents=True, exist_ok=True)
    log(f"当前前台：{d.app_current()}")
    w, h = get_screen_size(d)
    log(f"屏幕：{w}x{h}")
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport}")
    dump_xml(d, diag_dir / "window.xml")
    save_screen(diag_dir / "screen.png")

    # 诊断深度思考按钮 + 状态
    try:
        button = _find_deep_thinking_button(d)
        if button:
            log(f"深度思考按钮：text={button['text']!r}, desc={button['desc']!r}, "
                f"rid={button['rid']!r}, bounds={button['bounds']!r}, "
                f"clickable={button['clickable']}, "
                f"selected={button.get('selected')}, checked={button.get('checked')}, "
                f"activated={button.get('activated')}")
            b = bounds_to_tuple(button["bounds"])
            if b:
                # 直接保存按钮区域截图，方便肉眼确认
                try:
                    data = adb_screenshot_bytes()
                    img = Image.open(BytesIO(data)).convert("RGB")
                    x1, y1, x2, y2 = b
                    crop = img.crop((max(0, x1 - 6), max(0, y1 - 6),
                                     min(img.width, x2 + 6), min(img.height, y2 + 6)))
                    crop.save(diag_dir / "deep_thinking_button.png")
                    log(f"已保存按钮截图：{diag_dir / 'deep_thinking_button.png'}")
                except Exception as exc:
                    LOG.debug("保存按钮截图失败：%s", exc)

                enabled = is_deep_thinking_enabled(d, button)
                log(f"深度思考状态判定：{'已开启' if enabled else '未开启'}")
        else:
            log("未找到深度思考按钮节点")
    except Exception as exc:
        LOG.debug("诊断深度思考按钮失败：%s", exc)

    log(f"诊断完成：{diag_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepSeek Android Worker v6")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log("=" * 70)
    log("DeepSeek Android Worker v6")
    log(f"package={DEEPSEEK_PACKAGE}")
    log(f"platform={PLATFORM_NAME}")
    log("=" * 70)

    d = None
    try:
        d = connect_device()
        start_deepseek(d)
        if args.diagnose:
            diagnose(d)
            return 0
        run_worker(d)
        return 0
    except KeyboardInterrupt:
        log("收到 Ctrl+C，停止 Worker")
        return 130
    except Exception as exc:
        log(f"Worker 致命错误：{exc}")
        LOG.debug(traceback.format_exc())
        return 1
    finally:
        cleanup_temp_files()
        if d is not None:
            try:
                d.app_stop(DEEPSEEK_PACKAGE)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())