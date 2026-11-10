# -*- coding: utf-8 -*-
"""
qianwen_worker.py —— 千问 Android Worker v8.0（工程集成版）
============================================================
由 qianwenapp.py v8.0 改造接入 qianwen_auto 工程：
  - worker_lib 使用本项目内置的 worker_lib.py（任务池/截图上传/回调）
  - 截图输出目录优先取 config.SCREENSHOT_DIR，与整个工程保持一致

原 v8.0 说明：
  ★ 引入 OCR 双通道采集
  ★ XML + OCR 双通道标题检测
  ★ 思考详情页采集
  ★ 参考资料面板长截图
  ★ 长截图拼接 + 去重 + 文本提取
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

# ★ OCR 引擎（可选，缺失则降级到 XML-only）
try:
    from rapidocr_onnxruntime import RapidOCR
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False
    RapidOCR = None


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
    from config import SCREENSHOT_DIR as OUTPUT_DIR
except Exception:
    OUTPUT_DIR = BASE_DIR / "screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PLATFORM_NAME = "qianwen_app"
DEEPSEEK_PACKAGE = "com.deepseek.chat"  # 仅供兼容，千问不用这个

ANSWER_HARD_TIMEOUT = 210      # 3.5 分钟硬上限，超时直接兜底返回，绝不死循环
ANSWER_MIN_WAIT = 6.0
ANSWER_MIN_COMPLETE_LENGTH = 40
ANSWER_STABLE_SECONDS = 6.0    # 回答文本 6s 无变化即判定完成
ANSWER_STABLE_MIN_ROUNDS = 3
POLL_INTERVAL = 1.0
POLL_INTERVAL_SLOW = 2.0
IMAGE_POLL_INTERVAL = 0.8        # 动作栏图像轮询间隔（比 dump XML 快得多）

MAX_TOP_SWIPES = 15          # 原 25 — 提高 fraction 后需要次数大幅减少
MAX_BOTTOM_SWIPES = 15       # 原 25
SCROLL_STABLE_ROUNDS = 2     # 原 3 — 两轮无移动即判定到底/到顶

MAX_CAPTURE_FRAMES = 200
CAPTURE_SWIPE_DURATION = 0.45  # 原 1.2 — 滑动动画更短，肉眼几乎瞬滑
CAPTURE_SETTLE_WAIT = 0.35     # 原 2.0 — 现代 App 滚动惯性极短，0.35s 足够画面稳定
CAPTURE_FRACTION = 0.62        # 原 0.42 — 每屏滑更远，减少需要采集的屏数

# 长截图拼接
OVERLAP_GOOD_SCORE = 4.0      # 重叠区行特征平均绝对差，低于该值才算可信匹配
OVERLAP_NEAR_RATIO = 0.30     # 匹配不可信时，在「期望位移」±30% 屏高范围内重搜
FLOAT_BTN_SHIFT_MIN = 80      # 修补浮动按钮时，内容至少要上移这么多像素才干净

# 思考详情页
THINKING_DETAIL_SWIPE_RATIO = 0.70  # 原 0.60 — 每屏滑更远
THINKING_DETAIL_SWIPE_DURATION = 0.40  # 原 0.9 — 瞬滑
THINKING_DETAIL_SETTLE = 0.35       # 原 1.3 — 短暂等待
THINKING_DETAIL_STABLE_ROUNDS = 2
THINKING_DETAIL_MAX_FRAMES = 80
THINKING_DETAIL_LOAD_TIMEOUT = 50.0
THINKING_DETAIL_LOAD_POLL = 2.0
THINKING_DETAIL_AFTER_LOAD_WAIT = 0.5   # 原 1.0

# ★ OCR 相关
OCR_RECOGNIZE_MIN_SCORE = 0.5   # OCR 置信度阈值
OCR_TITLE_MAX_LEN = 20          # 标题为短文本
OCR_CONTENT_MIN_LEN = 100       # XML 内容低于此值则改用 OCR

LONG_TEXT_THRESHOLD = 150

BODY_MIN_LEN = 15
BODY_END_PUNCT_CN = "。！？…"
BODY_END_PUNCT_EN = ".!?"

# 深度思考模式开关
DEEP_THINK_WORDS = (
    "深度思考", "深度思索", "深入思考", "深度思考模式",
    "DeepThink", "Deep Think", "DeepThinking", "Reasoning",
)

THINKING_PANEL_TITLES = (
    "思考过程", "深度思考过程", "已深度思考", "已完成思考",
    "思考中", "正在思考", "推理过程", "深度思考",
    "Thinking", "Thought", "Reasoning",
)

# ★ 详情页标题
THINKING_DETAIL_TITLE = "深度思考"
THINKING_DETAIL_TITLE_MAX_LEN = 20

EXPAND_ALL_BUTTON_WORDS = (
    "查看全部", "展开全部", "查看更多", "显示全部",
    "展开全文", "查看详情", "查看完整",
)

THINKING_CARD_HINTS = (
    "参考了", "完成思考", "已深度思考", "深度思考完成",
)

GENERATING_WORDS = (
    "停止生成", "停止回答", "正在生成", "正在回答", "回答中", "思考中",
    "Stop generating", "Generating",
)
COMPLETION_WORDS = (
    "复制", "重新生成", "分享", "点赞", "点踩", "朗读",
    "Copy", "Regenerate", "Share",
)
INPUT_HINTS = (
    "发消息", "按住说话", "发消息或按住说话",
    "向千问提问", "输入", "Ask anything", "Message",
)
NEW_CHAT_WORDS = (
    "新建对话", "新对话", "新的对话", "创建新对话", "开启新对话",
    "New chat", "New conversation",
)
SEND_WORDS = ("发送", "Send")
BOTTOM_WORDS = ("回到底部", "返回底部", "到底部", "Jump to bottom", "Scroll to bottom")
THINKING_WORDS = ("深度思考", "DeepThink", "Thinking", "思考过程")
SOURCE_WORDS = ("参考了", "参考资料", "参考来源", "Sources", "References")

IGNORE_EXACT = {
    "千问", "Qwen", "千问办事", "通义千问",
    "分享", "Share", "复制", "Copy", "重新生成", "Regenerate",
    "点赞", "点踩", "Like", "Dislike", "发送", "Send",
    "关闭", "取消", "返回", "退出", "Close", "Cancel", "Back",
    "深度思考", "AI 生图", "AI生图", "拍题答疑", "AI 生视频", "AI生视频",
    "作业批改", "AI 搜索", "AI搜索", "智能搜索", "联网搜索",
    "快速模式", "专家模式",
    "回到顶部", "回到底部", "返回顶部",
    "参考来源", "参考资料",
    "AI写作", "AI 写作",
    "朗读", "播放", "复制原文", "刷新", "编辑", "分享给好友",
    "收藏", "下载", "更多", "语音播报",
    "查看全部", "展开", "收起", "展开全文", "显示全部",
    "思考过程", "已深度思考",
    "内容由AI生成", "内容由 AI 生成",
}

IGNORE_SUBSTR = (
    "回答由 AI 生成", "仅供参考", "内容由 AI 生成",
    "以上内容由 AI 生成", "内容由AI生成",
    "拍照效果对比您可以", "推荐给您", "为您推荐",
    "相关视频", "相关推荐", "延伸阅读",
    "暂无思考内容",
)

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")

KNOWN_INPUT_IDS = (
    "com.aliyun.tongyi:id/input",
    "com.aliyun.tongyi:id/input_text",
    "com.aliyun.tongyi:id/et_input",
    "com.aliyun.tongyi:id/edit_text",
    "com.aliyun.tongyi:id/message_input",
    "com.aliyun.tongyi:id/chat_input",
    "com.aliyun.tongyi:id/et_message",
)
KNOWN_SEND_IDS = (
    "com.aliyun.tongyi:id/send",
    "com.aliyun.tongyi:id/send_button",
    "com.aliyun.tongyi:id/btn_send",
    "com.aliyun.tongyi:id/iv_send",
    "com.aliyun.tongyi:id/action_send",
    "com.aliyun.tongyi:id/send_btn",
    "com.aliyun.tongyi:id/btn_send_message",
)
KNOWN_NEW_CHAT_IDS = (
    "com.aliyun.tongyi:id/new_chat",
    "com.aliyun.tongyi:id/create_new_chat",
    "com.aliyun.tongyi:id/new_conversation",
    "com.aliyun.tongyi:id/iv_new_chat",
    "com.aliyun.tongyi:id/btn_new_chat",
)
KNOWN_MENU_IDS = (
    "com.aliyun.tongyi:id/menu",
    "com.aliyun.tongyi:id/iv_menu",
    "com.aliyun.tongyi:id/btn_menu",
    "com.aliyun.tongyi:id/hamburger",
    "com.aliyun.tongyi:id/navigation",
)
KNOWN_DEEP_THINK_IDS = (
    "com.aliyun.tongyi:id/deep_think",
    "com.aliyun.tongyi:id/deep_thinking",
    "com.aliyun.tongyi:id/btn_deep_think",
    "com.aliyun.tongyi:id/iv_deep_think",
    "com.aliyun.tongyi:id/think_switch",
    "com.aliyun.tongyi:id/btn_thinking",
)

REFERENCE_ENTRY_RE = re.compile(r"参考(?:了)?\s*\d+\s*篇资料")
REFERENCE_ENTRY_FALLBACK = ("参考了", "参考资料", "参考来源")
REFERENCE_PANEL_MARKERS = ("参考来源", "参考资料", "References", "Sources")

# 旧关键词保留（其他地方可能还在引用）
DRAWER_KEYWORDS = (
    "新建对话", "新对话", "我的空间", "智能体",
    # 千问侧栏高频特征词（截图确认）
    "云空间", "收藏", "档案", "定时任务", "对话历史",
)

# 侧栏 Hallmark 特征词 —— 主界面不会同时出现这些词
# 用来精准区分「主界面」 vs 「侧栏已展开」
DRAWER_HALLMARK_WORDS = (
    "云空间", "收藏", "档案", "定时任务",
    "对话历史", "新建对话", "新对话",
)

NEW_CHAT_BTN_HINTS = ("新建对话", "新对话", "新的对话", "创建新对话")


# ============================================================
# 日志
# ============================================================

LOG = logging.getLogger("qianwen_mobile")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log(msg: str) -> None:
    LOG.info(msg)


# ============================================================
# ★ OCR 引擎（懒加载）
# ============================================================

_OCR_ENGINE = None


def _get_ocr_engine():
    """懒加载 OCR 引擎，避免启动时消耗大量时间。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    if not _HAS_OCR:
        return None
    try:
        log("初始化 OCR 引擎（RapidOCR）...")
        t0 = time.time()
        _OCR_ENGINE = RapidOCR()
        log(f"✓ OCR 引擎已就绪，耗时 {time.time()-t0:.1f}s")
    except Exception as exc:
        log(f"⚠ OCR 引擎初始化失败：{exc}")
        _OCR_ENGINE = None
    return _OCR_ENGINE


def _ocr_image(img: Image.Image) -> list:
    """对 PIL 图片做 OCR 识别。"""
    engine = _get_ocr_engine()
    if engine is None:
        return []

    try:
        if np is None:
            return []
        arr = np.asarray(img.convert("RGB"))
        result, _elapse = engine(arr)
        if not result:
            return []

        items = []
        for box, text, score in result:
            if not text:
                continue
            if score < OCR_RECOGNIZE_MIN_SCORE:
                continue
            ys = [pt[1] for pt in box]
            xs = [pt[0] for pt in box]
            y_top = int(min(ys))
            x_left = int(min(xs))
            items.append({
                "text": clean_text(text),
                "score": float(score),
                "y": y_top,
                "x": x_left,
            })
        items.sort(key=lambda x: (x["y"], x["x"]))
        return items
    except Exception as exc:
        LOG.debug("OCR 识别失败：%s", exc)
        return []


def _ocr_screen(d) -> list:
    """对当前屏幕截图做 OCR，返回识别项列表。"""
    try:
        data = adb_screenshot_bytes()
        img = Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        LOG.debug("OCR 截图失败：%s", exc)
        return []
    return _ocr_image(img)


