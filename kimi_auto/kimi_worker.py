# -*- coding: utf-8 -*-
"""
kimi_worker.py —— Kimi App 单题问答采集 Worker
================================================================

职责（对应本轮需求）：
  1. 新建对话          → new_chat()      （点 content-desc="开启新会话"）
  2. 输入问题          → input_text()
  3. 发送问题          → send_message()  （点 content-desc="发送讯息"）
  4. 监测思考与回答完毕 → wait_answer_finish()（固定底部 + 检测「复制」按钮，豆包式判定）
  5. 采集 AI 回答正文   → 长截图过程中同步采集（左对齐节点，缩进过滤掉思考内容）
  6. 采集思考过程       → expand_and_collect_thinking()
                          （点「思考已完成」标题展开 → 采集缩进(灰色)节点 → 收起面板）
  7. 顶部到底部长截图   → screenshot_main_with_text()
                          （逐屏截图 + 相邻帧重叠区匹配拼接；**下边界取输入框上方，绝不截到输入框**）
  8. HTML 生成 / 截图上传 / 结果回调 → handle_unit()

Kimi 与 DeepSeek 的关键差异（决定了本文件的实现方式）：
  * Kimi 是 **Jetpack Compose** 应用，几乎没有 resource-id，
    但 content-desc 很丰富（开启新会话 / 发送讯息 / 更多输入方式 / 导航按钮 …）→ 以 desc 为锚点；
  * Kimi 的「思考正文」与「回答正文」**字体颜色几乎一样（都接近黑色）**，
    无法像 DeepSeek 那样用颜色区分；实测结构差异为 **左边界缩进**：
        思考正文 / 工具提示  x1 ≈ 132
        回答正文            x1 ≈ 48
        用户提问（右对齐）   x2 ≈ 1032
    因此本文件统一用「缩进阈值」把 思考 / 回答 / 提问 三者分开；
  * 顶部悬浮栏（导航按钮 / 快速 进阶 / 开启新会话）会盖住消息区上沿，
    底部「回到底部」悬浮按钮会盖住消息区下沿 → 截图视窗上下都要留出保留带。

可直接单跑（连任务池）：python kimi_worker.py
"""

from __future__ import annotations

import argparse
import html
import logging
import re
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

from config import (
    SCREENSHOT_DIR as OUTPUT_DIR,
    PLATFORM_NAME,
    KIMI_HOME_INPUT_HINTS,
    KIMI_NEW_CHAT_DESC,
    KIMI_NAV_DESC,
    KIMI_DRAWER_AVATAR_DESC,
    KIMI_DRAWER_SEARCH_WORD,
    KIMI_SEND_DESC,
    KIMI_JUMP_BOTTOM_DESC,
    KIMI_MORE_INPUT_DESC,
    KIMI_VOICE_INPUT_DESC,
    KIMI_THINK_BODY_X1_MIN,
    KIMI_ANSWER_X1_MAX,
    KIMI_THINKING_TITLE_PATTERN,
    KIMI_TOOL_HINT_PATTERN,
    KIMI_ACTION_BAR_DESCS,
    KIMI_FOOTER_HINT,
    KIMI_HEADER_WORDS,
    ANSWER_HARD_TIMEOUT,
    ANSWER_MIN_WAIT,
    ANSWER_MIN_COMPLETE_LENGTH,
    POLL_INTERVAL,
    POLL_INTERVAL_SLOW,
    MAX_TOP_SWIPES,
    MAX_BOTTOM_SWIPES,
    SCROLL_STABLE_ROUNDS,
    MAX_CAPTURE_FRAMES,
    CAPTURE_SWIPE_DURATION,
    CAPTURE_SETTLE_WAIT,
    CAPTURE_FRACTION,
)

