# -*- coding: utf-8 -*-
"""
yuanbao_worker.py —— yuanbao_auto 项目
============================================================
腾讯元宝 Android App 自动化 Worker（问答采集 + 截图）

基于 yuanbao_app.py v13 改编，并入 yuanbao_auto 工程体系：
  1. 基础配置（平台标识/包名候选/截图目录）统一收敛到项目 config.py
  2. 登录/登出/人机验证由 yuanbao_login.py + captcha_solver.py 负责，
     本模块只做「新建对话 → 深度思考 → 问答 → 采集 → 截图 → 回调」
  3. 采集能力（v13 全量保留）：
     - 主回答长截图（滚动拼接）
     - 深度思考过程采集（自动展开"找到了 N 篇相关资料"）
     - 引用来源面板长截图
     - 系统通知噪声过滤
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
from typing import Optional, Sequence

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
# worker_lib
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
# 基础配置（收敛到 config.py）
# ============================================================

try:
    from config import (
        BASE_DIR,
        SCREENSHOT_DIR as OUTPUT_DIR,
        PLATFORM_NAME,
        YUANBAO_PACKAGE_CANDIDATES,
    )
except ImportError:   # 独立运行兜底（无 config.py 时）
    BASE_DIR = Path(__file__).resolve().parent
    OUTPUT_DIR = BASE_DIR / "screenshots"
    PLATFORM_NAME = "tencent_app"
    YUANBAO_PACKAGE_CANDIDATES = (
        "com.tencent.hunyuan.app.chat",
        "com.tencent.hunyuan.app",
        "com.tencent.hunyuan",
        "com.tencent.yuanbao",
        "com.tencent.mobileqq.yuanbao",
    )
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ANSWER_HARD_TIMEOUT = 600
ANSWER_MIN_WAIT = 6.0
ANSWER_MIN_COMPLETE_LENGTH = 40
ANSWER_STABLE_SECONDS = 4.0
ANSWER_STABLE_MIN_ROUNDS = 3
POLL_INTERVAL = 1.0
POLL_INTERVAL_SLOW = 2.0

MAX_TOP_SWIPES = 20
MAX_BOTTOM_SWIPES = 20
SCROLL_STABLE_ROUNDS = 3
SCROLL_SWIPE_FRACTION = 0.72

MAX_CAPTURE_FRAMES = 40
CAPTURE_SWIPE_DURATION = 1.0
CAPTURE_SETTLE_WAIT = 1.2
CAPTURE_FRACTION = 0.45
NO_TEXT_STREAK_LIMIT = 2

MAX_PANEL_FRAMES = 35
PANEL_SWIPE_DURATION = 1.5
PANEL_SETTLE_WAIT = 1.5
PANEL_LOAD_WAIT = 2.5
PANEL_NO_NEW_TEXT_LIMIT = 3
PANEL_NO_SCROLL_LIMIT = 2
PANEL_ANCHOR_MARGIN = 20
PANEL_MICRO_SWIPE_START = 0.72
PANEL_MICRO_SWIPE_END = 0.52
PANEL_MICRO_SWIPE_DURATION = 0.9
PANEL_SCREEN_BOTTOM_GUARD_RATIO = 0.012
PANEL_MIN_SCROLL_HEIGHT_RATIO = 0.40
PANEL_MIN_SCROLL_WIDTH_RATIO = 0.65
PANEL_MOVE_DIFF_THRESHOLD = 2.0
PANEL_STITCH_SEARCH_MIN_RATIO = 0.05
PANEL_STITCH_SEARCH_MAX_RATIO = 0.95
PANEL_STITCH_COARSE_STEP = 4
PANEL_STITCH_REFINE_RADIUS = 8

LONG_TEXT_THRESHOLD = 150

BODY_MIN_LEN = 15
BODY_END_PUNCT_CN = "。！？…"
BODY_END_PUNCT_EN = ".!?"

# 深度思考
DEEP_THINK_BUTTON_TEXTS = ("深度思考",)
DEEP_THINK_MENU_ITEMS = ("深度思考", "深入思考", "深度思考模式")
THINKING_ENTRY_PATTERN = re.compile(
    r"(?:已)?深度思考\s*[\(（]?\s*用时\s*\d+\s*秒\s*[\)）]?"
)
THINKING_REF_MARKERS = (
    "找到了", "篇相关资料", "相关内容", "引用源", "引用来源", "参考资料",
)

# ★ v13：系统通知噪声（避免污染思考正文）
SYSTEM_NOISE_PATTERNS = (
    re.compile(r"^(?:上午|下午|晚上|凌晨|中午|早上|傍晚)\s*\d{1,2}[:：]\d{2}$"),
    re.compile(r"^\d{1,2}[:：]\d{2}(?::\d{2})?$"),
    re.compile(r"Android\s*系统通知"),
    re.compile(r"系统通知\s*[：:]"),
    re.compile(r"WLAN\s*信号"),
    re.compile(r"手机信号"),
    re.compile(r"信号满格"),
    re.compile(r"正在充电"),
    re.compile(r"充电中"),
    re.compile(r"充电完成"),
    re.compile(r"已完成\s*\d+\s*%"),
    re.compile(r"电量\s*\d+\s*%"),
    re.compile(r"配置实体键盘"),
    re.compile(r"实体键盘"),
    re.compile(r"已连接\s*WLAN"),
    re.compile(r"^已连接$"),
    re.compile(r"^已断开$"),
    re.compile(r"USB\s*连接"),
    re.compile(r"蓝牙.*已连接"),
    re.compile(r"当前时间"),
    re.compile(r"^星期[一二三四五六日天]$"),
)

# ★ v13：思考面板内"找到了 N 篇相关资料"的可展开入口
THINKING_REF_TOGGLE_RE = re.compile(r"找到了\s*\d+\s*篇(?:相关)?资料")

GENERATING_WORDS = (
    "停止生成", "停止回答", "正在生成", "正在回答", "回答中", "思考中",
    "深度思考中", "正在思考", "正在深度思考",
    "Stop generating", "Generating",
)

INPUT_HINTS = (
    "发消息或按住说话",
    "发消息或按住说话...",
    "发消息",
    "按住说话",
    "向元宝提问",
    "输入",
    "Ask anything",
    "Message",
)

NEW_CHAT_WORDS = (
    "新建对话", "新对话", "新的对话", "创建新对话", "开启新对话",
    "New chat", "New conversation",
)
SEND_WORDS = ("发送", "Send")

BOTTOM_WORDS = (
    "回到底部", "返回底部", "到底部", "回到底", "回到最新",
    "Jump to bottom", "Scroll to bottom",
)

THINKING_WORDS = ("深度思考", "DeepThink", "Thinking", "思考过程", "深度思考过程")
SOURCE_WORDS = ("参考了", "参考资料", "参考来源", "引用源", "引用来源",
                "Sources", "References")

TAIL_NOISE_EXACT = {
    "expand", "Expand", "EXPAND", "展开", "展开全部", "查看更多", "点击展开",
    "collapse", "Collapse", "收起", "查看更多内容",
}

IGNORE_EXACT = {
    "元宝", "腾讯元宝", "Yuanbao", "Tencent Yuanbao", "元宝·腾讯",
    "分享", "Share", "复制", "Copy", "重新生成", "Regenerate",
    "点赞", "点踩", "Like", "Dislike", "发送", "Send",
    "关闭", "取消", "返回", "退出", "Close", "Cancel", "Back",
    "上一条", "发现", "我们", "首页", "我的", "消息", "问元宝",
    "深度思考", "深度思考中", "Thinking",
    "发消息或按住说话...", "发消息或按住说话", "发消息", "按住说话",
    "相机", "拍照", "相册",
    "语音输入", "语音", "麦克风",
    "加号", "更多", "添加",
    "回到底部", "返回底部", "回到最新",
    "参考来源", "参考资料", "参考了",
    "引用源", "引用来源",
    "朗读", "播放", "刷新", "编辑", "分享给好友",
    "收藏", "下载", "更多", "语音播报",
    "全屏查看", "查看全部",
    "expand", "Expand", "EXPAND", "展开", "展开全部", "查看更多",
    "collapse", "Collapse", "收起",
    "快速思考", "立即回答", "选择模型",
    "思考更充分，回答更优质",
}

IGNORE_SUBSTR = (
    "回答由 AI 生成", "仅供参考", "内容由 AI 生成",
    "以上内容由 AI 生成", "内容由AI生成",
    "推荐给您", "为您推荐",
    "相关视频", "相关推荐", "延伸阅读",
    "腾讯元宝", "混元大模型",
    "—— 以下为元宝生成 ——",
    "思考更充分", "回答更优质",
)

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")

REFERENCE_ENTRY_RE = re.compile(
    r"(?:引用源(?:\s*\d+)?|引用来源(?:\s*\d+)?"
    r"|参考(?:了)?\s*\d+\s*篇(?:资料|网页|内容))"
)
REFERENCE_ENTRY_FALLBACK = ("引用源", "引用来源",
                            "参考了", "参考资料", "参考来源")
REFERENCE_PANEL_MARKERS = ("引用来源", "参考来源", "参考资料",
                           "References", "Sources")

DRAWER_KEYWORDS = ("新建对话", "新对话", "我的", "历史对话", "对话记录", "智能体")


# ============================================================
# 日志
# ============================================================

LOG = logging.getLogger("yuanbao_mobile")


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


_device_serial = None  # 进程级缓存，只取一次


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

    # 优先匹配配置的端口（"5554" in serial 同时命中 emulator-5554 和 127.0.0.1:5554）
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
# UIAutomator 工具
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
        })
    return result


_SCREEN_SIZE_CACHE: Optional[tuple] = None


def get_screen_size(d) -> tuple:
    global _SCREEN_SIZE_CACHE
    if _SCREEN_SIZE_CACHE is not None:
        return _SCREEN_SIZE_CACHE
    try:
        data = adb_screenshot_bytes()
        img = Image.open(BytesIO(data))
        _SCREEN_SIZE_CACHE = (img.width, img.height)
        log(f"屏幕尺寸（截图实测）：{_SCREEN_SIZE_CACHE[0]}x{_SCREEN_SIZE_CACHE[1]}")
        return _SCREEN_SIZE_CACHE
    except Exception as exc:
        LOG.debug("截图测尺寸失败：%s", exc)
    try:
        result = run_adb("shell", "wm", "size", timeout=10)
        text = result.stdout.decode("utf-8", errors="ignore")
        m = re.search(r"(?:Physical|Override) size:\s*(\d+)\s*x\s*(\d+)", text)
        if m:
            _SCREEN_SIZE_CACHE = (int(m.group(1)), int(m.group(2)))
            log(f"屏幕尺寸（wm size）：{_SCREEN_SIZE_CACHE[0]}x{_SCREEN_SIZE_CACHE[1]}")
            return _SCREEN_SIZE_CACHE
    except Exception as exc:
        LOG.debug("wm size 失败：%s", exc)
    try:
        info = d.info
        _SCREEN_SIZE_CACHE = (int(info["displayWidth"]), int(info["displayHeight"]))
        log(f"屏幕尺寸（d.info）：{_SCREEN_SIZE_CACHE[0]}x{_SCREEN_SIZE_CACHE[1]}")
        return _SCREEN_SIZE_CACHE
    except Exception:
        _SCREEN_SIZE_CACHE = (1080, 1920)
        return _SCREEN_SIZE_CACHE


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


# ============================================================
# 元宝 App 生命周期
# ============================================================

def resolve_yuanbao_package(d) -> str:
    try:
        cur = d.app_current().get("package") or ""
        for pkg in YUANBAO_PACKAGE_CANDIDATES:
            if cur == pkg:
                return pkg
    except Exception:
        pass
    for pkg in YUANBAO_PACKAGE_CANDIDATES:
        try:
            d.app_start(pkg, stop=False)
            time.sleep(2.5)
            cur = d.app_current().get("package")
            if cur == pkg:
                log(f"元宝包名确认：{pkg}")
                return pkg
        except Exception as exc:
            LOG.debug("尝试启动 %s 失败：%s", pkg, exc)
    raise RuntimeError(
        "无法启动腾讯元宝 App，请确认已安装且包名在 "
        f"{YUANBAO_PACKAGE_CANDIDATES} 中。"
    )


def start_yuanbao(d) -> str:
    log("启动腾讯元宝 Android App")
    pkg = resolve_yuanbao_package(d)
    time.sleep(2.0)
    try:
        if d.app_current().get("package") != pkg:
            d.app_start(pkg, stop=False)
            time.sleep(2.5)
        if d.app_current().get("package") != pkg:
            raise RuntimeError("元宝没有进入前台")
    except Exception as exc:
        raise RuntimeError(f"无法确认元宝前台状态：{exc}")
    return pkg


# ============================================================
# 抽屉 / 新建对话
# ============================================================

def _drawer_is_open(d) -> bool:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    for n in nodes:
        value = n["text"] or n["desc"]
        if not value:
            continue
        if not any(kw in value for kw in DRAWER_KEYWORDS):
            continue
        b = bounds_to_tuple(n["bounds"])
        if b and b[0] < w * 0.30 and b[3] < h * 0.40:
            return True
    return False


def _open_left_drawer(d) -> bool:
    if _drawer_is_open(d):
        return True
    for kw in ("菜单", "Menu", "menu", "打开菜单", "更多", "导航", "侧边栏"):
        try:
            obj = d(descriptionContains=kw)
            if obj.exists(timeout=0.5):
                obj.click()
                time.sleep(1.2)
                if _drawer_is_open(d):
                    return True
        except Exception:
            pass
    try:
        w, h = get_screen_size(d)
        for x_ratio in (0.055, 0.045, 0.065, 0.075, 0.085):
            for y_ratio in (0.035, 0.05, 0.06, 0.07):
                try:
                    d.click(int(w * x_ratio), int(h * y_ratio))
                    time.sleep(0.9)
                    if _drawer_is_open(d):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    try:
        w, h = get_screen_size(d)
        d.swipe(int(w * 0.02), int(h * 0.5), int(w * 0.55), int(h * 0.5), duration=0.3)
        time.sleep(1.0)
        if _drawer_is_open(d):
            return True
    except Exception:
        pass
    return False


def _is_in_new_chat(d) -> bool:
    if _drawer_is_open(d):
        return False
    if find_input(d) is None:
        return False
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return True
    total_len = 0
    for n in nodes:
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] < int(h * 0.08) or b[3] > int(h * 0.78):
            continue
        cls = n["class"].lower()
        if "textview" not in cls:
            continue
        value = n["text"] or ""
        if any(kw in value for kw in ("你好", "元宝", "有什么", "可以帮你", "找我聊")):
            continue
        if len(value) < 3:
            continue
        total_len += len(value)
    return total_len < 120


def new_chat(d) -> bool:
    log("创建新对话（元宝）")
    if not _open_left_drawer(d):
        log("⚠ 打开左侧抽屉失败（继续尝试直接找新建对话）")
    time.sleep(0.6)

    clicked = False
    for word in NEW_CHAT_WORDS:
        for selector in (
            {"text": word},
            {"textContains": word},
            {"descriptionContains": word},
        ):
            try:
                obj = d(**selector)
                if obj.exists(timeout=0.6):
                    obj.click()
                    clicked = True
                    log(f"✓ 点击了'{word}'（选择器：{selector}）")
                    break
            except Exception:
                pass
        if clicked:
            break

    if not clicked:
        try:
            w, _h = get_screen_size(d)
            nodes = parse_nodes(dump_xml(d))
            cands = []
            for n in nodes:
                value = n["text"] or n["desc"]
                if not value:
                    continue
                if not any(kw in value for kw in ("新建对话", "新对话", "创建新对话")):
                    continue
                b = bounds_to_tuple(n["bounds"])
                if not b:
                    continue
                if b[0] > w * 0.55:
                    continue
                cands.append((b[1], n))
            cands.sort(key=lambda x: x[0])
            for _, node in cands:
                if click_node(d, node):
                    clicked = True
                    log(f"✓ XML 兜底点击：{node['text'] or node['desc']}")
                    break
        except Exception:
            pass

    if not clicked:
        log("✗ 未能在抽屉里找到'新建对话'")
        try:
            d.press("back")
            time.sleep(0.8)
        except Exception:
            pass
        return False

    time.sleep(2.2)
    if _drawer_is_open(d):
        log("提示：点击后抽屉仍在，尝试按 back 关闭")
        try:
            d.press("back")
            time.sleep(1.2)
        except Exception:
            pass
    if _is_in_new_chat(d):
        log("✓ 已进入新对话")
        return True
    log("已点击'新建对话'，但未能完全确认新会话状态（乐观继续）")
    return True


# ============================================================
# 开启深度思考
# ============================================================

def _is_deep_think_selected(nodes: list) -> bool:
    for n in nodes:
        value = (n["text"] or n["desc"] or "").strip()
        if value not in DEEP_THINK_MENU_ITEMS:
            continue
        if n.get("selected") or n.get("checked"):
            return True
    return False


def enable_deep_thinking(d) -> bool:
    log("尝试开启深度思考模式")

    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("开启深度思考失败（dump）：%s", exc)
        return False

    header_btn = None
    for n in nodes:
        value = (n["text"] or n["desc"] or "").strip()
        if value not in DEEP_THINK_BUTTON_TEXTS:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] > h * 0.14:
            continue
        if b[3] < 20:
            continue
        if header_btn is None or b[1] < header_btn["y"]:
            header_btn = {"node": n, "y": b[1]}

    if not header_btn:
        log("⚠ 未找到头部的'深度思考'按钮")
        return False

    log(f"找到头部'深度思考'按钮：bounds={header_btn['node']['bounds']}")

    if not click_node(d, header_btn["node"]):
        log("⚠ 点击头部'深度思考'按钮失败")
        return False
    time.sleep(1.4)

    try:
        nodes2 = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("开启深度思考失败（二次 dump）：%s", exc)
        return False

    menu_opt = None
    for n in nodes2:
        value = (n["text"] or n["desc"] or "").strip()
        if value not in DEEP_THINK_MENU_ITEMS:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] < h * 0.06 or b[1] > h * 0.42:
            continue
        score = 0
        if n["clickable"]:
            score -= 100
        if n.get("selected") or n.get("checked"):
            score -= 50
        if menu_opt is None or score < menu_opt["score"] or (
            score == menu_opt["score"] and b[1] < menu_opt["y"]
        ):
            menu_opt = {"node": n, "y": b[1], "score": score}

    if not menu_opt:
        log("⚠ 下拉菜单中未找到'深度思考'选项，关闭菜单")
        try:
            d.press("back")
            time.sleep(0.8)
        except Exception:
            pass
        return False

    log(f"找到'深度思考'菜单项：bounds={menu_opt['node']['bounds']} "
        f"clickable={menu_opt['node']['clickable']}")

    if not click_node(d, menu_opt["node"]):
        log("⚠ 点击'深度思考'菜单项失败")
        return False
    time.sleep(1.6)

    log("✓ 深度思考模式已开启")
    return True


# ============================================================
# 输入框 / 发送按钮
# ============================================================

def find_input(d):
    for kwargs in (
        {"className": "android.widget.EditText"},
        {"textContains": "发消息或按住说话"},
        {"descriptionContains": "发消息或按住说话"},
        {"textContains": "发消息"},
        {"descriptionContains": "发消息"},
        {"textContains": "按住说话"},
        {"descriptionContains": "按住说话"},
        {"textContains": "向元宝提问"},
        {"textContains": "Ask anything"},
    ):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=0.5):
                return obj
        except Exception:
            pass
    try:
        nodes = parse_nodes(dump_xml(d))
        h = get_screen_size(d)[1]
        for n in nodes:
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            cls = n["class"].lower()
            value = (n["text"] or n["desc"]).lower()
            is_edit = "edittext" in cls
            is_hint = any(hint.lower() in value for hint in INPUT_HINTS)
            if (is_edit or is_hint) and b[1] >= int(h * 0.78):
                obj = d(className="android.widget.EditText")
                if obj.exists(timeout=0.3):
                    return obj
    except Exception:
        pass
    return None


def _find_input_bounds(d) -> Optional[tuple]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None
    best = None
    best_score = -1
    for n in nodes:
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        x1, y1, x2, y2 = b
        if y1 < int(h * 0.78):
            continue
        cls = n["class"].lower()
        is_edit = "edittext" in cls
        rid = n["rid"].lower()
        value = (n["text"] or n["desc"]).lower()
        is_hint = any(hint.lower() in value for hint in INPUT_HINTS)
        rid_input = any(k in rid for k in
                        ("input", "composer", "edit_message", "et_message", "chat_edit"))
        if not (is_edit or is_hint or rid_input):
            continue
        width = x2 - x1
        score = y2 * 1000 + width
        if score > best_score:
            best_score = score
            best = b
    return best


def _message_sent(d, question: str = "") -> bool:
    if _input_cleared(d):
        return True
    try:
        xml = dump_xml(d)
    except Exception:
        return False
    if xml_contains_generating(xml):
        return True
    if question:
        try:
            nodes = parse_nodes(xml)
            h_screen = get_screen_size(d)[1]
            prefix = question.strip()[:15]
            for n in nodes:
                value = n["text"] or ""
                if len(value) < 6:
                    continue
                if prefix and prefix in value:
                    b = bounds_to_tuple(n["bounds"])
                    if b and b[1] > h_screen * 0.08 and b[3] < h_screen * 0.75:
                        return True
        except Exception:
            pass
    return False


def _input_cleared(d) -> bool:
    try:
        h = get_screen_size(d)[1]
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return True
    edit_nodes = []
    for n in nodes:
        cls = n["class"].lower()
        b = bounds_to_tuple(n["bounds"])
        if not b or "edittext" not in cls:
            continue
        if b[3] < int(h * 0.78):
            continue
        edit_nodes.append((b[3], n, b))
    if not edit_nodes:
        return True
    edit_nodes.sort(key=lambda x: -x[0])
    _, node, _ = edit_nodes[0]
    value = (node["text"] or "").strip()
    if not value:
        return True
    if any(hint in value for hint in INPUT_HINTS):
        return True
    if len(value) > 2:
        return False
    return True


def find_send_button(d) -> Optional[dict]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    EXCLUDE_KW = (
        "voice", "microphone", "record", "语音", "录音", "说话",
        "camera", "拍照", "photo", "album", "相册", "图片",
        "plus", "add", "加号", "添加", "更多",
        "menu", "菜单", "hamburger", "navigation",
        "back", "返回", "close", "关闭", "cancel", "取消",
        "深度思考", "生图", "拍题", "视频", "作业",
        "emoji", "表情", "贴纸", "sticker",
        "文件", "file", "上传", "upload",
        "新对话", "新建对话",
        "回到底部", "向下", "down",
        "引用源", "引用来源",
    )

    candidates = []
    for n in nodes:
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if not n["enabled"] or not n["visible"] or not n["clickable"]:
            continue
        x1, y1, x2, y2 = b
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        bw = x2 - x1
        bh = y2 - y1
        if cy < int(h * 0.85):
            continue
        if cx < int(w * 0.82):
            continue
        if bw < 40 or bw > 130:
            continue
        if bh < 40 or bh > 130:
            continue
        if abs(bw - bh) > 35:
            continue
        combined = (
            (n["text"] or "") + " " + (n["desc"] or "") + " " + n["rid"]
        ).lower()
        if any(kw in combined for kw in EXCLUDE_KW):
            continue
        score = cy * 100 + cx
        candidates.append((score, n))

    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[0])
    return candidates[0][1]


def _click_send_by_coords(d, question: str = "") -> bool:
    try:
        w, h = get_screen_size(d)
    except Exception:
        w, h = 1080, 1920
    input_bounds = _find_input_bounds(d)
    candidates = []
    if input_bounds:
        _x1, y1, x2, y2 = input_bounds
        cy = (y1 + y2) // 2
        for dx in (30, 40, 50, 60, 20, 70):
            candidates.append((min(w - 5, x2 + dx), cy))
    for xr, yr in (
        (0.955, 0.862), (0.945, 0.865), (0.965, 0.860),
        (0.955, 0.870), (0.950, 0.855), (0.960, 0.865),
        (0.940, 0.860), (0.950, 0.875),
    ):
        candidates.append((int(w * xr), int(h * yr)))

    tried = set()
    for x, y in candidates:
        key = (x // 5, y // 5)
        if key in tried:
            continue
        tried.add(key)
        try:
            log(f"尝试点击发送坐标：({x}, {y})")
            d.click(x, y)
            time.sleep(1.2)
            if _message_sent(d, question):
                return True
        except Exception as exc:
            LOG.debug("坐标点击失败 %s,%s: %s", x, y, exc)
    return False


def input_text(d, question: str) -> None:
    edit = find_input(d)
    if edit is None:
        raise RuntimeError("找不到元宝输入框")
    log(f"输入问题：{question[:100]}")
    try:
        edit.click()
    except Exception:
        pass
    time.sleep(0.3)
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


def send_message(d, question: str = "") -> bool:
    log("发送问题")
    btn = find_send_button(d)
    if btn:
        log(f"找到发送按钮：bounds={btn['bounds']} rid={btn['rid'] or '-'} "
            f"text={(btn['text'] or '')[:20]} desc={(btn['desc'] or '')[:20]}")
        if click_node(d, btn):
            time.sleep(1.5)
            if _message_sent(d, question):
                log("✓ 发送成功（XML 精确定位）")
                return True
            log("点击后消息未发送，继续尝试其他方式")
    else:
        log("未在 XML 中找到发送按钮，进入兜底流程")

    for word in SEND_WORDS:
        for selector in ({"text": word}, {"description": word},
                         {"textContains": word}):
            try:
                obj = d(**selector)
                if obj.exists(timeout=0.4):
                    obj.click()
                    time.sleep(1.5)
                    if _message_sent(d, question):
                        log(f"✓ 发送成功（{selector}）")
                        return True
            except Exception:
                pass

    log("尝试坐标兜底点击发送按钮")
    if _click_send_by_coords(d, question):
        log("✓ 发送成功（坐标兜底）")
        return True

    log("尝试 Enter 兜底")
    try:
        d.press("enter")
        time.sleep(1.5)
        if _message_sent(d, question):
            log("✓ 发送成功（Enter）")
            return True
    except Exception:
        pass

    log("✗ 所有发送方式均未成功")
    return False


# ============================================================
# Composer / 回答完成
# ============================================================

def inspect_composer_state(d) -> dict:
    result = {
        "ready": False, "score": 0,
        "input_found": False, "input_enabled": False,
        "input_bottom": False, "hint_found": False,
        "stop_control": False, "send_control": False,
        "input_text": "",
    }
    try:
        h = get_screen_size(d)[1]
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return result

    for n in nodes:
        value = clean_text(n["text"] or n["desc"])
        low = value.lower()
        rid = n["rid"].lower()
        cls = n["class"].lower()
        b = bounds_to_tuple(n["bounds"])
        hint_hit = any(hint.lower() in low for hint in INPUT_HINTS)
        is_edit = "edittext" in cls
        rid_input = any(k in rid for k in ("input", "message", "composer", "prompt"))

        if (is_edit or hint_hit or rid_input) and b and b[3] >= int(h * 0.75):
            result["input_found"] = True
            result["input_bottom"] = True
            result["input_enabled"] = result["input_enabled"] or n["enabled"]
            if hint_hit:
                result["hint_found"] = True
            if value:
                result["input_text"] = value

        if any(word.lower() in low for word in
               ("停止生成", "停止回答", "stop generating")):
            if n["enabled"] and (n["clickable"] or b):
                result["stop_control"] = True

        if value in SEND_WORDS or "send" in rid:
            if b and b[1] >= int(h * 0.75):
                result["send_control"] = True

    if result["input_found"]:  result["score"] += 2
    if result["input_bottom"]: result["score"] += 1
    if result["input_enabled"]: result["score"] += 2
    if result["hint_found"]:   result["score"] += 2
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
        if len(t) < 4:
            continue
        if THINKING_ENTRY_PATTERN.search(t):
            continue
        valid.append(t)
    if not valid:
        return ""
    return max(valid, key=len)


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
    }


def wait_answer_finish(d) -> str:
    log("等待元宝回答完成（三重硬校验）")
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
            return answer
    if saw_answer and len(last_answer) >= ANSWER_MIN_COMPLETE_LENGTH:
        log(f"✓ 超时兜底接受 last_answer：len={len(last_answer)}")
        return last_answer
    raise RuntimeError(
        f"等待回答超时：最长 {len(last_answer)} 字符，"
        f"未达 {ANSWER_MIN_COMPLETE_LENGTH} 门槛"
    )


# ============================================================
# 图像签名 / 容差比较
# ============================================================

def image_signature(img: Image.Image) -> bytes:
    small = img.convert("L").resize((64, 96))
    if np is not None:
        arr = np.asarray(small, dtype=np.uint8)
        arr = (arr // 32).astype(np.uint8)
        return arr.tobytes()
    return small.tobytes()


def _frames_almost_same(a: bytes, b: bytes, tol: int = 24,
                        ratio: float = 0.05) -> bool:
    if a == b:
        return True
    if not a or not b or len(a) != len(b):
        return False
    diff = 0
    limit = int(len(a) * ratio)
    for x, y in zip(a, b):
        if abs(int(x) - int(y)) > tol:
            diff += 1
            if diff > limit:
                return False
    return True


# ============================================================
# 工具栏识别
# ============================================================

def _find_toolbar_row(nodes: list, w: int, h: int) -> Optional[dict]:
    small_btns = []
    for n in nodes:
        if not n["clickable"] or not n["enabled"] or not n["visible"]:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        x1, y1, x2, y2 = b
        bw, bh = x2 - x1, y2 - y1
        if bw < 35 or bw > 130:
            continue
        if bh < 35 or bh > 130:
            continue
        if abs(bw - bh) > 40:
            continue
        if y1 < h * 0.15 or y2 > h * 0.88:
            continue
        text = ((n["text"] or "") + " " + (n["desc"] or "")).lower()
        rid = (n["rid"] or "").lower()
        EXCLUDE = ("send", "voice", "camera", "input", "edit", "menu",
                   "back", "close", "plus", "add", "new_chat", "newchat",
                   "发消息", "按住说话", "相机", "相册", "语音")
        if any(k in text or k in rid for k in EXCLUDE):
            continue
        small_btns.append(n)

    if len(small_btns) < 3:
        return None

    def _cy(n):
        b = bounds_to_tuple(n["bounds"])
        return (b[1] + b[3]) // 2

    small_btns.sort(key=_cy)
    rows = []
    cur = [small_btns[0]]
    for n in small_btns[1:]:
        if _cy(n) - _cy(cur[-1]) <= 30:
            cur.append(n)
        else:
            if len(cur) >= 3:
                rows.append(cur)
            cur = [n]
    if len(cur) >= 3:
        rows.append(cur)

    if not rows:
        return None
    rows.sort(key=lambda row: max(_cy(n) for n in row), reverse=True)
    toolbar_nodes = rows[0]

    y1 = min(bounds_to_tuple(n["bounds"])[1] for n in toolbar_nodes)
    y2 = max(bounds_to_tuple(n["bounds"])[3] for n in toolbar_nodes)
    right_btn = max(toolbar_nodes, key=lambda n: bounds_to_tuple(n["bounds"])[2])
    return {"y1": y1, "y2": y2, "nodes": toolbar_nodes, "right_btn": right_btn}


# ============================================================
# 消息视窗
# ============================================================

def get_message_viewport(d) -> tuple:
    w, h = get_screen_size(d)
    left, top = 0, int(h * 0.06)
    right, bottom = w, int(h * 0.86)

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
                top = max(top, min(y1, int(h * 0.12)))
                right = min(right, x2)
                bottom = min(bottom, max(y2, int(h * 0.75)))

    input_top_candidates = []
    for node in nodes:
        b = bounds_to_tuple(node["bounds"])
        if not b:
            continue
        y1 = b[1]
        if y1 < h * 0.78:
            continue
        cls = (node["class"] or "").lower()
        value = (node["text"] or node["desc"]) or ""
        rid = (node["rid"] or "").lower()
        is_edit = "edittext" in cls
        is_hint = any(hint.lower() in value.lower() for hint in INPUT_HINTS)
        rid_input = any(k in rid for k in
                        ("input", "composer", "edit_message", "et_message", "chat_edit"))
        if is_edit or is_hint or rid_input:
            input_top_candidates.append(y1)

    if input_top_candidates:
        input_top = min(input_top_candidates)
        candidate_bottom = input_top - 12
        if candidate_bottom > top + int(h * 0.45):
            bottom = min(bottom, candidate_bottom)

    for node in nodes:
        value = (node["text"] or "") or ""
        if value in ("上一条", "发现", "我们", "首页", "我的", "消息", "问元宝"):
            b = bounds_to_tuple(node["bounds"])
            if b and b[1] > h * 0.88:
                candidate_bottom = b[1] - 5
                if candidate_bottom > top + int(h * 0.45):
                    bottom = min(bottom, candidate_bottom)

    min_height = int(h * 0.45)
    if bottom - top < min_height:
        bottom = min(h, top + min_height)
    return (int(left), int(top), int(right), int(bottom))


# ============================================================
# 截图
# ============================================================

def save_message_area(d, path: Path, viewport: Optional[tuple] = None) -> Image.Image:
    data = adb_screenshot_bytes()
    full = Image.open(BytesIO(data)).convert("RGB")
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
                obj.click()
                time.sleep(0.5)
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
        _swipe_in_viewport(d, "down", fraction=SCROLL_SWIPE_FRACTION,
                           duration=0.4, viewport=viewport)
        time.sleep(0.4)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_top_probe.png", viewport)
            sig = image_signature(img)
            if last_sig is not None and _frames_almost_same(sig, last_sig):
                stable += 1
                if stable >= SCROLL_STABLE_ROUNDS:
                    log("✓ 已到达聊天顶部")
                    return True
            else:
                stable = 0
            last_sig = sig
        except Exception as exc:
            LOG.debug("顶部探测失败：%s", exc)
    log("顶部定位达到最大次数")
    return True


def scroll_to_bottom(d) -> bool:
    log("滚回聊天底部")
    for _ in range(2):
        if click_bottom_button(d):
            time.sleep(0.5)
            return True

    viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_BOTTOM_SWIPES):
        _swipe_in_viewport(d, "up", fraction=SCROLL_SWIPE_FRACTION,
                           duration=0.4, viewport=viewport)
        time.sleep(0.4)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_bottom_probe.png", viewport)
            sig = image_signature(img)
            if last_sig is not None and _frames_almost_same(sig, last_sig):
                stable += 1
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


# ============================================================
# 长截图拼接
# ============================================================

def _row_features(img: Image.Image, samples: int = 32) -> "np.ndarray":
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = arr.shape
    if w <= samples:
        return arr
    step = w // samples
    cols = np.arange(0, w, step)[:samples]
    return arr[:, cols]


def _prepare_stitch_features(img: Image.Image, sample_width: int = 96):
    if np is None:
        raise RuntimeError("长截图需要 numpy：pip install numpy")
    gray = img.convert("L")
    h = gray.height
    width = min(sample_width, gray.width)
    small = gray.resize((width, h), Image.Resampling.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    if arr.shape[1] > 16:
        arr = arr[:, 8:-8]
    edge = np.abs(np.diff(arr, axis=1))
    return arr, edge


def _overlap_score(up_gray, up_edge, lo_gray, lo_edge, overlap: int) -> float:
    if overlap <= 0:
        return float("inf")
    a = up_gray[-overlap:]
    b = lo_gray[:overlap]
    ae = up_edge[-overlap:]
    be = lo_edge[:overlap]
    if a.shape != b.shape:
        return float("inf")
    gray_mae = float(np.mean(np.abs(a - b))) / 255.0
    edge_mae = float(np.mean(np.abs(ae - be))) / 255.0
    row_a = np.mean(a, axis=1)
    row_b = np.mean(b, axis=1)
    row_mae = float(np.mean(np.abs(row_a - row_b))) / 255.0
    return gray_mae * 0.55 + edge_mae * 0.30 + row_mae * 0.15


def _find_vertical_overlap_precise(upper: Image.Image,
                                   lower: Image.Image,
                                   min_ratio: float = PANEL_STITCH_SEARCH_MIN_RATIO,
                                   max_ratio: float = PANEL_STITCH_SEARCH_MAX_RATIO) -> tuple:
    if np is None:
        raise RuntimeError("长截图需要 numpy：pip install numpy")

    if upper.size != lower.size:
        width = min(upper.width, lower.width)
        upper = upper.crop((0, 0, width, upper.height))
        lower = lower.crop((0, 0, width, lower.height))

    h = min(upper.height, lower.height)
    if h < 80:
        return max(5, h // 4), float("inf")

    up_gray, up_edge = _prepare_stitch_features(upper)
    lo_gray, lo_edge = _prepare_stitch_features(lower)

    min_ov = max(20, int(h * min_ratio))
    max_ov = min(h - 20, int(h * max_ratio))
    if max_ov <= min_ov:
        return max(5, min(h - 1, h // 3)), float("inf")

    candidates = list(range(min_ov, max_ov + 1, PANEL_STITCH_COARSE_STEP))
    if candidates[-1] != max_ov:
        candidates.append(max_ov)

    best_ov = candidates[0]
    best_score = float("inf")
    for ov in candidates:
        score = _overlap_score(up_gray, up_edge, lo_gray, lo_edge, ov)
        if score < best_score:
            best_score = score
            best_ov = ov

    refine_lo = max(min_ov, best_ov - PANEL_STITCH_REFINE_RADIUS)
    refine_hi = min(max_ov, best_ov + PANEL_STITCH_REFINE_RADIUS)
    for ov in range(refine_lo, refine_hi + 1):
        score = _overlap_score(up_gray, up_edge, lo_gray, lo_edge, ov)
        if score < best_score:
            best_score = score
            best_ov = ov

    return int(best_ov), float(best_score)


def find_overlap(upper: Image.Image, lower: Image.Image) -> int:
    overlap, _ = _find_vertical_overlap_precise(upper, lower)
    return overlap


def _images_almost_same(a: Image.Image, b: Image.Image, threshold: float = 0.97) -> bool:
    if a.size != b.size:
        return False
    if np is None:
        return a.tobytes() == b.tobytes()
    a_small = a.convert("L").resize((96, 96), Image.Resampling.BILINEAR)
    b_small = b.convert("L").resize((96, 96), Image.Resampling.BILINEAR)
    arr_a = np.asarray(a_small, dtype=np.float32)
    arr_b = np.asarray(b_small, dtype=np.float32)
    diff = float(np.mean(np.abs(arr_a - arr_b))) / 255.0
    return (1.0 - diff) >= threshold


def _estimate_overlap_from_text(prev_items: Optional[list],
                                curr_items: Optional[list],
                                frame_height: int) -> Optional[tuple]:
    if not prev_items or not curr_items or frame_height < 100:
        return None

    prev_map = {}
    for item in prev_items:
        fp = item.get("fp")
        y = item.get("y")
        if not fp or y is None:
            continue
        prev_map.setdefault(fp, []).append(int(y))

    deltas = []
    for item in curr_items:
        fp = item.get("fp")
        cy = item.get("y")
        if not fp or cy is None or fp not in prev_map:
            continue
        for py in prev_map[fp][:5]:
            dy = py - int(cy)
            if frame_height * 0.04 <= dy <= frame_height * 0.92:
                deltas.append(dy)

    if not deltas:
        return None

    arr = np.asarray(deltas, dtype=np.float32) if np is not None else deltas
    if np is not None:
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median)))
    else:
        s = sorted(deltas)
        median = float(s[len(s) // 2])
        mad = 0.0

    if median <= 0 or median >= frame_height - 20:
        return None

    overlap = int(round(frame_height - median))
    overlap = max(20, min(frame_height - 20, overlap))
    confidence = max(0.0, min(1.0, 1.0 - mad / 30.0))
    return overlap, confidence


def _stitch_pair(upper: Image.Image,
                 lower: Image.Image,
                 prev_items: Optional[list] = None,
                 curr_items: Optional[list] = None) -> tuple:
    if upper.size != lower.size:
        width = min(upper.width, lower.width)
        upper = upper.crop((0, 0, width, upper.height))
        lower = lower.crop((0, 0, width, lower.height))

    if _images_almost_same(upper, lower, threshold=0.995):
        return upper.copy(), upper.height, "same", 0.0

    text_est = _estimate_overlap_from_text(prev_items, curr_items, upper.height)
    if text_est is not None:
        overlap, confidence = text_est
        if confidence >= 0.35:
            new_part = lower.crop((0, overlap, lower.width, lower.height))
            if new_part.height > 0:
                merged = Image.new("RGB", (upper.width, upper.height + new_part.height))
                merged.paste(upper, (0, 0))
                merged.paste(new_part, (0, upper.height))
                return merged, overlap, "text-anchor", confidence

    overlap, score = _find_vertical_overlap_precise(upper, lower)
    overlap = max(20, min(overlap, lower.height - 1))
    new_part = lower.crop((0, overlap, lower.width, lower.height))
    if new_part.height <= 0:
        return upper.copy(), overlap, "pixel", score

    merged = Image.new("RGB", (upper.width, upper.height + new_part.height))
    merged.paste(upper, (0, 0))
    merged.paste(new_part, (0, upper.height))
    return merged, overlap, "pixel", score


def stitch_frames(frames: list,
                  output: Path,
                  frame_items: Optional[list] = None) -> Image.Image:
    if not frames:
        raise RuntimeError("没有可拼接的截图帧")

    normalized = []
    for img in frames:
        if img is None:
            continue
        normalized.append(img.convert("RGB"))
    if not normalized:
        raise RuntimeError("没有有效截图帧")

    if frame_items is None:
        frame_items = [None] * len(normalized)
    elif len(frame_items) < len(normalized):
        frame_items = list(frame_items) + [None] * (len(normalized) - len(frame_items))

    canvas = normalized[0].copy()
    prev_frame = normalized[0]
    prev_items = frame_items[0]
    used_frames = 1

    for idx in range(1, len(normalized)):
        curr = normalized[idx]
        curr_items = frame_items[idx]

        if _images_almost_same(prev_frame, curr, threshold=0.995):
            log(f"  拼接第 {idx+1} 帧：与上一帧相同，跳过")
            prev_frame = curr
            prev_items = curr_items
            continue

        _pair_image, overlap, method, score = _stitch_pair(
            prev_frame, curr, prev_items=prev_items, curr_items=curr_items
        )
        overlap = max(20, min(overlap, curr.height - 1))
        new_part = curr.crop((0, overlap, curr.width, curr.height))
        if new_part.height <= 0:
            prev_frame = curr
            prev_items = curr_items
            continue

        merged_canvas = Image.new("RGB", (canvas.width, canvas.height + new_part.height))
        merged_canvas.paste(canvas, (0, 0))
        merged_canvas.paste(new_part, (0, canvas.height))
        canvas = merged_canvas
        used_frames += 1

        if method == "text-anchor":
            log(f"  拼接第 {idx+1} 帧：文本锚点 overlap={overlap}px confidence={score:.2f}")
        elif method == "pixel":
            log(f"  拼接第 {idx+1} 帧：像素匹配 overlap={overlap}px score={score:.4f}")
        else:
            log(f"  拼接第 {idx+1} 帧：重复帧跳过")

        prev_frame = curr
        prev_items = curr_items

    canvas.save(output, format="PNG", optimize=False)
    log(f"✓ 长截图拼接完成：有效帧={used_frames}/{len(normalized)}，最终尺寸={canvas.size}")
    return canvas


def _cut_head_from_long_image(long_path: Path, cut_height: int) -> bool:
    try:
        img = Image.open(long_path).convert("RGB")
        if cut_height <= 0 or cut_height >= img.height - 50:
            return False
        cropped = img.crop((0, cut_height, img.width, img.height))
        cropped.save(long_path, format="PNG")
        log(f"从长图顶部裁剪 {cut_height}px（新高度={cropped.height}）")
        return True
    except Exception as exc:
        LOG.debug("从长图顶部裁剪失败：%s", exc)
        return False


def _cut_tail_from_long_image(long_path: Path, cut_height: int,
                               margin: int = 8) -> bool:
    try:
        img = Image.open(long_path).convert("RGB")
        img_h = img.height
        cut_y = img_h - max(0, cut_height - margin)
        if cut_y <= 10 or cut_y >= img_h:
            return False
        log(f"从长图底部裁剪 {img_h - cut_y}px（原高={img_h}, 裁到 y={cut_y}）")
        cropped = img.crop((0, 0, img.width, cut_y))
        cropped.save(long_path, format="PNG")
        return True
    except Exception as exc:
        LOG.debug("裁剪长图失败：%s", exc)
        return False


# ============================================================
# 文本清理 / 过滤 / 采集
# ============================================================

def _clean_special_chars(text: str) -> str:
    if not text:
        return ""
    keep = []
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            continue
        if 0xE000 <= cp <= 0xF8FF:
            continue
        if 0x1F000 <= cp <= 0x1FFFF:
            continue
        if 0x2600 <= cp <= 0x27BF:
            continue
        if 0x2B00 <= cp <= 0x2BFF:
            continue
        if 0x2460 <= cp <= 0x24FF:
            continue
        if cp in (0xFE0F, 0x200D):
            continue
        keep.append(ch)
    result = "".join(keep)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{2,}", "\n", result)
    return result.strip()


def _normalize_text_for_dedup(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\u3000", " ")
    t = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", t)
    return t


def _text_fingerprint(text: str) -> str:
    n = _normalize_text_for_dedup(text)
    if not n:
        return ""
    if len(n) <= 100:
        return n
    return n[:100] + f"|{len(n)}"


def _is_card_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if DURATION_RE.match(t):
        return True
    if len(t) < 6 and not re.search(r"[，。！？、；：,.!?;:…]", t):
        return True
    return False


def _is_system_notification(text: str) -> bool:
    """★ v13：判断文本是否为手机系统通知（时间、信号、充电、蓝牙等）。"""
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    for pat in SYSTEM_NOISE_PATTERNS:
        if pat.search(t):
            return True
    return False


def _is_noise_text(text: str, question: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if not t:
        return True

    # ★ v13：优先过滤系统通知
    if _is_system_notification(t):
        return True

    cleaned = _clean_special_chars(t)
    if not cleaned:
        return True
    if len(t) < 20 and t in IGNORE_EXACT:
        return True
    for sub in IGNORE_SUBSTR:
        if sub in t and len(t) < 200:
            return True
    if question:
        q = question.strip()
        if t == q:
            return True
        if len(t) <= len(q) + 15 and q in t:
            return True
    if REFERENCE_ENTRY_RE.match(t):
        return True
    if re.match(r"^(?:引用源|引用来源|参考了)\s*\d*\s*(?:篇(?:资料|网页))?\s*$", t):
        return True
    if len(cleaned) < 2:
        return True
    if re.match(r"^[\s\d\W]+$", cleaned) and len(cleaned) < 10:
        return True
    return False


def _looks_like_body(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if len(t) < BODY_MIN_LEN:
        return False
    if t in IGNORE_EXACT:
        return False
    if t in TAIL_NOISE_EXACT:
        return False
    if any(sub in t for sub in IGNORE_SUBSTR):
        return False
    if t[-1] in BODY_END_PUNCT_CN:
        return True
    if t[-1] in BODY_END_PUNCT_EN:
        return True
    if re.match(r"^[-•*]\s+\S.{10,}", t):
        return True
    if re.match(r"^\d+[.、)]\s*\S.{10,}", t):
        return True
    return False


def _trim_tail_to_body(texts: list) -> list:
    if not texts:
        return texts
    for i in range(len(texts) - 1, -1, -1):
        if _looks_like_body(texts[i]):
            return texts[:i + 1]
    return texts


def _filter_substring_dupes(texts: list) -> list:
    if not texts:
        return []
    seen_fps = set()
    unique = []
    for t in texts:
        fp = _text_fingerprint(t)
        if not fp or fp in seen_fps:
            continue
        seen_fps.add(fp)
        unique.append(t)
    normalized = [_normalize_text_for_dedup(t) for t in unique]
    result = []
    for i, t in enumerate(unique):
        ns = normalized[i]
        is_sub = False
        for j, other in enumerate(unique):
            if i == j:
                continue
            no = normalized[j]
            if no == ns:
                continue
            if len(no) > len(ns) + 4 and ns in no:
                is_sub = True
                break
        if not is_sub:
            result.append(t)
    return result


def _join_answer_texts(texts: list) -> str:
    if not texts:
        return ""
    parts = [t.strip() for t in texts if t and t.strip()]
    return "\n\n".join(parts)


def _collect_screen_texts(d, viewport: tuple, question: str, seen_fps: set) -> list:
    items = []
    try:
        xml = dump_xml(d)
        nodes = parse_nodes(xml)
    except Exception as exc:
        LOG.debug("采集文本失败：%s", exc)
        return items
    vt, vb = viewport[1], viewport[3]
    for n in nodes:
        for field in ("text", "desc"):
            text = n.get(field) or ""
            if not text:
                continue
            cleaned = _clean_special_chars(text)
            if not cleaned or len(cleaned) < 2:
                continue
            if _is_noise_text(cleaned, question):
                continue
            if _is_card_noise(cleaned):
                continue
            if THINKING_ENTRY_PATTERN.search(cleaned):
                continue
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            by1, by2 = b[1], b[3]
            is_long = len(cleaned) >= LONG_TEXT_THRESHOLD
            if not is_long:
                if by2 <= vt or by1 >= vb:
                    continue
            fp = _text_fingerprint(cleaned)
            if not fp or fp in seen_fps:
                continue
            seen_fps.add(fp)
            items.append((by1, cleaned))
    items.sort(key=lambda x: x[0])
    return items


# ============================================================
# 深度思考过程采集
# ============================================================

def _find_thinking_entry(d) -> Optional[dict]:
    """查找"已深度思考(用时 N 秒)"入口。返回最靠上的一个。"""
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    h = get_screen_size(d)[1]

    candidates = []
    for n in nodes:
        value = (n["text"] or n["desc"] or "").strip()
        if not value:
            continue
        if THINKING_ENTRY_PATTERN.search(value):
            b = bounds_to_tuple(n["bounds"])
            if b and b[1] > h * 0.04 and b[3] < h * 0.90:
                candidates.append({"node": n, "bounds": b})

    if not candidates:
        return None
    candidates.sort(key=lambda x: x["bounds"][1])
    return candidates[0]


def _is_in_thinking_overlay(d) -> bool:
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    h = get_screen_size(d)[1]
    hit_entry = False
    for n in nodes:
        v = (n["text"] or n["desc"] or "").strip()
        if not v:
            continue
        if THINKING_ENTRY_PATTERN.search(v):
            b = bounds_to_tuple(n["bounds"])
            if b and b[1] < h * 0.20:
                hit_entry = True
                break
    return hit_entry


# ★ v13：查找"找到了 N 篇相关资料"入口
def _find_thinking_refs_toggle(d) -> Optional[dict]:
    """在思考面板内查找"找到了 N 篇相关资料"的可点击入口。"""
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    h = get_screen_size(d)[1]

    candidates = []
    for n in nodes:
        value = (n["text"] or n["desc"] or "").strip()
        if not value:
            continue
        if THINKING_REF_TOGGLE_RE.search(value):
            b = bounds_to_tuple(n["bounds"])
            if b and b[1] > h * 0.04 and b[3] < h * 0.96:
                candidates.append({"node": n, "bounds": b, "text": value})

    if not candidates:
        return None
    candidates.sort(key=lambda x: x["bounds"][1])
    return candidates[0]


# ★ v13：展开"找到了 N 篇相关资料"
def _expand_thinking_refs(d) -> bool:
    """
    在已展开的思考面板内，找到并点击"找到了 N 篇相关资料"，
    把其内部的引用列表展开。
    """
    toggle = _find_thinking_refs_toggle(d)
    if not toggle:
        log("  未找到'找到了 N 篇相关资料'入口")
        return False

    log(f"  找到参考资料入口：{toggle['text'][:40]} bounds={toggle['bounds']}")

    # 如果入口本身已经可点击，就直接点
    if not toggle["node"]["clickable"]:
        # 尝试点击父容器（有时文字节点不可点击，父容器可点击）
        try:
            w, h = get_screen_size(d)
            nodes = parse_nodes(dump_xml(d))
            parent_candidates = []
            for n in nodes:
                if not n["clickable"]:
                    continue
                b = bounds_to_tuple(n["bounds"])
                if not b:
                    continue
                # 找到包含 toggle bounds 的 clickable 父容器
                tb = toggle["bounds"]
                if b[0] <= tb[0] and b[1] <= tb[1] and b[2] >= tb[2] and b[3] >= tb[3]:
                    area = (b[2] - b[0]) * (b[3] - b[1])
                    parent_candidates.append((area, n))
            if parent_candidates:
                parent_candidates.sort(key=lambda x: x[0])
                toggle = {"node": parent_candidates[0][1], "bounds": toggle["bounds"],
                          "text": toggle["text"]}
                log(f"  使用父容器点击：bounds={toggle['node']['bounds']}")
        except Exception as exc:
            LOG.debug("查找父容器失败：%s", exc)

    if not click_node(d, toggle["node"]):
        log("  点击参考资料入口失败")
        return False

    time.sleep(1.8)

    # 校验是否展开：观察是否出现 "1. xxx" 或文章标题类条目
    try:
        nodes = parse_nodes(dump_xml(d))
        has_items = False
        for n in nodes:
            v = (n["text"] or "").strip()
            if re.match(r"^\d+[\.、]\s*\S", v) and len(v) > 10:
                has_items = True
                break
        if has_items:
            log("  ✓ 参考资料列表已展开")
            return True
    except Exception:
        pass

    log("  展开后未检测到列表条目（继续采集，可能无引用）")
    return True


def collect_thinking_content(d, known_answer: str = "") -> str:
    """
    采集深度思考过程。

    步骤：
      1. 滚到顶
      2. 找"已深度思考(用时 N 秒)"入口
      3. 点击展开
      4. ★ v13：在面板内点击"找到了 N 篇相关资料"，展开参考资料列表
      5. 多屏滚动采集（排除系统通知、UI 噪声、已知主回答片段）
      6. 关闭展开视图
      7. 处理成纯文本返回
    """
    log("开始采集深度思考过程")

    # 1. 滚到顶
    scroll_to_top(d)
    time.sleep(1.0)

    # 2. 找入口
    entry = _find_thinking_entry(d)
    if not entry:
        log("⚠ 未找到'已深度思考'入口（可能未开启深度思考）")
        return ""

    entry_text = (entry["node"]["text"] or entry["node"]["desc"]).strip()
    log(f"找到思考入口：{entry_text[:50]} bounds={entry['bounds']}")

    # 3. 点击展开
    if not click_node(d, entry["node"]):
        log("⚠ 点击思考入口失败")
        return ""
    time.sleep(2.6)

    # 4. ★ v13：展开"找到了 N 篇相关资料"
    try:
        _expand_thinking_refs(d)
    except Exception as exc:
        LOG.debug("展开参考资料失败（继续）：%s", exc)
    time.sleep(0.8)

    # 5. 多屏滚动采集
    thinking_items = []
    seen_fps = set()

    answer_snippets = []
    if known_answer:
        for snippet_len in (15, 25, 40):
            if len(known_answer) >= snippet_len:
                answer_snippets.append(known_answer[:snippet_len])

    viewport = get_message_viewport(d)

    for round_idx in range(10):
        try:
            xml = dump_xml(d)
            nodes = parse_nodes(xml)
        except Exception as exc:
            LOG.debug("思考采集 dump 失败：%s", exc)
            break

        round_added = 0
        for n in nodes:
            for field in ("text", "desc"):
                text = n.get(field) or ""
                if not text:
                    continue
                cleaned = _clean_special_chars(text)
                if not cleaned or len(cleaned) < 2:
                    continue
                # 排除思考入口自身
                if THINKING_ENTRY_PATTERN.search(cleaned):
                    continue
                # 排除 UI 噪声（含系统通知）
                if _is_noise_text(cleaned, ""):
                    continue
                if _is_card_noise(cleaned):
                    continue
                # 排除已知主回答片段
                if any(s and s in cleaned for s in answer_snippets):
                    continue

                fp = _text_fingerprint(cleaned)
                if not fp or fp in seen_fps:
                    continue
                seen_fps.add(fp)

                b = bounds_to_tuple(n["bounds"])
                y = b[1] if b else 0
                thinking_items.append((y, cleaned))
                round_added += 1

        log(f"  思考第 {round_idx + 1} 屏：新增 {round_added} 条文本")

        if round_added == 0 and round_idx > 0:
            log("  思考内容采集完成（无新内容）")
            break

        _swipe_in_viewport(d, "up", fraction=0.5,
                          duration=1.0, viewport=viewport)
        time.sleep(1.5)

    # 6. 关闭展开视图
    try:
        if _is_in_thinking_overlay(d):
            log("尝试按 back 关闭思考展开视图")
            d.press("back")
            time.sleep(1.2)
        else:
            try:
                scroll_to_top(d)
                time.sleep(0.6)
                entry2 = _find_thinking_entry(d)
                if entry2:
                    click_node(d, entry2["node"])
                    time.sleep(1.0)
            except Exception:
                pass
    except Exception as exc:
        LOG.debug("关闭思考视图失败：%s", exc)

    # 7. 处理
    text_list = [t for _, t in thinking_items]
    filtered = _filter_substring_dupes(text_list)
    result = _join_answer_texts(filtered)

    log(f"✓ 思考过程采集完成：{len(result)} 字符")
    return result


# ============================================================
# 主问答长截图
# ============================================================

def _measure_user_question_offset(d, viewport: tuple, question: str) -> int:
    if not question:
        return 0
    try:
        vt = viewport[1]
        vb = viewport[3]
        nodes = parse_nodes(dump_xml(d))
        prefix = question.strip()[:20]
        q_bottom = None
        for n in nodes:
            text = n["text"] or ""
            if not text:
                continue
            if prefix and prefix in text:
                b = bounds_to_tuple(n["bounds"])
                if not b:
                    continue
                y1, y2 = b[1], b[3]
                if y1 < vt or y2 > vb:
                    continue
                if y1 > vt + (vb - vt) * 0.5:
                    continue
                if q_bottom is None or y2 > q_bottom:
                    q_bottom = y2
        if q_bottom is not None:
            offset = q_bottom - vt + 12
            if offset > 0:
                log(f"检测到用户提问气泡底部 y={q_bottom}，从长图顶部裁掉 {offset}px")
                return offset
    except Exception as exc:
        LOG.debug("检测用户提问气泡失败：%s", exc)
    return 0


def _measure_toolbar_offset_from_bottom(d, viewport: tuple) -> Optional[int]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
        toolbar = _find_toolbar_row(nodes, w, h)
        if not toolbar:
            return None
        toolbar_y1 = toolbar["y1"]
        viewport_bottom = viewport[3]
        if toolbar_y1 >= viewport_bottom:
            return None
        offset = viewport_bottom - toolbar_y1
        log(f"记录工具栏：y1={toolbar_y1}, 视窗底部={viewport_bottom}, "
            f"距底部={offset}px, 按钮数={len(toolbar['nodes'])}")
        return offset
    except Exception as exc:
        LOG.debug("测量工具栏偏移失败：%s", exc)
        return None


def screenshot_main_with_text(d, output: Path, question: str) -> tuple:
    log("开始主问答长截图（v13）")

    scroll_to_bottom(d)
    time.sleep(0.5)
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport} 高={viewport[3]-viewport[1]}px")

    toolbar_offset = _measure_toolbar_offset_from_bottom(d, viewport)
    question_offset = _measure_user_question_offset(d, viewport, question)

    if not scroll_to_top(d):
        raise RuntimeError("无法确认聊天顶部")
    time.sleep(0.8)

    frames = []
    frame_items = []
    prev_sig = None
    no_text_streak = 0
    seen_fps = set()
    screens_texts = []
    screen_idx = 0

    for i in range(MAX_CAPTURE_FRAMES):
        current = save_message_area(d, OUTPUT_DIR / f"_frame_{i:03d}.png", viewport)
        sig = image_signature(current)

        append_frame = (prev_sig is None or
                        not _frames_almost_same(sig, prev_sig, tol=16, ratio=0.02))

        visible_items = _collect_screen_texts(d, viewport, question, set())
        new_items = []
        for item in visible_items:
            fp = _text_fingerprint(item[1])
            if not fp or fp in seen_fps:
                continue
            seen_fps.add(fp)
            new_items.append(item)

        if append_frame:
            frames.append(current.copy())
            frame_items.append([{"y": y, "fp": _text_fingerprint(t), "text": t}
                                for y, t in visible_items
                                if _text_fingerprint(t)])
            log(f"已采集第 {len(frames)} 屏 尺寸={current.size}")
        else:
            log("当前屏与上一屏高度相似，交给拼接器二次判定")

        if new_items:
            no_text_streak = 0
            screens_texts.append((screen_idx, new_items))
            screen_idx += 1
        else:
            no_text_streak += 1

        prev_sig = sig

        if no_text_streak >= NO_TEXT_STREAK_LIMIT:
            log(f"连续 {no_text_streak} 屏无新文本，执行一次底部确认滑动")

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

        if no_text_streak >= NO_TEXT_STREAK_LIMIT:
            try:
                after = save_message_area(d, OUTPUT_DIR / "_probe_after.png", viewport)
                if _frames_almost_same(image_signature(after), prev_sig, tol=10, ratio=0.01):
                    _swipe_in_viewport(d, "up", fraction=0.22,
                                       duration=0.8, viewport=viewport)
                    time.sleep(1.0)
                    after2 = save_message_area(d, OUTPUT_DIR / "_probe_after.png", viewport)
                    if _images_almost_same(after, after2, threshold=0.995):
                        log("✓ 连续小幅滑动均无变化，主问答到底")
                        break
            except Exception as exc:
                LOG.debug("主问答到底确认失败：%s", exc)

    if not frames:
        raise RuntimeError("没有采集到聊天截图")

    log(f"共采集 {len(frames)} 屏，开始 v13 精确拼接")
    stitch_frames(frames, output, frame_items=frame_items)

    if question_offset > 0:
        if _cut_head_from_long_image(output, question_offset):
            log(f"✓ 已从长图顶部裁剪用户提问气泡 {question_offset}px")

    if toolbar_offset is not None:
        if _cut_tail_from_long_image(output, toolbar_offset):
            log(f"✓ 已从长图底部裁剪工具栏 {toolbar_offset}px")

    ordered_texts = []
    for _, items in screens_texts:
        for _, t in items:
            ordered_texts.append(t)
    deduped = _filter_substring_dupes(ordered_texts)
    trimmed = _trim_tail_to_body(deduped)
    answer_text = _join_answer_texts(trimmed)
    return answer_text, ""


# ============================================================
# 引用来源面板
# ============================================================

def _find_reference_entry(d) -> Optional[dict]:
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    for node in nodes:
        value = (node["text"] or "") + " " + (node["desc"] or "")
        if not value.strip():
            continue
        if "引用源" in value or "引用来源" in value:
            b = bounds_to_tuple(node["bounds"])
            if b:
                return {"node": node, "bounds": b}

    for node in nodes:
        value = node["text"] or node["desc"]
        if not value:
            continue
        if REFERENCE_ENTRY_RE.search(value):
            b = bounds_to_tuple(node["bounds"])
            if b:
                return {"node": node, "bounds": b}

    for node in nodes:
        value = node["text"] or node["desc"]
        if not value:
            continue
        if any(kw in value for kw in REFERENCE_ENTRY_FALLBACK):
            b = bounds_to_tuple(node["bounds"])
            if b and len(value) < 40:
                return {"node": node, "bounds": b}

    try:
        w, h = get_screen_size(d)
        toolbar = _find_toolbar_row(nodes, w, h)
        if toolbar:
            right_btn = toolbar["right_btn"]
            log(f"未找到'引用源'文字，使用工具栏最右按钮兜底："
                f"bounds={right_btn['bounds']}")
            return {"node": right_btn, "bounds": bounds_to_tuple(right_btn["bounds"])}
    except Exception as exc:
        LOG.debug("工具栏兜底失败：%s", exc)
    return None


def _has_reference_panel(d) -> bool:
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    for node in nodes:
        value = node["text"] or node["desc"]
        if not value:
            continue
        if any(marker in value for marker in REFERENCE_PANEL_MARKERS):
            return True
    return False


def open_reference_panel(d, max_attempts: int = 3) -> bool:
    log("尝试打开参考资料面板")
    for attempt in range(max_attempts):
        if attempt == 0:
            scroll_to_bottom(d)
            time.sleep(0.5)

        entry = _find_reference_entry(d)
        if not entry:
            log(f"未找到资料入口（第 {attempt+1} 次）")
            try:
                _swipe_in_viewport(d, "down", fraction=0.3, duration=0.6)
                time.sleep(0.6)
            except Exception:
                pass
            continue
        log(f"找到入口：text={(entry['node']['text'] or '')[:30]!r} "
            f"desc={(entry['node']['desc'] or '')[:30]!r} "
            f"bounds={entry['bounds']}")
        if click_node(d, entry["node"]):
            time.sleep(PANEL_LOAD_WAIT)
            if _has_reference_panel(d):
                log("✓ 参考资料面板已打开")
                return True
            log("点击后未检测到面板")
    return False


def _find_panel_scroll_bounds(d) -> Optional[tuple]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("查找面板滚动容器失败：%s", exc)
        return None

    candidates = []
    for n in nodes:
        if not n.get("visible", True):
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        x1, y1, x2, y2 = b
        bw, bh = x2 - x1, y2 - y1
        if bw < w * PANEL_MIN_SCROLL_WIDTH_RATIO:
            continue
        if bh < h * PANEL_MIN_SCROLL_HEIGHT_RATIO:
            continue
        if y2 <= y1:
            continue

        cls = (n["class"] or "").lower()
        rid = (n["rid"] or "").lower()
        text = ((n["text"] or "") + " " + (n["desc"] or "")).lower()
        scroll_hint = (
            n["scrollable"]
            or any(k in cls for k in ("recyclerview", "scrollview", "nestedscroll", "lazycolumn"))
            or any(k in rid for k in ("recycler", "scroll", "source", "reference"))
            or any(k in text for k in ("引用源", "参考资料", "参考来源", "sources", "references"))
        )
        if not scroll_hint:
            continue

        score = float(bw * bh)
        if n["scrollable"]:
            score *= 1.35
        if "recycler" in cls or "scroll" in cls or "lazy" in cls:
            score *= 1.12
        candidates.append((score, b, n))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    b = candidates[0][1]
    guard_bottom = max(8, int(h * PANEL_SCREEN_BOTTOM_GUARD_RATIO))
    result = (max(0, b[0]), max(0, b[1]),
              min(w, b[2]), min(h - guard_bottom, b[3]))
    if result[3] - result[1] < int(h * PANEL_MIN_SCROLL_HEIGHT_RATIO):
        return None
    log(f"★ XML 检测面板滚动容器：{result} 高={result[3]-result[1]}px")
    return result


def _detect_panel_bounds_by_image() -> Optional[tuple]:
    if np is None:
        return None
    try:
        data = adb_screenshot_bytes()
        img = Image.open(BytesIO(data)).convert("L")
        w, h = img.size
        arr = np.asarray(img, dtype=np.uint8)

        bright_ratio = (arr >= 180).mean(axis=1)
        mask = bright_ratio >= 0.38

        best = None
        start = None
        for i, on in enumerate(mask):
            if on and start is None:
                start = i
            if (not on or i == h - 1) and start is not None:
                end = i if on and i == h - 1 else i - 1
                if best is None or end - start > best[1] - best[0]:
                    best = (start, end)
                start = None

        if not best or best[1] - best[0] < h * 0.25:
            log("图片检测面板：未找到足够大的连续亮区")
            return None

        top, bottom = best
        region = arr[top:bottom + 1]
        col_ratio = (region >= 180).mean(axis=0)
        col_mask = col_ratio >= 0.35
        xs = np.where(col_mask)[0]
        if len(xs) < max(100, int(w * 0.25)):
            left, right = 0, w
        else:
            left = int(xs.min())
            right = int(xs.max()) + 1
            left = max(0, left - 8)
            right = min(w, right + 8)

        guard_bottom = max(8, int(h * PANEL_SCREEN_BOTTOM_GUARD_RATIO))
        result = (left, max(0, top - 4), right, min(h - guard_bottom, bottom + 5))
        log(f"★ 图片检测面板：{result} 高={result[3]-result[1]}px")
        return result
    except Exception as exc:
        LOG.debug("图片检测面板失败：%s", exc)
        return None


def _get_panel_viewport(d) -> tuple:
    w, h = get_screen_size(d)
    scroll_bounds = _find_panel_scroll_bounds(d)
    if scroll_bounds:
        sx1, st, sx2, sb = scroll_bounds
        top_candidates = [st]
        try:
            nodes = parse_nodes(dump_xml(d))
            for n in nodes:
                value = (n["text"] or n["desc"] or "").strip()
                if not value:
                    continue
                if not any(marker in value for marker in REFERENCE_PANEL_MARKERS):
                    continue
                b = bounds_to_tuple(n["bounds"])
                if not b:
                    continue
                if b[1] <= st + int(h * 0.18):
                    top_candidates.append(b[1])
        except Exception:
            pass

        capture_top = max(0, min(top_candidates))
        capture_bottom = sb
        result = (sx1, capture_top, sx2, capture_bottom)
        log(f"面板截图范围（XML）：{result} 高={result[3]-result[1]}px")
        return result

    detected = _detect_panel_bounds_by_image()
    if detected:
        return detected

    guard_bottom = max(8, int(h * PANEL_SCREEN_BOTTOM_GUARD_RATIO))
    fallback = (0, int(h * 0.04), w, h - guard_bottom)
    log(f"图片/XML 检测均失败，退化到近整屏：{fallback} 高={fallback[3]-fallback[1]}px")
    return fallback


def _get_panel_scroll_viewport(d) -> tuple:
    scroll_bounds = _find_panel_scroll_bounds(d)
    if scroll_bounds:
        return scroll_bounds
    return _get_panel_viewport(d)


def _collect_panel_items(d, viewport: tuple) -> list:
    items = []
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return items
    vt, vb = viewport[1], viewport[3]
    for n in nodes:
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        y1, y2 = b[1], b[3]
        if y2 <= vt or y1 >= vb:
            continue
        for field in ("text", "desc"):
            t = (n.get(field) or "").strip()
            if not t or len(t) < 3:
                continue
            if t in IGNORE_EXACT:
                continue
            fp = _text_fingerprint(t)
            if not fp:
                continue
            items.append({"y": y1, "fp": fp, "text": t})
    items.sort(key=lambda x: x["y"])
    return items


def _panel_scrolled(prev_img: Image.Image, curr_img: Image.Image) -> bool:
    if prev_img.size != curr_img.size:
        return True
    if np is None:
        return prev_img.tobytes() != curr_img.tobytes()
    try:
        h = prev_img.height
        y0 = int(h * 0.12)
        y1 = max(y0 + 1, int(h * 0.98))
        a = np.asarray(prev_img.crop((0, y0, prev_img.width, y1)).convert("L"), dtype=np.float32)
        b = np.asarray(curr_img.crop((0, y0, curr_img.width, y1)).convert("L"), dtype=np.float32)
        if a.shape != b.shape:
            return True

        diff = float(np.mean(np.abs(a - b)))
        hh = a.shape[0]
        band_diffs = []
        for ra, rb in ((0, int(hh * 0.30)),
                       (int(hh * 0.35), int(hh * 0.65)),
                       (int(hh * 0.70), hh)):
            if rb <= ra:
                continue
            band_diffs.append(float(np.mean(np.abs(a[ra:rb] - b[ra:rb]))))

        strongest = max(band_diffs) if band_diffs else diff
        log(f"  面板像素差异={diff:.2f}，最大分区差异={strongest:.2f}（阈值={PANEL_MOVE_DIFF_THRESHOLD:.2f}）")
        return diff > PANEL_MOVE_DIFF_THRESHOLD or strongest > PANEL_MOVE_DIFF_THRESHOLD * 1.35
    except Exception as exc:
        LOG.debug("面板滚动判定异常：%s", exc)
        return True


def _swipe_panel_in_bounds(d, panel_bounds: tuple,
                           start_ratio: float = 0.86,
                           end_ratio: float = 0.28,
                           duration: float = PANEL_SWIPE_DURATION) -> bool:
    left, top, right, bottom = panel_bounds
    cx = (left + right) // 2
    area_h = bottom - top
    if area_h < 300:
        log(f"  面板高度不足（{area_h}px），无法滑动")
        return False
    y_start = int(top + area_h * start_ratio)
    y_end = int(top + area_h * end_ratio)
    try:
        log(f"  面板滑动：({cx}, {y_start}) -> ({cx}, {y_end}) dur={duration}s "
            f"(scroll_top={top}, scroll_bottom={bottom})")
        d.swipe(cx, y_start, cx, y_end, duration=duration)
        return True
    except Exception as exc:
        LOG.debug("面板滑动失败：%s", exc)
        return False


def screenshot_reference_panel(d, output: Path) -> bool:
    log("开始参考资料面板长截图（v13：无损卡片拼接）")

    panel_viewport = _get_panel_viewport(d)
    scroll_viewport = _get_panel_scroll_viewport(d)
    log(f"面板截图视窗：{panel_viewport} 高={panel_viewport[3]-panel_viewport[1]}px")
    log(f"面板滚动视窗：{scroll_viewport} 高={scroll_viewport[3]-scroll_viewport[1]}px")

    frames = []
    frame_items = []
    seen_fps = set()
    no_scroll_streak = 0
    no_new_text_streak = 0
    last_captured = None
    last_items = None

    for i in range(MAX_PANEL_FRAMES):
        try:
            current = save_message_area(
                d, OUTPUT_DIR / f"_panel_{i:03d}.png", panel_viewport
            )
        except Exception as exc:
            LOG.debug("面板截图失败：%s", exc)
            break

        items = _collect_panel_items(d, panel_viewport)
        new_items = [it for it in items if it["fp"] not in seen_fps]

        frames.append(current.copy())
        frame_items.append(items)
        last_captured = current.copy()
        last_items = items

        for it in items:
            seen_fps.add(it["fp"])

        if new_items:
            no_new_text_streak = 0
        else:
            no_new_text_streak += 1

        log(f"面板第 {i+1} 屏：完整保留 {current.height}px，当前新文本 {len(new_items)} 条，"
            f"累计指纹 {len(seen_fps)}")

        moved_by_swipe = False
        if _swipe_panel_in_bounds(d, scroll_viewport,
                                  start_ratio=0.86, end_ratio=0.28):
            time.sleep(PANEL_SETTLE_WAIT)
            try:
                probe = save_message_area(d, OUTPUT_DIR / "_panel_probe.png", panel_viewport)
                moved_by_swipe = _panel_scrolled(current, probe)
            except Exception as exc:
                LOG.debug("面板大步滚动探测失败：%s", exc)
                moved_by_swipe = True

        if moved_by_swipe:
            no_scroll_streak = 0
            continue

        no_scroll_streak += 1
        log(f"  面板大步滑动未产生主体变化（{no_scroll_streak}/{PANEL_NO_SCROLL_LIMIT}）")

        if _swipe_panel_in_bounds(
            d, scroll_viewport,
            start_ratio=PANEL_MICRO_SWIPE_START,
            end_ratio=PANEL_MICRO_SWIPE_END,
            duration=PANEL_MICRO_SWIPE_DURATION,
        ):
            time.sleep(PANEL_SETTLE_WAIT)
            try:
                micro = save_message_area(d, OUTPUT_DIR / "_panel_probe.png", panel_viewport)
                micro_moved = _panel_scrolled(current, micro)
            except Exception as exc:
                LOG.debug("面板短滑探测失败：%s", exc)
                micro_moved = True

            if micro_moved:
                no_scroll_streak = 0
                continue

        if no_new_text_streak >= PANEL_NO_NEW_TEXT_LIMIT:
            log(f"  已连续 {no_new_text_streak} 屏无新文本；等待最终底部确认")

        if no_scroll_streak >= PANEL_NO_SCROLL_LIMIT:
            break

    if not frames:
        log("面板未采集到任何帧")
        return False

    try:
        final = save_message_area(d, OUTPUT_DIR / "_panel_final.png", panel_viewport)
        if last_captured is None or not _images_almost_same(last_captured, final, threshold=0.995):
            final_items = _collect_panel_items(d, panel_viewport)
            frames.append(final.copy())
            frame_items.append(final_items)
            log("✓ 追加最终底部帧，避免最后一张引用卡片截断")
    except Exception as exc:
        LOG.debug("最终底部帧回收失败：%s", exc)

    log(f"面板共采集 {len(frames)} 屏，累计 {len(seen_fps)} 条文本，开始 v13 精确拼接")
    stitch_frames(frames, output, frame_items=frame_items)
    log(f"面板长截图完成：{output} 最终尺寸={Image.open(output).size}")
    return True


def close_reference_panel(d) -> None:
    log("关闭参考资料面板")
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
        candidates = []
        for n in nodes:
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            desc = (n["desc"] or "").lower()
            text = (n["text"] or "").lower()
            is_close_like = (
                "关闭" in desc or "close" in desc
                or text in {"×", "x", "✕", "✗"}
                or "close" in n["rid"].lower()
            )
            if not is_close_like:
                continue
            if b[1] > int(h * 0.25) or b[3] < int(h * 0.03):
                continue
            if b[0] < int(w * 0.5):
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            candidates.append((area, n))
        for _, node in sorted(candidates, key=lambda x: x[0]):
            if click_node(d, node):
                time.sleep(0.8)
                return
    except Exception:
        pass

    for word in ("关闭", "Close", "取消", "Cancel"):
        try:
            obj = d(description=word)
            if obj.exists(timeout=0.5):
                obj.click()
                time.sleep(0.8)
                return
        except Exception:
            pass

    try:
        d.press("back")
        time.sleep(0.8)
        if _has_reference_panel(d):
            d.press("back")
            time.sleep(0.8)
    except Exception:
        pass


def merge_two_screenshots(top_path: Path,
                          bottom_path: Optional[Path],
                          output: Path) -> bool:
    if not top_path.exists():
        return False
    if not bottom_path or not bottom_path.exists():
        try:
            Image.open(top_path).save(output, format="PNG")
            return True
        except Exception:
            return False
    try:
        a = Image.open(top_path).convert("RGB")
        b = Image.open(bottom_path).convert("RGB")
        w = max(a.width, b.width)
        h = a.height + b.height + 20
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(a, (0, 0))
        canvas.paste(b, (0, a.height + 20))
        canvas.save(output, format="PNG")
        return True
    except Exception as exc:
        LOG.debug("合并图片失败：%s", exc)
        return False


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
        "_panel_probe.png", "_panel_retry.png", "_panel_final.png",
    ):
        try:
            (OUTPUT_DIR / pattern).unlink(missing_ok=True)
        except Exception:
            pass
    for path in (list(OUTPUT_DIR.glob("_frame_*.png"))
                 + list(OUTPUT_DIR.glob("_panel_*.png"))):
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

    # 1. 新建对话
    if not new_chat(d):
        raise RuntimeError("未能确认新建对话。")
    time.sleep(1.5)

    # 2. 开启深度思考模式
    try:
        enable_deep_thinking(d)
    except Exception as exc:
        LOG.debug("开启深度思考异常（继续）：%s", exc)

    # 3. 输入问题
    input_text(d, question)

    # 4. 发送
    if not send_message(d, question):
        raise RuntimeError("问题发送失败")

    # 5. 等待回答完成
    coarse_answer = wait_answer_finish(d)
    log(f"粗判回答完成，粗文本 {len(coarse_answer)} 字符")

    # 6. 主问答长截图
    scroll_to_bottom(d)
    time.sleep(0.5)
    main_path = task_dir / "answer_main.png"
    answer_text, _ = screenshot_main_with_text(d, main_path, question)

    if len(clean_text(answer_text)) < len(clean_text(coarse_answer)):
        answer_text = coarse_answer
    answer_text = clean_text(answer_text)
    if not answer_text:
        raise RuntimeError("回答文本为空")
    log(f"✓ 完整回答：{len(answer_text)} 字符")

    # 7. 采集深度思考过程（★ v13：内部会自动展开"找到了 N 篇相关资料"）
    thinking_text = ""
    try:
        scroll_to_bottom(d)
        time.sleep(0.6)
        thinking_text = collect_thinking_content(d, known_answer=answer_text)
        log(f"✓ 思考过程：{len(thinking_text)} 字符")
    except Exception as exc:
        LOG.debug("采集思考过程失败（继续）：%s", exc)
        thinking_text = ""

    # 8. 引用来源面板
    sources_path = task_dir / "answer_sources.png"
    has_sources = False
    try:
        scroll_to_bottom(d); time.sleep(0.6)
        if open_reference_panel(d):
            if screenshot_reference_panel(d, sources_path):
                has_sources = True
            close_reference_panel(d)
    except Exception as exc:
        LOG.debug("参考资料面板处理失败：%s", exc)
        try:
            close_reference_panel(d)
        except Exception:
            pass

    # 9. 合并图片
    final_image = task_dir / "answer.png"
    if has_sources:
        merge_two_screenshots(main_path, sources_path, final_image)
        log("✓ 主问答 + 参考资料面板已合并")
    else:
        merge_two_screenshots(main_path, None, final_image)
        log("（无参考资料面板，仅主问答截图）")

    # 10. 生成 HTML / 落盘
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
    if not _HAS_WORKER_LIB:
        raise RuntimeError("任务池模式需要 worker_lib.py。")

    log("上传截图")
    image_url = worker_upload(str(final_image), task_id=unit_id)
    if not image_url:
        raise RuntimeError("截图上传失败")

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
    diag_dir = BASE_DIR / "yuanbao_diagnose" / timestamp
    diag_dir.mkdir(parents=True, exist_ok=True)
    log(f"当前前台：{d.app_current()}")
    w, h = get_screen_size(d)
    log(f"屏幕：{w}x{h}")

    detected = _detect_panel_bounds_by_image()
    if detected:
        log(f"★ 图片检测面板边界：{detected}")
    else:
        log("图片检测面板边界：失败（当前界面无面板或检测算法未命中）")

    entry = _find_thinking_entry(d)
    if entry:
        log(f"★ 检测到思考入口：text={entry['node']['text'][:50]!r} bounds={entry['bounds']}")
    else:
        log("未检测到'已深度思考'入口")

    refs = _find_thinking_refs_toggle(d)
    if refs:
        log(f"★ 检测到参考资料入口：text={refs['text'][:50]!r} bounds={refs['bounds']}")
    else:
        log("未检测到'找到了 N 篇相关资料'入口")

    dump_xml(d, diag_dir / "window.xml")
    save_screen(diag_dir / "screen.png")
    log(f"诊断完成：{diag_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="腾讯元宝 Android Worker v13")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log("=" * 70)
    log("腾讯元宝 Android Worker v13")
    log(f"package candidates={YUANBAO_PACKAGE_CANDIDATES}")
    log(f"platform={PLATFORM_NAME}")
    log("=" * 70)

    d = None
    pkg = None
    try:
        d = connect_device()
        pkg = start_yuanbao(d)
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
        if d is not None and pkg:
            try:
                d.app_stop(pkg)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())