# ============================================================
# ADB
# ============================================================

def run_adb(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """调用 adb 命令。如果已知设备序列号且命令不是 devices/connect，自动加 -s。"""
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


def _adb_tap(x: int, y: int) -> bool:
    """adb shell input tap 精准点击 —— uiautomator2 d.click 在 clickable=false 时会失灵。"""
    try:
        return run_adb("shell", "input", "tap", str(int(x)), str(int(y)), timeout=10).returncode == 0
    except Exception:
        return False


def _tap_bounds(d, cx: int, cy: int, tag: str = "") -> bool:
    """优先 adb tap，失败回退 uiautomator2 d.click。"""
    if _adb_tap(cx, cy):
        time.sleep(0.45)
        log(f"adb 已点击坐标 ({cx},{cy}) {tag}")
        return True
    try:
        d.click(cx, cy)
        time.sleep(0.45)
        log(f"uiautomator2 已点击坐标 ({cx},{cy}) {tag}")
        return True
    except Exception:
        return False


def _safe_dump(d) -> str:
    """dump 失败返回空串，避免设备忙时打断流程。"""
    try:
        return dump_xml(d)
    except Exception as e:
        LOG.debug(f"dump 失败: {e}")
        return ""


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


def click_first_by_words(d, words: Sequence[str], *,
                         require_clickable: bool = True) -> bool:
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

def start_qianwen(d) -> str:
    from qianwen_login import resolve_qianwen_package
    log("启动千问 Android App")
    pkg = resolve_qianwen_package(d)
    time.sleep(2.0)
    try:
        if d.app_current().get("package") != pkg:
            d.app_start(pkg, stop=False)
            time.sleep(2.5)
        if d.app_current().get("package") != pkg:
            raise RuntimeError("千问没有进入前台")
    except Exception as exc:
        raise RuntimeError(f"无法确认千问前台状态：{exc}")
    return pkg


# ============================================================
# 抽屉 / 新对话
# ============================================================

def _drawer_is_open(d) -> bool:
    """判定左侧抽屉是否已展开（XML 特征词通道）。

    抽屉展开后必然出现「云空间 / 收藏 / 档案 / 定时任务 / 对话历史 / 新建对话」，
    主界面上不会同时出现这些文案 —— 命中 ≥2 个即判定为已展开。
    """
    xml = _safe_dump(d)
    if not xml:
        return False

    hit = 0
    for kw in DRAWER_HALLMARK_WORDS:
        if kw in xml:
            hit += 1
            if hit >= 2:
                return True
    return False


def _drawer_open_by_template(d) -> bool:
    """OpenCV 抽屉判定：设置图标只在抽屉里出现，比 XML 隐藏节点可靠。"""
    try:
        from template_matcher import DRAWER_REGION, find_template
        return find_template(
            "open_setting", threshold=0.75,
            region=DRAWER_REGION, save_fail=False,
        ) is not None
    except Exception:
        return False


def _drawer_open_by_image(d) -> bool:
    """图像通道判定抽屉是否展开（XML 拿不到文案时的兜底）。

    抽屉展开时：左侧是白色抽屉面板，右侧露出的主界面被半透明黑色遮罩压暗，
    所以「左侧亮度 - 右侧亮度」会明显为正，且右侧整体偏暗。
    """
    try:
        img = save_screen(OUTPUT_DIR / "_drawer_probe.png").convert("L")
    except Exception as exc:
        LOG.debug("抽屉图像判定截图失败：%s", exc)
        return False

    w, h = img.size
    try:
        left = img.crop((int(w * 0.08), int(h * 0.20), int(w * 0.55), int(h * 0.78)))
        right = img.crop((int(w * 0.92), int(h * 0.20), int(w * 0.99), int(h * 0.78)))
    except Exception:
        return False

    lp = list(left.getdata())
    rp = list(right.getdata())
    if not lp or not rp:
        return False

    lmean = sum(lp) / len(lp)
    rmean = sum(rp) / len(rp)
    opened = rmean < 170 and (lmean - rmean) > 55
    LOG.debug("[DRAWER-IMG] left=%.1f right=%.1f opened=%s", lmean, rmean, opened)
    return opened


def _find_top_left_button(d) -> Optional[dict]:
    """在主页左上角精准定位「打开抽屉」汉堡按钮。

    ★ 新版千问顶部栏布局（真实截图）：
      [=汉堡] [新建对话笔框] [快速▼] [千问Go!] [🔔] [🔍] [⋯]
      汉堡最靠左（x≈4~6%），新建对话在它右边（x≈12~14%），两者紧贴。

    定位策略（从强到弱）：
      1) desc/text/rid 关键词精确匹配（"菜单"/"侧边栏"/"抽屉"/"menu"）
      2) 在左上角区域找**最左**的可点击小图标（汉堡永远是最左那个）
      3) 直接按固定比例坐标兜底（x=5% y=6.5%）
    """
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if not xml:
        # XML 都拿不到 → 直接比例坐标
        log("[左上角] XML 空，用固定比例坐标兜底")
        cx, cy = int(w * 0.055), int(h * 0.065)
        return {"center": (cx, cy), "bounds": (cx - 28, cy - 28, cx + 28, cy + 28),
                "label": "(XML空 比例兜底)"}

    candidates = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        bw, bh = x2 - x1, y2 - y1

        # 左上角区域：x < 30%（更宽），y 在状态栏之下、顶部栏之内
        if x1 > w * 0.30:
            continue
        if y1 < int(h * 0.03):          # 状态栏以上跳过
            continue
        if y2 > h * 0.20:               # 顶部栏以下跳过
            continue

        # 尺寸过滤（放宽）
        if bw < 20 or bh < 20:
            continue
        if bw > 180 or bh > 180:
            continue

        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        rid = attrs.get("resource-id") or ""

        # 排除状态栏时间（如 "4:49"）、电量
        if re.fullmatch(r"\d{1,2}[:：]\d{2}", text):
            continue
        if re.fullmatch(r"\d+%", text):
            continue

        combined_low = (desc + " " + text + " " + rid).lower()
        label = desc or text or rid or "(无描述)"

        # 关键词强匹配
        STRONG_KEYWORDS = (
            "menu", "drawer", "hamburger", "侧边栏", "抽屉", "导航",
            "打开侧边栏", "打开抽屉", "侧栏", "菜单", "back", "返回",
            "navigation", "toggle",
        )
        has_keyword = any(k in combined_low for k in STRONG_KEYWORDS)

        # 评分：x1 越小越优先（汉堡永远是最左那个图标）
        # 有 keyword → 大幅加分，clickable → 加分
        score = x1 * 1.0 + y1 * 0.1
        if attrs.get("clickable") == "true":
            score -= 300
        if has_keyword:
            score -= 800
        # 尺寸接近正方形（icon 特征）加分
        if 0.6 < bw / max(1, bh) < 1.8:
            score -= 50

        candidates.append((score, {
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "bounds": (x1, y1, x2, y2),
            "label": label,
            "has_keyword": has_keyword,
        }))

    if not candidates:
        # 完全没有候选 → 比例坐标兜底
        log("[左上角] 无候选节点，用固定比例坐标兜底")
        cx, cy = int(w * 0.055), int(h * 0.065)
        return {"center": (cx, cy), "bounds": (cx - 28, cy - 28, cx + 28, cy + 28),
                "label": "(无候选 比例兜底)"}

    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]

    # 没有 keyword 但候选里最左那个 → 大概率就是汉堡
    if not best.get("has_keyword"):
        log(f"[左上角] 未命中关键词，选最左候选：label={best['label']} center={best['center']}")

    return best


def _wait_drawer_open(d, timeout: float = 4.0) -> bool:
    """点击后轮询等待抽屉展开：只接受图像证据，避免 XML 隐藏节点误判。"""
    started = time.time()
    while time.time() - started < timeout:
        if _drawer_open_by_template(d) or _drawer_open_by_image(d):
            return True
        time.sleep(0.30)
    return False


def _open_left_drawer(d, max_rounds: int = 3) -> bool:
    """打开左侧抽屉（点主页左上角汉堡按钮）。

    ★ 优先用 OpenCV 模板匹配（button_icons/open_left_drawer.png）——
      WebView 渲染的按钮没有 content-desc，选择器/XML 都找不到，
      模板匹配直接按"按钮长什么样"定位，最靠谱。

    多级兜底顺序：
      1) 模板匹配（最精准）
      2) _find_top_left_button 真实节点
      3) 固定比例坐标网格
      4) 选择器关键词
    """
    if _drawer_open_by_template(d):
        log("✓ 抽屉已处于展开状态（设置图标验证），无需再点")
        return True

    if _drawer_open_by_image(d):
        log("✓ 抽屉已展开（图像判定），无需再点")
        return True

    w, h = get_screen_size(d)

    # ★ 策略 0：OpenCV 模板匹配（放在最前面！）
    try:
        from template_matcher import click_open_drawer, click_template, TOP_BAR_REGION
        pos = click_open_drawer(label="模板开抽屉")
        if pos is not None and _wait_drawer_open(d, timeout=3.0):
            log("✓ 抽屉已展开（模板匹配）")
            return True
        # 可能第一次模板就点中了但动画还没出，再等一轮
        for _ in range(2):
            time.sleep(0.35)
            if _wait_drawer_open(d, timeout=2.0):
                log("✓ 抽屉已展开（模板匹配·二次等待）")
                return True
    except Exception as exc:
        log.debug("模板匹配异常（继续兜底）：%s", exc)

    # 策略 1：真实节点定位
    for attempt in range(1, max_rounds + 1):
        btn = _find_top_left_button(d)
        if btn:
            cx, cy = btn["center"]
            log(f"[节点第{attempt}轮] 左上角按钮：{btn['label']} center=({cx},{cy})")
            _tap_bounds(d, cx, cy, f"左上角({btn['label']})")
        else:
            log(f"[节点第{attempt}轮] 未能定位到左上角按钮节点")

        if _wait_drawer_open(d, timeout=3.0):
            log("✓ 抽屉已展开（节点定位）")
            return True

    # 策略 2：固定比例坐标网格
    log("节点定位多轮失败，改用固定比例坐标网格兜底")
    for x_ratio in (0.040, 0.055, 0.070, 0.030, 0.085):
        for y_ratio in (0.050, 0.065, 0.080, 0.040, 0.095):
            cx, cy = int(w * x_ratio), int(h * y_ratio)
            _tap_bounds(d, cx, cy, f"汉堡坐标兜底({x_ratio},{y_ratio})")
            if _wait_drawer_open(d, timeout=2.0):
                log("✓ 抽屉已展开（坐标兜底）")
                return True

    # 策略 3：选择器关键词
    log("坐标兜底也失败，用选择器关键词试")
    for desc_kw in ("侧边栏", "抽屉", "菜单", "返回", "打开侧边栏"):
        for sel in ({"descriptionContains": desc_kw}, {"textContains": desc_kw}):
            try:
                obj = d(**sel)
                if obj.exists(timeout=0.5):
                    obj.click()
                    if _wait_drawer_open(d, timeout=2.0):
                        return True
            except Exception:
                pass

    log("⚠ 所有策略都未成功打开抽屉")
    return False


