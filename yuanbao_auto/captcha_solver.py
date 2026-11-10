# -*- coding: utf-8 -*-
"""
captcha_solver.py —— 腾讯元宝登录人机验证破解器（项目核心难点）
================================================================

场景（对应用户标注的人机验证截图）：
  登录过程中会弹出一个全屏人机验证：
    - 顶部蓝色横幅：标题「选择最符合描述的图片」+ 一句图片描述（如「一束白玫瑰」）
    - 中间是 3x3 九宫格真实照片
    - 底部一行：? 图标、「换一批」刷新按钮、蓝色「确定」按钮
  只有点中符合描述的图片并点「确定」，验证才会通过、登录流程才会继续；
  点错或超时后题目会随机换新，需要重新分析、重新点选，直到通过为止。

求解思路（AI 大模型驱动，全自动重试）：
  1.【弹窗检测】XML 匹配标题/换一批等标记词；文字不进 XML（WebView）时
     用 OpenCV 兜底检测居中弹窗。
  2.【描述获取】优先从 XML 提取引号里的描述（「一束白玫瑰」）；
     提取不到则把整块验证区域发给千问 VL 让它自己读描述。
  3.【九宫格定位】三级策略：XML 九个 ImageView 取并集 →
     XML 最大图片节点 → 比例兜底。
  4.【目标选择】把九宫格裁剪图发给千问 VL，让它返回最符合描述的格子编号
     （1~9，从左到右、从上到下），按九宫格几何中心换算点击坐标。
     —— 格子编号比裸坐标稳得多，不受模型 grounding 误差影响。
  5.【点击 + 确定】点中目标格子 → 点「确定」（XML 优先 / 比例兜底）。
  6.【结果判定】验证框消失 = 通过；仍在 = 已换新题，回到第 2 步，
     最多 CAPTCHA_MAX_ATTEMPTS 轮，全程无需人工干预。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from io import BytesIO
from typing import Optional, Tuple

import requests
from PIL import Image, ImageDraw

try:
    import cv2
    import numpy as np
    _HAS_CV = True
except ImportError:
    cv2 = None
    np = None
    _HAS_CV = False

from config import (
    QWEN_API_KEY,
    QWEN_BASE_URL,
    QWEN_MODEL,
    QWEN_TIMEOUT,
    QWEN_MAX_RETRIES,
    CAPTCHA_MAX_ATTEMPTS,
    CAPTCHA_CLICK_WAIT,
    CAPTCHA_DEBUG_KEEP,
    SCREENSHOT_DIR,
)

logger = logging.getLogger(__name__)

# 调试截图目录（保留验证过程证据，方便事后复盘点错原因）
DEBUG_DIR = SCREENSHOT_DIR / "captcha_debug"
if CAPTCHA_DEBUG_KEEP:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# =====================================================================
#  常量：元宝验证弹窗的特征
# =====================================================================

# 验证标题（蓝色横幅第一行）
CAPTCHA_TITLE = "选择最符合描述的图片"

# 验证弹窗的辅助标记词（标题不进 XML 时的第二信号）
CAPTCHA_MARKERS = (
    CAPTCHA_TITLE, "换一批", "人机验证", "安全验证",
    "完成验证", "行为验证", "混元AI生成",
)

# 描述文字通常包在引号里：'一束白玫瑰'
DESCRIPTION_QUOTE_RE = re.compile(r"[“\"「『‘']([^”\"」』’']{2,40})[”\"」』’']")

# 确定按钮文案（注意：不要匹配「不同意」之类，元宝这里是精确「确定」）
CONFIRM_WORD = "确定"
REFRESH_WORDS = ("换一批",)

# 点错时可能出现的提示词（用于日志区分「刷新新题」还是「答错重试」）
FAIL_HINT_WORDS = ("答案错误", "验证失败", "请重试", "不正确", "再试一次", "错误")


# =====================================================================
#  基础工具
# =====================================================================

def _adb_screenshot() -> Image.Image:
    """adb 截整屏，返回 PIL.Image（RGB）。"""
    # 延迟导入，避免与 yuanbao_login 循环依赖；确保带上 -s 序列号
    from yuanbao_login import get_device_serial, run_adb
    get_device_serial()  # 确保 _device_serial 已缓存，run_adb 自动加 -s
    result = run_adb("exec-out", "screencap", "-p", timeout=30)
    if result.returncode != 0:
        raise RuntimeError("adb screencap 失败：" +
                           result.stderr.decode("utf-8", errors="ignore"))
    return Image.open(BytesIO(result.stdout)).convert("RGB")


def _dump_xml(d) -> str:
    try:
        return d.dump_hierarchy(compressed=False)
    except Exception:
        return ""


def _parse_nodes(xml: str) -> list:
    """把 XML 里的 node 解析成 dict 列表（只保留本模块需要的字段）。"""
    import xml.etree.ElementTree as ET
    nodes = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return nodes
    for node in root.iter("node"):
        nodes.append({
            "text": (node.attrib.get("text") or "").strip(),
            "desc": (node.attrib.get("content-desc") or "").strip(),
            "rid": node.attrib.get("resource-id", ""),
            "class": node.attrib.get("class", ""),
            "bounds": node.attrib.get("bounds", ""),
        })
    return nodes


def _bounds_to_tuple(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return tuple(map(int, m.groups()))


def _save_debug_dump(d, tag: str, screenshot: Optional[Image.Image] = None) -> None:
    """留存现场：整屏截图 + UI XML。检测异常时调用，文件发开发者可精确定位。"""
    if not CAPTCHA_DEBUG_KEEP:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img = screenshot or _adb_screenshot()
        img.save(DEBUG_DIR / f"{ts}_{tag}_screen.png")
        xml = _dump_xml(d)
        (DEBUG_DIR / f"{ts}_{tag}_hierarchy.xml").write_text(xml, encoding="utf-8")
        logger.info(f"[captcha] 现场已留存: {DEBUG_DIR}\\{ts}_{tag}_*")
    except Exception as exc:
        logger.debug(f"[captcha] 留存现场失败: {exc}")


# =====================================================================
#  第一步：验证弹窗检测 + 描述文字提取
# =====================================================================

def get_captcha_description_from_xml(xml: str) -> Optional[str]:
    """从 XML 里提取验证描述（如「一束白玫瑰」）。找不到返回 None。

    提取优先级：
      1. 任意节点 text/desc 里引号包住的内容
      2. 标题节点（选择最符合描述的图片）之后、面积较大的独立文字节点
    """
    if not xml:
        return None
    nodes = _parse_nodes(xml)

    # 1) 引号描述
    for node in nodes:
        for value in (node["text"], node["desc"]):
            if not value or CAPTCHA_TITLE in value:
                continue
            m = DESCRIPTION_QUOTE_RE.search(value)
            if m:
                return m.group(1).strip()

    # 2) 标题附近的大号文字节点（描述在标题正下方，字号更大）
    title_y = None
    for node in nodes:
        value = node["text"] or node["desc"]
        if value and CAPTCHA_TITLE in value:
            b = _bounds_to_tuple(node["bounds"])
            if b:
                title_y = b[3]
            break
    if title_y is not None:
        best, best_len = None, 0
        for node in nodes:
            value = (node["text"] or node["desc"]).strip()
            if not value or CAPTCHA_TITLE in value:
                continue
            if value in (CONFIRM_WORD, "换一批") or len(value) > 40:
                continue
            b = _bounds_to_tuple(node["bounds"])
            if not b:
                continue
            # 描述必须紧挨标题下方（间距 < 屏高 15%）且和标题横向重叠
            if not (title_y <= b[1] <= title_y + 260):
                continue
            if len(value) > best_len:
                best_len, best = len(value), value
        if best:
            return best
    return None


def _find_dialog_rect_cv(img: "np.ndarray") -> Optional[Tuple[int, int, int, int]]:
    """OpenCV 找居中的大块「验证覆盖层」。

    元宝验证是接近整屏的浅色覆盖层，中间夹着彩色九宫格：
    思路改为找「边缘密度高的居中矩形」（九宫格照片边缘远多于背景）。
    防误判约束：面积 5%~85%、宽高不超过屏幕 95%、中心在中部区域。
    """
    if not _HAS_CV:
        return None
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best, best_area = None, 0
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if not (w * h * 0.05 <= area <= w * h * 0.85):
            continue
        if cw > w * 0.95 or ch > h * 0.95:
            continue
        cx, cy = x + cw // 2, y + ch // 2
        if not (w * 0.10 < cx < w * 0.90 and h * 0.10 < cy < h * 0.90):
            continue
        if area > best_area:
            best_area, best = area, (x, y, x + cw, y + ch)
    return best


def detect_captcha(d, xml: Optional[str] = None,
                   screenshot: Optional[Image.Image] = None
                   ) -> Tuple[bool, Optional[str], Optional[Image.Image]]:
    """多信号检测验证弹窗是否在屏上。

    :return: (是否在, 描述文字或None, 截图或None)
      - 描述文字为 None 不代表没弹窗（可能文字没进 XML），仅表示需要大模型读描述
    """
    if xml is None:
        xml = _dump_xml(d)

    # 信号 a：标题 / 标记词 / 引号描述（任一出现即认为验证在屏）
    description = get_captcha_description_from_xml(xml)
    if description:
        return True, description, screenshot
    if any(m in xml for m in CAPTCHA_MARKERS):
        return True, None, screenshot

    # 信号 b：OpenCV 视觉兜底（WebView 场景 XML 全空）
    if _HAS_CV:
        try:
            if screenshot is None:
                screenshot = _adb_screenshot()
            if _find_dialog_rect_cv(np.array(screenshot)):
                return True, None, screenshot
        except Exception as exc:
            logger.debug(f"[captcha] OpenCV 弹窗检测异常: {exc}")

    return False, None, screenshot


def is_captcha_present(d) -> bool:
    """判断人机验证当前是否还显示着。"""
    present, _, _ = detect_captcha(d)
    return present


# =====================================================================
#  第二步：九宫格区域定位（XML → 比例兜底）
# =====================================================================

def _locate_grid_by_xml(nodes: list, screen_w: int, screen_h: int
                        ) -> Tuple[Optional[Tuple[int, int, int, int]], int]:
    """策略 a：XML 里找九宫格。

    返回 (网格并集矩形, 格子数)。判定条件：
      - ImageView / Image 类节点，宽 >= 屏宽 10%、高 >= 屏高 8%
      - 收集到的所有候选按行聚类：能凑出 >= 9 个（或 6/12 宫格也兼容）取并集
    """
    cells = []
    for node in nodes:
        cls = node["class"].lower()
        if "imageview" not in cls and "image" not in cls:
            continue
        b = _bounds_to_tuple(node["bounds"])
        if not b:
            continue
        x1, y1, x2, y2 = b
        w, h = x2 - x1, y2 - y1
        if w < screen_w * 0.10 or h < screen_h * 0.08:
            continue
        if w > screen_w * 0.75 or h > screen_h * 0.55:
            continue
        cells.append(b)

    if len(cells) >= 9:
        # 按中心 y 聚成 3 行、行内按 x 排序，取前 9 个（适配 9 宫格）
        cells.sort(key=lambda b: (b[1], b[0]))
        grid = (
            min(b[0] for b in cells[:9]), min(b[1] for b in cells[:9]),
            max(b[2] for b in cells[:9]), max(b[3] for b in cells[:9]),
        )
        return grid, len(cells)
    if cells:
        # 只找到零散图片节点（可能是整块拼图）：取面积最大的
        best = max(cells, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        return best, len(cells)
    return None, 0


def _fallback_grid_rect(screen_w: int, screen_h: int,
                        dialog: Optional[Tuple[int, int, int, int]]
                        ) -> Tuple[int, int, int, int]:
    """策略 b：比例兜底。

    来自真实验证截图（576x1024 竖屏）的相对位置：
      九宫格约 x[155,408] y[187,378] → 相对屏幕 x[0.27,0.71] y[0.18,0.37]
    有弹窗矩形时按弹窗内比例缩放；没有时直接用屏幕中央区域。
    """
    if dialog:
        dx1, dy1, dx2, dy2 = dialog
        dw, dh = dx2 - dx1, dy2 - dy1
        return (
            int(dx1 + dw * 0.16), int(dy1 + dh * 0.10),
            int(dx2 - dw * 0.16), int(dy1 + dh * 0.55),
        )
    return (
        int(screen_w * 0.24), int(screen_h * 0.16),
        int(screen_w * 0.76), int(screen_h * 0.40),
    )


def locate_grid_rect(
    d, screenshot: Image.Image, xml: str,
) -> Tuple[Tuple[int, int, int, int], Optional[Tuple[int, int, int, int]]]:
    """两级策略定位九宫格区域。

    :return: (九宫格全屏坐标, 弹窗坐标或None) —— 弹窗坐标留给「大模型读描述」裁剪用
    """
    sw, sh = screenshot.size
    nodes = _parse_nodes(xml)

    # 策略 a：XML
    rect, cell_cnt = _locate_grid_by_xml(nodes, sw, sh)
    if rect:
        logger.info(f"[captcha] 策略a(XML) 命中九宫格区域（{cell_cnt} 个图块）: {rect}")
        return rect, None

    # 策略 b：OpenCV 检出弹窗 → 弹窗内比例
    dialog = None
    if _HAS_CV:
        try:
            img_np = np.array(screenshot)
            dialog = _find_dialog_rect_cv(img_np)
            if dialog:
                logger.info(f"[captcha] OpenCV 检出验证覆盖层: {dialog}")
        except Exception as exc:
            logger.debug(f"[captcha] OpenCV 弹窗检测异常: {exc}")

    # 比例兜底
    rect = _fallback_grid_rect(sw, sh, dialog)
    logger.warning(f"[captcha] 比例兜底 九宫格区域: {rect}")
    return rect, dialog


# =====================================================================
#  第三步：千问视觉大模型选格子
# =====================================================================

def _img_to_b64(img: Image.Image) -> str:
    """PIL.Image → base64（JPEG，控制体积加快上传）。"""
    buf = BytesIO()
    max_side = 960
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.LANCZOS)
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ask_qwen_for_cell(captcha_img: Image.Image,
                      description: Optional[str]) -> Optional[dict]:
    """把九宫格图发给千问 VL，返回 {"cell": 1~9}（可带模型读到的 desc）。

    - description 已知：直接让模型按编号选格子；
    - description 未知（XML 没读到）：让模型先读蓝色横幅里的描述再选格子，
      此时传入的图应包含描述横幅区域（上层负责把裁剪范围往上扩）。
    解析失败 / 请求失败返回 None（上层重试）。
    """
    if not QWEN_API_KEY:
        logger.error("[captcha] 未配置 QWEN_API_KEY（.env 或环境变量），无法求解")
        return None

    b64 = _img_to_b64(captcha_img)

    if description:
        user_text = (
            "这是一个人机验证的九宫格选图题。任务：找出最符合描述的一张照片。\n"
            f"验证描述：「{description}」\n"
            "九宫格编号规则：从左到右、从上到下依次为 1、2、3、4、5、6、7、8、9。\n"
            "请仔细比对每张照片与描述的匹配程度（注意颜色、物品、场景等细节），"
            "选出唯一最符合的一张。\n"
            "只输出一个 JSON，格式 {\"cell\": 数字}，数字取值 1~9。"
            "不要输出任何其他文字。"
        )
    else:
        user_text = (
            "这是一个人机验证截图。顶部蓝色横幅里有一行图片描述"
            "（通常带引号，例如「一束白玫瑰」），下方是九宫格照片。\n"
            "请你：1）读出横幅里的描述文字；2）在九宫格中找出最符合该描述的一张。\n"
            "九宫格编号规则：从左到右、从上到下依次为 1~9。\n"
            "只输出一个 JSON，格式 {\"desc\": \"描述文字\", \"cell\": 数字}。"
            "不要输出任何其他文字。"
        )

    payload = {
        "model": QWEN_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": user_text},
            ],
        }],
        "temperature": 0.0,   # 选择题不要随机性
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json",
    }
    url = QWEN_BASE_URL.rstrip("/") + "/chat/completions"

    content = ""
    for attempt in range(1, QWEN_MAX_RETRIES + 2):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=QWEN_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[captcha] 千问 HTTP {resp.status_code}: "
                               f"{resp.text[:200]}（第 {attempt} 次）")
                continue
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            break
        except Exception as exc:
            logger.warning(f"[captcha] 千问请求异常（第 {attempt} 次）: {exc}")
    if not content:
        return None

    logger.info(f"[captcha] 千问原始返回: {content[:200]}")
    return _parse_cell_from_reply(content)


def _parse_cell_from_reply(content: str) -> Optional[dict]:
    """从模型回复里解析格子编号，兼容多种输出格式：
      1) {"cell": 5} / {"desc":"...","cell":5}     （我们要求的标准 JSON）
      2) 第 5 张 / 格子5 / cell=5 / 编号 5          （模型自由发挥）
      3) (x, y) 坐标对                              （模型改报坐标时兜底）
    返回 {"cell": 1~9} 或 None。
    """
    # 格式 1：JSON 里的 "cell"
    m = re.search(r'"cell"\s*[:：=]\s*([1-9])', content)
    if m:
        result = {"cell": int(m.group(1))}
        md = re.search(r'"desc"\s*[:：]\s*"([^"]{1,40})"', content)
        if md:
            result["desc"] = md.group(1)
        return result

    # 格式 2：中文自由发挥
    m = re.search(r'(?:第|格子|格|cell|编号)\s*([1-9])\s*(?:张|格)?', content)
    if m:
        return {"cell": int(m.group(1))}

    # 格式 3：模型只回了一个 1~9 的数字
    m = re.fullmatch(r"\s*[\"']?([1-9])[\"']?\s*", content)
    if m:
        return {"cell": int(m.group(1))}

    logger.warning(f"[captcha] 无法从回复解析格子编号: {content[:200]}")
    return None


def _cell_to_click_point(grid: Tuple[int, int, int, int], cell: int
                         ) -> Tuple[int, int]:
    """格子编号（1~9，从左到右从上到下）→ 九宫格内该格中心点全屏坐标。"""
    x1, y1, x2, y2 = grid
    gw, gh = (x2 - x1) / 3.0, (y2 - y1) / 3.0
    idx = max(1, min(9, int(cell))) - 1
    row, col = divmod(idx, 3)
    cx = int(x1 + col * gw + gw / 2)
    cy = int(y1 + row * gh + gh / 2)
    return cx, cy


# =====================================================================
#  辅助：点击「确定」/「换一批」
# =====================================================================

def _click_confirm(d, xml: str, grid: Tuple[int, int, int, int]) -> bool:
    """点「确定」按钮：XML 精确匹配 → 比例兜底（九宫格右下方）。"""
    # 策略 1：XML text（精确匹配，避免误点「不同意」之类相近文案）
    for kwargs in ({"text": CONFIRM_WORD},
                   {"description": CONFIRM_WORD}):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=1):
                obj.click()
                logger.info(f"[captcha] 已点击「{CONFIRM_WORD}」（{kwargs}）")
                return True
        except Exception:
            pass
    # 策略 2：比例兜底 —— 确定按钮在九宫格右下方（真实截图实测）
    x1, y1, x2, y2 = grid
    gw, gh = x2 - x1, y2 - y1
    cx = int(x1 + gw * 0.93)
    cy = int(y2 + gh * 0.10)
    logger.info(f"[captcha] 比例兜底点击「{CONFIRM_WORD}」 ({cx},{cy})")
    try:
        d.click(cx, cy)
        return True
    except Exception as exc:
        logger.error(f"[captcha] 点击「{CONFIRM_WORD}」失败: {exc}")
        return False


def _click_refresh(d, xml: str) -> bool:
    """点「换一批」换一道新题（大模型连续读不出答案时的自救）。"""
    for w in REFRESH_WORDS:
        for kwargs in ({"textContains": w}, {"descriptionContains": w}):
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=1):
                    obj.click()
                    logger.info(f"[captcha] 已点击「{w}」")
                    time.sleep(1.5)
                    return True
            except Exception:
                pass
    return False


# =====================================================================
#  调试截图（保留证据，方便复盘点错的题）
# =====================================================================

def _save_debug(full_img: Image.Image, rect: Tuple[int, int, int, int],
                click_xy: Optional[Tuple[int, int]], tag: str) -> None:
    if not CAPTCHA_DEBUG_KEEP:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        x1, y1, x2, y2 = rect
        full_img.crop((x1, y1, x2, y2)).save(DEBUG_DIR / f"{ts}_{tag}_crop.png")
        marked = full_img.copy()
        draw = ImageDraw.Draw(marked)
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=3)
        if click_xy:
            cx, cy = click_xy
            draw.ellipse([cx - 12, cy - 12, cx + 12, cy + 12],
                         outline=(0, 255, 0), width=4)
            draw.line([cx - 20, cy, cx + 20, cy], fill=(0, 255, 0), width=3)
            draw.line([cx, cy - 20, cx, cy + 20], fill=(0, 255, 0), width=3)
        marked.save(DEBUG_DIR / f"{ts}_{tag}_marked.png")
    except Exception as exc:
        logger.debug(f"[captcha] 保存调试截图失败: {exc}")


# =====================================================================
#  主入口：求解人机验证
# =====================================================================

def solve_captcha(d, max_attempts: int = CAPTCHA_MAX_ATTEMPTS) -> bool:
    """求解元宝「选择最符合描述的图片」人机验证。

    流程（最多 max_attempts 轮，点错自动换新题接着解，直到验证成功）：
      1. 多信号检测验证弹窗 → 尝试从 XML 读图片描述
      2. 截屏 → 定位九宫格区域 → 裁剪
         （XML 读不到描述时，裁剪范围往上扩纳入蓝色横幅，让大模型连描述一起读）
      3. 千问 VL 返回最符合描述的格子编号（1~9）→ 按九宫格几何换算点击坐标
      4. 点中目标格子 → 点「确定」
      5. 等待 CAPTCHA_CLICK_WAIT 秒 → 验证框消失则通过；
         大模型连续给不出编号时点「换一批」换题自救

    :return: True=验证通过（弹窗消失）；False=多次尝试仍未过
    """
    none_streak = 0   # 大模型连续给不出编号的次数
    for attempt in range(1, max_attempts + 1):
        xml = _dump_xml(d)
        present, description, screenshot = detect_captcha(d, xml=xml)

        # 验证框已经不在 = 已通过（也可能首次调用时本就没弹）
        if not present:
            logger.info(f"[captcha] 第 {attempt} 轮：未检测到验证弹窗，视为已通过")
            return True

        logger.info(f"[captcha] ===== 第 {attempt}/{max_attempts} 轮 =====")
        if description:
            logger.info(f"[captcha] 验证描述(XML): {description}")
        else:
            logger.info("[captcha] XML 未暴露描述文字，切换大模型读描述模式")

        # 大模型连续两轮没给编号 → 点「换一批」主动换题
        if none_streak >= 2:
            logger.warning("[captcha] 大模型连续给不出编号，点「换一批」换新题")
            if _click_refresh(d, xml):
                none_streak = 0
                time.sleep(1.5)
            continue

        # 截屏 + 定位九宫格
        try:
            full_img = screenshot or _adb_screenshot()
        except Exception as exc:
            logger.error(f"[captcha] 截屏失败: {exc}")
            time.sleep(1.5)
            continue
        grid, dialog = locate_grid_rect(d, full_img, xml)
        x1, y1, x2, y2 = grid
        if x2 - x1 < 90 or y2 - y1 < 90:
            logger.error(f"[captcha] 九宫格区域异常（过小）: {grid}")
            time.sleep(1.5)
            continue

        # 裁剪给大模型的图：
        #  - 有描述：只裁九宫格（选图更专注）
        #  - 无描述：裁剪范围往上扩 45% 格高、往下扩 25% 格高，
        #    纳入蓝色描述横幅与「确定」区域，让模型连描述一起读
        if description:
            send_rect = grid
        else:
            gh = y2 - y1
            send_rect = (x1, max(0, y1 - int(gh * 0.45)),
                         x2, min(full_img.height, y2 + int(gh * 0.25)))
        send_img = full_img.crop(send_rect)

        # 大模型选格子
        result = ask_qwen_for_cell(send_img, description)
        if result is None or "cell" not in result:
            logger.warning("[captcha] 大模型未能给出格子编号，本轮跳过")
            none_streak += 1
            _save_debug(full_img, send_rect, None, f"a{attempt}_nollm")
            time.sleep(1.5)
            continue

        cell = result["cell"]
        if result.get("desc") and not description:
            description = result["desc"]
            logger.info(f"[captcha] 大模型读到的描述: {description}")
        none_streak = 0

        # 格子编号 → 点击坐标
        click_x, click_y = _cell_to_click_point(grid, cell)
        logger.info(f"[captcha] 选中格子 {cell} → 屏幕 ({click_x},{click_y})")
        _save_debug(full_img, grid, (click_x, click_y), f"a{attempt}_click")

        # 点击目标格子
        try:
            d.click(click_x, click_y)
        except Exception as exc:
            logger.error(f"[captcha] 点击格子失败: {exc}")
            time.sleep(1.5)
            continue
        time.sleep(1.0)   # 等选中态出现（选中格会出现高亮描边）

        # 点「确定」提交
        _click_confirm(d, xml, grid)

        # 等结果 → 验证框是否消失
        time.sleep(CAPTCHA_CLICK_WAIT)
        xml_after = _dump_xml(d)
        still_present, new_desc, _ = detect_captcha(d, xml=xml_after)
        if not still_present:
            logger.info(f"[captcha] ✅ 第 {attempt} 轮点击后验证框消失，验证通过")
            return True

        # 还在：区分「答错刷新」并进入下一轮（新题会由下一轮重新调大模型分析）
        if any(w in xml_after for w in FAIL_HINT_WORDS):
            logger.warning(f"[captcha] 第 {attempt} 轮答案错误，已刷新新题: {new_desc}")
        else:
            logger.warning(f"[captcha] 第 {attempt} 轮未通过，当前描述: {new_desc}")

    logger.error(f"[captcha] ❌ 连续 {max_attempts} 轮未通过人机验证")
    _save_debug_dump(d, "giveup")
    return False


def wait_captcha_and_solve(d, appear_timeout: float = 12.0,
                           max_attempts: int = CAPTCHA_MAX_ATTEMPTS) -> bool:
    """等待人机验证出现并求解（元宝在提交验证码/登录后弹出）。

    有些情况下流程不发验证直接过（风控宽松时），
    判定「真没弹窗」的证据是超时且界面已离开验证状态。

    :return: True=弹窗未出现或已通过；False=弹窗出现但没过
    """
    deadline = time.time() + appear_timeout
    appeared = False
    last_screenshot = None
    while time.time() < deadline:
        xml = _dump_xml(d)
        present, description, shot = detect_captcha(d, xml=xml)
        if present:
            appeared = True
            last_screenshot = shot
            logger.info(f"[captcha] 检测到人机验证"
                        f"{('，描述: ' + description) if description else '（描述未入XML）'}")
            break
        time.sleep(0.7)

    if not appeared:
        logger.info("[captcha] 等待期内未出现人机验证，继续主流程")
        return True

    return solve_captcha(d, max_attempts=max_attempts)
