# -*- coding: utf-8 -*-
"""
wenxin_worker.py —— 文心 App 问答采集 Worker（基于 deepseek_worker.py v6 适配）
============================================================================
整体流程（对应用户标注图片 1~10）：
  1. 新建对话：点左上角「☰」打开左侧抽屉 → 点「新建对话」
  2. 切换模式：确保处于「对话」模式（非「工作」），可选按配置切换模型
  3. 点击输入框，输入问题
  4. 发送问题，等待 AI 回答完成
  5. 采集思考过程（如有，灰色文本）与回答正文（黑色文本）
  6. 从对话顶部到底部拼接长截图（避开底部输入框，不截入输入框）
  7. 打开回答末尾的「网页来源」入口，对来源面板同样拼长截图
  8. 合并两张长截图 → 生成 HTML → 上传截图 → 回调任务结果

与 deepseek_worker.py 的关键差异：
  - 包名换成文心 com.baidu.newapp，平台标识 wenxin_app；
  - 文心所有 resource-id 均被混淆为 obfuscated，完全不依赖 resource-id，
    一律「文本/desc 锚点 + 比例坐标兜底」；
  - 移除 DeepSeek「深度思考按钮开启」模块，替换为文心「切换模式」步骤；
  - 来源入口文案换成文心的「网页来源」等写法；
  - 截图拼接、字体颜色分类（灰=思考 / 黑=正文）、到底判定等核心机制全部保留。
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import subprocess
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence, Set, Tuple

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

PLATFORM_NAME = "wenxin_app"
WENXIN_PACKAGE = "com.baidu.newapp"

ANSWER_HARD_TIMEOUT = 600
ANSWER_MIN_WAIT = 6.0
ANSWER_MIN_COMPLETE_LENGTH = 40  # 回答最低有效字符数（避免短回答因门槛过高被误判为未完成）
ANSWER_STABLE_SECONDS = 4.0
ANSWER_STABLE_MIN_ROUNDS = 3
POLL_INTERVAL = 1.0
POLL_INTERVAL_SLOW = 2.0

# 发送后最低等待时间（秒）——防止 WebView 不可见时画面稳定兜底过早误判
MIN_ANSWER_WAIT_AFTER_SEND = 20.0

MAX_TOP_SWIPES = 25
MAX_BOTTOM_SWIPES = 25
SCROLL_STABLE_ROUNDS = 3

MAX_CAPTURE_FRAMES = 200
CAPTURE_SWIPE_DURATION = 1.2
CAPTURE_SETTLE_WAIT = 2.0
CAPTURE_FRACTION = 0.42

GENERATING_WORDS = (
    "停止生成", "停止回答", "生成中", "正在生成", "正在回答", "回答中",
    "思考中", "深度思考", "深度思考中", "已深度思考", "正在思考",
    "Stop generating", "Stop response", "Generating response",
    "Thinking", "Deep thinking", "Thinking...",
)
COMPLETION_WORDS = (
    "复制", "重新生成", "再试一次", "分享", "点赞", "点踩",
    "Copy", "Regenerate", "Share", "Like", "Dislike",
)
# 文心输入框提示语（标注图③：发消息或按住说话）
INPUT_HINTS = ("发消息或按住说话", "发消息", "输入", "Ask anything", "Message", "Send a message")
# 文心「新建对话」入口（左侧抽屉内，标注图②）
NEW_CHAT_WORDS = ("新建对话", "新对话", "新聊天", "New chat")
SEND_WORDS = ("发送", "Send")
BOTTOM_WORDS = ("回到底部", "返回底部", "到底部", "Jump to bottom", "Scroll to bottom")
THINKING_WORDS = ("深度思考", "已思考", "思考中", "DeepThink", "Thinking", "思考过程")
# 文心「网页来源」入口相关文案（用户明确要求采集网页来源入口长截图）
SOURCE_WORDS = ("网页来源", "来源", "参考网页", "参考资料", "参考", "搜索", "Sources", "Search")

# ---------- 思考面板标题 / 展开按钮 相关常量 ----------

THINKING_TITLE_PATTERN = re.compile(
    r"已思考|思考中|已深度思考|深度思考中|DeepThink|Thought for|Thinking"
)

THINKING_EXPAND_DESCS = (
    "展开", "展开思考", "展开思考过程",
    "Expand", "Show thinking", "Show more",
)

IGNORE_EXACT = {
    "文心", "文心一言", "Hi，我是文心", "我是文心",
    "新对话", "新建对话", "新建工作", "新聊天", "New chat",
    "与文心的对话", "我的空间", "对话", "工作",
    "更多技能", "放心写", "魔法创作", "解题老师",
    "分享", "Share", "复制", "Copy", "重新生成", "Regenerate",
    "点赞", "点踩", "Like", "Dislike", "发送", "Send",
    "网页来源", "来源", "Sources", "关闭", "取消", "返回", "退出",
    "深度思考", "智能搜索", "联网搜索", "快速模式", "专家模式",
    "搜索结果", "回到顶部", "回到底部",
}

IGNORE_SUBSTR = (
    "回答由 AI 生成", "仅供参考", "内容由 AI 生成", "内容由AI生成",
    "以上内容由 AI 生成",
    # 新对话空页欢迎语（不是回答内容）
    "灵感在此着陆", "比起寻找答案", "今天想从哪一个奇思妙想写起",
)

# 文心 App 的 resource-id 全部被混淆为 com.baidu.newapp:id/obfuscated，
# 无任何可用 id —— 三组列表刻意留空，仅保留结构兼容（查找时自动跳过）。
KNOWN_INPUT_IDS: tuple = ()
KNOWN_SEND_IDS: tuple = ()
KNOWN_NEW_CHAT_IDS: tuple = ()


# ============================================================
# 日志
# ============================================================

LOG = logging.getLogger("wenxin_mobile")


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


def dump_xml(d, path: Optional[Path] = None, timeout: float = 12.0) -> str:
    """dump_hierarchy 带超时保护 + 可选落盘。

    AI 思考中 / 回答 WebView 加载时，无障碍树可能长时间不就绪，
    dump_hierarchy 无内部超时会卡死整个采集流程。此处用守护线程兜底，
    超时返回空串并打 warning，由调用方（wait_answer_finish 等）重试。
    """
    result = {"xml": ""}
    done = threading.Event()

    def _do():
        try:
            result["xml"] = d.dump_hierarchy(compressed=False)
        except Exception as exc:
            LOG.debug("dump_hierarchy 异常: %s", exc)
        finally:
            done.set()

    threading.Thread(target=_do, daemon=True, name="worker-dump-xml").start()
    if not done.wait(timeout):
        LOG.warning("⚠️ dump_hierarchy 超时（>%.1fs），返回空 XML —— "
                    "AI 可能正在思考/加载，调用方将重试", timeout)
        return ""

    xml = result["xml"]
    if path and xml:
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

def start_wenxin(d) -> None:
    log("启动文心 Android App")
    d.app_start(WENXIN_PACKAGE, stop=False)
    time.sleep(3)
    try:
        if d.app_current().get("package") != WENXIN_PACKAGE:
            d.app_start(WENXIN_PACKAGE, stop=False)
            time.sleep(3)
        if d.app_current().get("package") != WENXIN_PACKAGE:
            raise RuntimeError("文心没有进入前台")
    except Exception as exc:
        raise RuntimeError(f"无法确认文心前台状态：{exc}")


# ============================================================
# 新对话 / 输入 / 发送
# ============================================================

def _open_left_drawer(d) -> bool:
    """打开左侧抽屉（标注图1：点左上角「☰」按钮）。

    按钮是纯图标无文字：先试无障碍 desc 候选，再按左上角比例坐标兜底。
    验证：抽屉打开后出现「新建对话 / 我的空间」锚点。
    """
    for attempt in range(3):
        clicked = False
        for word in ("打开抽屉", "打开侧边栏", "侧边栏", "菜单", "对话历史", "历史记录"):
            try:
                obj = d(description=word)
                if obj.exists(timeout=0.6):
                    obj.click()
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            w, h = get_screen_size(d)
            d.click(int(w * 0.075), int(h * 0.062))
            log("已按坐标点击左上角抽屉按钮")
        time.sleep(1.2)

        xml = dump_xml(d)
        if "新建对话" in xml or "我的空间" in xml:
            log("✓ 左侧抽屉已打开")
            return True
        log(f"第 {attempt + 1}/3 次打开抽屉未见特征，重试")
    return False


def new_chat(d) -> bool:
    """新建对话（标注图1~②）：左上角开抽屉 → 点「新建对话」→ 验证回到主对话页。

    验证标准：抽屉关闭、主界面出现输入框提示「发消息或按住说话」。
    """
    log("创建新对话（抽屉 → 新建对话）")
    for attempt in range(3):
        if not _open_left_drawer(d):
            log("打开抽屉失败，重试新建对话")
            continue

        clicked = False
        # 优先精确文本「新建对话」
        try:
            obj = d(text="新建对话")
            if obj.exists(timeout=1.5):
                obj.click()
                clicked = True
        except Exception:
            pass
        if not clicked:
            clicked = click_first_by_words(d, NEW_CHAT_WORDS)
        if not clicked:
            try:
                obj = d(descriptionContains="新建对话")
                if obj.exists(timeout=1):
                    obj.click()
                    clicked = True
            except Exception:
                pass
        time.sleep(1.5)

        if not clicked:
            log(f"第 {attempt + 1}/3 次未找到「新建对话」按钮，重试")
            continue

        # 验证：回到主对话页（抽屉特征消失 + 输入框提示出现）
        xml = dump_xml(d)
        input_ok = any(h in xml for h in INPUT_HINTS) or \
            "android.widget.EditText" in xml

        # 检测历史对话残留：新对话页不应有历史回答的操作按钮或历史消息文本
        has_history = False
        history_indicators = (
            "复制", "重新生成", "再试一次", "分享", "点赞", "点踩",
            "Copy", "Regenerate", "Share", "Like", "Dislike",
            "已思考", "思考完成", "深度思考", "展开全部",
        )
        for indicator in history_indicators:
            if indicator in xml:
                has_history = True
                log(f"⚠ 检测到历史对话残留：{indicator}")
                break

        if input_ok and "我的空间" not in xml and not has_history:
            log("✓ 已进入全新对话页（无历史残留）")
            return True
        elif input_ok and "我的空间" not in xml and has_history:
            log("⚠ 已进入对话页但检测到历史对话残留，尝试清理")
            # 尝试按返回键退出当前对话再重新进入
            try:
                d.press("back")
                time.sleep(1.0)
                xml = dump_xml(d)
                if any(h in xml for h in INPUT_HINTS) and "我的空间" not in xml:
                    # 再次检查历史残留
                    still_has_history = any(ind in xml for ind in history_indicators)
                    if not still_has_history:
                        log("✓ 返回键清理后进入全新对话页")
                        return True
            except Exception:
                pass
            log(f"第 {attempt + 1}/3 次新建对话后仍有历史残留，重试")
            continue

        # 兜底：按一次返回键再验一次（可能点了但抽屉还盖着）
        try:
            d.press("back")
            time.sleep(1.0)
            xml = dump_xml(d)
            if any(h in xml for h in INPUT_HINTS) and "我的空间" not in xml:
                # 再次检查历史残留
                still_has_history = any(ind in xml for ind in history_indicators)
                if not still_has_history:
                    log("✓ 已进入全新对话页（返回键兜底，无历史残留）")
                    return True
        except Exception:
            pass
        log(f"第 {attempt + 1}/3 次新建对话后未确认主对话页，重试")
    return False


def find_input(d):
    """找底部输入框（文心：EditText，hint「发消息或按住说话」）。"""
    for rid in KNOWN_INPUT_IDS:   # 文心无可用 id，循环天然跳过
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
    """输入问题（标注图③：点击输入框，输入问题）。

    真人化输入：
      - 点击输入框后随机停顿（0.3~0.7s），模拟真人聚焦
      - 短问题（<50字）逐字键入 + 随机间隔（0.05~0.15s/字）
      - 长问题分段输入（每段 3~8 字，段间随机停顿 0.1~0.3s）
      - 输入完成后随机停顿（0.5~1.0s）再发送
    """
    import random
    edit = find_input(d)
    if edit is None:
        raise RuntimeError("找不到文心输入框")
    log(f"输入问题：{question[:100]}")
    text = str(question)

    try:
        edit.click()
        time.sleep(random.uniform(0.3, 0.7))  # 点击后等一下，像真人聚焦输入框
    except Exception:
        pass

    # 先清空输入框
    try:
        edit.clear_text()
        time.sleep(random.uniform(0.2, 0.4))
    except Exception:
        pass

    # 真人化输入：短问题逐字，长问题分段
    try:
        if len(text) <= 50:
            # 短问题逐字输入（每字随机间隔 0.05~0.15s）
            for i in range(1, len(text) + 1):
                edit.set_text(text[:i])
                time.sleep(random.uniform(0.05, 0.15))
        else:
            # 长问题分段输入（每段 3~8 字，段间随机停顿）
            i = 0
            while i < len(text):
                chunk_size = random.randint(3, 8)
                end = min(i + chunk_size, len(text))
                edit.set_text(text[:end])
                i = end
                if i < len(text):
                    time.sleep(random.uniform(0.1, 0.3))
    except Exception:
        # 真人化输入失败则回退到一次性 set_text
        try:
            edit.set_text(text)
        except Exception as first_exc:
            log(f"set_text 失败，尝试 fast input：{first_exc}")
            try:
                d.set_fastinput_ime(True)
                d.send_keys(text, clear=True)
            finally:
                try:
                    d.set_fastinput_ime(False)
                except Exception:
                    pass

    # 输入完成后停顿，像真人检查输入内容
    time.sleep(random.uniform(0.5, 1.0))


def _verify_sent(d, question: str) -> bool:
    """发送结果校验（文心的发送按钮是纯图标，必须验证点击是否真的生效）。

    判定（满足其一即视为已发出）：
      1. 出现「停止生成」等生成中控件；
      2. 输入框已被清空（问题进入消息区，输入框恢复 hint 或为空）。
    """
    time.sleep(1.2)
    xml = dump_xml(d)
    if xml_contains_generating(xml):
        return True
    try:
        nodes = parse_nodes(xml)
    except Exception:
        return False
    edit_found = False
    for n in nodes:
        if "edittext" not in n["class"].lower():
            continue
        edit_found = True
        t = (n["text"] or "").strip()
        if question and t.strip() == question.strip():
            return False          # 问题还完整躺在输入框里 → 没发出去
        if not t or any(h in t for h in ("发消息", "输入")):
            return True           # 已清空 / 恢复 hint → 发送成功
    if not edit_found:
        # 找不到输入框时（极端情况），退化为看问题是否进入消息区
        return bool(question) and question[:10] in xml
    return False


def send_message(d, question: str = "") -> bool:
    """发送问题。发送按钮为纯图标，逐策略尝试，每种策略都做发送后验证。"""
    log("发送问题")

    def _try_action(fn) -> bool:
        if not fn():
            return False
        return _verify_sent(d, question)

    # 策略 1：已知 resource-id（文心无，自动跳过）
    if _try_action(lambda: click_first_by_ids(d, KNOWN_SEND_IDS)):
        log("✓ 发送成功（resource-id）")
        return True

    # 策略 2：文本 / desc「发送」
    for word in SEND_WORDS:
        if _try_action(lambda: _click_send_word(d, word)):
            log(f"✓ 发送成功（{word}）")
            return True

    # 策略 3：模糊词点击（clickable 节点中含发送/Send）
    if _try_action(lambda: click_first_by_words(d, SEND_WORDS)):
        log("✓ 发送成功（模糊词）")
        return True

    # 策略 4：rid 含 send 的最小可点击节点（兼容个别未混淆版本）
    if _try_action(lambda: _click_send_by_rid(d)):
        log("✓ 发送成功（rid 含 send）")
        return True

    # 策略 5：结构定位 —— 底部工具栏「最右侧」的可点击节点即发送按钮
    #   （实机 XML：语音/模式下拉/+ /发送 依次排列，发送按钮无 text/desc/可用 id，
    #    但它是工具栏行里 x 坐标最大的可点击节点）。
    if _try_action(lambda: _click_send_rightmost(d)):
        log("✓ 发送成功（工具栏最右节点）")
        return True

    # 策略 6：坐标兜底 —— 发送按钮在输入框右下、屏宽约 0.894、屏高约 0.882 处
    #   （旧值 0.945h 偏下 121px 会点到按钮下方空白区导致发送失败）
    def _coord_send():
        w, h = get_screen_size(d)
        d.click(int(w * 0.894), int(h * 0.882))
        return True
    if _try_action(_coord_send):
        log("✓ 发送成功（右下角坐标兜底）")
        return True

    # 策略 7：Enter 键兜底
    try:
        d.press("enter")
        if _verify_sent(d, question):
            log("✓ 发送成功（Enter 兜底）")
            return True
    except Exception:
        pass

    log("✗ 所有发送策略均未确认成功")
    return False


def _click_send_rightmost(d) -> bool:
    """点击底部工具栏最右侧的可点击节点（即发送按钮）。

    文心发送按钮是纯图标，无 text/desc/可用 resource-id。
    工具栏行从左到右：语音 | 模式下拉 | + | 发送。
    取工具栏行（y 在屏高 0.82~0.94）中 x 最靠右的可点击节点点击。
    """
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False

    row_top = int(h * 0.82)
    row_bot = int(h * 0.94)
    best = None
    for n in nodes:
        if not n.get("clickable") or not n.get("enabled") or not n.get("visible"):
            continue
        b = bounds_to_tuple(n.get("bounds", ""))
        if not b:
            continue
        cy = (b[1] + b[3]) // 2
        if not (row_top <= cy <= row_bot):
            continue
        # 排除过宽的容器（工具栏整行本身可能 clickable）
        if (b[2] - b[0]) > int(w * 0.35):
            continue
        if best is None or b[0] > best[0]:
            best = b
    if best is None:
        return False
    cx = (best[0] + best[2]) // 2
    cy = (best[1] + best[3]) // 2
    LOG.debug("按工具栏最右节点定位发送按钮: %s", best)
    try:
        d.click(cx, cy)
        return True
    except Exception:
        return False


def _click_send_word(d, word: str) -> bool:
    for kwargs in ({"text": word}, {"description": word}):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=0.5):
                obj.click()
                return True
        except Exception:
            pass
    return False


def _click_send_by_rid(d) -> bool:
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
                return True
    except Exception:
        pass
    return False


# ============================================================
# 切换模式（替代 DeepSeek 的「开启深度思考」步骤）
# ============================================================

def switch_mode(d) -> bool:
    """「切换模式」步骤（标注流程：新建对话 → **切换模式** → 输入 → 发送）。

    做两件事，均失败不致命：
      1. 确保处于「对话」模式：顶部若出现「对话 / 工作」页签，点「对话」；
      2. 可选模型切换：config.WENXIN_CHAT_MODE 非空时，点顶部
         「新对话 ▼ / 对话 ▼」下拉，选择目标模型菜单项。
         留空 = 保持 App 默认模型（最稳）。
    """
    log("→ 检查 / 切换对话模式")
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("读取界面失败，跳过模式切换：%s", exc)
        return False

    top_limit = int(h * 0.15)

    # ---------- 1. 确保处于「对话」视图（含「深度思考」下拉的聊天页） ----------
    # ⚠️ 实机发现：已在「对话」视图时再次点击「对话」页签，会切换到另一个
    #    没有「深度思考」下拉的子视图，导致后续无法切换深度思考模式。
    #    因此：底部工具栏已存在「深度思考」下拉 → 直接跳过；
    #          否则仅在「工作」被选中时才点「对话」。
    try:
        # 先看底部是否已有深度思考下拉（在正确的聊天视图上）
        already_chat = any(
            (n.get("text") or "").strip() in MODE_NAME_CANDIDATES
            and bounds_to_tuple(n.get("bounds", ""))
            and bounds_to_tuple(n["bounds"])[1] > int(h * 0.7)
            for n in nodes
        )
        if already_chat:
            log("✓ 已在「对话」视图（底部有深度思考下拉），无需切换页签")
        else:
            has_work_tab = False
            work_selected = False
            dialog_tab = None
            dialog_selected = False
            for n in nodes:
                text = (n["text"] or "").strip()
                b = bounds_to_tuple(n["bounds"])
                if not b or b[3] > top_limit:
                    continue
                if text == "工作":
                    has_work_tab = True
                    work_selected = bool(n.get("selected"))
                elif text == "对话":
                    dialog_tab = b
                    dialog_selected = bool(n.get("selected"))

            # 只在「工作」被选中（或对话未选中）时才点「对话」，避免重复点击破坏布局
            if has_work_tab and dialog_tab is not None and work_selected and not dialog_selected:
                d.click((dialog_tab[0] + dialog_tab[2]) // 2,
                        (dialog_tab[1] + dialog_tab[3]) // 2)
                time.sleep(1.2)
                log("✓ 已点顶部「对话」页签（从「工作」切回）")
            elif has_work_tab and dialog_tab is not None:
                log("✓ 已在「对话」页签，无需切换")
    except Exception as exc:
        LOG.debug("切换「对话」页签失败（忽略）：%s", exc)

    # ---------- 2. 可选：下拉切换具体模型 ----------
    target = ""
    try:
        from config import WENXIN_CHAT_MODE as _cfg_mode
        target = (_cfg_mode or "").strip()
    except Exception:
        target = ""

    if not target:
        log("✓ 保持 App 当前默认模型（WENXIN_CHAT_MODE 未配置）")
        return True

    try:
        # 「新对话 ▼ / 对话 ▼」下拉在顶部标题位，文本为「新对话」或「对话」
        dropdown = None
        for n in nodes:
            text = (n["text"] or "").strip()
            b = bounds_to_tuple(n["bounds"])
            if not b or b[3] > top_limit:
                continue
            if text in ("新对话", "对话"):
                dropdown = b
                break
        if dropdown is None:
            log("⚠ 未找到顶部模型下拉，跳过模型切换")
            return False
        d.click((dropdown[0] + dropdown[2]) // 2,
                (dropdown[1] + dropdown[3]) // 2)
        time.sleep(1.2)

        # 菜单中选择目标模型
        picked = False
        for kwargs in ({"text": target}, {"textContains": target}):
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=1.5):
                    obj.click()
                    picked = True
                    break
            except Exception:
                pass
        time.sleep(1.0)
        if picked:
            log(f"✓ 已切换模型：{target}")
            return True
        log(f"⚠ 模型菜单中未找到「{target}」，按返回键保持默认")
        try:
            d.press("back")
        except Exception:
            pass
        return False
    except Exception as exc:
        LOG.debug("模型切换异常（忽略）：%s", exc)
        return False


# ============================================================
# 深度思考模式切换（底部工具栏「深度思考 ∨」下拉）
# ============================================================

DEEP_THINK_MODE = "深度思考"
# 模式下拉按钮上可能出现的模式名称（用于识别当前模式文本节点）。
# 实机 XML：底部工具栏一个 clickable LinearLayout 内含 TextView 显示当前模式名。
MODE_NAME_CANDIDATES = (
    "深度思考", "快速回答", "极速模式", "自定义",
    "专家模式", "联网搜索", "智能搜索",
)


def _find_mode_dropdown(d):
    """在底部工具栏定位「模式下拉」按钮及其当前模式文本。

    :return: (current_mode_text, dropdown_bounds_tuple) 或 (None, None)
    """
    try:
        w, h = get_screen_size(d)
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None, None

    # 底部工具栏纵向范围（经验值：y 在屏高 0.78~0.95 之间）
    top = int(h * 0.78)
    bot = int(h * 0.96)

    for n in nodes:
        b = bounds_to_tuple(n.get("bounds", ""))
        if not b:
            continue
        cy = (b[1] + b[3]) // 2
        if not (top <= cy <= bot):
            continue
        text = (n.get("text") or "").strip()
        if text in MODE_NAME_CANDIDATES:
            # 找包含该文本节点的可点击祖先（下拉按钮本体）
            dropdown = None
            for p in nodes:
                pb = bounds_to_tuple(p.get("bounds", ""))
                if (pb and p.get("clickable")
                        and pb[0] <= b[0] and pb[1] <= b[1]
                        and pb[2] >= b[2] and pb[3] >= b[3]
                        and (pb[2] - pb[0]) < (w * 0.4)):
                    dropdown = pb
                    break
            return text, (dropdown or b)
    return None, None


def _ensure_deep_think_mode(d) -> bool:
    """确保处于「深度思考」模式。

    实机结构：底部工具栏（语音按钮右侧）有一个模式下拉按钮，文本显示当前模式名。
      - 当前已是「深度思考」→ 直接跳过，绝不重复点击；
      - 否则点开下拉 → 在菜单中选「深度思考」→ 验证工具栏文本已变为「深度思考」。
    失败不致命（返回 False），主流程继续。
    """
    log("→ 检查 / 切换「深度思考」模式")
    try:
        w, h = get_screen_size(d)
    except Exception:
        w, h = 1080, 1920

    # 先尝试识别当前模式（页面可能正在重载，重试几次）
    current = None
    dropdown = None
    for _ in range(3):
        current, dropdown = _find_mode_dropdown(d)
        if current is not None:
            break
        time.sleep(0.5)

    if current == DEEP_THINK_MODE:
        log(f"✓ 已是「{DEEP_THINK_MODE}」模式，无需切换")
        return True

    # ---- 点击下拉按钮打开模式菜单 ----
    if dropdown:
        cx = (dropdown[0] + dropdown[2]) // 2
        cy = (dropdown[1] + dropdown[3]) // 2
    else:
        # 兜底坐标：语音按钮右侧的模式下拉位（约 x=0.33w, y=0.88h）
        cx, cy = int(w * 0.33), int(h * 0.88)
    log(f"点击模式下拉按钮 ({cx},{cy})，选择「{DEEP_THINK_MODE}」")
    d.click(cx, cy)
    time.sleep(1.2)

    # ---- 在弹出菜单中点击「深度思考」 ----
    picked = False
    for kwargs in ({"text": DEEP_THINK_MODE}, {"textContains": DEEP_THINK_MODE}):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=1.5):
                obj.click()
                picked = True
                break
        except Exception:
            pass
    if not picked:
        # 菜单里没有「深度思考」——常见原因是当前已是该模式、菜单项不列出/已选中。
        # 关闭菜单后复查工具栏：若是「深度思考」则视为已满足。
        log("⚠ 模式菜单中未找到「深度思考」选项，关闭菜单后复查当前模式")
        try:
            d.press("back")
        except Exception:
            pass
        time.sleep(0.6)
        current2, _ = _find_mode_dropdown(d)
        if current2 == DEEP_THINK_MODE:
            log(f"✓ 复查确认已是「{DEEP_THINK_MODE}」模式")
            return True
        log("⚠ 复查仍非「深度思考」模式，可能菜单选项文案不同，跳过切换")
        return False

    time.sleep(0.8)

    # ---- 验证：工具栏模式文本是否已变为「深度思考」 ----
    current2, _ = _find_mode_dropdown(d)
    if current2 == DEEP_THINK_MODE:
        log(f"✓ 已切换为「{DEEP_THINK_MODE}」模式")
        return True
    log("⚠ 切换后工具栏未确认到「深度思考」文本（可能已生效但节点未刷新）")
    return False


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

        hint_hit = any(hint.lower() in low for hint in INPUT_HINTS)
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
            # 「停止生成」类按钮需要 clickable/enabled
            # 「思考中」类静态文本不需要 clickable，只要存在即说明在生成
            is_stop_btn = any(w in low for w in ("停止", "stop", "cancel"))
            if is_stop_btn:
                if node["enabled"] and (node["clickable"] or "stop" in node["rid"].lower()):
                    return True
            else:
                # 思考中/深度思考等静态提示，存在即视为生成中
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
    """取回答区最长文本节点，用于「是否已有回答 / 回答是否还在增长」的粗略判定。

    注意：文心回答由 Compose 渲染，文本不在 android.widget.TextView 上，
    因此必须扫描**全部节点**的 text 属性，不能限定 class=TextView。
    通过屏幕区域过滤（排除顶栏 7% 与底部输入区 22%）+ 忽略词表降噪。
    """
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""

    # 估算屏幕高度：取所有节点 bounds 的最大下缘
    h = 0
    for node in root.iter("node"):
        b = node.attrib.get("bounds", "")
        m = re.match(r"\[\d+,(\d+)\]\[\d+,(\d+)\]", b)
        if m:
            h = max(h, int(m.group(2)))
    if h <= 0:
        h = 1920
    top_limit = int(h * 0.07)
    bottom_limit = int(h * 0.78)

    valid = []
    for node in root.iter("node"):
        text = clean_text(node.attrib.get("text", ""))
        if not text or len(text) < 6:
            continue
        if text in IGNORE_EXACT:
            continue
        if any(sub in text for sub in IGNORE_SUBSTR):
            continue
        b = node.attrib.get("bounds", "")
        m = re.match(r"\[\d+,(\d+)\]\[\d+,(\d+)\]", b)
        if m:
            _, top, _, bottom = int(m.group(1)), int(m.group(1)), int(m.group(2)), int(m.group(2))
            # 仅保留落在回答区（顶栏之下、输入区之上）的文本
            if top < top_limit or bottom > bottom_limit:
                continue
        valid.append(text)

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


# 文心回答正文渲染在 WebView 内，无障碍树完全拿不到正文文本
# （只能取到标题，正文 count=0）。因此正文采集的兜底方案是：
# 点击回答卡片操作栏最左侧的「复制」图标 → 读剪贴板。
# 复制图标无 text/content-desc，位于 WebView 内部且随滚动上下移动，
# 故先滚到底部让操作栏尽量靠下，再沿候选 x 竖线扫描点击。
_COPY_ICON_X_CANDIDATES = (100, 112, 124, 660, 680, 700)
_COPY_ICON_Y_RANGE = (380, 1120)
_COPY_ICON_Y_STEP = 20


def copy_answer_to_clipboard(d, timeout: float = 60.0) -> str:
    """点击回答卡片的「复制」图标，返回剪贴板中的答案正文。

    文心的复制按钮是纯图标（无 text/content-desc），且位于 WebView 内部，
    无障碍树不可见。本函数先滚到底部（画面稳定即到底），再沿候选 x 竖线
    扫描点击，命中后剪贴板会出现完整答案文本。

    :return: 剪贴板文本（空串表示未命中）
    """
    # 先滚到底部：连续滑动直到画面不再变化（说明已到回答末尾，操作栏可见）
    try:
        w, h = get_screen_size(d)
        prev_sig = None
        stable = 0
        for _ in range(30):
            d.swipe(w // 2, int(h * 0.62), w // 2, int(h * 0.22), 0.22)
            time.sleep(0.4)
            try:
                sig = _screen_message_signature(d)
                if sig and prev_sig is not None and _sig_similar(sig, prev_sig):
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                    prev_sig = sig
            except Exception:
                stable = 0
    except Exception:
        pass

    try:
        d.set_clipboard("")
    except Exception:
        pass
    time.sleep(0.3)

    deadline = time.time() + timeout
    for y in range(_COPY_ICON_Y_RANGE[0], _COPY_ICON_Y_RANGE[1], _COPY_ICON_Y_STEP):
        if time.time() > deadline:
            break
        for x in _COPY_ICON_X_CANDIDATES:
            try:
                d.click(x, y)
            except Exception:
                continue
            time.sleep(0.55)
            try:
                cb = d.clipboard or ""
            except Exception:
                cb = ""
            if len(cb) >= 10:
                LOG.debug("命中复制图标 (%d,%d)，剪贴板 %d 字", x, y, len(cb))
                return cb
    return ""


def wait_answer_finish(d) -> str:
    """等待文心回答完成。

    主判定（文本增长停滞）：
      answer_len >= 门槛 且 文本持续 N 秒未变化 且 输入框就绪 且 无「停止生成」按钮。

    兜底判定（画面签名稳定）—— 应对文心 Compose 文本节点不暴露/被切得过碎
    导致 answer_len 始终偏小的情况：
      消息区画面签名持续 N 秒不变 且 输入框就绪 且 无生成中指示 → 视为完成。
    """
    log("等待文心回答完成（文本增长停滞 + 画面签名稳定 双判定）")
    start = time.time()
    deadline = start + ANSWER_HARD_TIMEOUT

    last_answer = ""
    last_growth_time = time.time()
    stable_rounds = 0
    saw_answer = False
    last_log = 0.0
    last_snapshot = 0.0
    last_state = None

    # 画面签名稳定性追踪
    last_sig = None
    sig_stable_since = time.time()

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
            sig = state.get("screen_sig") or b""

            # ---- 文本增长追踪 ----
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

            # ---- 画面签名稳定性追踪 ----
            if sig and last_sig is not None and _sig_similar(sig, last_sig):
                # 画面没变，稳定计时延续
                pass
            else:
                sig_stable_since = now  # 画面变了，重置稳定起点
            last_sig = sig
            sig_stable_sec = now - sig_stable_since if sig else 0.0

            if now - last_log >= 2.0:
                log(
                    f"回答状态：len={answer_len}, stable={stable_rounds}, "
                    f"no_growth={no_growth:.1f}s, sig_stable={sig_stable_sec:.1f}s, "
                    f"composer={composer['ready']}(score={composer['score']}), "
                    f"stop={composer['stop_control']}, generating={state['generating']}"
                )
                last_log = now

            base_ready = (
                not composer["stop_control"]
                and not state["generating"]
                and composer["ready"]
            )

            # 发送后最低等待时间保障：防止 WebView 不可见时过早误判
            min_wait_ok = (now - start) >= MIN_ANSWER_WAIT_AFTER_SEND

            # 判定一：文本增长停滞（主路径）
            if (saw_answer and base_ready
                    and no_growth >= ANSWER_STABLE_SECONDS
                    and stable_rounds >= ANSWER_STABLE_MIN_ROUNDS
                    and min_wait_ok):
                log(f"✓ 回答完成（文本停滞）：len={answer_len}, no_growth={no_growth:.1f}s")
                _save_answer_debug(d, "completed_text")
                return answer

            # 判定二：画面签名稳定（兜底路径，文本提取不可靠时生效）
            # 注意：必须 saw_answer=True（提取到足够文本）才允许画面稳定兜底，
            # 防止 answer_len=0 时误判完成
            if (saw_answer and base_ready and sig and sig_stable_sec >= ANSWER_STABLE_SECONDS
                    and no_growth >= ANSWER_STABLE_SECONDS
                    and min_wait_ok):
                log(f"✓ 回答完成（画面稳定兜底）：len={answer_len}, "
                    f"sig_stable={sig_stable_sec:.1f}s")
                _save_answer_debug(d, "completed_sig")
                return answer or last_answer

            # 判定三：WebView 不可见特殊兜底 —— answer_len=0 但画面稳定超长时间
            # 文心正文在 WebView 内，无障碍树完全拿不到文本，answer_len 始终为 0。
            # 画面稳定超过 30s 且最低等待时间已过，给警告但仍判定完成。
            if (not saw_answer and base_ready and sig and sig_stable_sec >= 30.0
                    and min_wait_ok):
                log(f"⚠ WebView 正文不可见（len=0），画面稳定 {sig_stable_sec:.0f}s，判定完成")
                _save_answer_debug(d, "completed_webview_fallback")
                return answer or last_answer

            time.sleep(POLL_INTERVAL)
        except Exception as exc:
            LOG.debug("回答状态检测异常：%s", exc)
            time.sleep(POLL_INTERVAL_SLOW)

    log("达到回答主等待时间，进入超时兜底。")
    if last_state is not None:
        answer = clean_text(last_state["answer"])
        if len(answer) >= ANSWER_MIN_COMPLETE_LENGTH and not last_state["composer"]["stop_control"]:
            log(f"✓ 超时兜底接受回答：len={len(answer)}")
            _save_answer_debug(d, "completed_timeout")
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

# 文心底部「技能胶囊」行（更多技能/放心写/魔法创作/解题老师）位于输入框上方，
# 与输入框同属输入区 —— 视窗下缘必须排到它们之上，长截图才不会带入输入区。
_COMPOSER_WORDS = {"深度思考", "智能搜索", "联网搜索", "DeepThink", "Web Search",
                   "更多技能", "放心写", "魔法创作", "解题老师"}


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
        is_composer_btn = value in _COMPOSER_WORDS
        if is_edit or is_hint or is_composer_btn:
            candidate_tops.append(b[1])

    if candidate_tops:
        input_top = min(candidate_tops)
        bottom = min(bottom, input_top - 5)

    bottom = max(bottom, top + 100)
    return (int(left), int(top), int(right), int(bottom))


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
#  与 DeepSeek 同源：
#    * 思考过程内容 —— 灰色（默认 #7d7f85，可在 config 调）
#    * AI 回答正文   —— 黑色（默认 #191b23，可在 config 调）
#
#  uiautomator 的 XML 只给文本和坐标，不携带字体颜色，
#  因此只能在文本节点的矩形范围内直接采样截图像素来判定：
#    区域内绝大多数像素是白色背景（高亮度），字形笔画是深色，
#    取「亮度最低的一小撮像素」的中位色 = 笔画核心色（避开抗锯齿过渡）。
# ============================================================

try:
    from config import WENXIN_THINKING_RGB as THINKING_TEXT_RGB  # type: ignore
except Exception:
    THINKING_TEXT_RGB = (0x7D, 0x7F, 0x85)   # 灰色 —— 思考过程
try:
    from config import WENXIN_ANSWER_RGB as ANSWER_TEXT_RGB  # type: ignore
except Exception:
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

    与参考色的欧氏距离就近分类；距离两个参考色都很远时判 unknown，
    由调用方宽松处理。
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
    max_ov = int(min(len(up_feat), len(lo_feat)) * 0.70)
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
            os_ = other.strip()
            if os_ == ts:
                continue
            if len(os_) > len(ts) + 8 and ts in os_:
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
      1. 颜色过滤（主）：字体核心色为灰色的节点直接丢弃，
         只保留黑色与无法判色的节点。
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

        hsh = hash(cleaned)
        if hsh in seen_hashes:
            continue
        seen_hashes.add(hsh)

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
      * 字体颜色 —— 只收灰色文本节点；
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

    # 注意：不使用 before_texts 做差集。回答生成完毕时思考面板常已自动
    # 展开，可见思考段会进快照被误当「旧内容」跳过 —— 只靠颜色判据全量收集灰色文本。
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

            # 排除来源入口药丸（「网页来源」「N 篇网页」等小灰字，不是思考内容）
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
    log("开始主问答长截图（慢速小步 + 同步收集文本）")

    exclude_texts = exclude_texts or set()

    if not scroll_to_top(d):
        raise RuntimeError("无法确认聊天顶部")

    time.sleep(1.0)
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport}")

    frames = []
    prev_sig = None
    stable_bottom = 0

    seen_hashes = set()
    screens_texts = []
    screen_idx = 0

    idle_rounds = 0   # 保险丝：连续无新画面且无新文本的轮数

    for i in range(MAX_CAPTURE_FRAMES):
        # 一次截屏复用：既用于帧拼接，也用于字体颜色判定（省一次 adb 截屏）
        full_img = _grab_full_screen()
        current = save_message_area(d, OUTPUT_DIR / f"_frame_{i:03d}.png",
                                    viewport, full=full_img)
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

        items = _collect_screen_texts(
            d, viewport, question, seen_hashes,
            exclude_texts=exclude_texts, screen_np=screen_np,
        )

        if items:
            screens_texts.append((screen_idx, items))
            screen_idx += 1
            log(f"  屏 {screen_idx} 新增正文 {len(items)} 条")

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

        try:
            after = save_message_area(d, OUTPUT_DIR / "_probe_after.png",
                                      viewport, full=_grab_full_screen())
            after_sig = image_signature(after)
            # 关键修正：与「本次滑动前」的画面比较（而不是 frames[-1]），
            # 滑动没有带来有效位移 = 到底；容差吸收回弹微动
            if _sig_similar(after_sig, sig):
                stable_bottom += 1
                log(f"[BOTTOM] 未移动 {stable_bottom}/{SCROLL_STABLE_ROUNDS}")
                if stable_bottom >= SCROLL_STABLE_ROUNDS:
                    log("✓ 主问答长截图到达底部")
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
                break

    if not frames:
        raise RuntimeError("没有采集到聊天截图")

    if len(frames) > 1 and _sig_similar(image_signature(frames[-1]),
                                        image_signature(frames[-2])):
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


# ============================================================
# 网页来源面板（用户明确要求：网页来源入口长截图）
# ============================================================

# 文心来源入口文案。常见形式：
#   「网页来源」「来源」「参考网页」「N 篇网页」「N 个网页」「参考 N 篇」「搜索到 N 个网页」
SOURCE_ENTRY_PATTERN = re.compile(
    r"网页来源|参考网页|参考资料|搜索来源|搜索到\s*\d+\s*个?网页|"
    r"\d+\s*个?网页|\d+\s*篇网页|参考\s*\d+\s*篇|^\s*来源\s*$"
)


def _find_sources_entry(d) -> Optional[dict]:
    """找「网页来源」入口（回答末尾的来源药丸按钮）。

    返回最靠下的候选：入口位于回答正文之后，通常在屏幕偏下的位置。
    """
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return None

    candidates = []
    for node in nodes:
        value = (node["text"] or node["desc"] or "").strip()
        if not value or len(value) > 30:
            continue
        if SOURCE_ENTRY_PATTERN.search(value):
            b = bounds_to_tuple(node["bounds"])
            if b:
                candidates.append({"node": node, "bounds": b})

    if candidates:
        candidates.sort(key=lambda c: c["bounds"][1])
        return candidates[-1]

    # 兜底：可点击的短文本「来源 / 参考 / 搜索」节点（取最靠下者）
    fallback = []
    for node in nodes:
        value = (node["text"] or node["desc"] or "").strip()
        if not value or len(value) > 12 or not node["clickable"]:
            continue
        if any(w in value for w in SOURCE_WORDS):
            b = bounds_to_tuple(node["bounds"])
            if b:
                fallback.append({"node": node, "bounds": b})
    if fallback:
        fallback.sort(key=lambda c: c["bounds"][1])
        return fallback[-1]
    return None


# 来源面板标题特征（打开后按此判定）
_PANEL_TITLE_WORDS = ("网页来源", "搜索来源", "搜索结果", "参考来源", "参考资料")


def _has_search_panel(d) -> bool:
    """网页来源面板是否已打开（按标题特征判断）。"""
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception:
        return False
    for node in nodes:
        value = (node["text"] or node["desc"] or "").strip()
        if not value:
            continue
        if any(w in value for w in _PANEL_TITLE_WORDS):
            return True
    return False


def _try_open_entry(d, entry: dict) -> bool:
    """点击来源入口并判断面板是否真的打开。

    判定：标题含「网页来源/搜索结果」等特征；或点击前后画面发生大幅变化
    （有些机型面板标题不标准，但一定会显著改变画面）。
    """
    try:
        before_sig = image_signature(_grab_full_screen())
    except Exception:
        before_sig = None

    if not click_node(d, entry["node"]):
        return False
    time.sleep(2.0)

    if _has_search_panel(d):
        return True

    if before_sig is not None:
        try:
            after_sig = image_signature(_grab_full_screen())
            if _sig_distance(after_sig, before_sig) > 15.0:
                return True
        except Exception:
            pass
    return False


def open_sources_panel(d, max_attempts: int = 3) -> bool:
    """打开网页来源面板。

    入口位于回答末尾，位置随回答长度而变，因此分三步找：
      1. 先看当前位置（主截图后一般已在底部，入口通常可见）
      2. 找不到就向下逐屏扫描
      3. 仍未找到则回到顶部、再向下扫一遍，覆盖长回答中漏掉的区域

    :return: True=面板已成功打开
    """
    log("尝试打开网页来源面板")

    entry = _find_sources_entry(d)
    if entry:
        log(f"找到来源入口：{entry['node']['text'][:40]}")
        if _try_open_entry(d, entry):
            log("✓ 网页来源面板已打开")
            return True
        log("入口点击后未打开面板，继续扫描")

    for attempt in range(max_attempts):
        _swipe_in_viewport(d, "up", fraction=0.5, duration=0.6)
        time.sleep(1.0)
        entry = _find_sources_entry(d)
        if entry:
            log(f"下扫第 {attempt+1} 轮找到入口：{entry['node']['text'][:40]}")
            if _try_open_entry(d, entry):
                log("✓ 网页来源面板已打开")
                return True

    try:
        scroll_to_top(d)
        time.sleep(1.0)
    except Exception:
        pass
    for attempt in range(max_attempts):
        entry = _find_sources_entry(d)
        if entry:
            log(f"顶扫第 {attempt+1} 轮找到入口：{entry['node']['text'][:40]}")
            if _try_open_entry(d, entry):
                log("✓ 网页来源面板已打开")
                return True
        _swipe_in_viewport(d, "up", fraction=0.5, duration=0.6)
        time.sleep(1.0)

    log("✗ 未能打开网页来源面板（该题可能确实没有联网来源）")
    return False


def _get_panel_viewport(d) -> tuple:
    w, h = get_screen_size(d)
    top = int(h * 0.06)
    try:
        nodes = parse_nodes(dump_xml(d))
        for n in nodes:
            if any(wd in (n["text"] or "") for wd in _PANEL_TITLE_WORDS):
                b = bounds_to_tuple(n["bounds"])
                if b:
                    top = max(0, b[1] - int(h * 0.03))
                    break
    except Exception:
        pass
    return (int(w * 0.02), top, int(w * 0.98), int(h * 0.94))


def screenshot_panel(d, output: Path) -> bool:
    log("开始网页来源面板长截图")
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
            new_frame = True
        elif not _sig_similar(sig, prev_sig):
            frames.append(current.copy())
            log(f"面板已采集第 {len(frames)} 屏")
            new_frame = True
        else:
            new_frame = False
        prev_sig = sig

        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

        try:
            after = save_message_area(d, OUTPUT_DIR / "_panel_probe.png", viewport)
            after_sig = image_signature(after)
            # 与「本次滑动前」画面比较（容差），与主问答长截图同一套到底判定
            if _sig_similar(after_sig, sig):
                stable_bottom += 1
                log(f"[PANEL-BOTTOM] 未移动 {stable_bottom}/{SCROLL_STABLE_ROUNDS}")
                if stable_bottom >= SCROLL_STABLE_ROUNDS:
                    log("✓ 面板长截图到达底部")
                    break
            else:
                stable_bottom = 0
        except Exception:
            pass

    if not frames:
        log("面板未采集到任何帧")
        return False
    if len(frames) > 1 and _sig_similar(image_signature(frames[-1]),
                                        image_signature(frames[-2])):
        frames.pop()
    stitch_frames(frames, output)
    log(f"面板长截图完成：{output}")
    return True


def close_sources_panel(d) -> None:
    log("关闭网页来源面板")
    try:
        for word in ("关闭", "Close", "取消", "Cancel"):
            obj = d(description=word)
            if obj.exists(timeout=0.5):
                obj.click(); time.sleep(0.8); return
    except Exception:
        pass
    try:
        d.press("back"); time.sleep(0.8)
        if _has_search_panel(d):
            d.press("back"); time.sleep(0.8)
    except Exception:
        pass


def merge_two_screenshots(top_path: Path, bottom_path: Optional[Path], output: Path) -> bool:
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
        "_panel_probe.png",
    ):
        try:
            (OUTPUT_DIR / pattern).unlink(missing_ok=True)
        except Exception:
            pass
    for path in list(OUTPUT_DIR.glob("_frame_*.png")) + list(OUTPUT_DIR.glob("_panel_*.png")):
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

    import random

    # 1. 新对话（抽屉 → 新建对话）
    if not new_chat(d):
        raise RuntimeError("未能确认新建对话。")
    time.sleep(random.uniform(1.5, 2.5))  # 随机停顿，像真人等待页面加载

    # ---------- 切换模式（确保「对话」模式，可选模型切换，失败不致命） ----------
    try:
        switch_mode(d)
    except Exception as exc:
        LOG.warning("切换模式处理失败（继续执行）：%s", exc)
    time.sleep(random.uniform(0.5, 1.2))
    # ----------------------------------------------------------

    # 2. 输入
    input_text(d, question)

    # 2.5 切换「深度思考」模式（必须在输入之后——
    #     实机验证：空输入时底部不渲染模式下拉，输入文字后「深度思考 ∨」才出现）
    try:
        _ensure_deep_think_mode(d)
    except Exception as exc:
        LOG.warning("深度思考模式切换失败（继续发送）：%s", exc)
    time.sleep(random.uniform(0.3, 0.8))

    # 3. 发送（带发送结果校验）—— 发送前再停顿一下，像真人确认内容
    time.sleep(random.uniform(0.3, 0.8))
    if not send_message(d, question):
        raise RuntimeError("问题发送失败")

    # 3.5 发送后检测历史对话残留（文心 bug：新建对话后发送消息才显示旧内容）
    time.sleep(random.uniform(1.5, 2.5))  # 等 UI 稳定
    xml = dump_xml(d)
    history_indicators = (
        "复制", "重新生成", "再试一次", "分享", "点赞", "点踩",
        "Copy", "Regenerate", "Share", "Like", "Dislike",
        "已思考", "思考完成", "深度思考", "展开全部",
    )
    has_history = any(ind in xml for ind in history_indicators)
    if has_history:
        log("⚠ 发送后检测到历史对话残留，重新新建对话并重发问题")
        # 重新新建对话
        if not new_chat(d):
            raise RuntimeError("发送后历史残留，重新新建对话失败")
        time.sleep(random.uniform(1.5, 2.5))
        # 重新输入 + 发送
        input_text(d, question)
        try:
            _ensure_deep_think_mode(d)
        except Exception:
            pass
        time.sleep(random.uniform(0.3, 0.8))
        if not send_message(d, question):
            raise RuntimeError("重新发送问题失败")
        time.sleep(random.uniform(1.5, 2.5))  # 等 UI 稳定

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

    # 文心兜底：正文渲染在 WebView 内，无障碍树拿不到正文文本。
    # 若上面仍未采到足够正文，点击回答卡片「复制」图标 → 读剪贴板取全文。
    if len(clean_text(answer_text)) < 20:
        log("无障碍树未采到正文，尝试「复制」图标 → 剪贴板兜底")
        try:
            clip_text = copy_answer_to_clipboard(d)
            if len(clip_text) >= 20:
                log(f"✓ 剪贴板兜底采到正文 {len(clip_text)} 字符")
                answer_text = clip_text
            else:
                log("剪贴板兜底未取到正文")
        except Exception as exc:
            LOG.warning("剪贴板兜底失败：%s", exc)

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

    # 6. 网页来源面板截图
    #    来源入口在回答末尾，先回到底部让入口可见；
    #    找不到时 open_sources_panel 会自己上下扫描
    sources_path = task_dir / "answer_sources.png"
    has_sources = False
    try:
        scroll_to_bottom(d); time.sleep(1.0)
        if open_sources_panel(d):
            if screenshot_panel(d, sources_path):
                has_sources = True
            close_sources_panel(d)
    except Exception as exc:
        LOG.debug("网页来源面板处理失败（可忽略）：%s", exc)
        try:
            close_sources_panel(d)
        except Exception:
            pass

    # 7. 合并图片（主问答 + 网页来源面板）
    final_image = task_dir / "answer.png"
    if has_sources:
        merge_two_screenshots(main_path, sources_path, final_image)
        log("✓ 主问答 + 网页来源面板已合并")
    else:
        merge_two_screenshots(main_path, None, final_image)
        log("（无网页来源面板，仅主问答截图）")

    # 8. 生成 HTML
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
    diag_dir = BASE_DIR / "wenxin_diagnose" / timestamp
    diag_dir.mkdir(parents=True, exist_ok=True)
    log(f"当前前台：{d.app_current()}")
    w, h = get_screen_size(d)
    log(f"屏幕：{w}x{h}")
    viewport = get_message_viewport(d)
    log(f"消息视窗：{viewport}")
    dump_xml(d, diag_dir / "window.xml")
    save_screen(diag_dir / "screen.png")

    # 诊断顶部模式区域（对话/工作 页签、模型下拉）
    try:
        nodes = parse_nodes(dump_xml(d))
        top_limit = int(h * 0.15)
        for n in nodes:
            text = (n["text"] or "").strip()
            if text in ("新对话", "对话", "工作"):
                b = bounds_to_tuple(n["bounds"])
                if b and b[3] <= top_limit:
                    log(f"顶部模式节点：text={text!r}, bounds={n['bounds']}, "
                        f"clickable={n['clickable']}")
    except Exception as exc:
        LOG.debug("诊断模式区域失败：%s", exc)

    log(f"诊断完成：{diag_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wenxin Android Worker")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log("=" * 70)
    log("Wenxin Android Worker")
    log(f"package={WENXIN_PACKAGE}")
    log(f"platform={PLATFORM_NAME}")
    log("=" * 70)

    d = None
    try:
        d = connect_device()
        start_wenxin(d)
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
                d.app_stop(WENXIN_PACKAGE)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