def _find_new_chat_btn_in_drawer(d):
    """在抽屉里精准定位底部的「新建对话」按钮。

    截图确认：按钮是白色胶囊，文案「新建对话」，位于抽屉最底部（y≈87%），
    横向从抽屉左边缘延伸到约屏幕 69% 宽处。
    ❌ 旧 bug：要求 x2 < 0.60w —— 而按钮右边缘到 0.69w，直接被过滤掉，
       只能退回坐标兜底乱点，所以总是点不中。
    """
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if not xml:
        return None

    candidates = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        label = text or desc
        if not label:
            continue

        # 精确文案匹配（避免误命中「对话历史」下的会话标题）
        matched_word = None
        for word in NEW_CHAT_WORDS:
            if label == word:
                matched_word = word
                break
        if not matched_word:
            continue

        # 必须在抽屉范围内（左侧 30% 起、右侧不超过 90%）
        if x1 > w * 0.30 or x2 > w * 0.90:
            continue
        bw, bh = x2 - x1, y2 - y1
        if bw < 90 or bh < 28:
            continue  # 太小 → 不是胶囊按钮（例如淡出的「新对话」提示字）

        score = -y1                      # 越靠底部越优先
        if attrs.get("clickable") == "true":
            score -= 1000

        candidates.append((score, {
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "bounds": (x1, y1, x2, y2),
            "label": matched_word,
        }))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _find_new_chat_btn_on_home(d) -> Optional[dict]:
    """在**主页左上角**找到汉堡按钮紧挨着的「新建对话」图标（笔+方框）。

    ★ 新版千问（2026.11）主界面顶部栏布局：
      [=汉堡] [新建对话 笔+方框] [模式▼] [千问Go!] [🔔] [🔍] [⋯]
      汉堡在最左 x≈5%，新建对话紧贴右侧 x≈12~14%。

    策略：
      1) 先精确定位汉堡（_find_top_left_button）当锚点
      2) 在锚点右侧 0~90px 内搜可点击小图标
      3) 有"新建对话"/"新建"/"new chat"关键词的优先
      4) 完全找不到 → 固定比例坐标（w*0.125, h*0.095）兜底
    """
    w, h = get_screen_size(d)
    xml = _safe_dump(d)

    # 先拿到汉堡真实位置
    hamburger = _find_top_left_button(d)
    hamburger_x2 = hamburger["bounds"][2] if hamburger else None

    if not xml:
        # XML 空 → 比例兜底
        cx, cy = int(w * 0.125), int(h * 0.095)
        log("[主页新建] XML 空，用固定比例兜底 center=({},{})".format(cx, cy))
        return {"center": (cx, cy), "bounds": (cx - 28, cy - 28, cx + 28, cy + 28),
                "label": "(比例兜底)", "has_keyword": False}

    candidates = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        bw, bh = x2 - x1, y2 - y1

        # 顶部栏区域（放宽）
        if x1 > w * 0.40:
            continue
        if y1 < int(h * 0.03):
            continue
        if y2 > h * 0.20:
            continue
        if bw < 20 or bh < 20:
            continue
        if bw > 180 or bh > 180:
            continue
        # 排除状态栏时间/电量
        text_raw = (attrs.get("text") or "").strip()
        if re.fullmatch(r"\d{1,2}[:：]\d{2}", text_raw) or re.fullmatch(r"\d+%", text_raw):
            continue

        desc = (attrs.get("content-desc") or "").strip()
        text = text_raw
        rid = (attrs.get("resource-id") or "").lower()
        combined_low = (desc + " " + text + " " + rid).lower()

        label = None
        has_keyword = False
        for word in ("新建对话", "新对话", "创建新对话", "New chat", "新建", "新聊天"):
            if word.lower() in combined_low:
                label = word
                has_keyword = True
                break
        if label is None:
            for kw in ("new_chat", "newchat", "new_conversation", "create_chat", "add_chat"):
                if kw in rid:
                    label = f"(rid={kw})"
                    has_keyword = True
                    break

        # 如果有汉堡锚点，检查是否紧邻其右侧
        near_hamburger = False
        if hamburger_x2 is not None:
            gap = x1 - hamburger_x2
            if 0 <= gap <= 90:
                near_hamburger = True
                if label is None:
                    label = f"(汉堡右侧紧邻 gap={gap})"

        if label is None:
            continue

        score = x1
        if attrs.get("clickable") == "true":
            score -= 400
        if has_keyword:
            score -= 600
        if near_hamburger:
            score -= 500

        candidates.append((score, {
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "bounds": (x1, y1, x2, y2),
            "label": label,
            "has_keyword": has_keyword,
            "near_hamburger": near_hamburger,
        }))

    if not candidates:
        # 直接比例兜底
        cx, cy = int(w * 0.125), int(h * 0.095)
        log(f"[主页新建] 无候选，用固定比例兜底 center=({cx},{cy})")
        return {"center": (cx, cy), "bounds": (cx - 28, cy - 28, cx + 28, cy + 28),
                "label": "(无候选 比例兜底)", "has_keyword": False}

    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]
    if not best.get("has_keyword"):
        log(f"[主页新建] 未命中关键词，选最优候选：{best['label']} center={best['center']}")

    return best


def _is_fresh_chat(d) -> bool:
    """当前是否「新对话欢迎页」。

    新版千问新对话 = 欢迎页：含"我是千问"/"为你答疑"等欢迎语，且有输入框。
    旧对话里不会出现欢迎语，所以这是判断"新建对话真的成功"的强特征。
    """
    xml = _safe_dump(d)
    if not xml:
        return False
    WELCOME_HINTS = ("我是千问", "为你答疑", "随时找我聊天", "有什么可以帮你",
                     "我能帮你做什么", "Ask anything")
    return any(h in xml for h in WELCOME_HINTS)