# 复用登录模块里的 App 生命周期与设备工具（避免重复实现）
from kimi_login import (
    connect_phone,
    get_device_serial,
    run_adb,
    start_kimi,
    kimi_package,
    force_reset,
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 顶部悬浮栏以下的保留带（比例）：避开「导航按钮 / 快速 进阶」遮挡
HEADER_RESERVE_FRACTION = 0.112      # 0.112 * 1920 ≈ 215px
# 底部保留带（比例）：避开「回到底部」悬浮按钮（实测 y 1449~1536）
BOTTOM_RESERVE_FRACTION = 0.105      # 0.105 * 1920 ≈ 202px
# 右侧裁剪像素：避开回答里每条标题最右侧的「向下展开箭头」图标
# （实测箭头位于 x≈870~970，1080 屏宽下裁掉右侧 210px 即可排除）
RIGHT_CROP_PX = 210

# 截图视窗下边界到输入框的额外安全间距
COMPOSER_SAFE_GAP = 8

# 纯噪声文本（整段等于这些时丢弃）
IGNORE_EXACT = {
    "开启新会话", "更多", "快速", "进阶", "复制", "分享", "评价回答", "read_aloud",
    "发送讯息", "更多输入方式", "切换至语音输入", "尽管问，带图也行", "随时发消息…",
    "内容由 AI 生成", "思考中", "思考已完成", "导航按钮", "取消", "确认", "返回",
    "你好", "新建项目", "PPT", "集群", "构建", "Kimi", "去登录", "登录",
    "快速模式", "进阶模式", "发送", "停止", "重新生成",
}
IGNORE_SUBSTR = (
    "内容由 AI 生成", "由 AI 生成", "仅供参考", "模型名称", "备案号",
)

# worker_lib（任务池：认领 / 心跳 / 归还 / 上传 / 回调）
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

LOG = logging.getLogger("kimi_mobile")


# ============================================================
# 日志
# ============================================================

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log(msg: str) -> None:
    LOG.info(msg)


# ============================================================
# ADB / 截图基础
# ============================================================

def adb_screenshot_bytes() -> bytes:
    """adb 截整屏，返回 PNG 原始字节。

    优先 `adb exec-out screencap -p`（Python 直接拿 bytes，二进制安全）；
    失败时退回「shell screencap 到 /sdcard + pull」，避免个别模拟器 exec-out 异常。
    """
    # 确保设备序列号已获取（run_adb 从 kimi_login 导入，自动加 -s）
    get_device_serial()
    result = run_adb("exec-out", "screencap", "-p", timeout=30)
    data = result.stdout
    if result.returncode == 0 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    log("exec-out 截屏异常，改用 shell screencap + pull")
    tmp = "/sdcard/_kimi_shot.png"
    run_adb("shell", "screencap", "-p", tmp, timeout=30)
    local = OUTPUT_DIR / "_kimi_shot.png"
    run_adb("pull", tmp, str(local), timeout=30)
    run_adb("shell", "rm", "-f", tmp, timeout=15)
    return local.read_bytes()


def save_screen(path: Path) -> Image.Image:
    data = adb_screenshot_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Image.open(BytesIO(data)).convert("RGB")


def _grab_full_screen() -> Image.Image:
    """adb 截整屏并返回 PIL.Image（RGB），用于裁剪与拼接。"""
    return Image.open(BytesIO(adb_screenshot_bytes())).convert("RGB")


def connect_device():
    """独立运行时的设备连接（带友好报错）。"""
    if u2 is None:
        raise RuntimeError("缺少 uiautomator2：pip install -U uiautomator2")
    return connect_phone()


# ============================================================
# XML 解析 / 通用节点工具
# ============================================================

def dump_xml(d, path: Optional[Path] = None) -> str:
    """导出当前界面 XML（带重试）。

    ⚠️ Compose 界面常驻动画会让 `uiautomator dump` 报 "could not get idle state"，
    统一走 uiautomator2 的 dump_hierarchy 并重试 3 次。
    """
    xml = ""
    for attempt in range(3):
        try:
            xml = d.dump_hierarchy(compressed=False)
            if xml:
                break
        except Exception as exc:
            LOG.debug("dump_hierarchy 第 %d 次失败：%s", attempt + 1, exc)
        time.sleep(0.5)
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
    """把 XML 解析成节点字典列表（文本 / desc / 位置 / 可点击性 / 滚动性）。"""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    result = []
    for node in root.iter("node"):
        b = bounds_to_tuple(node.attrib.get("bounds", ""))
        if not b:
            continue
        result.append({
            "text": clean_text(node.attrib.get("text", "")),
            "desc": clean_text(node.attrib.get("content-desc", "")),
            "rid": node.attrib.get("resource-id", ""),
            "class": node.attrib.get("class", ""),
            "bounds": b,
            "clickable": node.attrib.get("clickable") == "true",
            "enabled": node.attrib.get("enabled", "true") != "false",
            "visible": node.attrib.get("visible-to-user", "true") != "false",
            "scrollable": node.attrib.get("scrollable") == "true",
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


def click_node(d, nodes: list, node: dict) -> bool:
    """点击节点中心；节点本身不可点时，改为点击「包含该中心的最小可点击节点」。"""
    x1, y1, x2, y2 = node["bounds"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    try:
        if not node["clickable"]:
            owners = []
            for n in nodes:
                if not n["clickable"] or not n["enabled"]:
                    continue
                a1, b1, a2, b2 = n["bounds"]
                if a1 <= cx <= a2 and b1 <= cy <= b2:
                    owners.append(((a2 - a1) * (b2 - b1), n))
            if owners:
                owners.sort(key=lambda x: x[0])
                bx1, by1, bx2, by2 = owners[0][1]["bounds"]
                d.click((bx1 + bx2) // 2, (by1 + by2) // 2)
                return True
        d.click(cx, cy)
        return True
    except Exception as exc:
        LOG.debug("点击节点失败：%s", exc)
        return False


def find_node(nodes: list, *, text=None, desc=None, text_contains=None,
              desc_contains=None) -> Optional[dict]:
    """按 text / content-desc 精确或包含匹配，返回面积最小的候选节点。"""
    cands = []
    for n in nodes:
        if not n["enabled"] or not n["visible"]:
            continue
        if text is not None and n["text"] != text:
            continue
        if desc is not None and n["desc"] != desc:
            continue
        if text_contains is not None and text_contains not in n["text"]:
            continue
        if desc_contains is not None and desc_contains not in n["desc"]:
            continue
        b = n["bounds"]
        cands.append(((b[2] - b[0]) * (b[3] - b[1]), n))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def click_by_desc(d, desc: str, *, contains=False) -> bool:
    nodes = parse_nodes(dump_xml(d))
    n = find_node(nodes, desc_contains=desc) if contains else find_node(nodes, desc=desc)
    if n and click_node(d, nodes, n):
        return True
    return False


# ============================================================
# App 生命周期
# ============================================================

def start_kimi_app(d) -> None:
    start_kimi(d)   # 复用 kimi_login 中的健壮实现（连续两次确认前台）


# ============================================================
# 新建对话 / 输入 / 发送
# ============================================================

def _is_fresh_conversation(xml: str) -> bool:
    """新对话判定：出现输入框 hint，且没有任何回答操作条 / 思考标题。"""
    if not xml:
        return False
    if not any(h in xml for h in KIMI_HOME_INPUT_HINTS):
        return False
    if "思考已完成" in xml or "思考中" in xml:
        return False
    for desc in KIMI_ACTION_BAR_DESCS:
        if desc in xml:
            return False
    return True


# ---- 抽屉辅助（worker 里自建，避免循环 import）----

def _is_drawer_open(xml: str) -> bool:
    """左侧抽屉是否张开：有「用户头像」desc 或抽屉搜索框特征。"""
    if not xml:
        return False
    return KIMI_DRAWER_AVATAR_DESC in xml or KIMI_DRAWER_SEARCH_WORD in xml


def _open_drawer(d) -> bool:
    """点左上角「导航按钮」打开抽屉，验证抽屉已张开。"""
    for _ in range(3):
        if click_by_desc(d, KIMI_NAV_DESC):
            time.sleep(1.2)
        else:
            w, h = get_screen_size(d)
            d.click(int(w * 0.105), int(h * 0.072))
            time.sleep(1.2)
        if _is_drawer_open(dump_xml(d)):
            return True
    return False


def _close_drawer_if_open(d) -> bool:
    """抽屉张开时，点右侧遮罩关闭（坐标兜底）。"""
    xml = dump_xml(d)
    if not _is_drawer_open(xml):
        return True
    w, h = get_screen_size(d)
    d.click(int(w * 0.96), int(h * 0.5))
    time.sleep(0.8)
    return not _is_drawer_open(dump_xml(d))


# ---- 新建对话：直接点击（desc / 标注坐标）优先，OpenCV 模板匹配兜底 ----

# 「+」新建按钮图标模板：按项目目录相对定位，避免路径搬家后失效。
NEW_CONVERSATION_TEMPLATE = (
    Path(__file__).resolve().parent / "kimi_button_icons" / "new_conversition_icon.png"
)

# 图②标注：抽屉右上角「+」新建按钮的归一化坐标
# （实测抽屉截图 580x1024 中按钮中心约 (431, 95)）
NEW_CHAT_BTN_REL_POS = (0.743, 0.093)

# 只在左侧抽屉右上角 ROI 匹配，避免整屏其他图形干扰。
# 根据用户日志，抽屉打开时旧算法最高分为 0.6824，因此阈值设为 0.60；
# ROI 限定位置后再以点击后的页面状态验证，降低误触。
_ICON_THRESHOLD = 0.60
_ICON_SCALE_MIN = 0.65
_ICON_SCALE_MAX = 1.80
_ICON_SCALE_STEP = 0.05
_ICON_MATCH_RETRIES = 3
_ICON_RETRY_INTERVAL = 0.8


def _click_new_conversation_direct(d) -> bool:
    """按图②标注直接点击抽屉右上角「+」新建按钮。

    顺序：
      1. uiautomator2 desc 点击（最快）；
      2. XML 节点取 bounds 中心点击（防 desc 失效）；
      3. 以上失败，按标注归一化坐标直接点击（(0.743W, 0.093H)）。
    """
    # 1) u2 desc 点击
    try:
        obj = d(description=KIMI_NEW_CHAT_DESC)
        if obj.exists:
            obj.click()
            log("已按 desc「开启新会话」点击新建按钮")
            time.sleep(1.2)
            return True
    except Exception as exc:
        LOG.debug("u2 desc 点击新建按钮失败：%s", exc)

    # 2) XML 节点中心兜底
    try:
        nodes = parse_nodes(dump_xml(d))
        n = find_node(nodes, desc=KIMI_NEW_CHAT_DESC)
        if n:
            b = n["bounds"]
            d.click((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            log("已按 XML desc 节点中心点击新建按钮")
            time.sleep(1.2)
            return True
    except Exception as exc:
        LOG.debug("XML desc 点击新建按钮失败：%s", exc)

    # 3) 标注比例坐标兜底（抽屉右上角「+」）
    w, h = get_screen_size(d)
    x, y = int(w * NEW_CHAT_BTN_REL_POS[0]), int(h * NEW_CHAT_BTN_REL_POS[1])
    d.click(x, y)
    log(f"已按标注坐标点击新建按钮：({x},{y})")
    time.sleep(1.2)
    return True


def _load_cv2():
    """延迟加载 OpenCV，缺少依赖时给出明确错误。"""
    try:
        import cv2
        return cv2
    except ImportError as exc:
        raise RuntimeError(
            "新建对话的视觉匹配需要 OpenCV，请执行：pip install opencv-python"
        ) from exc


def _screen_image_for_matching(cv2):
    """通过现有 ADB 截图函数取当前屏幕，并转换为灰度图。"""
    image_bytes = adb_screenshot_bytes()
    if not image_bytes:
        raise RuntimeError("ADB 截图返回空数据，无法进行图像匹配")
    encoded = np.frombuffer(image_bytes, dtype=np.uint8) if np is not None else None
    if encoded is None:
        import numpy as _np
        encoded = _np.frombuffer(image_bytes, dtype=_np.uint8)
    screen = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if screen is None:
        raise RuntimeError("OpenCV 无法解码当前屏幕截图")
    return screen, cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)


def _read_icon_template(cv2, template_path: Path):
    """加载模板并统一成灰度图；模板不存在或尺寸无效时立即报错。"""
    if not template_path.is_file():
        raise FileNotFoundError(
            f"找不到新建对话图标模板：{template_path}\\n"
            "请确认图片路径、文件名拼写以及运行程序的 Windows 用户目录。"
        )
    template = cv2.imread(str(template_path), cv2.IMREAD_UNCHANGED)
    if template is None:
        raise RuntimeError(f"OpenCV 无法读取图标模板：{template_path}")
    if len(template.shape) == 2:
        gray = template
    elif template.shape[2] == 4:
        # PNG 透明区域先合成到白色背景，避免透明像素的 RGB 垃圾值干扰匹配。
        bgr = cv2.cvtColor(template, cv2.COLOR_BGRA2BGR)
        alpha = template[:, :, 3].astype("float32") / 255.0
        import numpy as _np
        white = _np.full_like(bgr, 255)
        composited = (bgr.astype("float32") * alpha[:, :, None]
                      + white.astype("float32") * (1.0 - alpha[:, :, None]))
        gray = cv2.cvtColor(composited.astype("uint8"), cv2.COLOR_BGR2GRAY)
    else:
        gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    if gray.size == 0 or gray.shape[0] < 8 or gray.shape[1] < 8:
        raise ValueError(f"图标模板尺寸过小或无效：{gray.shape}")
    return gray


def _find_new_conversation_icon(cv2, screen_gray, template_gray):
    """仅在侧边抽屉右上角 ROI 内做多尺度模板匹配。

    返回 (center_x, center_y, score, box)，坐标为整张屏幕坐标。
    ROI 按屏幕比例计算，适配不同分辨率；不会在整屏搜索或点击其他相似图形。
    """
    screen_h, screen_w = screen_gray.shape[:2]

    # 根据用户提供的实际截图：新建对话按钮位于抽屉顶部右侧，约 x=0.74W、y=0.09H。
    # 留出适度余量应对不同设备状态栏/分辨率，但不扩展到消息正文或底部按钮。
    roi_x1 = int(screen_w * 0.58)
    roi_x2 = int(screen_w * 0.91)
    roi_y1 = int(screen_h * 0.025)
    roi_y2 = int(screen_h * 0.19)
    roi = screen_gray[roi_y1:roi_y2, roi_x1:roi_x2]
    if roi.size == 0:
        log("新建对话 ROI 无效，无法执行模板匹配")
        return None

    template_h, template_w = template_gray.shape[:2]
    best = None
    scale = _ICON_SCALE_MIN

    while scale <= _ICON_SCALE_MAX + 1e-9:
        width = max(8, int(round(template_w * scale)))
        height = max(8, int(round(template_h * scale)))
        roi_h, roi_w = roi.shape[:2]
        if width <= roi_w and height <= roi_h:
            resized = cv2.resize(
                template_gray, (width, height),
                interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            )
            response = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
            _, max_score, _, max_loc = cv2.minMaxLoc(response)
            local_x, local_y = max_loc
            x1, y1 = roi_x1 + local_x, roi_y1 + local_y
            candidate = (
                float(max_score),
                int(x1 + width // 2),
                int(y1 + height // 2),
                (int(x1), int(y1), int(x1 + width), int(y1 + height)),
                scale,
            )
            if best is None or candidate[0] > best[0]:
                best = candidate
        scale = round(scale + _ICON_SCALE_STEP, 4)

    log(f"新建对话局部扫描范围：x={roi_x1}:{roi_x2}, y={roi_y1}:{roi_y2}")
    if best is None or best[0] < _ICON_THRESHOLD:
        if best is not None:
            log(
                f"抽屉右上角 ROI 最高匹配分数不足：score={best[0]:.4f}, "
                f"threshold={_ICON_THRESHOLD:.2f}, box={best[3]}"
            )
        return None

    score, cx, cy, box, matched_scale = best
    log(
        f"OpenCV 命中抽屉右上角新建对话图标：center=({cx},{cy}), "
        f"score={score:.4f}, scale={matched_scale:.2f}, box={box}"
    )
    return cx, cy, score, box


def _click_new_conversation_icon(d) -> bool:
    """仅扫描抽屉右上角 ROI；达到阈值后点击匹配区域中心。"""
    cv2 = _load_cv2()
    template_gray = _read_icon_template(cv2, NEW_CONVERSATION_TEMPLATE)

    for attempt in range(1, _ICON_MATCH_RETRIES + 1):
        try:
            _, screen_gray = _screen_image_for_matching(cv2)
            hit = _find_new_conversation_icon(cv2, screen_gray, template_gray)
            if hit is None:
                log(f"第 {attempt}/{_ICON_MATCH_RETRIES} 次视觉扫描未找到可信图标")
            else:
                x, y, score, _ = hit
                # 只有视觉匹配成功才点击；不使用坐标、文字或 desc 兜底。
                d.click(x, y)
                log(f"已点击新建对话图标中心：({x},{y})，score={score:.4f}")
                time.sleep(1.2)
                return True
        except Exception as exc:
            log(f"第 {attempt}/{_ICON_MATCH_RETRIES} 次视觉匹配失败：{exc}")
        if attempt < _ICON_MATCH_RETRIES:
            time.sleep(_ICON_RETRY_INTERVAL)
    return False


def new_chat(d) -> bool:
    """
    创建新对话。

    流程（按用户标注图①~②，优先直接点击，失败再模板匹配兜底）：
      1. 已是空白新会话 → 直接返回；
      2. 打开左侧抽屉（图①：点左上角「导航按钮」）；
      3. 直接点击抽屉右上角「+」新建按钮（图②）：
         desc「开启新会话」→ XML 节点中心 → 标注比例坐标；
      4. 点击后验证是否进入空白新会话；
      5. 若直接点击未生效，改用 OpenCV 模板匹配兜底；
      6. 再次验证，仍失败则返回 False。

    直接点击路径不依赖截图模板，速度快、稳定性高；
    模板匹配仅作为 UI 改版后的保险。
    """
    log("开始创建新对话（优先直接点击，模板兜底）")

    # 只用于避免重复创建；不参与按钮定位。
    try:
        if _is_fresh_conversation(dump_xml(d)):
            log("当前已经是空白新会话，无需重复创建")
            return True
    except Exception as exc:
        log(f"初始会话状态检查失败，继续执行：{exc}")

    # 图①：打开左侧抽屉，让「+」新建按钮可见。
    try:
        xml = dump_xml(d)
        if not _is_drawer_open(xml):
            log("抽屉未打开，点击左上角「导航按钮」")
            if not _open_drawer(d):
                log("无法打开侧边抽屉，停止操作，避免盲目点击")
                return False
        else:
            log("侧边抽屉已打开")
    except Exception as exc:
        log(f"准备侧边抽屉时发生异常：{exc}")
        return False

    # 图②：直接点击「+」新建按钮（desc → XML → 标注坐标）。
    _click_new_conversation_direct(d)
    time.sleep(1.0)
    try:
        if _is_fresh_conversation(dump_xml(d)):
            log("✓ 已确认进入空白新会话（直接点击成功）")
            return True
    except Exception as exc:
        LOG.debug("直接点击后状态验证异常：%s", exc)

    # 直接点击未生效 → OpenCV 模板匹配兜底。
    log("直接点击未确认成功，改用 OpenCV 模板匹配兜底")
    try:
        xml = dump_xml(d)
        if not _is_drawer_open(xml):
            log("抽屉已关闭，重新打开以供模板匹配")
            if not _open_drawer(d):
                log("无法重新打开抽屉，放弃模板匹配")
                return False
        if _click_new_conversation_icon(d):
            time.sleep(1.5)
            if _is_fresh_conversation(dump_xml(d)):
                log("✓ 已通过 OpenCV 点击并确认进入空白新会话")
                return True
            log("模板匹配点击后仍未进入空白新会话")
        else:
            log("OpenCV 模板匹配未找到可信图标")
    except Exception as exc:
        log(f"模板匹配兜底异常：{exc}")

    log("✗ 新建对话失败")
    return False


def find_input(d):
    """查找输入框。

    优先真实 EditText；Kimi 未聚焦时输入框可能只暴露一个 hint 文本节点
    （「尽管问，带图也行」/「随时发消息…」），此时返回该 hint 节点用于点击聚焦。
    """
    nodes = parse_nodes(dump_xml(d))
    for n in nodes:
        if "EditText" in n["class"]:
            return {"kind": "edit", "node": n}
    for hint in KIMI_HOME_INPUT_HINTS:
        n = find_node(nodes, text_contains=hint)
        if n:
            return {"kind": "hint", "node": n}
    return None


def _input_region_top(d) -> Optional[int]:
    """输入框（composer）顶边 y：取输入类节点里最靠上的一个。"""
    nodes = parse_nodes(dump_xml(d))
    tops = []
    for n in nodes:
        if "EditText" in n["class"]:
            tops.append(n["bounds"][1])
            continue
        if n["desc"] in (KIMI_MORE_INPUT_DESC, KIMI_VOICE_INPUT_DESC, KIMI_SEND_DESC):
            tops.append(n["bounds"][1])
            continue
        if any(hint in n["text"] for hint in KIMI_HOME_INPUT_HINTS):
            tops.append(n["bounds"][1])
    if not tops:
        return None
    return min(tops)


def input_text(d, question: str) -> None:
    """把问题写入输入框，并校验确实写进去了。"""
    target = find_input(d)
    if target is None:
        raise RuntimeError("找不到 Kimi 输入框")
    log(f"输入问题：{question[:100]}")

    nodes = parse_nodes(dump_xml(d))
    click_node(d, nodes, target["node"])
    time.sleep(0.8)

    # 点击后输入框通常变成真正的 EditText，再取一次
    for attempt in range(3):
        current = find_input(d)
        edit_node = (current or {}).get("node") if current else None
        if current and current["kind"] == "edit":
            try:
                obj = d(className="android.widget.EditText")
                if obj.exists(timeout=1):
                    try:
                        obj.clear_text()
                    except Exception:
                        pass
                    obj.set_text(str(question))
                    time.sleep(0.8)
                    if _verify_input_written(d, question):
                        return
            except Exception as exc:
                LOG.debug("set_text 失败，尝试 fastinput：%s", exc)
        # fastinput 兜底（Compose 输入框对 set_text 兼容性一般）
        try:
            d.set_fastinput_ime(True)
            d.send_keys(str(question), clear=True)
            time.sleep(0.8)
            if _verify_input_written(d, question):
                return
        finally:
            try:
                d.set_fastinput_ime(False)
            except Exception:
                pass
        log(f"第 {attempt + 1} 次写入问题未确认，重试")
        time.sleep(0.6)

    raise RuntimeError("问题没有写入输入框")


def _verify_input_written(d, question: str) -> bool:
    """校验输入框已包含问题文本（取前 12 个字符比对，避免换行/省略号差异）。"""
    probe = clean_text(question)[:12]
    xml = dump_xml(d)
    if probe and probe in xml:
        return True
    # 输入框非空时 hint 会从「尽管问，带图也行」变成「随时发消息…」，也算写入了
    return ("随时发消息" in xml) and not any(
        h in xml for h in KIMI_HOME_INPUT_HINTS if h == "尽管问，带图也行"
    )


def send_message(d) -> bool:
    """点「发送讯息」发送问题。**只点一次，绝不重试**。

    ⚠️ 关键：Kimi 的发送按钮（中心 ≈ 960,1800）在消息发出后会**立刻变成
    「停止生成」按钮**（同一位置）。如果这里循环重试 3 次，第二次点击
    就等于点了「停止生成」，把正在生成的回答直接掐掉。
    所以 send_message 必须 **单次点击 + 单次校验**，校验失败也不再点。
    后续 wait_answer_finish 会自己判任务状态。
    """
    log("发送问题（单次点击）")
    clicked = _click_send_button_by_bounds(d)
    # 给 Kimi 足够时间处理发送动作 + 切换按钮状态
    time.sleep(2.0)

    if _input_cleared(d):
        log("✓ 问题已发送（输入框已清空）")
        return True

    # 没清空也不再点了 —— 可能已经发出去了只是 XML 还没刷新到空状态，
    # 继续等后续 wait_answer_finish 来最终判定
    log("发送后输入框未清空（按钮状态切换可能延迟），不再重点击，交给后续流程判定")
    return True   # 视为"已点发送"，不是"发送失败"


def _click_send_button_by_bounds(d) -> bool:
    """取 desc='发送讯息' 节点的 bounds，算像素中心直接 d.click(x, y)。

    三级策略：
      1. XML 里 desc='发送讯息' → bounds → 像素中心点击（最准）
      2. u2 d(description='发送讯息').info → bounds → 像素中心点击
      3. 兜底比例坐标：输入框右侧约 w*0.94, h*0.938（1080×1920 ≈ 1015,1800）
    """
    # 策略 1：XML 找 desc='发送讯息'
    try:
        nodes = parse_nodes(dump_xml(d))
        for n in nodes:
            if n.get("desc") == KIMI_SEND_DESC:
                b = n.get("bounds")
                if b and len(b) == 4:
                    x1, y1, x2, y2 = b
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    log(f"XML 找到发送讯息 bounds={b} → 中心 ({cx},{cy})")
                    d.click(cx, cy)
                    return True
    except Exception as exc:
        log(f"XML 查发送按钮异常: {exc}")

    # 策略 2：u2 选择器
    try:
        obj = d(description=KIMI_SEND_DESC)
        if obj.exists(timeout=1):
            info = obj.info
            b = info.get("bounds", {})
            x1, y1 = int(b.get("left", 0)), int(b.get("top", 0))
            x2, y2 = int(b.get("right", 0)), int(b.get("bottom", 0))
            if x2 > x1 and y2 > y1:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                log(f"u2 找到发送讯息 bounds=[{x1},{y1}][{x2},{y2}] → 中心 ({cx},{cy})")
                d.click(cx, cy)
                return True
    except Exception as exc:
        log(f"u2 查发送按钮异常: {exc}")

    # 策略 3：兜底比例坐标
    w, h = get_screen_size(d)
    cx, cy = int(w * 0.94), int(h * 0.938)   # 1080×1920 ≈ (1015, 1800)
    log(f"兜底比例坐标点击发送: ({cx},{cy})")
    d.click(cx, cy)
    return True


def _input_cleared(d) -> bool:
    """发送成功判据：输入框恢复为空白 hint。"""
    xml = dump_xml(d)
    if not xml:
        return False
    if "尽管问，带图也行" in xml:
        return True
    # hint 为「随时发消息…」也可能是空输入框（未聚焦时的占位），
    # 此时结合“消息区内出现新内容”判断，交由 wait_answer_finish 兜底
    return False


# ============================================================
# 消息区视窗 / 截图
# ============================================================

def get_message_viewport(d, *, reserve_bottom: bool = True) -> tuple:
    """计算「消息区」裁剪视窗（left, top, right, bottom）。

    * left   —— 固定 0（消息从左边缘开始）
    * top    —— 躲开顶部悬浮栏（导航按钮 / 快速 进阶 / 开启新会话）
    * right  —— 裁掉右侧 RIGHT_CROP_PX，排除回答标题最右侧的「向下展开箭头」
    * bottom —— 躲开输入框（composer），保证长截图**不含输入框**；
                reserve_bottom=True 时再额外预留一条底部保留带，
               避开「回到底部」悬浮按钮（它在底部会盖住消息内容）。
    """
    w, h = get_screen_size(d)
    nodes = parse_nodes(dump_xml(d))

    # ---- 顶部：悬浮栏元素的最大下边界 + 间距 ----
    top = int(h * HEADER_RESERVE_FRACTION)
    for n in nodes:
        label = n["text"] or n["desc"]
        if not label:
            continue
        if any(word == label for word in KIMI_HEADER_WORDS) and n["bounds"][1] < h * 0.2:
            top = max(top, n["bounds"][3] + 4)

    # ---- 右侧：裁掉箭头列 ----
    right = max(100, w - RIGHT_CROP_PX)

    # ---- 底部：输入框顶边 - 安全间距 ----
    composer_top = _input_region_top(d)
    bottom = (composer_top - COMPOSER_SAFE_GAP) if composer_top else int(h * 0.855)
    if reserve_bottom:
        bottom = min(bottom, int(h * (1 - BOTTOM_RESERVE_FRACTION)))

    bottom = max(bottom, top + 200)
    right = min(right, w)
    return (0, int(top), int(right), int(bottom))


def save_message_area(d, path: Path, viewport: tuple,
                      full: Optional[Image.Image] = None) -> Image.Image:
    """按视窗裁剪消息区并落盘。full 可复用调用方已截的整屏图，省一次 adb 截屏。"""
    if full is None:
        full = _grab_full_screen()
    left, top, right, bottom = viewport
    left = max(0, min(left, full.width - 1))
    top = max(0, min(top, full.height - 1))
    right = max(left + 1, min(right, full.width))
    bottom = max(top + 1, min(bottom, full.height))
    crop = full.crop((left, top, right, bottom))
    crop.save(path, format="PNG")
    return crop


def image_signature(img: Image.Image) -> bytes:
    """图像签名（缩略后的像素），用于「两帧是否相同」的容差比较。"""
    small = img.copy()
    small.thumbnail((240, 600))
    return small.convert("RGB").tobytes()


SIG_SIMILAR_THRESHOLD = 3.0   # 平均像素差阈值：3.0 能吸收回弹/抗锯齿抖动，又能识别真实位移


def _sig_distance(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        return float("inf")
    if np is None:
        return 0.0 if a == b else float("inf")
    x = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
    y = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
    return float(np.mean(np.abs(x - y)))


def _sig_similar(a: Optional[bytes], b: Optional[bytes]) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    return _sig_distance(a, b) <= SIG_SIMILAR_THRESHOLD


def _swipe_in_viewport(d, direction: str, *, fraction: float = CAPTURE_FRACTION,
                       duration: float = CAPTURE_SWIPE_DURATION, viewport: tuple):
    """在视窗内部滑动（不会误触顶部悬浮栏 / 输入框）。"""
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


def click_jump_to_bottom(d) -> bool:
    """点「回到底部」悬浮按钮（content-desc=JumpToBottom）。"""
    nodes = parse_nodes(dump_xml(d))
    n = find_node(nodes, desc_contains=KIMI_JUMP_BOTTOM_DESC)
    if n and click_node(d, nodes, n):
        time.sleep(0.8)
        return True
    return False


def scroll_to_top(d, viewport: Optional[tuple] = None) -> bool:
    """滚到会话顶部（以消息区画面是否变化为准）。"""
    log("滚到会话顶部")
    if viewport is None:
        viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_TOP_SWIPES):
        _swipe_in_viewport(d, "down", fraction=0.55, duration=0.5, viewport=viewport)
        time.sleep(0.5)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_top_probe.png", viewport)
            sig = image_signature(img)
            if _sig_similar(sig, last_sig):
                stable += 1
                if stable >= SCROLL_STABLE_ROUNDS:
                    log("✓ 已到达会话顶部")
                    return True
            else:
                stable = 0
                last_sig = sig
        except Exception as exc:
            LOG.debug("顶部探测失败：%s", exc)
    log("顶部定位达到最大次数（可能未真正到顶）")
    return True


def scroll_to_bottom(d, viewport: Optional[tuple] = None) -> bool:
    """滚回会话底部：先尝试「回到底部」按钮，再退化到滑动探测。"""
    log("滚回会话底部")
    for _ in range(3):
        if click_jump_to_bottom(d):
            time.sleep(0.8)
            return True
    if viewport is None:
        viewport = get_message_viewport(d)
    last_sig = None
    stable = 0
    for i in range(MAX_BOTTOM_SWIPES):
        _swipe_in_viewport(d, "up", fraction=0.55, duration=0.5, viewport=viewport)
        time.sleep(0.5)
        try:
            img = save_message_area(d, OUTPUT_DIR / "_bottom_probe.png", viewport)
            sig = image_signature(img)
            if _sig_similar(sig, last_sig):
                stable += 1
                if stable >= SCROLL_STABLE_ROUNDS:
                    log("✓ 已到达会话底部")
                    return True
            else:
                stable = 0
                last_sig = sig
        except Exception as exc:
            LOG.debug("底部探测失败：%s", exc)
    log("底部定位达到最大次数")
    return True


# ============================================================
# 长截图拼接（相邻帧重叠区匹配）
# ============================================================

def _row_features(img: Image.Image, samples: int = 48):
    """把图像按行降采样成特征矩阵（每行取 samples 列灰度），用于求重叠行数。"""
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = arr.shape
    if w <= samples:
        return arr
    step = w // samples
    cols = np.arange(0, w, step)[:samples]
    return arr[:, cols]


# 重叠匹配质量阈值：mean-abs-diff 低于此值才算可信匹配
OVERLAP_GOOD_SCORE = 6.0
OVERLAP_FALLBACK_RATIO = 0.30   # 匹配置信度低时退回到「滑动步长比例」估重叠


def find_overlap(upper: Image.Image, lower: Image.Image,
                 swipe_fraction: float = CAPTURE_FRACTION) -> int:
    """求「上一帧底部」与「下一帧顶部」的重叠行数。

    策略：
      1. 粗搜（步长 4）找到最佳重叠候选；
      2. 精搜（步长 1）在候选附近 ±6 像素内精确定位；
      3. 若最佳匹配分数 > OVERLAP_GOOD_SCORE（不可信），退回到
         基于滑动比例的保守估计：overlap = frame_h * (1 - swipe_fraction)。
    """
    if np is None:
        raise RuntimeError("长截图需要 numpy：pip install numpy")
    up_feat = _row_features(upper)
    lo_feat = _row_features(lower)
    h = min(len(up_feat), len(lo_feat))
    # 滑动比例预估：滑动步长 = 视窗 * fraction，重叠 ≈ 视窗 * (1 - fraction)
    est_ov = max(20, int(h * (1.0 - swipe_fraction)))
    min_ov = max(10, int(h * 0.10))
    max_ov = int(h * 0.80)
    if max_ov <= min_ov:
        return est_ov

    # ---- 粗搜（步长 4）----
    best_ov = est_ov
    best_score = float("inf")
    for ov in range(min_ov, max_ov + 1, 4):
        a = up_feat[-ov:]
        b = lo_feat[:ov]
        if a.shape != b.shape:
            continue
        score = float(np.mean(np.abs(a - b)))
        if score < best_score:
            best_score = score
            best_ov = ov

    # ---- 精搜（步长 1，候选 ±6）----
    lo2 = max(min_ov, best_ov - 6)
    hi2 = min(max_ov, best_ov + 6)
    for ov in range(lo2, hi2 + 1):
        a = up_feat[-ov:]
        b = lo_feat[:ov]
        if a.shape != b.shape:
            continue
        score = float(np.mean(np.abs(a - b)))
        if score < best_score:
            best_score = score
            best_ov = ov

    # ---- 质量校验：分数太差就用比例预估 ----
    if best_score > OVERLAP_GOOD_SCORE:
        LOG.debug("重叠匹配置信度低 (score=%.1f)，使用比例预估 ov=%d",
                  best_score, est_ov)
        return est_ov

    return best_ov


def stitch_frames(frames: list, output: Path) -> Image.Image:
    """把多帧按重叠区拼接成一张长图并落盘。

    拼接策略：
      * 用 find_overlap 精确求相邻帧重叠行数；
      * 在接缝处做 3px 羽化（渐变混合），消除哪怕 1px 错位造成的可见接缝；
      * 若某帧与上一帧几乎完全相同（重叠 > 90% 帧高），跳过该帧避免重复。
    """
    if not frames:
        raise RuntimeError("没有可拼接的截图帧")
    if np is None:
        # 无 numpy 时退回简单拼接
        canvas = frames[0].copy()
        for idx in range(1, len(frames)):
            lower = frames[idx]
            ov = max(5, lower.height // 3)
            new_part = lower.crop((0, ov, lower.width, lower.height))
            if new_part.height <= 0:
                continue
            merged = Image.new("RGB", (canvas.width, canvas.height + new_part.height))
            merged.paste(canvas, (0, 0))
            merged.paste(new_part, (0, canvas.height))
            canvas = merged
        canvas.save(output, format="PNG", optimize=False)
        return canvas

    canvas = frames[0].copy()
    feather = 3   # 羽化带宽（像素）

    for idx in range(1, len(frames)):
        upper = frames[idx - 1]
        lower = frames[idx]

        # 跳过几乎重复的帧（避免同屏内容重复拼接）
        if upper.height > 0 and _sig_similar(image_signature(upper),
                                             image_signature(lower)):
            LOG.debug("第 %d 帧与上一帧几乎相同，跳过", idx)
            continue

        ov = find_overlap(upper, lower)
        ov = max(5, min(ov, lower.height - 1))

        # 重叠区过大（>90% 帧高）→ 认为是重复帧，跳过
        if ov >= int(lower.height * 0.90):
            LOG.debug("第 %d 帧重叠过大 (ov=%d, h=%d)，跳过", idx, ov, lower.height)
            continue

        new_part = lower.crop((0, ov, lower.width, lower.height))
        if new_part.height <= 0:
            continue

        merged_h = canvas.height + new_part.height
        merged = Image.new("RGB", (canvas.width, merged_h))
        merged.paste(canvas, (0, 0))
        merged.paste(new_part, (0, canvas.height))

        # ---- 接缝羽化：取 canvas 末尾 feather 行 与 new_part 开头 feather 行做渐变混合 ----
        try:
            seam_y = canvas.height
            if canvas.height >= feather and new_part.height >= feather:
                top_band = np.asarray(canvas.crop(
                    (0, canvas.height - feather, canvas.width, canvas.height)),
                    dtype=np.float32)
                bot_band = np.asarray(new_part.crop(
                    (0, 0, new_part.width, feather)), dtype=np.float32)
                # alpha: 上→下，从 1→0 取 top_band，从 0→1 取 bot_band
                alpha = np.linspace(1.0, 0.0, feather, dtype=np.float32)[:, None, None]
                blended = (top_band * alpha + bot_band * (1.0 - alpha)).astype(np.uint8)
                merged.paste(Image.fromarray(blended), (0, seam_y - feather))
        except Exception as exc:
            LOG.debug("接缝羽化失败（使用硬拼接）：%s", exc)

        canvas = merged

    canvas.save(output, format="PNG", optimize=False)
    return canvas


# ============================================================
# 文本清洗 / 噪声过滤
# ============================================================

def _clean_special_chars(text: str) -> str:
    """清理 emoji / 私用区 / 零宽字符，并压缩空白。"""
    if not text:
        return ""
    keep = []
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:        # surrogate
            continue
        if 0xE000 <= cp <= 0xF8FF:        # 私用区
            continue
        if 0x1F000 <= cp <= 0x1FFFF:      # emoji 主区
            continue
        if 0x2600 <= cp <= 0x27BF:        # 杂项符号 / dingbats
            continue
        if 0x2B00 <= cp <= 0x2BFF:        # 补充符号
            continue
        if 0x2460 <= cp <= 0x24FF:        # 带圈数字
            continue
        if cp in (0xFE0F, 0x200D):        # 变体选择符 / 零宽连接
            continue
        keep.append(ch)
    result = "".join(keep)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{2,}", "\n", result)
    return result.strip()


def _is_noise_text(text: str, question: str) -> bool:
    """判断一段文本是否属于噪声（标题栏 / 操作条 / 页脚 / 提问本身等）。"""
    if not text:
        return True
    t = text.strip()
    if not t:
        return True
    cleaned = _clean_special_chars(t)
    if not cleaned or len(cleaned) < 2:
        return True
    if len(t) < 20 and t in IGNORE_EXACT:
        return True
    for sub in IGNORE_SUBSTR:
        if sub in t and len(t) < 120:
            return True
    if KIMI_FOOTER_HINT in t:
        return True
    if re.search(KIMI_THINKING_TITLE_PATTERN, cleaned):
        return True
    if re.search(KIMI_TOOL_HINT_PATTERN, cleaned):
        return True
    if question:
        q = question.strip()
        if t == q:
            return True
        if len(t) <= len(q) + 15 and q in t:
            return True
    if re.match(r"^[\s\d\W]+$", cleaned) and len(cleaned) < 10:
        return True
    return False


def _filter_substring_dupes(texts: list) -> list:
    """去掉完全重复的条目，以及被其他更长条目包含的短条目。"""
    if not texts:
        return []
    seen = set()
    unique = []
    for t in texts:
        key = t.strip()
        if not key or key in seen:
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


def _join_texts(texts: list) -> str:
    parts = [t.strip() for t in texts if t and t.strip()]
    return "\n\n".join(parts)


# ============================================================
# 回答正文采集（左对齐节点）
# ============================================================

def _overlaps_viewport(b: tuple, viewport: tuple) -> bool:
    """节点与消息视窗是否「有纵向交叠」。

    用交叠（而非完全包含）判定，原因：
      XML 里的文本节点是**整段字符串**（长段落常跨越视窗上下边界），
      只要与视窗有交叠就说明该段内容属于当前屏；配合跨屏 hash 去重，
      既能采全段落，又不会重复。完全落在视窗外的节点才丢弃（已滚过 / 尚未出现）。
    """
    _, top, _, bottom = viewport
    return b[3] > top and b[1] < bottom


def _collect_screen_answer_texts(
    d, viewport: tuple, question: str, seen_hashes: set,
    exclude_texts: Optional[Set[str]] = None,
) -> list:
    """采集当前屏的「AI 回答正文」，返回 [(y, text), ...]（按 y 升序）。

    判据（Kimi 结构特征）：
      * 左对齐（x1 <= KIMI_ANSWER_X1_MAX）→ 回答正文；
        缩进（x1 >= KIMI_THINK_BODY_X1_MIN）→ 思考正文 / 工具提示，直接排除；
        右对齐（x2 接近屏幕右缘）→ 用户提问，也排除（提问由任务字段提供）。
      * 再叠加 exclude_texts（思考段落）与噪声过滤，双保险。
    """
    items = []
    try:
        nodes = parse_nodes(dump_xml(d))
    except Exception as exc:
        LOG.debug("采集文本失败：%s", exc)
        return items

    exclude_texts = exclude_texts or set()

    for n in nodes:
        text = n["text"]
        if not text:
            continue
        b = n["bounds"]
        # 只取与消息视窗有交叠的节点（完全滚过 / 尚未出现的丢弃）
        if not _overlaps_viewport(b, viewport):
            continue
        # 左/右边界过滤：只保留左对齐的回答正文
        if b[0] > KIMI_ANSWER_X1_MAX:
            continue
        if _is_noise_text(text, question):
            continue

        cleaned = _clean_special_chars(text)
        if not cleaned or len(cleaned) < 2:
            continue
        if cleaned in exclude_texts:
            continue
        # 与思考段落互为子串的也排除
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


def best_answer_text(xml: str, question: str = "", *, viewport: Optional[tuple] = None) -> str:
    """从单帧 XML 里粗取回答正文（左对齐节点拼接），用于「文本增长」判定。"""
    if not xml:
        return ""
    texts = []
    for n in parse_nodes(xml):
        text = n["text"]
        if not text:
            continue
        b = n["bounds"]
        if viewport is not None and not _overlaps_viewport(b, viewport):
            continue
        if b[0] > KIMI_ANSWER_X1_MAX:
            continue
        if _is_noise_text(text, question):
            continue
        cleaned = _clean_special_chars(text)
        if cleaned:
            texts.append((b[1], cleaned))
    texts.sort(key=lambda x: x[0])
    return clean_text(_join_texts([t for _, t in texts]))


# ============================================================
# 等待「思考 + 回答」完毕（豆包式：固定底部 + 复制按钮判定）
# ============================================================

def answer_copy_button_exists(d) -> bool:
    """检测回答操作条的「复制」按钮是否已出现（出现 = 本条回答渲染完毕）。

    Kimi 是 Compose 应用，没有 resource-id，但回答末尾操作条
    （read_aloud / 复制 / 评价回答 / 更多 / 分享）里的「复制」
    有稳定的 content-desc="复制"。只检测，不点击。
    """
    # 第一优先级：uiautomator2 直接按 desc 选择（最快）
    try:
        if d(description="复制").exists:
            return True
    except Exception:
        pass
    # 兜底：XML 节点扫描，精确匹配 desc
    try:
        for n in parse_nodes(dump_xml(d)):
            if (n["desc"] or "").strip() == "复制":
                return True
    except Exception:
        pass
    return False


def wait_answer_finish(d, question: str = "") -> str:
    """等待回答彻底完成，并返回粗判的回答正文（供后续兜底）。

    核心规则（参照 doubao_auto）：
      1. 回答期间始终「固定」在会话底部 —— 一旦不在底部，
         Kimi 会弹出 JumpToBottom 悬浮按钮，立即点它回到底部；
         这样回答结束时操作条一定在屏幕内，检测不会漏；
      2. 只以「复制」按钮作为回答完成标志（生成中是「停止生成」按钮，
         操作条不会出现，天然不会误判）；
      3. 连续 2 轮检测到「复制」才确认完成，防止界面抖动误报；
      4. 确认完成后再强制点一次回到底部，保证后续采集从底部状态开始。
    """
    log("等待回答完成：固定底部，检测「复制」按钮")
    deadline = time.time() + ANSWER_HARD_TIMEOUT
    seen_copy = 0
    last_bottom_click = 0.0

    # 刚发送后先给 App 一点时间开始生成（此时还没有回答操作条）
    log(f"发送后强制等待 {ANSWER_MIN_WAIT:.0f} 秒……")
    time.sleep(ANSWER_MIN_WAIT)

    while time.time() < deadline:
        try:
            # 第一优先级：检测「复制」按钮
            if answer_copy_button_exists(d):
                seen_copy += 1
                log(f"检测到「复制」按钮，第 {seen_copy}/2 次")
                if seen_copy >= 2:
                    log("✓ 确认回答完成：已检测到「复制」按钮")
                    # 确认完成以后再强制一次到底部
                    click_jump_to_bottom(d)
                    time.sleep(0.8)
                    try:
                        return clean_text(best_answer_text(dump_xml(d), question))
                    except Exception:
                        return ""
            else:
                seen_copy = 0
                # 不在底部时会出现 JumpToBottom 悬浮按钮 → 点它固定在底部。
                # 每 1.5 秒最多点一次，避免疯狂点击。
                now = time.time()
                if now - last_bottom_click >= 1.5:
                    if click_jump_to_bottom(d):
                        last_bottom_click = now
            time.sleep(POLL_INTERVAL)
        except Exception as exc:
            LOG.debug("回答状态检测异常：%s", exc)
            time.sleep(POLL_INTERVAL_SLOW)

    raise RuntimeError(
        f"等待回答超时：{ANSWER_HARD_TIMEOUT}s 内未检测到「复制」按钮"
    )


# ============================================================
# 思考过程：展开 / 采集 / 收起
# ============================================================

def _thinking_title_nodes(nodes: list) -> list:
    """找出所有「思考已完成 / 思考中」标题节点（按 y 升序）。"""
    out = []
    for n in nodes:
        label = n["text"] or n["desc"]
        if not label:
            continue
        if not re.search(KIMI_THINKING_TITLE_PATTERN, label):
            continue
        if (n["bounds"][3] - n["bounds"][1]) > 160:   # 排除误匹配的大段正文
            continue
        out.append(n)
    out.sort(key=lambda x: x["bounds"][1])
    return out


def _thinking_body_nodes(nodes: list, title_y: int, viewport: tuple) -> list:
    """收集标题下方、与消息视窗有交叠的「缩进正文」节点（= 思考正文）。"""
    body = []
    for n in nodes:
        text = n["text"]
        if not text:
            continue
        b = n["bounds"]
        if not _overlaps_viewport(b, viewport):
            continue
        # 思考正文必须位于标题下方（标题可能与内容同屏，也可能已滚出屏幕）
        if b[3] <= max(title_y, viewport[1]):
            continue
        if b[0] < KIMI_THINK_BODY_X1_MIN:      # 非缩进 → 属于回答正文
            continue
        cleaned = _clean_special_chars(text)
        if not cleaned or len(cleaned) < 2:
            continue
        if cleaned in IGNORE_EXACT:
            continue
        if re.search(KIMI_THINKING_TITLE_PATTERN, cleaned):
            continue
        if re.search(KIMI_TOOL_HINT_PATTERN, cleaned):
            continue
        body.append((b[1], cleaned))
    body.sort(key=lambda x: x[0])
    return body


def _thinking_panel_expanded(d, viewport: tuple) -> bool:
    """思考面板是否已展开：标题下方已出现缩进正文节点。"""
    nodes = parse_nodes(dump_xml(d))
    titles = _thinking_title_nodes(nodes)
    if not titles:
        return False
    return bool(_thinking_body_nodes(nodes, titles[-1]["bounds"][1], viewport))


def toggle_thinking_panel(d, viewport: tuple) -> bool:
    """点击「思考已完成」标题以切换思考面板展开/收起；返回操作后是否处于展开态。"""
    nodes = parse_nodes(dump_xml(d))
    titles = _thinking_title_nodes(nodes)
    if not titles:
        log("未找到「思考已完成」标题行")
        return False
    title = titles[-1]          # 取最后一条（即当前问题的回答）
    click_node(d, nodes, title)
    time.sleep(1.8)
    return _thinking_panel_expanded(d, viewport)


def expand_thinking_panel(d, viewport: tuple) -> bool:
    """确保思考面板处于展开态（点一次不行就再点一次，兼容默认已展开的情况）。"""
    log("→ 尝试展开思考过程")
    if _thinking_panel_expanded(d, viewport):
        return True
    if toggle_thinking_panel(d, viewport):
        return True
    # 有些机型默认已展开，第一次点击把它收起了 → 再点一次展开
    return toggle_thinking_panel(d, viewport)


def collapse_thinking_panel(d, viewport: tuple) -> bool:
    """把已展开的思考面板收起（让主截图更干净）；失败不影响主流程。"""
    try:
        if _thinking_panel_expanded(d, viewport):
            if toggle_thinking_panel(d, viewport):
                # 切换后仍展开 → 再点一次
                toggle_thinking_panel(d, viewport)
            log("思考面板已收起")
        return True
    except Exception as exc:
        LOG.debug("收起思考面板失败：%s", exc)
        return False


def collect_thinking_content(d, viewport: tuple, max_swipes: int = 40) -> list:
    """采集展开后的思考正文（缩进节点），尽量采全。

    收尾判据：已在思考区采到内容后，一旦屏幕出现 **左对齐的回答正文**，
    说明思考区已走完（布局顺序固定为「缩进思考 → 左对齐正文」），立即停止。
    """
    seen: Set[str] = set()
    collected: list = []
    idle_rounds = 0
    titles = _thinking_title_nodes(parse_nodes(dump_xml(d)))
    title_y = titles[-1]["bounds"][1] if titles else viewport[1]

    for step in range(max_swipes):
        nodes = parse_nodes(dump_xml(d))
        round_items = _thinking_body_nodes(nodes, title_y, viewport)

        # 结构收尾信号：出现左对齐的回答正文（且已采到思考内容）
        answer_seen = False
        if collected:
            for n in nodes:
                text = n["text"]
                if not text:
                    continue
                b = n["bounds"]
                if not _overlaps_viewport(b, viewport):
                    continue
                if b[0] <= KIMI_ANSWER_X1_MAX and not _is_noise_text(text, ""):
                    answer_seen = True
                    break

        fresh = [(y, t) for (y, t) in round_items if t not in seen]
        for _, t in fresh:
            seen.add(t)
        if fresh:
            fresh.sort(key=lambda x: x[0])
            collected.extend(t for _, t in fresh)
            idle_rounds = 0
        elif step > 0:
            idle_rounds += 1

        if answer_seen:
            LOG.debug("检测到左对齐正文，思考过程采集结束（第 %d 轮）", step + 1)
            break
        if idle_rounds >= 3:
            break

        _swipe_in_viewport(d, "up", fraction=0.35, duration=0.8, viewport=viewport)
        time.sleep(1.2)

    return _filter_substring_dupes(collected)


def expand_and_collect_thinking(d, viewport: tuple) -> str:
    """展开思考面板并采集思考正文，返回纯文本（失败返回空串，不阻断主流程）。"""
    log("=" * 50)
    log("开始采集思考过程内容")

    if not expand_thinking_panel(d, viewport):
        log("✗ 无法展开思考过程，返回空")
        return ""

    time.sleep(2.0)
    paras = collect_thinking_content(d, viewport)

    if not paras:
        log("未采集到思考文本，重试一次…")
        time.sleep(1.0)
        paras = collect_thinking_content(d, viewport)

    thinking_text = clean_text(_join_texts(paras)) if paras else ""
    if thinking_text:
        log(f"✓ 思考过程采集完成：{len(thinking_text)} 字符，{len(paras)} 段")
    else:
        log("✗ 未采集到思考过程内容")
    log("=" * 50)
    return thinking_text


# ============================================================
# 主问答长截图 + 同步文本采集
# ============================================================

def screenshot_main_with_text(d, output: Path, question: str,
                              exclude_texts: Optional[Set[str]] = None) -> str:
    """从顶部到底部逐屏截图并拼接成长图，同时采集回答正文，返回正文文本。

    参照 doubao_auto 的长截图方案：
      1. 先滚到顶部，且做「最终复核」——再向上滑一次画面不动才开始截图，
         绝不允许顶部没确认就开始；
      2. 从顶部向下慢速小步滚动（CAPTURE_FRACTION 步长 + 1.2s 慢滑 + 2.0s 稳定），
         保证相邻帧有大量重叠，拼接准确；
      3. 到底判定不用「画面不动」，而是**「复制」按钮出现**——
         滚到顶部后回答末尾在屏幕外（没有复制按钮），向下滚动直到
         回答操作条重新进入屏幕，即说明已到达本条回答末尾；
         含复制按钮的这一帧会先被采集，然后再停止，回答尾部绝不缺漏；
      4. 每屏同步采集左对齐回答正文，一次遍历同时完成截图与文本采集。
    """
    log("开始主问答长截图（顶部硬确认 + 复制按钮到底判定）")
    exclude_texts = exclude_texts or set()

    if not scroll_to_top(d):
        raise RuntimeError("无法确认会话顶部")
    time.sleep(1.0)

    viewport = get_message_viewport(d, reserve_bottom=True)
    log(f"消息视窗（不含输入框）：{viewport}")

    # ---- 最终顶部复核：再向上滑一次，画面不动才允许第一帧截图（防惯性假顶）----
    try:
        before = save_message_area(d, OUTPUT_DIR / "_top_final_before.png", viewport)
        _swipe_in_viewport(d, "down", fraction=0.35, duration=0.5, viewport=viewport)
        time.sleep(0.8)
        after = save_message_area(d, OUTPUT_DIR / "_top_final_after.png", viewport)
        if not _sig_similar(image_signature(before), image_signature(after)):
            log("顶部复核发现仍可向上移动，重新寻找真正顶部")
            if not scroll_to_top(d):
                raise RuntimeError("顶部复核失败：无法确认会话顶部")
            time.sleep(1.0)
    except RuntimeError:
        raise
    except Exception as exc:
        LOG.debug("顶部复核异常（继续执行）：%s", exc)

    log("========== 顶部已确认，开始逐屏采集 ==========")

    frames: list = []
    seen_hashes: set = set()
    screens_texts: list = []

    for i in range(MAX_CAPTURE_FRAMES):
        # ===== 1. 先截当前屏，与上一帧不同才加入 =====
        full_img = _grab_full_screen()
        current = save_message_area(d, OUTPUT_DIR / f"_frame_{i:03d}.png",
                                    viewport, full=full_img)
        if not frames:
            frames.append(current.copy())
            log("已采集第 1 屏（顶部第一屏）")
        elif not _sig_similar(image_signature(current),
                              image_signature(frames[-1])):
            frames.append(current.copy())
            log(f"已采集第 {len(frames)} 屏")

        # ===== 2. 同步采集当前屏回答正文 =====
        items = _collect_screen_answer_texts(
            d, viewport, question, seen_hashes, exclude_texts=exclude_texts,
        )
        if items:
            screens_texts.append(items)

        # ===== 3. 复制按钮探测：出现 = 本条回答末尾已进入屏幕 =====
        # 含复制按钮的当前帧已被采集（步骤 1），此时停止，回答尾部完整保留。
        if answer_copy_button_exists(d):
            log("检测到「复制」按钮 → 已到达回答末尾，停止滚动")
            break

        # ===== 4. 向下慢速小步滚动（大量重叠保证拼接准确）=====
        _swipe_in_viewport(d, "up", fraction=CAPTURE_FRACTION,
                           duration=CAPTURE_SWIPE_DURATION, viewport=viewport)
        time.sleep(CAPTURE_SETTLE_WAIT)

    if not frames:
        raise RuntimeError("没有采集到会话截图")

    log(f"共采集 {len(frames)} 屏，开始拼接")
    stitch_frames(frames, output)
    log(f"主问答长截图完成：{output}")

    ordered = [t for items in screens_texts for _, t in items]
    filtered = _filter_substring_dupes(ordered)
    log(f"正文条目：原始 {len(ordered)} → 过滤后 {len(filtered)}")
    return _join_texts(filtered)


# ============================================================
# HTML 转换
# ============================================================

def html_inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


def text_to_html(text: str) -> str:
    """把纯文本转成简单 HTML（支持段落 / 无序列表 / 有序列表 / 行内加粗与代码）。"""
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
# 临时文件清理
# ============================================================

def cleanup_temp_files() -> None:
    for pattern in ("_top_probe.png", "_bottom_probe.png",
                    "_top_final_before.png", "_top_final_after.png",
                    "_kimi_shot.png"):
        try:
            (OUTPUT_DIR / pattern).unlink(missing_ok=True)
        except Exception:
            pass
    for path in list(OUTPUT_DIR.glob("_frame_*.png")):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Worker：单个任务单元
# ============================================================

def capture_one_answer(d, question: str, task_dir: Path) -> tuple:
    """单个问题从「新建对话」到「长截图落盘」的完整采集，返回 (answer_text, thinking_text)。

    抽出来单独一个函数，好处有二：
      1. run.py --once 本地冒烟可以直接复用（不触发上传/回调）；
      2. handle_unit 只负责「采集 + 上传 + 回调」，职责更清晰。
    """
    # 1. 新建对话（保证会话为空，避免上下文干扰与串题）
    # ⭐ 允许 new_chat 返回 False 时继续执行 —— 因为点击匹配图标本身已经成功
    #    （score >= threshold 且 d.click 已发出），只是后续"验证是否进入空白新会话"
    #    那步 XML 条件太严没通过。如果界面真没进新对话，后续 input/send 会再失败，
    #    到时 run.py 的 consecutive_failures 会统一处理；但如果只是验证条件误判，
    #    实际已进新对话 → 这一行警告后继续执行就是对的。
    if not new_chat(d):
        log("⚠️ new_chat 验证未通过，但视觉点击已执行，继续执行（后续步骤会再兜底）")

    # 2. 输入问题
    input_text(d, question)

    # 3. 发送问题
    if not send_message(d):
        raise RuntimeError("问题发送失败")

    # 4. 监测「思考 + 回答」完毕
    coarse_answer = wait_answer_finish(d, question)
    log(f"回答已结束，粗判正文 {len(coarse_answer)} 字符")

    # 5. 采集思考过程（先采思考，得到 exclude 集合，避免污染回答正文）
    thinking_text = ""
    try:
        viewport = get_message_viewport(d, reserve_bottom=True)
        scroll_to_top(d, viewport)
        time.sleep(1.0)
        thinking_text = expand_and_collect_thinking(d, viewport)
    except Exception as exc:
        LOG.warning("采集思考过程失败（继续执行）：%s", exc)

    # 6. 收起思考面板（主截图更干净）→ 回到底部
    try:
        viewport = get_message_viewport(d, reserve_bottom=True)
        collapse_thinking_panel(d, viewport)
    except Exception as exc:
        LOG.debug("收起思考面板忽略：%s", exc)
    scroll_to_bottom(d)
    time.sleep(0.8)

    # 7. 主问答长截图 + 回答正文采集
    exclude_texts: Set[str] = set()
    if thinking_text:
        for para in thinking_text.split("\n\n"):
            p = para.strip()
            if p and len(p) >= 4:
                exclude_texts.add(p)

    main_path = task_dir / "answer_main.png"
    answer_text = screenshot_main_with_text(
        d, main_path, question, exclude_texts=exclude_texts,
    )

    if len(clean_text(answer_text)) < 20:
        log(f"未采集到有效正文，回退粗判文本（{len(coarse_answer)} 字符）")
        answer_text = coarse_answer

    answer_text = clean_text(answer_text)
    thinking_text = clean_text(thinking_text)

    # ---- 最终防线：正文与思考过程段落级去重 ----
    if thinking_text and answer_text:
        think_paras = [p.strip() for p in thinking_text.split("\n\n") if len(p.strip()) >= 6]
        if think_paras:
            kept, dropped = [], 0
            for para in answer_text.split("\n\n"):
                ps = para.strip()
                contaminated = any(
                    ps == tp or (len(ps) >= 6 and ps in tp) or (len(tp) >= 10 and tp in ps)
                    for tp in think_paras
                )
                if contaminated:
                    dropped += 1
                    continue
                kept.append(para)
            if dropped:
                log(f"最终防线：从正文剔除 {dropped} 段与思考重复的内容")
                answer_text = "\n\n".join(kept)

    if len(answer_text) < ANSWER_MIN_COMPLETE_LENGTH // 2:
        raise RuntimeError(f"回答文本过短（{len(answer_text)} 字符）")

    log(f"✓ 完整正文回答：{len(answer_text)} 字符")
    log(f"✓ 思考过程：{len(thinking_text)} 字符")

    # 8. 生成 HTML 与中间产物
    answer_html = text_to_html(answer_text)
    thinking_html = text_to_html(thinking_text)
    if not answer_html:
        raise RuntimeError("没有生成有效 HTML")

    (task_dir / "answer.html").write_text(answer_html, encoding="utf-8")
    (task_dir / "answer_plain.txt").write_text(answer_text, encoding="utf-8")
    (task_dir / "thinking.html").write_text(thinking_html, encoding="utf-8")
    (task_dir / "thinking_plain.txt").write_text(thinking_text, encoding="utf-8")
    (task_dir / "question.txt").write_text(question, encoding="utf-8")
    return answer_text, thinking_text


def handle_unit(d, unit: dict) -> None:
    """执行一个任务单元：采集（capture_one_answer）→ 上传长截图 → 回调结果。"""
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

    answer_text, thinking_text = capture_one_answer(d, question, task_dir)

    # 9. 上传截图 + 回调结果
    if not _HAS_WORKER_LIB:
        raise RuntimeError("任务池模式需要 worker_lib.py")

    main_path = task_dir / "answer_main.png"
    log("上传长截图")
    image_url = worker_upload(str(main_path), task_id=unit_id)
    if not image_url:
        raise RuntimeError("截图上传失败")

    log("回调任务结果")
    ok = worker_callback(
        unit,
        status="SUCCESS",
        answer_text=text_to_html(answer_text),
        thinking_content=text_to_html(thinking_text),
        source_info="",
        image_url=image_url,
        error_msg=None,
    )
    if not ok:
        raise RuntimeError("结果 callback 失败")

    log(f"✓ 任务完成：{task_no or unit_id}")


def run_worker(d) -> None:
    if not _HAS_WORKER_LIB or run_worker_loop is None:
        raise RuntimeError("没有找到 worker_lib.py")
    log(f"启动任务池 Worker：platform={PLATFORM_NAME}")
    run_worker_loop(PLATFORM_NAME, d, handle_unit)


# ============================================================
# 诊断 / CLI
# ============================================================

def diagnose(d) -> None:
    """把当前界面的关键信息 dump 出来，便于排查 UI 变化。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    diag_dir = OUTPUT_DIR / "kimi_diagnose" / stamp
    diag_dir.mkdir(parents=True, exist_ok=True)

    try:
        log(f"当前前台：{d.app_current()}")
    except Exception as exc:
        LOG.debug("读前台失败：%s", exc)
    w, h = get_screen_size(d)
    log(f"屏幕：{w}x{h}")
    vp = get_message_viewport(d)
    log(f"消息视窗（含底部保留带）：{vp}")
    vp_full = get_message_viewport(d, reserve_bottom=False)
    log(f"消息视窗（到输入框上方）：{vp_full}")
    dump_xml(d, diag_dir / "window.xml")
    save_screen(diag_dir / "screen.png")
    log(f"诊断完成：{diag_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kimi Android Worker")
    parser.add_argument("--diagnose", action="store_true", help="只做界面诊断")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log("=" * 70)
    log("Kimi Android Worker")
    log(f"package={kimi_package()}   platform={PLATFORM_NAME}")
    log("=" * 70)

    d = None
    try:
        d = connect_device()
        start_kimi_app(d)
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
                force_reset(d)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