def _verify_new_chat(d, timeout: float = 3.0) -> bool:
    """点击新建后轮询验证：必须真的回到新对话欢迎页，才算成功。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_fresh_chat(d):
            return True
        time.sleep(0.4)
    return _is_fresh_chat(d)


def new_chat(d) -> bool:
    """新建对话：优先走**主页左上角**直接点新建对话图标（笔+方框），
    失败再退回「打开抽屉 → 底部新建按钮」路径。

    ★ 所有路径点击后都必须用 _verify_new_chat 验证真的回到欢迎页，
      绝不把"adb 命令执行成功"当成"新建成功"。
    """
    log("创建新对话（千问，模板优先）")

    # 已经在新对话欢迎页 → 无需再点
    if _is_fresh_chat(d):
        log("✓ 当前已是新对话欢迎页，无需新建")
        return True

    # ---- 路径 0：OpenCV 模板匹配（顶部栏新建对话图标，笔+方框）----
    try:
        from template_matcher import click_open_new_chat, click_template
        pos = click_open_new_chat(label="模板新建对话")
        if pos is not None and _verify_new_chat(d, timeout=3.0):
            log("✓ 已新建对话（模板匹配·已验证）")
            return True
        # 点中了但页面还没切，再等一轮
        for _ in range(2):
            time.sleep(0.35)
            if _verify_new_chat(d, timeout=1.5):
                log("✓ 已新建对话（模板匹配·二次等待·已验证）")
                return True
        log("[模板] 点击后未回到欢迎页，改走抽屉路径")
    except Exception as exc:
        log.debug("模板匹配异常（继续抽屉路径）：%s", exc)

    # ---- 路径 B：打开左侧抽屉 → 点抽屉底部新建按钮（兜底）----
    log("走抽屉兜底路径新建对话")
    if not _open_left_drawer(d):
        log("✗ 无法打开左侧抽屉，新建对话失败")
        return False

    time.sleep(0.3)

    clicked = False

    btn = _find_new_chat_btn_in_drawer(d)
    if btn:
        cx, cy = btn["center"]
        log(f"抽屉内定位到「{btn['label']}」 center=({cx},{cy})")
        clicked = _tap_bounds(d, cx, cy, f"新建对话({btn['label']})")

    if not clicked:
        for word in NEW_CHAT_WORDS:
            try:
                obj = d(text=word)
                if obj.exists(timeout=0.6):
                    obj.click()
                    clicked = True
                    log(f"✓ 选择器点击「{word}」")
                    break
            except Exception:
                pass

    if not clicked:
        w, h = get_screen_size(d)
        cx, cy = int(w * 0.415), int(h * 0.875)
        log(f"坐标兜底点击新建对话 ({cx},{cy})")
        clicked = _tap_bounds(d, cx, cy, "新建对话兜底")

    if not clicked:
        log("✗ 未能点击「新建对话」")
        return False

    time.sleep(0.6)

    # 抽屉仍展开则按 back 收起
    if _drawer_is_open(d):
        try:
            d.press("back")
            time.sleep(0.45)
        except Exception:
            pass

    # ★ 最终必须验证真的回到新对话欢迎页
    if _verify_new_chat(d, timeout=3.0):
        log("✓ 已新建对话（抽屉路径，已验证）")
        return True

    log("✗ 新建对话后未回到欢迎页，判定失败")
    return False


# ============================================================
# 切换思考研究模式（千问新版底部工具栏模式选择器）
# ============================================================
#
# 千问新版底部工具栏结构（截图确认）：
#   [思考研究/快速▼] [工作助理] [双11抢优惠] [AI生图] ...
#
# 模式切换流程：
#   1. 检测底部工具栏当前是否已显示「思考研究」→ 是则直接返回
#   2. 找底部工具栏上的「快速」按钮（带下拉箭头）→ 点击
#   3. 弹菜单 → 菜单里点「思考研究」
#   4. 验证切换成功

# 底部工具栏模式选择器的可能文字
MODE_TOGGLE_WORDS = ("快速", "快速模式", "快速▼", "快速~")
TARGET_MODE_WORD = "思考研究"
MENU_TARGET_WORDS = ("思考研究", "深度研究", "深度搜索、深度研究")


def _get_bottom_toolbar_nodes(d) -> list:
    """取底部工具栏区域（屏幕底部 20%）的所有节点，带 bounds 中心。"""
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if not xml:
        return []
    result = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        # 限制在底部 15~30% 高度区域
        if y1 < h * 0.68 or y2 > h * 0.88:
            continue
        attrs["_bounds"] = (x1, y1, x2, y2)
        attrs["_center"] = ((x1 + x2) // 2, (y1 + y2) // 2)
        result.append(attrs)
    return result


def _is_in_thinking_research_mode(d) -> bool:
    """检测底部工具栏当前是否已显示「思考研究」。"""
    nodes = _get_bottom_toolbar_nodes(d)
    for n in nodes:
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        combined = text + desc
        if TARGET_MODE_WORD in combined:
            # 选中标记可能在父节点或同级，这里宽松匹配
            return True
    return False


def _find_mode_toggle_btn(d):
    """在底部工具栏找「快速」按钮（带下拉箭头）。返回 (_center, attrs) 或 None。"""
    nodes = _get_bottom_toolbar_nodes(d)
    for n in nodes:
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        combined = text + desc
        for kw in MODE_TOGGLE_WORDS:
            if kw in combined or kw.replace("▼", "") in combined or kw.replace("~", "") in combined:
                # 必须是一个相对较大的可点击按钮
                x1, y1, x2, y2 = n["_bounds"]
                bw, bh = x2 - x1, y2 - y1
                if bw < 40 or bh < 30:
                    continue  # 太小不是按钮
                return (n["_center"], n)
    return None


def _click_thinking_research_in_menu(d) -> bool:
    """模式选择器弹窗里点「思考研究」。"""
    # 弹窗通常在屏幕下半部
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if xml:
        for m in re.finditer(r"<node\b[^>]*?/?>", xml):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not bm:
                continue
            x1, y1, x2, y2 = (int(v) for v in bm.groups())
            text = (attrs.get("text") or "").strip()
            desc = (attrs.get("content-desc") or "").strip()
            combined = text + desc
            # 精确匹配 "思考研究" 或包含它的文字（可能附说明如"深度搜索、深度研究"）
            if TARGET_MODE_WORD in combined and "深度" not in combined.replace("思考研究", ""):
                # 限制在屏幕中央偏下区域（弹窗一般在这）
                if y1 < h * 0.20:
                    continue
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                if _tap_bounds(d, cx, cy, f"菜单思考研究({text})"):
                    return True

    # 选择器兜底
    for word in MENU_TARGET_WORDS:
        try:
            obj = d(text=word)
            if obj.exists(timeout=0.5):
                obj.click()
                time.sleep(0.8)
                return True
            obj = d(textContains=word)
            if obj.exists(timeout=0.5):
                obj.click()
                time.sleep(0.8)
                return True
        except Exception:
            pass

    # 坐标兜底：弹窗里思考研究一般在第一个选项下方（快速在上方打勾）
    # 估计位置：屏幕中心偏下，y ≈ 35~45%
    log("菜单里没找到「思考研究」文字，尝试 adb 坐标兜底")
    for x_ratio in (0.50, 0.45, 0.55):
        for y_ratio in (0.38, 0.42, 0.45, 0.35):
            if _tap_bounds(d, int(w * x_ratio), int(h * y_ratio),
                            f"菜单兜底({x_ratio},{y_ratio})"):
                time.sleep(0.8)
                return True

    return False


def enable_thinking_research(d) -> bool:
    """切换到「思考研究」模式（千问新版底部工具栏模式选择器）。

    先检测是否已在目标模式 → 是就跳过；
    不是则点「快速」按钮弹菜单 → 点「思考研究」。
    """
    log("切换千问模式：思考研究")

    # ---- 1. 检测是否已在目标模式 ----
    if _is_in_thinking_research_mode(d):
        log("✓ 已处于「思考研究」模式，无需切换")
        return True

    # ---- 2. 找「快速」按钮并点击打开模式菜单 ----
    toggle = _find_mode_toggle_btn(d)
    if toggle:
        (cx, cy), node = toggle
        text = (node.get("text") or node.get("content-desc") or "").strip()
        log(f"找到模式切换按钮「{text}」，位置=({cx},{cy})")
        if not _tap_bounds(d, cx, cy, f"快速按钮({text})"):
            log("⚠ 点击快速按钮失败，尝试选择器兜底")
            for kw in MODE_TOGGLE_WORDS:
                try:
                    obj = d(text=kw.replace("▼", "").replace("~", ""))
                    if obj.exists(timeout=0.5):
                        obj.click()
                        break
                    obj = d(textContains=kw.replace("▼", "").replace("~", ""))
                    if obj.exists(timeout=0.5):
                        obj.click()
                        break
                except Exception:
                    pass
    else:
        log("⚠ 未找到「快速」按钮，尝试选择器兜底")
        for kw in MODE_TOGGLE_WORDS:
            try:
                obj = d(text=kw.replace("▼", "").replace("~", ""))
                if obj.exists(timeout=0.5):
                    obj.click()
                    break
            except Exception:
                pass

    time.sleep(1.2)  # 等菜单弹出动画

    # ---- 3. 在弹菜单里点「思考研究」----
    if not _click_thinking_research_in_menu(d):
        log("⚠ 菜单里没点到「思考研究」，尝试按 back 后重试一次")
        try:
            d.press("back")
            time.sleep(0.8)
        except Exception:
            pass
        toggle = _find_mode_toggle_btn(d)
        if toggle:
            (cx, cy), _ = toggle
            _tap_bounds(d, cx, cy, "快速按钮(重试)")
            time.sleep(1.2)
            _click_thinking_research_in_menu(d)

    time.sleep(0.8)

    # ---- 4. 验证切换成功 ----
    if _is_in_thinking_research_mode(d):
        log("✓ 已切换到「思考研究」模式")
        return True

    log("⚠ 未能确认切换成功（底部工具栏未显示思考研究），但乐观继续")
    return True


# 兼容旧接口名（调用链暂时还在用 enable_deep_thinking）
enable_deep_thinking = enable_thinking_research


# ============================================================
# 输入框 / 发送按钮
# ============================================================

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
        {"textContains": "按住说话"},
        {"descriptionContains": "按住说话"},
        {"textContains": "向千问提问"},
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
        w, h = get_screen_size(d)
        for n in nodes:
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            cls = n["class"].lower()
            value = (n["text"] or n["desc"]).lower()
            is_edit = "edittext" in cls
            is_hint = any(hint.lower() in value for hint in INPUT_HINTS)
            if (is_edit or is_hint) and b[3] >= int(h * 0.70):
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
        cls = n["class"].lower()
        is_edit = "edittext" in cls
        rid = n["rid"].lower()
        value = (n["text"] or n["desc"]).lower()
        is_hint = any(hint.lower() in value for hint in INPUT_HINTS)
        rid_input = any(k in rid for k in ("input", "message", "composer", "et_"))
        if not (is_edit or is_hint or rid_input):
            continue
        x1, y1, x2, y2 = b
        if y1 < int(h * 0.68):
            continue
        width = x2 - x1
        score = y2 * 1000 + width
        if score > best_score:
            best_score = score
            best = b
    return best


def _has_ai_write_panel(d) -> bool:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    for n in nodes:
        value = (n["text"] or n["desc"]) or ""
        v = value.replace(" ", "")
        if "AI写作" in v or "ai写作" in v.lower():
            b = bounds_to_tuple(n["bounds"])
            if b and b[1] < h * 0.75:
                return True
    return False


def _close_ai_write_panel(d) -> bool:
    if not _has_ai_write_panel(d):
        return True
    log("检测到 AI 写作浮层，尝试关闭")

    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
        candidates = []
        for n in nodes:
            b = bounds_to_tuple(n["bounds"])
            if not b:
                continue
            text = (n["text"] or "").strip()
            desc = (n["desc"] or "").lower()
            rid = n["rid"].lower()
            is_close = (
                text in ("×", "x", "✕", "✗", "X")
                or "关闭" in (n["desc"] or "")
                or "close" in desc
                or "close" in rid
            )
            if not is_close:
                continue
            if b[0] < w * 0.7:
                continue
            if b[1] > h * 0.85:
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            candidates.append((area, n))
        for _, node in sorted(candidates, key=lambda x: x[0]):
            if click_node(d, node):
                time.sleep(0.7)
                if not _has_ai_write_panel(d):
                    log("✓ 已关闭 AI 写作浮层")
                    return True
    except Exception:
        pass

    try:
        d.press("back")
        time.sleep(0.8)
        if not _has_ai_write_panel(d):
            log("✓ 已关闭 AI 写作浮层（back）")
            return True
    except Exception:
        pass

    log("⚠ 未能关闭 AI 写作浮层")
    return False


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
        "ai写作", "ai 写作", "aiwrite", "ai_write",
        "深度思考", "生图", "拍题", "视频", "作业",
        "emoji", "表情", "贴纸", "sticker",
        "文件", "file", "上传", "upload",
        "新对话", "新建对话",
    )

    candidates = []
    for n in nodes:
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if not n["enabled"] or not n["visible"]:
            continue
        if not n["clickable"]:
            continue

        x1, y1, x2, y2 = b
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        bw = x2 - x1
        bh = y2 - y1

        if cy < int(h * 0.85):
            continue
        if cx < int(w * 0.85):
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
        if b[3] < int(h * 0.70):
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
        for dx in (30, 40, 50, 60, 20):
            candidates.append((min(w - 5, x2 + dx), cy))

    for xr, yr in (
        (0.955, 0.862),
        (0.945, 0.865),
        (0.965, 0.860),
        (0.955, 0.870),
        (0.950, 0.855),
        (0.960, 0.865),
        (0.940, 0.860),
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


def _detect_voice_mode(d) -> bool:
    """检测当前是否处于语音输入模式（"按住说话" 按钮可见）。"""
    xml = _safe_dump(d)
    if not xml:
        return False
    for kw in ("按住说话", "按住 说话", "Hold to talk", "hold to talk",
               "按住麦克风说话", "录音中"):
        if kw in xml:
            return True
    # 兜底：看底部有没有麦克风类文字
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        for key in ("text", "content-desc"):
            val = (attrs.get(key) or "").strip()
            if "按住说" in val or "hold to" in val.lower() or val == "语音":
                return True
    return False


def _switch_voice_to_keyboard(d) -> bool:
    """当检测到输入区是语音模式（"按住说话"）时，点键盘图标切换为文本输入。

    策略优先级：
      1) OpenCV 模板匹配（button_icons/switch_keyboard.png）—— 最靠谱
      2) XML 里找 keyboard/ime/key 相关 desc/text 的底部左小图标
      3) 固定比例坐标兜底（w*12%, h*85%）
      每步点击后都要验证语音模式真的消失，才算成功。
    """
    if not _detect_voice_mode(d):
        return False
    log("检测到语音输入模式，尝试切换键盘...")

    w, h = get_screen_size(d)

    # 步骤 1：OpenCV 模板匹配
    try:
        from template_matcher import click_switch_keyboard
        pos = click_switch_keyboard(label="模板切键盘")
        if pos is not None:
            time.sleep(0.45)
            if not _detect_voice_mode(d):
                log("✓ 已切换为键盘输入模式（模板匹配）")
                return True
            log("[模板切键盘] 点击后语音模式仍存在，继续兜底")
    except Exception as exc:
        log.debug("模板切键盘异常：%s", exc)

    # 步骤 2：XML 关键词兜底
    xml = _safe_dump(d) or ""
    if xml:
        for m in re.finditer(r"<node\b[^>]*?/?>", xml):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not bm:
                continue
            x1, y1, x2, y2 = (int(v) for v in bm.groups())
            bw, bh = x2 - x1, y2 - y1
            if y1 < int(h * 0.75) or y2 > int(h * 0.97):
                continue
            if x1 > w * 0.35:
                continue
            if bw < 20 or bh < 20:
                continue
            desc = (attrs.get("content-desc") or "").strip()
            text = (attrs.get("text") or "").strip()
            rid = (attrs.get("resource-id") or "").lower()
            combined_low = (desc + " " + text + " " + rid).lower()
            for kw in ("keyboard", "key", "ime", "键盘", "语音", "voice", "mic", "microphone"):
                if kw in combined_low:
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    if _tap_bounds(d, cx, cy, f"切键盘({desc or text or kw})"):
                        time.sleep(0.5)
                        if not _detect_voice_mode(d):
                            log("✓ 已切换为键盘输入模式（XML 关键词）")
                            return True
                    break

    # 步骤 3：固定比例坐标兜底
    cx, cy = int(w * 0.12), int(h * 0.855)
    log(f"[语音→键盘] 兜底点击 ({cx},{cy})")
    if _tap_bounds(d, cx, cy, "切键盘(兜底)"):
        time.sleep(0.5)
        if not _detect_voice_mode(d):
            log("✓ 已切换为键盘输入模式（兜底坐标）")
            return True
        # 点偏了，再试 ±y
        for dy in (-30, 30, 60, -60):
            if _tap_bounds(d, cx, cy + dy, f"切键盘 dy={dy}"):
                time.sleep(0.4)
                if not _detect_voice_mode(d):
                    log("✓ 已切换为键盘输入模式（兜底重试）")
                    return True
    return False


def input_text(d, question: str) -> None:
    # ---- 前置：如果是语音输入模式，先切到键盘 ----
    try:
        _switch_voice_to_keyboard(d)
    except Exception as exc:
        log(f"语音→键盘切换异常（继续）：{exc}")

    edit = find_input(d)
    if edit is None:
        raise RuntimeError("找不到千问输入框")
    # 如果 find_input 找到的还是"按住说话"按钮（没切成文本），也不能用
    try:
        hint = ""
        try:
            hint = (edit.get_text() or "").strip()
        except Exception:
            pass
        if "按住说" in hint or "hold to" in hint.lower():
            raise RuntimeError(f"找到的输入控件是语音按钮，无法 set_text（hint={hint}）")
    except RuntimeError:
        raise
    except Exception:
        pass

    log(f"输入问题：{question[:100]}")
    try:
        edit.click()
    except Exception:
        pass
    time.sleep(0.2)

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
    time.sleep(0.3)


def send_message(d, question: str = "") -> bool:
    log("发送问题")

    _close_ai_write_panel(d)
    time.sleep(0.2)

    btn = find_send_button(d)
    if btn:
        log(f"找到发送按钮：bounds={btn['bounds']} rid={btn['rid'] or '-'}")
        if click_node(d, btn):
            time.sleep(0.6)
            if _message_sent(d, question):
                log("✓ 发送成功（XML 精确定位）")
                return True
            log("点击后消息未发送，继续尝试")
    else:
        log("未在 XML 中找到发送按钮，进入兜底流程")

    if click_first_by_ids(d, KNOWN_SEND_IDS):
        time.sleep(0.6)
        if _message_sent(d, question):
            log("✓ 发送成功（已知 id）")
            return True

    for word in SEND_WORDS:
        for selector in ({"text": word}, {"description": word}):
            try:
                obj = d(**selector)
                if obj.exists(timeout=0.4):
                    obj.click()
                    time.sleep(0.6)
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
        time.sleep(0.6)
        if _message_sent(d, question):
            log("✓ 发送成功（Enter）")
            return True
    except Exception:
        pass

    log("✗ 所有发送方式均未成功")
    return False


# ============================================================
# Composer 状态 / 回答完成检测
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

        hint_hit = any(h.lower() in low for hint in INPUT_HINTS)
        is_edit = "edittext" in cls
        rid_input = any(k in rid for k in ("input", "message", "composer", "prompt"))

        if (is_edit or hint_hit or rid_input) and b and b[3] >= int(h * 0.70):
            result["input_found"] = True
            result["input_bottom"] = True
            result["input_enabled"] = result["input_enabled"] or n["enabled"]
            if hint_hit:
                result["hint_found"] = True
            if value:
                result["input_text"] = value

        if any(
            word.lower() in low
            for word in ("停止生成", "停止回答", "stop generating")
        ):
            if n["enabled"] and (n["clickable"] or b):
                result["stop_control"] = True

        if value in SEND_WORDS or "send" in rid:
            if b and b[1] >= int(h * 0.70):
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


def answer_copy_button_exists(d) -> bool:
    """检测千问最新回答底部的「复制」按钮是否存在。

    千问 AI 回答完后，消息底部会有一排操作按钮：
      语音  复制  收藏  重新生成  ...
    其中「复制」按钮是回答完成的强信号（和豆包一样！）。

    检测优先级：
      1. content-desc = 复制（千问用 desc 标注这个 ImageView）
      2. text = 复制（兜底）
    """
    # 策略1：content-desc 精确匹配（千问 ImageView 按钮通常用 desc）
    for selector in ({"description": "复制"}, {"descriptionContains": "复制"}):
        try:
            obj = d(**selector)
            if obj.exists(timeout=0.3):
                return True
        except Exception:
            pass

    # 策略2：text 精确匹配（兜底，某些版本可能用 text）
    for selector in ({"text": "复制"}, {"textContains": "复制"}):
        try:
            obj = d(**selector)
            if obj.exists(timeout=0.3):
                return True
        except Exception:
            pass

    # 策略3：XML 定位 —— 在回答底部操作栏区域找含"复制"文字的可点击节点
    xml = _safe_dump(d)
    if xml:
        w, h = get_screen_size(d)
        for m in re.finditer(r"<node\b[^>]*?/?>", xml):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not bm:
                continue
            x1, y1, x2, y2 = (int(v) for v in bm.groups())
            text = (attrs.get("text") or "").strip()
            desc = (attrs.get("content-desc") or "").strip()
            combined = text + desc
            if "复制" not in combined:
                continue
            # 排除顶部栏的"复制原文"之类（它在屏幕上方，而回答底部的复制在下方）
            if y1 < h * 0.55:
                continue
            # 必须是可点击按钮
            if attrs.get("clickable") != "true" and "Image" not in (attrs.get("class") or ""):
                continue
            bw, bh = x2 - x1, y2 - y1
            if bw < 20 or bh < 20:
                continue
            return True

    return False


def force_bottom_before_answer_check(d) -> None:
    """把页面拉到底部（轻量版：先点「回到底部」，否则最多滑 2 次，不做选择器轮询）。"""
    try:
        w, h = get_screen_size(d)
        viewport = get_message_viewport(d)
    except Exception:
        return
    for _ in range(2):
        _swipe_in_viewport(d, "up", fraction=0.55, duration=0.35, viewport=viewport)
        time.sleep(0.35)


def _copy_button_in_xml(xml: str, w: int, h: int) -> bool:
    """在已 dump 的 XML 里找回答底部的「复制」按钮（不重复 dump，保证轮询轻量）。"""
    if not xml:
        return False
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        combined = (attrs.get("text") or "") + (attrs.get("content-desc") or "")
        if "复制" not in combined and "Copy" not in combined:
            continue
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        if y1 < h * 0.55:            # 排除顶部栏的「复制原文」
            continue
        if (x2 - x1) < 20 or (y2 - y1) < 20:
            continue
        return True
    return False


def _last_answer_bottom(xml: str, w: int, h: int) -> int:
    """取回答正文最低的 y 坐标（动作栏就在它正下方）。

    千问的正文在 WebView 里以 TextView 形式暴露，取最靠下的长文本节点即可。
    """
    if not xml:
        return 0
    bottom = 0
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        text = clean_text(attrs.get("text") or attrs.get("content-desc") or "")
        if len(text) < 8:
            continue
        if text in IGNORE_EXACT or any(sub in text for sub in IGNORE_SUBSTR):
            continue
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        y2 = int(bm.group(4))
        if y2 < h * 0.10 or y2 > h * 0.86:   # 顶部栏 / 输入区不算正文
            continue
        bottom = max(bottom, y2)
    return bottom


def _screen_gray(d):
    """截一张灰度图（动作栏检测用，比 dump XML 快很多）。"""
    try:
        data = adb_screenshot_bytes()
        return Image.open(BytesIO(data)).convert("L")
    except Exception:
        return None


def _icon_row_present(img, box) -> bool:
    """判断 box 区域内是否出现「一排小图标」（回答底部的 复制/点赞/点踩/收藏/分享）。

    原理：动作栏是一组 3~8 个小图标，横向排列、高度一致、彼此留白；
    用列投影找出深色像素的连通列段，过滤掉宽文本行（很宽）和噪声（很窄），
    若剩余 3 个以上尺寸相近的小段 → 认为动作栏已出现。
    """
    if img is None or np is None:
        return False

    x1, y1, x2, y2 = box
    w_img, h_img = img.size
    x1 = max(0, min(x1, w_img - 1))
    x2 = max(x1 + 1, min(x2, w_img))
    y1 = max(0, min(y1, h_img - 1))
    y2 = max(y1 + 1, min(y2, h_img))
    if x2 - x1 < 60 or y2 - y1 < 12:
        return False

    arr = np.asarray(img.crop((x1, y1, x2, y2)), dtype=np.uint8)
    if arr.size == 0:
        return False

    dark = arr < 170                      # 深色像素（图标/文字）
    col_has = dark.sum(axis=0) >= 2        # 该列至少有 2 个深色像素

    clusters = []
    start = None
    for i, v in enumerate(col_has):
        if v and start is None:
            start = i
        elif not v and start is not None:
            clusters.append((start, i - 1))
            start = None
    if start is not None:
        clusters.append((start, len(col_has) - 1))

    icons = []
    for a, b in clusters:
        cw = b - a + 1
        if cw < 5 or cw > 46:              # 太窄=噪声，太宽=文字行/按钮
            continue
        seg = dark[:, a:b + 1]
        rows = np.where(seg.any(axis=1))[0]
        if len(rows) == 0:
            continue
        ch = int(rows[-1] - rows[0] + 1)
        if ch < 6 or ch > 42:              # 图标的高宽都在这个小范围内
            continue
        icons.append((a, cw, ch))

    if len(icons) < 3 or len(icons) > 12:
        return False

    # 图标应集中在同一行：高度差异不能太大
    heights = [i[2] for i in icons]
    if max(heights) - min(heights) > 18:
        return False

    # 图标之间有明显的等距留白；而中文正文的逐字片段间隙只有 2~6px，用中位数间隔区分
    gaps = [icons[i + 1][0] - (icons[i][0] + icons[i][1]) for i in range(len(icons) - 1)]
    gaps.sort()
    if gaps[len(gaps) // 2] < 10:
        return False

    LOG.debug("[ACTION-BAR] 检出 %d 个小图标 %s", len(icons), icons)
    return True


def answer_action_bar_visible(d, xml: str = "", bottom_y: int = 0) -> bool:
    """检测回答底部动作栏（复制/点赞/点踩/收藏/分享）是否已出现。

    ❌ 旧方案：只在 XML 里找「复制」文案 —— 千问是 UC WebView，图标没有
       text/content-desc，永远找不到，导致回答完成后无限等待。
    ✅ 新方案：按参考截图，检测正文下方那一排小图标的图像结构。
    """
    w, h = get_screen_size(d)
    if not bottom_y:
        bottom_y = _last_answer_bottom(xml, w, h)
    if not bottom_y:
        return False          # 还没拿到正文底部坐标（首次 dump 前不做判定，避免误判）

    img = _screen_gray(d)
    if img is None:
        return False

    # 只判定「正文最低点正下方」这一小条：动作栏紧贴正文，且不会误命中下方工具区
    box = (int(w * 0.02), max(0, bottom_y - int(h * 0.008)),
           int(w * 0.55), min(int(h * 0.84), bottom_y + int(h * 0.045)))
    return _icon_row_present(img, box)


def _answer_looks_complete(answer: str) -> bool:
    """回答文本是否像"已生成完毕的正文"（排除仅剩思考折叠条等占位文案）。"""
    text = clean_text(answer or "")
    if len(text) < ANSWER_MIN_COMPLETE_LENGTH:
        return False
    if text.startswith(("已完成思考", "已深度思考", "思考中", "正在思考")):
        return False
    return True


def wait_answer_finish(d) -> str:
    """等待回答完成（主信号：动作栏图标行；副信号：复制文案 / 文本稳定）。

    参考截图：回答生成完毕后，正文下方会出现一排动作图标（复制、点赞、点踩、收藏、分享）。
    该动作栏一出现即代表回答完成 —— 这是最贴近界面真实状态的判据。

    轮询结构（保证既能快速命中，又不会拖慢）：
      · 每 0.8s 截一张图做动作栏检测（快，~1s/次），连续 2 次命中即完成
      · 每 3 次图像轮询后做一次 XML dump，用于更新正文底部坐标 + 复制文案 + 文本稳定判定
    """
    log("等待千问回答完成：检测回答底部动作栏（复制/点赞/收藏）")

    start_time = time.time()
    w, h = get_screen_size(d)
    time.sleep(ANSWER_MIN_WAIT)

    xml = ""
    answer = ""
    bottom_y = 0
    last_answer = ""
    stable_since = None
    bar_hits = 0
    miss_streak = 0
    last_heartbeat = time.time()

    while time.time() - start_time < ANSWER_HARD_TIMEOUT:
        # ---------- 图像通道：动作栏图标行 ----------
        try:
            if answer_action_bar_visible(d, xml, bottom_y):
                bar_hits += 1
                miss_streak = 0
                if bar_hits >= 2:
                    log("✓ 检测到回答底部动作栏（复制/点赞/收藏），判定回答完成")
                    if not answer:                       # 极快命中时还没 dump 过，补一次
                        xml = _safe_dump(d)
                        answer = best_answer_text(xml) if xml else ""
                    _save_answer_debug(d, "completed_action_bar")
                    return answer
            else:
                bar_hits = 0
                miss_streak += 1
        except Exception as exc:
            LOG.debug("动作栏检测异常：%s", exc)

        # ---------- 每 3 次图像轮询做一次 XML 检测 ----------
        if miss_streak and miss_streak % 3 == 0:
            xml = _safe_dump(d)
            answer = best_answer_text(xml) if xml else answer
            bottom_y = _last_answer_bottom(xml, w, h) if xml else bottom_y

            if _copy_button_in_xml(xml, w, h):
                log("✓ XML 检测到「复制」按钮，判定回答完成")
                _save_answer_debug(d, "completed_copy")
                return answer

            if answer and answer == last_answer and _answer_looks_complete(answer) \
                    and not xml_contains_generating(xml):
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= ANSWER_STABLE_SECONDS:
                    log(f"✓ 回答文本 {ANSWER_STABLE_SECONDS:.0f}s 无变化，判定完成（{len(answer)} 字符）")
                    _save_answer_debug(d, "completed_stable")
                    return answer
            else:
                stable_since = None
                last_answer = answer

            # 长时间看不到动作栏 → 轻量拉一次底部（最多 2 次滑动，无选择器轮询）
            if miss_streak % 12 == 0:
                force_bottom_before_answer_check(d)

        now = time.time()
        if now - last_heartbeat >= 5.0:
            log(f"[等待回答] {now - start_time:.0f}s/{ANSWER_HARD_TIMEOUT}s "
                f"字符={len(answer)} 动作栏={bar_hits} 正文底部={bottom_y}")
            last_heartbeat = now

        time.sleep(IMAGE_POLL_INTERVAL)

    # ---- 超时兜底 ----
    log("⚠ 达到回答超时，尝试兜底提取回答")
    xml = _safe_dump(d)
    fallback = best_answer_text(xml) if xml else ""
    if fallback and len(fallback) >= 10:
        log(f"✓ 超时兜底接受回答：{len(fallback)} 字符")
        _save_answer_debug(d, "completed_timeout")
        return fallback

    raise RuntimeError(
        f"等待回答超时（{ANSWER_HARD_TIMEOUT}s），未检测到回答完成信号"
    )


# ============================================================
# 消息视窗 / 截图基元
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
        is_toolbar_btn = value in {
            "深度思考", "AI 生图", "拍题答疑", "AI 生视频", "作业批改",
        }
        if is_edit or is_hint or is_toolbar_btn:
            candidate_tops.append(b[1])

    if candidate_tops:
        input_top = min(candidate_tops)
        bottom = min(bottom, input_top - 5)

    bottom = max(bottom, top + 100)
    return (int(left), int(top), int(right), int(bottom))


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


def image_signature(img: Image.Image) -> bytes:
    small = img.copy()
    small.thumbnail((240, 600))
    return small.convert("RGB").tobytes()


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
                time.sleep(0.6)
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
        _swipe_in_viewport(d, "down", fraction=0.62, duration=0.35, viewport=viewport)
        time.sleep(0.30)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_top_probe.png", viewport)
            sig = image_signature(img)
            if sig == last_sig:
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
            time.sleep(0.30)
            return True

    viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_BOTTOM_SWIPES):
        _swipe_in_viewport(d, "up", fraction=0.62, duration=0.35, viewport=viewport)
        time.sleep(0.30)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_bottom_probe.png", viewport)
            sig = image_signature(img)
            if sig == last_sig:
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


def _overlap_score(up_feat, lo_feat, ov: int) -> float:
    a = up_feat[-ov:]
    b = lo_feat[:ov]
    if a.shape != b.shape:
        return float("inf")
    return float(np.mean(np.abs(a - b)))


def _find_overlap_scored(upper: Image.Image, lower: Image.Image,
                         expected_delta: Optional[int] = None) -> tuple:
    """返回 (重叠高度, 匹配分数)。

    先做全局逐像素搜索（真实重叠常是奇数，步长 2 会跳过它导致错位）；
    若全局最优并不可信（正文章节里有重复段落时很容易锁到错误位置），
    就退回到「上一屏位移」附近的窗口重搜，避免拼出重复/错位的尾部。
    """
    if np is None:
        raise RuntimeError("长截图需要 numpy：pip install numpy")

    up_feat = _row_features(upper)
    lo_feat = _row_features(lower)
    # 允许接近整屏的重叠（80%~95%）：到底那几帧几乎完全一样；
    # 也允许「整屏重叠」（内容完全相同的两帧），否则会被误判成有新内容
    min_ov = max(10, int(min(len(up_feat), len(lo_feat)) * 0.08))
    max_ov = min(len(up_feat), len(lo_feat))
    if max_ov <= min_ov:
        return min_ov, float("inf")

    best_ov, best_score = min_ov, float("inf")
    for ov in range(min_ov, max_ov + 1):
        score = _overlap_score(up_feat, lo_feat, ov)
        if score < best_score:
            best_score = score
            best_ov = ov

    if best_score <= OVERLAP_GOOD_SCORE or expected_delta is None:
        return best_ov, best_score

    # 全局最优不可信 → 在期望位移附近重搜
    exp_ov = lower.height - expected_delta
    tol = max(20, int(lower.height * OVERLAP_NEAR_RATIO))
    near_ov, near_score = best_ov, best_score
    for ov in range(max(min_ov, exp_ov - tol), min(max_ov, exp_ov + tol) + 1):
        score = _overlap_score(up_feat, lo_feat, ov)
        if score < near_score:
            near_score = score
            near_ov = ov
    return near_ov, near_score


def _frame_deltas(frames: list) -> list:
    """相邻帧的内容位移（像素）：delta = 帧高 - 重叠高度。

    位移可信时用它作为下一缝的期望值（真实滑动距离很稳定），
    这样即使正文有重复段落，也不会把重叠匹配到错误的位置。
    """
    deltas = []
    expected = None
    for i in range(len(frames) - 1):
        ov, score = _find_overlap_scored(frames[i], frames[i + 1], expected)
        delta = max(0, frames[i + 1].height - ov)
        deltas.append(delta)
        if score <= OVERLAP_GOOD_SCORE:
            expected = delta
        elif expected is None:
            expected = delta
    return deltas


def _detect_float_button(img: Image.Image) -> Optional[tuple]:
    """定位消息区底部「回到底部 ↓」浮动按钮，返回它的外接矩形。

    实测特征：白色圆盘（比正文底色的 245 更白）里嵌一个左右对称的向下箭头，
    位置固定在消息区靠下处。这里只在该固定区域内搜索，并用箭头形状严格校验，
    避免把回答动作栏的分享/复制/收藏图标误当成按钮（误判会把图标擦掉或拼花）。
    """
    if np is None:
        return None

    W, H = img.size
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    win = max(48, int(W * 0.095))          # 按钮边长约 9.5% 屏宽
    half = win // 2
    pad = 6

    x_lo, x_hi = int(W * 0.30), int(W * 0.70) - win
    y_lo, y_hi = int(H * 0.84), H - win
    if x_hi <= x_lo or y_hi <= y_lo:
        return None

    for y in range(y_lo, y_hi + 1, 2):
        for x in range(x_lo, x_hi + 1, 2):
            inner = arr[y + pad:y + win - pad, x + pad:x + win - pad]
            if inner.size == 0:
                continue
            if float((inner >= 250).sum()) / inner.size < 0.45:
                continue                    # 圆盘不够白 → 不是按钮
            cx, cy = x + half, y + half
            core = arr[cy - 20:cy + 20, cx - 20:cx + 20]
            if core.shape != (40, 40):
                continue
            dmask = core < 150
            dark_frac = float(dmask.sum()) / dmask.size
            if not (0.02 <= dark_frac <= 0.30):
                continue
            if not _is_down_arrow(dmask):
                continue
            # 命中箭头后，用圆盘自身的纯白区域收紧外接矩形：
            # 只覆盖箭头附近的窗口会留下圆盘边缘的白边，拼进长截图里很明显
            bx1, by1 = max(0, cx - 40), max(0, cy - 40)
            disc = arr[by1:cy + 40, bx1:cx + 40] >= 250
            dys, dxs = np.where(disc)
            if len(dxs) == 0:
                continue
            dx1, dy1 = bx1 + int(dxs.min()), by1 + int(dys.min())
            dx2, dy2 = bx1 + int(dxs.max()), by1 + int(dys.max())
            dw, dh = dx2 - dx1 + 1, dy2 - dy1 + 1
            if not (50 <= dw <= 88 and 50 <= dh <= 88):
                continue                    # 亮区太大 → 不是圆盘（例如整片白色背景）
            return (max(0, dx1 - 2), max(0, dy1 - 2),
                    min(W, dx2 + 3), min(H, dy2 + 3))
    return None


def _is_down_arrow(mask: "np.ndarray") -> bool:
    """判断暗色像素块是否是「↓」箭头（细竖杆 + 下半部张开 + 左右对称）。

    分享箭头的暗块形状不对称，复制/收藏图标是方框，都会被这里排除。
    """
    ys, xs = np.where(mask)
    if len(xs) < 40:
        return False
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    bw, bh = x2 - x1 + 1, y2 - y1 + 1
    if not (12 <= bw <= 30 and 22 <= bh <= 34):
        return False

    sub = mask[y1:y2 + 1, x1:x2 + 1]
    counts = sub.sum(axis=1)
    h_rows = sub.shape[0]
    stem = counts[:max(1, int(h_rows * 0.4))]
    if float(stem.mean()) > 9:
        return False                        # 上半部必须是细杆，不能是文字/方块
    if int(counts.max()) < 12:
        return False                        # 箭头必须有明显张开
    if int(counts[-1]) > 6:
        return False                        # 底部要收尖

    # 箭头尖端落在中线上：底部 1/4 的中线必须是暗的（五角星、描边图标的底部中线是空的）
    center_cols = sub[max(0, int(h_rows * 0.75)):, max(0, sub.shape[1] // 2 - 3):sub.shape[1] // 2 + 4]
    if center_cols.size == 0 or float(center_cols.any(axis=1).mean()) < 0.8:
        return False

    center = (sub.shape[1] - 1) / 2.0
    sym = 0
    rows = 0
    for r in range(h_rows):
        cols = np.where(sub[r])[0]
        if len(cols) == 0:
            continue
        rows += 1
        if abs((center - cols.min()) - (cols.max() - center)) <= 2.5:
            sym += 1
    return rows > 0 and sym >= rows * 0.6


def _erase_float_buttons(frames: list, deltas: list) -> list:
    """用后面某一帧的对应内容覆盖掉每帧里的「回到底部」浮动按钮。

    原理：按钮固定在屏幕同一位置，而内容在滚动 → 同一段内容在后面的帧里会往上移，
    移到按钮区域之外，是干净的。用它覆盖按钮区域即可，且不会丢正文。
    下一帧可能没滚动（内容几乎没动）或检测漏判，所以向后多找几帧，
    只有内容真正上移到按钮高度之外才拿来当像素源。
    """
    if np is None or len(frames) < 2:
        return frames

    cleaned = 0
    failed = 0
    for i in range(len(frames) - 1):
        frame = frames[i]
        box = _detect_float_button(frame)
        if not box:
            continue

        x1, y1, x2, y2 = box
        cum = 0
        patched = False
        for j in range(i + 1, min(i + 4, len(frames))):
            cum += max(0, deltas[j - 1])
            if cum < FLOAT_BTN_SHIFT_MIN:
                continue                    # 上移不足 → 源像素里还带着按钮
            sy1, sy2 = y1 - cum, y2 - cum
            if sy1 < 0 or sy2 > frames[j].height:
                break                       # 再往后上移更多，只会更越界
            try:
                patch = frames[j].crop((x1, sy1, x2, sy2))
                frame.paste(patch, (x1, y1))
                if _detect_float_button(frame):
                    continue                # 补完仍能检到按钮 → 换更靠后的干净帧
                patched = True
                cleaned += 1
            except Exception as exc:
                LOG.debug("浮动按钮修补失败：%s", exc)
            break

        if not patched:
            failed += 1

    if cleaned:
        log(f"已修补 {cleaned} 帧中的「回到底部」浮动按钮")
    if failed:
        log(f"[WARN] {failed} 帧的浮动按钮无法修补（后面没有可用的干净帧）")
    return frames


def stitch_frames(frames: list, output: Path) -> Image.Image:
    if not frames:
        raise RuntimeError("没有可拼接的截图帧")

    # 先算好相邻帧位移：既用于拼接，也用于修补浮动按钮（必须是原始帧上的位移）
    deltas = _frame_deltas(frames)

    # ① 去掉浮动按钮（否则会被拼进长截图，参考图里标出的就是这个按钮）
    frames = _erase_float_buttons(frames, deltas)

    # ② 丢掉最后一帧：到底后多截的一帧几乎没有新内容，留着就会在尾部重复一段
    if len(frames) >= 2 and deltas[-1] <= int(frames[-1].height * 0.06):
        frames.pop()
        deltas.pop()
        log("丢弃最后一帧（几乎没有新增内容，避免尾部重复）")

    canvas = frames[0].copy()
    for idx in range(1, len(frames)):
        delta = deltas[idx - 1]
        if delta <= 2:
            continue                        # 和上一帧几乎完全相同 → 没有新内容
        delta = min(delta, frames[idx].height - 1)
        new_part = frames[idx].crop((0, frames[idx].height - delta,
                                     frames[idx].width, frames[idx].height))
        if new_part.height <= 0:
            continue
        merged = Image.new("RGB", (canvas.width, canvas.height + new_part.height))
        merged.paste(canvas, (0, 0))
        merged.paste(new_part, (0, canvas.height))
        canvas = merged
    canvas.save(output, format="PNG", optimize=False)
    return canvas


# ============================================================
# ★ v8.0：思考详情页采集（OCR 双通道）
# ============================================================

def _find_expand_all_button(d) -> Optional[dict]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    candidates = []
    for n in nodes:
        value = (n["text"] or n["desc"]) or ""
        if not value:
            continue
        if not any(kw in value for kw in EXPAND_ALL_BUTTON_WORDS):
            continue
        if len(value) > 12:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] > h * 0.80:
            continue
        if b[3] < h * 0.03:
            continue
        score = b[1]
        if n["clickable"]:
            score -= 10000
        candidates.append((score, n))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    return {
        "node": chosen,
        "bounds": bounds_to_tuple(chosen["bounds"]),
    }


def _find_thinking_card(d) -> Optional[dict]:
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    candidates = []
    for n in nodes:
        value = (n["text"] or n["desc"]) or ""
        if not value:
            continue
        if not any(kw in value for kw in THINKING_CARD_HINTS):
            continue
        if len(value) > 60:
            continue
        b = bounds_to_tuple(n["bounds"])
        if not b:
            continue
        if b[1] > h * 0.80:
            continue
        score = b[1]
        if n["clickable"]:
            score -= 10000
        candidates.append((score, n))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    return {
        "node": chosen,
        "bounds": bounds_to_tuple(chosen["bounds"]),
    }


# ============================================================
# 千问「思考研究」模式 — 思考过程采集
#
# 千问新版思考过程结构（真实截图）：
#
#   [AI 回答正文]
#   ┌─ 折叠区（未展开）─────────────────────────┐
#   │ 已完成思考，参考了 17 篇资料  >            │  ← 点击展开
#   └────────────────────────────────────────────┘
#
#   ┌─ 展开后（内联，非独立页面）───────────────┐
#   │ 深度思考                            ∨      │  ← 可再次点击折叠
#   │ • 调用搜索工具                            │
#   │   用户询问性能最佳的手机...                │
#   │ 🔍 搜索4个关键词，参考了17篇资料          │
#   │   "2026年手机处理器性能排行"              │
#   │   ...                                     │
#   │ [展开全部]                                │
#   │ ...（滚动查看完整思考链）                  │
#   └────────────────────────────────────────────┘
#
# 核心变化：思考过程是对话内联区域，不再是独立详情页！
# ============================================================

# 折叠态入口文字（AI 回答正上方可点击的折叠条）
THINKING_COLLAPSED_ENTRY_WORDS = (
    "已完成思考", "已深度思考", "思考过程",
    "参考了", "参考了.*篇",
)

# 展开后的思考抽屉标题
THINKING_EXPANDED_TITLE_WORDS = ("深度思考", "思考过程")


def _find_thinking_collapsed_entry(d) -> Optional[dict]:
    """找折叠态的思考入口按钮 —— 「已完成思考，参考了 xx 篇资料 >」

    策略：在屏幕中上区域找含"已完成思考"或"思考过程"或"参考了"的可点击节点。
    """
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if not xml:
        return None

    candidates = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        combined = text + desc

        if not combined:
            continue
        if len(combined) > 60:
            continue  # 太长的不是折叠条

        matched = False
        for kw in THINKING_COLLAPSED_ENTRY_WORDS:
            if kw in combined:
                matched = True
                break
        if not matched:
            continue

        # 位置过滤：在屏幕上半部分（AI 回答正上方）
        if y1 > h * 0.70:
            continue
        if y2 < h * 0.05:
            continue

        # 必须是一个相对较大的区域（折叠条一般宽度接近屏幕）
        bw = x2 - x1
        if bw < w * 0.30:
            continue

        # 偏好可点击
        score = y1
        if attrs.get("clickable") == "true":
            score -= 10000

        candidates.append((score, {
            "text": combined,
            "bounds": (x1, y1, x2, y2),
            "attrs": attrs,
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
        }))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    return chosen


def _find_expand_all_button_in_thinking(d) -> Optional[dict]:
    """在已展开的思考抽屉里找「展开全部」按钮（内部折叠内容的展开按钮）。"""
    w, h = get_screen_size(d)
    xml = _safe_dump(d)
    if not xml:
        return None

    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        text = (attrs.get("text") or "").strip()
        if text != "展开全部":
            continue
        return {
            "text": text,
            "bounds": (x1, y1, x2, y2),
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
        }
    return None


def _find_thinking_entry(d) -> Optional[dict]:
    """新管线入口：优先找内联折叠态「已完成思考，参考了xx篇」按钮。"""
    entry = _find_thinking_collapsed_entry(d)
    if entry:
        log(f"找到折叠态思考入口：{entry['text'][:30]!r} bounds={entry['bounds']}")
        return entry
    return None


def _has_thinking_expanded_title(d) -> bool:
    """检测思考抽屉是否已展开（XML 里出现「深度思考」标题）。"""
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    for n in nodes:
        for field in ("text", "desc"):
            v = (n.get(field) or "").strip()
            if not v or len(v) > 20:
                continue
            if v in THINKING_EXPANDED_TITLE_WORDS:
                return True
    return False


def _wait_thinking_expanded(d, timeout: float = 5.0) -> bool:
    """等待思考抽屉内联展开。"""
    log("  等待思考抽屉展开...")
    started = time.time()
    while time.time() - started < timeout:
        if _has_thinking_expanded_title(d):
            time.sleep(0.3)
            log("  ✓ 思考抽屉已展开")
            return True
        time.sleep(0.25)
    log(f"  ⚠ 思考抽屉展开超时（{timeout:.0f}s）")
    return _has_thinking_expanded_title(d)  # 最后试一次


def _exit_thinking_detail(d) -> None:
    """新管线：内联模式下，再次点击思考抽屉标题收起即可。"""
    # 尝试：再次点「深度思考」标题收起
    xml = _safe_dump(d)
    if xml:
        for m in re.finditer(r"<node\b[^>]*?/?>", xml):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not bm:
                continue
            x1, y1, x2, y2 = (int(v) for v in bm.groups())
            text = (attrs.get("text") or attrs.get("content-desc") or "").strip()
            if text in THINKING_EXPANDED_TITLE_WORDS:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                _tap_bounds(d, cx, cy, f"收起思考抽屉({text})")
                time.sleep(0.4)
                log("✓ 已点击思考抽屉标题收起")
                return

    # 兜底：按 back
    try:
        d.press("back")
        time.sleep(0.5)
    except Exception:
        pass


def _find_thinking_header_bounds(d) -> Optional[tuple]:
    """找到「深度思考」展开标题的 bounds —— 用于确定思考内容的 y 范围起点。"""
    xml = _safe_dump(d)
    if xml:
        for m in re.finditer(r"<node\b[^>]*?/?>", xml):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
            bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not bm:
                continue
            x1, y1, x2, y2 = (int(v) for v in bm.groups())
            text = (attrs.get("text") or "").strip()
            if text in THINKING_EXPANDED_TITLE_WORDS:
                return (x1, y1, x2, y2)
    return None


def _collect_thinking_detail_inline(d, question: str = "") -> str:
    """新管线：内联采集思考内容（在对话内联区域往下滚）。

    和旧版 _collect_thinking_detail 不同：
    - 旧版假设进入独立详情页 → 用 _thinking_detail_viewport 全视窗采集
    - 新版在对话内联区域采集 → 需要 y 范围过滤（在思考标题下方、AI 回答上方）
    """
    log("  内联采集思考内容...")
    w, h = get_screen_size(d)

    # 先滚到思考抽屉顶部（标题可见）
    header_bounds = _find_thinking_header_bounds(d)
    if header_bounds:
        log(f"  思考抽屉标题位置: {header_bounds}")

    # 点「展开全部」（如果有）—— 让完整思考链可见
    expand_all = _find_expand_all_button_in_thinking(d)
    if expand_all:
        log("  点「展开全部」按钮")
        _tap_bounds(d, expand_all["center"][0], expand_all["center"][1], "展开全部")
        time.sleep(0.40)

    # 开始采集：滚到底，逐屏收集文本
    seen_fps = set()
    all_items = []
    prev_sig = None
    stable_bottom = 0

    for round_idx in range(THINKING_DETAIL_MAX_FRAMES):
        xml = _safe_dump(d)
        frame_items = []

        if xml:
            for m in re.finditer(r"<node\b[^>]*?/?>", xml):
                attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
                bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
                if not bm:
                    continue
                x1, y1, x2, y2 = (int(v) for v in bm.groups())
                text = _clean_special_chars((attrs.get("text") or attrs.get("content-desc") or "").strip())
                if not text or len(text) < 2:
                    continue
                if re.fullmatch(r"[\s\d\W]+", text) and len(text) < 6:
                    continue
                if text in ("千问", "Qwen"):
                    continue
                if any(sub in text for sub in IGNORE_SUBSTR):
                    continue

                # y 范围过滤：只收集在思考抽屉区域内的节点
                # 思考抽屉一般在屏幕中上 20% ~ 70% 之间
                if y1 < h * 0.08:
                    continue  # 顶部栏
                if y1 > h * 0.75:
                    continue  # 底部工具栏/输入框
                # 如果有标题 bounds，更精准过滤：内容应该在标题下方
                if header_bounds and y1 < header_bounds[3]:
                    continue

                # 宽度过滤：排除底部工具栏按钮（小尺寸）
                bw = x2 - x1
                if bw < 40:
                    continue

                frame_items.append((y1, text))

        # 去重 + 累计
        added = 0
        for y, text in sorted(frame_items, key=lambda x: x[0]):
            fp = _text_fingerprint(text)
            if not fp or fp in seen_fps:
                continue
            seen_fps.add(fp)
            all_items.append((round_idx, y, text))
            added += 1

        log(f"  思考采集第 {round_idx + 1} 屏：新增 {added} 条，累计 {len(all_items)}")

        # 判断是否到底
        try:
            img = save_message_area(d, OUTPUT_DIR / "_think_bottom_probe.png",
                                     (0, int(h * 0.10), w, int(h * 0.75)))
            sig = image_signature(img)
            if sig == prev_sig and added == 0:
                stable_bottom += 1
                if stable_bottom >= THINKING_DETAIL_STABLE_ROUNDS:
                    log("  ✓ 思考内容已到底")
                    break
            else:
                stable_bottom = 0
            prev_sig = sig
        except Exception as exc:
            LOG.debug("思考底部探测失败：%s", exc)

        # 滑动（向上滚看更多）
        try:
            d.swipe(w // 2, int(h * 0.62), w // 2, int(h * 0.30),
                    duration=THINKING_DETAIL_SWIPE_DURATION)
            time.sleep(THINKING_DETAIL_SETTLE)
        except Exception:
            break

    all_items.sort(key=lambda x: (x[0], x[1]))
    raw_texts = [t for _, _, t in all_items]
    filtered = _filter_substring_dupes(raw_texts)
    result = _join_answer_texts(filtered)
    return result


def collect_thinking_content(d, question: str = "") -> str:
    """采集千问「思考研究」模式的思考过程（新版内联管线）。

    流程：
      1. 滚到对话顶部（AI 回答正上方应该能看到折叠入口）
      2. 找折叠态入口「已完成思考，参考了xx篇资料」→ 点击展开
      3. 验证抽屉展开（出现「深度思考」标题）
      4. 可选：点「展开全部」看完整思考链
      5. 在内联区域滚动采集所有思考过程文本
      6. 再次点击折叠标题收起
    """
    log("=" * 60)
    log("采集深度思考内容（v9.0：内联折叠区管线）")
    log("=" * 60)

    if not _HAS_OCR:
        log("⚠ 未安装 rapidocr-onnxruntime，将仅用 XML 采集")

    # ---- 1. 先滚到对话顶部，让思考折叠入口可见 ----
    log("  滚到对话顶部...")
    scroll_to_top(d)
    time.sleep(0.5)

    # ---- 2. 找折叠态入口 ----
    entry = _find_thinking_entry(d)
    if not entry:
        log("⚠ 未找到折叠态思考入口（可能不是思考研究模式或已经展开）")
        # 看看是不是已经展开了
        if _has_thinking_expanded_title(d):
            log("  但检测到抽屉已展开，直接采集")
        else:
            log("  确认无思考过程，返回空")
            return ""

    # ---- 3. 点击展开 ----
    if entry:
        cx, cy = entry["center"]
        log(f"  点击折叠入口：{entry['text'][:30]!r}")
        if not _tap_bounds(d, cx, cy, f"折叠入口({entry['text'][:20]})"):
            log("⚠ 点击折叠入口失败")
            return ""
        time.sleep(0.45)

        # ---- 4. 等待展开 ----
        _wait_thinking_expanded(d, timeout=5.0)

    # 诊断保存
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dump_xml(d, OUTPUT_DIR / f"think_detail_{stamp}.xml")
        save_screen(OUTPUT_DIR / f"think_detail_{stamp}.png")
    except Exception as exc:
        LOG.debug("保存思考诊断失败：%s", exc)

    if not _has_thinking_expanded_title(d):
        log("✗ 展开后仍未检测到「深度思考」标题，放弃采集")
        _exit_thinking_detail(d)
        return ""

    log("✓ 思考抽屉已展开，开始采集")

    # ---- 5. 内联滚动采集 ----
    thinking_text = ""
    try:
        thinking_text = _collect_thinking_detail_inline(d, question)
        log(f"✓ 思考过程文本采集完成：{len(thinking_text)} 字符")
    except Exception as exc:
        LOG.debug("思考过程采集失败：%s", exc)

    # ---- 6. 收起 ----
    _exit_thinking_detail(d)

    return thinking_text


# 兼容旧接口（如果有代码还在引用这几个旧函数）
_find_expand_all_button = _find_expand_all_button_in_thinking
_find_thinking_card = _find_thinking_collapsed_entry


# ============================================================
# 文本清理 / 过滤
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


def _collect_screen_texts(
    d,
    viewport: tuple,
    question: str,
    seen_fps: set,
) -> list:
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
# 主问答长截图 + 文本收集
# ============================================================

def screenshot_main_with_text(d, output: Path, question: str) -> tuple:
    log("开始主问答长截图（v8.0）")

    if not scroll_to_top(d):
        raise RuntimeError("无法确认聊天顶部")

    time.sleep(0.5)
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport}")

    frames = []
    prev_sig = None
    stable_bottom = 0

    seen_fps = set()
    screens_texts = []
    screen_idx = 0

    for i in range(MAX_CAPTURE_FRAMES):
        current = save_message_area(d, OUTPUT_DIR / f"_frame_{i:03d}.png", viewport)
        sig = image_signature(current)
        if not frames:
            frames.append(current.copy())
            log("已采集第 1 屏")
        elif sig != prev_sig:
            frames.append(current.copy())
            log(f"已采集第 {len(frames)} 屏")
        else:
            log("当前屏与上一屏相同，跳过")
        prev_sig = sig

        items = _collect_screen_texts(d, viewport, question, seen_fps)
        if not items:
            time.sleep(0.25)
            items = _collect_screen_texts(d, viewport, question, seen_fps)

        if items:
            screens_texts.append((screen_idx, items))
            screen_idx += 1
            log(f"  屏 {screen_idx} 新增文本 {len(items)} 条")
        else:
            log("  本屏无新文本")

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

        try:
            after = save_message_area(d, OUTPUT_DIR / "_probe_after.png", viewport)
            after_sig = image_signature(after)
            if frames and after_sig == image_signature(frames[-1]):
                stable_bottom += 1
                log(f"[BOTTOM] 未移动 {stable_bottom}/{SCROLL_STABLE_ROUNDS}")
                if stable_bottom >= SCROLL_STABLE_ROUNDS:
                    break
            else:
                stable_bottom = 0
        except Exception as exc:
            LOG.debug("底部探测异常：%s", exc)

    if not frames:
        raise RuntimeError("没有采集到聊天截图")

    if len(frames) > 1 and image_signature(frames[-1]) == image_signature(frames[-2]):
        frames.pop()

    log(f"共采集 {len(frames)} 屏，开始拼接")
    stitch_frames(frames, output)
    log(f"主问答长截图完成：{output}")

    ordered_texts = []
    for _, items in screens_texts:
        for _, t in items:
            ordered_texts.append(t)

    log(f"原始文本条目：{len(ordered_texts)}")

    deduped = _filter_substring_dupes(ordered_texts)
    log(f"去重后文本条目：{len(deduped)}")

    trimmed = _trim_tail_to_body(deduped)
    log(f"尾部裁剪后文本条目：{len(trimmed)}")

    answer_text = _join_answer_texts(trimmed)
    return answer_text, ""


# ============================================================
# 参考资料面板
# ============================================================

def _find_reference_entry(d) -> Optional[dict]:
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

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


def open_reference_panel(d, max_attempts: int = 2) -> bool:
    log("尝试打开参考资料面板")
    for attempt in range(max_attempts):
        entry = _find_reference_entry(d)
        if not entry:
            log(f"未找到资料入口（第 {attempt+1} 次）")
            try:
                _swipe_in_viewport(d, "down", fraction=0.3, duration=0.6)
                time.sleep(0.8)
            except Exception:
                pass
            continue

        log(f"找到入口：{(entry['node']['text'] or entry['node']['desc'])[:40]}")
        if click_node(d, entry["node"]):
            time.sleep(2.0)
            if _has_reference_panel(d):
                log("✓ 参考资料面板已打开")
                return True
            log("点击后未检测到面板")
    return False


def _get_panel_viewport(d) -> tuple:
    w, h = get_screen_size(d)
    top = int(h * 0.06)
    try:
        nodes = parse_nodes(dump_xml(d))
        for n in nodes:
            value = n["text"] or ""
            if any(m in value for m in REFERENCE_PANEL_MARKERS):
                b = bounds_to_tuple(n["bounds"])
                if b:
                    top = max(0, b[1] - int(h * 0.03))
                    break
    except Exception:
        pass
    return (int(w * 0.02), top, int(w * 0.98), int(h * 0.94))


def screenshot_reference_panel(d, output: Path) -> bool:
    log("开始参考资料面板长截图")
    viewport = _get_panel_viewport(d)
    frames = []
    prev_sig = None
    stable_bottom = 0

    for i in range(MAX_CAPTURE_FRAMES):
        try:
            current = save_message_area(d, OUTPUT_DIR / f"_panel_{i:03d}.png", viewport)
        except Exception as exc:
            LOG.debug("面板截图失败：%s", exc)
            break
        sig = image_signature(current)
        if not frames:
            frames.append(current.copy())
            log("面板已采集第 1 屏")
        elif sig != prev_sig:
            frames.append(current.copy())
            log(f"面板已采集第 {len(frames)} 屏")
        prev_sig = sig

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

        try:
            after = save_message_area(d, OUTPUT_DIR / "_panel_probe.png", viewport)
            after_sig = image_signature(after)
            if frames and after_sig == image_signature(frames[-1]):
                stable_bottom += 1
                log(f"[PANEL-BOTTOM] 未移动 {stable_bottom}/{SCROLL_STABLE_ROUNDS}")
                if stable_bottom >= SCROLL_STABLE_ROUNDS:
                    break
            else:
                stable_bottom = 0
        except Exception:
            pass

    if not frames:
        log("面板未采集到任何帧")
        return False
    if len(frames) > 1 and image_signature(frames[-1]) == image_signature(frames[-2]):
        frames.pop()
    stitch_frames(frames, output)
    log(f"面板长截图完成：{output}")
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
        "_panel_probe.png", "_think_probe.png",
        "_think_top_probe.png", "_think_bottom_probe.png",
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

    # 1. 新对话
    if not new_chat(d):
        raise RuntimeError("未能确认新建对话。")
    time.sleep(0.6)

    # 2. 开启深度思考
    try:
        enable_deep_thinking(d)
    except Exception as exc:
        LOG.debug("开启深度思考异常（继续）：%s", exc)

    # 3. 输入
    input_text(d, question)

    # 4. 发送
    if not send_message(d, question):
        raise RuntimeError("问题发送失败")

    # 5. 等待回答完成
    coarse_answer = wait_answer_finish(d)
    log(f"粗判回答完成，粗文本 {len(coarse_answer)} 字符")

    # 6. 采集深度思考
    thinking_text = ""
    try:
        scroll_to_bottom(d)
        time.sleep(0.35)
        scroll_to_top(d)
        time.sleep(0.5)
        thinking_text = collect_thinking_content(d, question=question)
        log(f"✓ 深度思考过程：{len(thinking_text)} 字符")
    except Exception as exc:
        LOG.debug("思考过程采集失败（继续）：%s", exc)
        thinking_text = ""

    # 7. 主回答长截图
    scroll_to_bottom(d)
    time.sleep(0.35)

    main_path = task_dir / "answer_main.png"
    answer_text, _ = screenshot_main_with_text(d, main_path, question)

    if len(clean_text(answer_text)) < len(clean_text(coarse_answer)):
        log(f"拼接文本较短，使用粗判文本兜底（{len(coarse_answer)} 字符）")
        answer_text = coarse_answer
    answer_text = clean_text(answer_text)
    thinking_text = clean_text(thinking_text)

    if len(answer_text) < ANSWER_MIN_COMPLETE_LENGTH // 2:
        raise RuntimeError(f"回答文本过短（{len(answer_text)} 字符）")

    log(f"✓ 完整回答：{len(answer_text)} 字符")
    log(f"✓ 思考过程：{len(thinking_text)} 字符")

    # 8. 参考资料面板
    sources_path = task_dir / "answer_sources.png"
    has_sources = False
    try:
        scroll_to_bottom(d); time.sleep(0.35)
        scroll_to_top(d); time.sleep(0.5)
        if open_reference_panel(d):
            if screenshot_reference_panel(d, sources_path):
                has_sources = True
            close_reference_panel(d)
    except Exception as exc:
        LOG.debug("参考资料面板处理失败（可忽略）：%s", exc)
        try:
            close_reference_panel(d)
        except Exception:
            pass

    # 9. 合并
    final_image = task_dir / "answer.png"
    if has_sources:
        merge_two_screenshots(main_path, sources_path, final_image)
        log("✓ 主问答 + 参考资料面板已合并")
    else:
        merge_two_screenshots(main_path, None, final_image)
        log("（无参考资料面板，仅主问答截图）")

    # 10. 落盘
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

    # 11. 上传
    if not _HAS_WORKER_LIB:
        raise RuntimeError("任务池模式需要 worker_lib.py。")

    log("上传截图")
    image_url = worker_upload(str(final_image), task_id=unit_id)
    if not image_url:
        raise RuntimeError("截图上传失败")

    # 12. 回调
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
    diag_dir = BASE_DIR / "qianwen_diagnose" / timestamp
    diag_dir.mkdir(parents=True, exist_ok=True)
    log(f"当前前台：{d.app_current()}")
    w, h = get_screen_size(d)
    log(f"屏幕：{w}x{h}")
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport}")

    entry = _find_thinking_entry(d)
    if entry:
        val = entry.get("text", "")[:40]
        log(f"★ 检测到思考入口：{val!r} bounds={entry['bounds']}")
    else:
        log("★ 未检测到折叠态思考入口")

    log(f"思考抽屉是否已展开：{_has_thinking_expanded_title(d)}")

    dump_xml(d, diag_dir / "window.xml")
    save_screen(diag_dir / "screen.png")
    log(f"诊断完成：{diag_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="千问 Android Worker v8.0")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log("=" * 70)
    log("千问 Android Worker v8.0（XML + OCR 双通道）")
    log(f"platform={PLATFORM_NAME}")
    log("=" * 70)

    d = None
    try:
        d = connect_device()
        start_qianwen(d)
        if args.diagnose:
            diagnose(d)
            return 0
        run_worker(d)
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
        return 0
    except Exception as exc:
        log(f"❌ 主循环异常：{exc}")
        traceback.print_exc()
        return 1
    finally:
        if d is not None:
            try:
                d.app_stop("com.aliyun.tongyi")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
