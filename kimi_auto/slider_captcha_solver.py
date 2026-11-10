# -*- coding: utf-8 -*-
"""
slider_captcha_solver.py —— Kimi 滑块拼图人机验证破解器
=========================================================

触发场景：
  多次登出/登录后，Kimi 在点「验证并登录」弹出滑块拼图弹窗：
    - 标题：「请完成安全验证」
    - 背景图上有灰色拼图缺口 + 同形拼图块贴在左下角
    - 底部蓝色滑块条，滑块在最左，要拖到缺口 x 位置对齐
    - 每次背景 / 缺口位置随机不同

解法（双引擎，AI 优先）：
  1.【弹窗检测】XML 匹配"安全验证"/"请完成"等标记词
  2.【区域裁剪】把弹窗从整屏里切出来 → 再切出背景图（拼图所在区域）
  3.【缺口定位】双引擎：
     a) AI 引擎（千问-VL，主引擎）
        把背景图发给千问 VL，让它返回缺口左上角 x / 背景图宽度，
        算出缺口相对位置 = 缺口x / 背景图宽
     b) OpenCV 引擎（兜底）
        用 Canny 边缘 + 轮廓匹配 / 或直接在缺口位置模板匹配
  4.【滑动执行】模拟真人轨迹（缓动 + 抖动），把蓝色滑块拖到目标 x
  5.【结果判定】验证框消失 = 通过；仍在 = 已换新题，回到第 2 步重来

代码组织参考 yuanbao_auto/captcha_solver.py（千问-VL 调用框架完全复用）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import random
import time
from io import BytesIO
from typing import Optional, Tuple

import requests
from PIL import Image

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
    SLIDE_CAPTCHA_MAX_ATTEMPTS,
    SLIDE_CAPTCHA_SWIPE_DURATION,
    SLIDE_CAPTCHA_AI_FIRST,
    CAPTCHA_DEBUG_KEEP,
    SCREENSHOT_DIR,
)

logger = logging.getLogger(__name__)

# 调试截图目录
DEBUG_DIR = SCREENSHOT_DIR / "captcha_debug"
if CAPTCHA_DEBUG_KEEP:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# =====================================================================
#  常量：滑块拼图弹窗特征
# =====================================================================
CAPTCHA_MARKERS = (
    "请完成安全验证", "安全验证", "完成安全验证", "滑动", "拼图",
    "拖动滑块", "人机验证", "行为验证",
)
REFRESH_WORDS = ("刷新", "换一批", "重试")   # 点错后可能出现的按钮词


# =====================================================================
#  基础工具
# =====================================================================

def _adb_screenshot() -> Image.Image:
    """adb exec-out screencap（二进制安全），返回 PIL.Image RGB。"""
    # 延迟导入，避免与 kimi_login 循环依赖；确保带上 -s 序列号
    from kimi_login import get_device_serial, run_adb
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


def _img_to_b64(img: Image.Image) -> str:
    """PIL.Image → JPEG base64（控制体积加快上传，最长边缩到 960px）。"""
    buf = BytesIO()
    max_side = 960
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.LANCZOS)
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# =====================================================================
#  第一步：弹窗检测
# =====================================================================

def is_slide_captcha_visible(d) -> bool:
    """滑块拼图弹窗是否正在屏幕上。

    判定依据：
      1. XML 里出现"安全验证" / "请完成安全验证" 等关键词
      2. XML 里出现"拖动" / "滑块" / "拼图" 等提示词
      3. 以上两者或（含 captcha 关键词 + 蓝色滑块条文字特征）
    """
    xml = _dump_xml(d)
    if not xml:
        return False
    hit_title = any(m in xml for m in CAPTCHA_MARKERS)
    if not hit_title:
        return False
    # 再补一个 "滑动"/"拖动" 的确认词，避免误匹配
    confirm_words = ("滑动", "拖动", "拼图", "滑块", "gap", "slide")
    confirm_hit = any(w in xml.lower() for w in confirm_words) or \
                  any(w in xml for w in confirm_words)
    return confirm_hit or hit_title   # 只要有 CAPTCHA_MARKERS 就够了


# =====================================================================
#  第二步：把弹窗从整屏里裁出来
# =====================================================================

def _locate_captcha_dialog(img: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """从整屏 PIL.Image 里定位滑块拼图弹窗的 bounds (x1,y1,x2,y2)。

    策略（OpenCV）：
      弹窗是居中的白色卡片，背景半透明遮罩。
      1. 转灰度 → Canny 边缘 → 找最大矩形轮廓
      2. 约束：宽度占屏幕 40%~90%，高度占 30%~70%，上下居中
    失败返回 None（调用方 fallback 到全屏扫描）。
    """
    if not _HAS_CV:
        return None

    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]

    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_rect = None
    best_area = 0
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if area < h * w * 0.05:     # 太小（小按钮）跳过
            continue
        if area > h * w * 0.7:      # 太大（接近全屏）跳过
            continue
        if cw < w * 0.4 or cw > w * 0.9:     # 宽度约束
            continue
        if ch < h * 0.3 or ch > h * 0.7:     # 高度约束
            continue
        # 居中约束：弹窗中心 x 应该在屏幕中心 ±10% 内
        cx = x + cw // 2
        if abs(cx - w // 2) > w * 0.1:
            continue
        if area > best_area:
            best_area = area
            best_rect = (x, y, x + cw, y + ch)

    return best_rect


def _crop_slide_puzzle(img: Image.Image,
                       dialog_bounds: Optional[Tuple[int, int, int, int]] = None
                       ) -> Tuple[Image.Image, int, int]:
    """从弹窗区域切出「背景图 + 下方滑块条」这一整块。

    返回 (拼图完整区, 对话框左上 x, 对话框左上 y) —— 用于后续把
    AI 返回的相对坐标换算回整屏绝对坐标。

    如果 dialog_bounds 给不出，用比例兜底（弹窗大概在屏幕中心 80% 宽 55% 高）。
    """
    W, H = img.size

    if dialog_bounds:
        x1, y1, x2, y2 = dialog_bounds
        dlg = img.crop((x1, y1, x2, y2))
        dx, dy = x1, y1
    else:
        # 兜底：屏幕中心区域（80% 宽 × 55% 高，居中）
        cw, ch = int(W * 0.80), int(H * 0.55)
        x1 = (W - cw) // 2
        y1 = (H - ch) // 2
        dlg = img.crop((x1, y1, x1 + cw, y1 + ch))
        dx, dy = x1, y1

    return dlg, dx, dy


# =====================================================================
#  第三步：缺口定位 —— AI 引擎（千问-VL）
# =====================================================================

_SLIDE_PROMPT = """你现在需要解决一个滑块拼图人机验证。我给你一张弹窗截图，其中包含：
  - 上方：完整背景图，图上有一个灰色的拼图缺口（随机位置），并且有一块同样形状的拼图块贴在缺口旁边或左下角
  - 下方：蓝色滑块条，滑块目前贴在最左边，需要向右拖动到缺口位置使拼图对齐

你的任务：**计算滑块需要向右滑动的距离**（单位：像素）。

计算方法：
  1. 找到背景图里灰色拼图缺口的左上角 x 坐标（相对于背景图左边缘）
  2. 找到蓝色滑块条轨道的宽度（从滑块初始位置中心到轨道最右端的像素距离）
  3. 缺口相对位置 = 缺口x / 背景图宽度（0~1 之间的比例）
  4. 需要滑动的像素距离 = 缺口相对位置 × 滑块条轨道宽度

注意：
  - 背景图宽度通常 = 滑块条轨道宽度
  - 拼图块是那个灰色/白色的形状块，它贴在缺口位置或者左下角
  - 缺口是拼图块本来应该在的位置（背景图上被"挖掉"的那个灰色缺口）
  - 滑块的初始位置中心不是轨道最左端（有一个滑块按钮本身的宽度），
    通常滑块中心距离轨道左端约 滑块按钮宽度的一半

只输出一个 JSON，格式：
  {{\"gap_ratio\": 0.0到1.0之间的小数, \"note\": \"简短说明你怎么算出的\"}}
其中 gap_ratio 是缺口相对位置（缺口x / 背景图宽度）。不要输出任何其他文字。"""


def _ask_qwen_gap_ratio(dialog_img: Image.Image) -> Optional[dict]:
    """把弹窗图发给千问 VL，返回 {"gap_ratio": 0~1}。失败返回 None。"""
    if not QWEN_API_KEY:
        logger.warning("[slide] 未配置 QWEN_API_KEY，跳过 AI 求解")
        return None

    b64 = _img_to_b64(dialog_img)

    payload = {
        "model": QWEN_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": _SLIDE_PROMPT},
            ],
        }],
        "temperature": 0.0,
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json",
    }
    url = QWEN_BASE_URL.rstrip("/") + "/chat/completions"

    for attempt in range(1, QWEN_MAX_RETRIES + 2):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=QWEN_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[slide] 千问 HTTP {resp.status_code}: "
                               f"{resp.text[:200]}（第 {attempt} 次）")
                continue
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            # 解析 JSON
            m = re.search(r"\{[^}]+\}", content, re.DOTALL)
            if not m:
                logger.warning(f"[slide] 千问返回里找不到 JSON：{content[:200]}")
                continue
            data = json.loads(m.group())
            ratio = data.get("gap_ratio")
            if not isinstance(ratio, (int, float)) or not (0.0 < ratio < 1.0):
                logger.warning(f"[slide] gap_ratio 无效: {data}")
                continue
            logger.info(f"[slide] AI 求解 gap_ratio={ratio:.4f}  note={data.get('note', '')}")
            return data
        except Exception as exc:           # noqa: BLE001
            logger.warning(f"[slide] 千问请求异常: {exc}（第 {attempt} 次）")

    return None


# =====================================================================
#  第三步（兜底）：缺口定位 —— OpenCV 引擎
# =====================================================================

def _opencv_gap_ratio(dialog_img: Image.Image) -> Optional[float]:
    """OpenCV 兜底：在背景图上用边缘匹配找缺口位置，返回 gap_ratio（0~1）。

    原理：
      1. 弹窗截图 → 切出"只有背景图 + 拼图块"的区域（排除滑块条）
      2. 拼图块是个深灰色的形状，缺口是背景上对应的深灰区域
      3. Canny 边缘 → 在图上找最显著的"有直角的矩形轮廓"且位置偏右（在拼图块左侧）
      4. 取缺口左上角 x / 背景图宽度 = gap_ratio
    """
    if not _HAS_CV:
        return None

    arr = np.array(dialog_img)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # 把滑块条区域（底部约 30%）裁掉，只看上方拼图区域
    puzzle_h = int(h * 0.7)
    puzzle = gray[:puzzle_h, :]
    ph, pw = puzzle.shape[:2]

    # Canny 边缘
    edges = cv2.Canny(puzzle, 50, 150)

    # 找所有轮廓 → 按面积过滤 → 按矩形度（矩形周长²/(16*面积)越接近1越矩形）排序
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rects = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if area < ph * pw * 0.005 or area > ph * pw * 0.15:
            continue   # 太小（噪点）或太大（全图）都跳过
        if cw < 20 or ch < 20:
            continue
        # 矩形度 = 轮廓面积 / boundingRect 面积（越接近 1 越矩形）
        rectness = cv2.contourArea(c) / max(1, area)
        rects.append((rectness, area, x, y, cw, ch))

    if not rects:
        logger.info("[slide] OpenCV 没找到合适轮廓，AI 又失败了，这轮跳过")
        return None

    # 按矩形度排序 → 取矩形度最高且 x 值不是最靠左的那一个
    # （拼图块贴在左下，缺口在它右边一点）
    rects.sort(key=lambda r: -r[0])
    gap_candidates = []
    for rectness, area, x, y, cw, ch in rects:
        # 缺口通常宽度在 50~120px 之间，宽度 > 高度（拼图块形状）
        if 40 < cw < 140 and 40 < ch < 140 and rectness > 0.3:
            gap_candidates.append((x, y, cw, ch, rectness, area))

    if not gap_candidates:
        return None

    # 取 x 值最大（最靠右）且面积足够的作为缺口
    # （拼图块通常贴左下，缺口在它右边）
    gap_candidates.sort(key=lambda g: -g[0])
    gx, gy, gw, gh, rectness, area = gap_candidates[0]
    ratio = gx / pw
    logger.info(f"[slide] OpenCV 求解 gap_ratio={ratio:.4f}  (x={gx}, rectness={rectness:.3f})")
    return ratio


# =====================================================================
#  第四步：模拟真人滑动
# =====================================================================

def _human_swipe(d, from_xy: Tuple[int, int], to_xy: Tuple[int, int],
                 duration: float = 1.2):
    """模拟真人从 from_xy 拖到 to_xy。

    轨迹特征（防风控）：
      1. 前 60% 距离快速（加速），后 40% 减速（类似 cos 缓动）
      2. 每步 y 加 ±2px 随机抖动（不是绝对直线）
      3. 步数约 duration * 30 ≈ 每帧 30ms（接近人眼 30fps）
      4. 最后收尾多走 3px 再拉回（模拟 overshoot）
    """
    fx, fy = from_xy
    tx, ty = to_xy
    dx, dy = tx - fx, ty - fy
    steps = max(20, int(duration * 30))

    # cos 缓动：t=0→0, t=1→1，前慢后快再慢（类 easeInOut）
    def ease(t):
        return 0.5 - 0.5 * np.cos(np.pi * t)

    points = []
    for i in range(steps + 1):
        t = i / steps
        p = ease(t)
        px = fx + dx * p
        py = fy + dy * p
        # y 方向随机抖动 ±1.5px
        py += random.uniform(-1.5, 1.5)
        points.append((int(px), int(py)))

    # overshoot：末尾再多走 3px 然后拉回 2px
    if dx > 0:
        points.append((int(tx + 3), int(ty + random.uniform(-1, 1))))
        points.append((int(tx + 1), int(ty + random.uniform(-1, 1))))
        points.append((int(tx), int(ty)))

    # 执行：down → 逐点 move → up
    try:
        d.swipe(fx, fy, points[-1][0], points[-1][1], duration=0.01)
    except Exception:
        pass   # u2.swipe 内部用 adb input swipe，不支持多点轨迹
    # 换用更精准的 drag（uiautomator2 原生 drag 只支持两点，
    # 所以先 down → 用 swipe 分段 → up）
    try:
        from uiautomator2 import UiObject
        # 用 raw 手势 API 模拟多点路径（走 kimi_login.run_adb，自动带 -s 序列号）
        from kimi_login import get_device_serial, run_adb
        get_device_serial()
        pts_str = ",".join(f"{x},{y}" for x, y in points)
        # adb input 的 swipe 支持 duration，我们拆成多段快速 swipe
        seg_dur = max(1, int(duration * 1000 // steps))
        for i in range(steps):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            run_adb("shell", "input", "swipe",
                    str(x1), str(y1), str(x2), str(y2), str(seg_dur),
                    timeout=5)
            time.sleep(seg_dur / 1000 * 0.6)   # 额外加一点间隔，避免过快
    except Exception as exc:             # noqa: BLE001
        logger.warning(f"[slide] 真人轨迹滑动异常，退回简化版: {exc}")
        d.swipe(fx, fy, tx, ty, duration=duration)


def _find_slider_bounds(dialog_bgr_np) -> Tuple[int, int, int, int]:
    """在弹窗截图（BGR numpy）上定位滑块条轨道的 bounds。

    策略：
      1. 下半部分（底部 40%）找蓝色区域（滑块条背景是白色，轨道是灰色）
      2. 找最大的水平矩形（宽度占弹窗宽度 70%~95%，高度占弹窗 3%~10%）
      3. 返回 (轨道左x, 轨道顶y, 轨道右x, 轨道底y)

    滑块初始位置 ≈ (轨道左x + 滑块按钮宽度/2, 轨道中心y)
    通常滑块按钮宽度 ≈ 轨道高度的 1.5~2 倍，中心距离轨道左端 ≈ 按钮宽度/2
    """
    if not _HAS_CV:
        return None

    h, w = dialog_bgr_np.shape[:2]
    # 只看下半部分（滑块条通常在弹窗底部）
    bottom = dialog_bgr_np[int(h*0.55):, :]
    bh, bw = bottom.shape[:2]

    gray = cv2.cvtColor(bottom, cv2.COLOR_BGR2GRAY)

    # 轨道是个水平长条，找最大水平矩形
    edges = cv2.Canny(gray, 30, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        # 水平长条：宽度 >> 高度
        if ch < 8 or cw < bw * 0.6:
            continue
        aspect = cw / max(1, ch)
        if aspect < 10 or aspect > 80:   # 太窄或太宽
            continue
        if cw > bw * 0.98:                # 几乎占满弹窗宽度
            continue
        score = cw * ch                   # 面积越大越可能是轨道
        if score > best_score:
            best_score = score
            best = (x, y, cw, ch)

    if best is None:
        return None

    x, y, cw, ch = best
    # 换算回弹窗绝对坐标（加上 55% 偏移）
    ox = 0
    oy = int(h * 0.55)
    return (ox + x, oy + y, ox + x + cw, oy + y + ch)


def _execute_swipe(dialog_bgr, gap_ratio: float,
                   dialog_abs_x: int, dialog_abs_y: int,
                   screen_abs_x: int, screen_abs_y: int,
                   swipe_duration: float) -> bool:
    """在弹窗图上算滑块条位置 → 按 gap_ratio 算目标 x → 真机滑动。

    :param dialog_bgr:     弹窗区域的 BGR numpy（定位轨道用）
    :param gap_ratio:      缺口相对位置（0~1）
    :param dialog_abs_x/y: 弹窗在整屏里的左上坐标（已经加到滑块 x/y 上得到整屏绝对坐标）
    :param screen_abs_x/y: 轨道中心在整屏的绝对坐标（用于 swipe）
    :param swipe_duration: 模拟真人滑动时长
    """
    track = _find_slider_bounds(dialog_bgr)
    if track is None:
        logger.warning("[slide] OpenCV 没找到滑块条轨道，退回比例估算")
        # 退回比例：轨道 ≈ 弹窗底部 10% 区域，宽度 ≈ 弹窗宽 90%
        dh, dw = dialog_bgr.shape[:2]
        tx1 = int(dw * 0.05)
        ty1 = int(dh * 0.72)
        tx2 = int(dw * 0.95)
        ty2 = int(dh * 0.82)
    else:
        tx1, ty1, tx2, ty2 = track

    track_w = tx2 - tx1
    track_cy = (ty1 + ty2) // 2
    slider_btn_w = ty2 - ty1   # 滑块按钮宽度 ≈ 轨道高度
    slider_cx_start = tx1 + slider_btn_w // 2   # 滑块初始中心 x

    # 目标 x = 初始中心 + gap_ratio × (轨道有效可滑距离)
    # 有效可滑距离 = 轨道宽 - 滑块按钮宽
    travel = (track_w - slider_btn_w) * gap_ratio
    target_x = int(slider_cx_start + travel)

    # 换算回整屏绝对坐标
    from_x = dialog_abs_x + slider_cx_start
    from_y = dialog_abs_y + track_cy
    to_x = dialog_abs_x + target_x
    to_y = from_y

    logger.info(f"[slide] 轨道 ({tx1},{ty1})-({tx2},{ty2}) 宽={track_w}  "
                f"gap_ratio={gap_ratio:.4f}  travel={int(travel)}px")
    logger.info(f"[slide] 滑动 ({from_x},{from_y}) → ({to_x},{to_y})  duration={swipe_duration}s")

    from uiautomator2 import connect as _u2_connect  # 避免循环 import
    d = _u2_connect()
    _human_swipe(d, (from_x, from_y), (to_x, to_y), duration=swipe_duration)
    return True


# =====================================================================
#  主入口：solve_slide_captcha
# =====================================================================

def solve_slide_captcha(d) -> bool:
    """检测到滑块拼图弹窗后调用此函数，破解并自动尝试通过。

    双引擎（AI 优先，OpenCV 兜底）+ 自动刷新重试：
      - 求解成功 → 自动执行滑动 → 等 2 秒后判断结果
      - 验证框消失 = 通过
      - 验证框仍在 = 已换新题 → 重新截图求解 → 重试
      - 超过 SLIDE_CAPTCHA_MAX_ATTEMPTS 轮 → 返回 False（让上层决定刷新或报错）

    :return: True = 已成功破解（验证框消失）；False = 本轮没通过
    """
    if not is_slide_captcha_visible(d):
        logger.info("[slide] 当前没有滑块拼图弹窗，直接返回")
        return True   # 没弹窗 = 不需要解 = 算通过

    logger.info("[slide] ===== 检测到滑块拼图弹窗，开始破解 =====")

    for attempt in range(1, SLIDE_CAPTCHA_MAX_ATTEMPTS + 1):
        logger.info(f"[slide] --- 第 {attempt}/{SLIDE_CAPTCHA_MAX_ATTEMPTS} 轮 ---")

        # ---- 1. 截图 ----
        img = _adb_screenshot()

        # ---- 2. 定位弹窗 → 裁拼图区 ----
        dlg_bounds = _locate_captcha_dialog(img)
        if dlg_bounds is None:
            logger.warning("[slide] OpenCV 定位弹窗失败，退回居中比例")
        dlg_img, dlg_dx, dlg_dy = _crop_slide_puzzle(img, dlg_bounds)

        # 调试保存
        if CAPTCHA_DEBUG_KEEP:
            ts = int(time.time())
            dlg_img.save(DEBUG_DIR / f"dialog_{attempt}_{ts}.png")

        # ---- 3. AI 求解 gap_ratio ----
        gap_ratio = None
        if SLIDE_CAPTCHA_AI_FIRST:
            ai_result = _ask_qwen_gap_ratio(dlg_img)
            if ai_result:
                gap_ratio = ai_result.get("gap_ratio")
            else:
                logger.info("[slide] AI 求解失败，换 OpenCV 兜底")

        # ---- 4. OpenCV 兜底 ----
        if gap_ratio is None:
            gap_ratio = _opencv_gap_ratio(dlg_img)
            if gap_ratio is None:
                logger.warning("[slide] OpenCV 也没解出 gap_ratio，本轮放弃")
                # 刷新换题再试
                _refresh_captcha_if_possible(d)
                time.sleep(1.5)
                continue

        # ---- 5. 执行滑动 ----
        dlg_bgr = cv2.cvtColor(np.array(dlg_img), cv2.COLOR_RGB2BGR) if _HAS_CV else None
        if dlg_bgr is None:
            logger.warning("[slide] 没有 OpenCV，无法定位滑块条轨道")
            _refresh_captcha_if_possible(d)
            time.sleep(1.5)
            continue

        _execute_swipe(
            dlg_bgr=dlg_bgr,
            gap_ratio=gap_ratio,
            dialog_abs_x=dlg_dx,
            dialog_abs_y=dlg_dy,
            screen_abs_x=0, screen_abs_y=0,   # _execute_swipe 内部自己算
            swipe_duration=SLIDE_CAPTCHA_SWIPE_DURATION,
        )

        # ---- 6. 等待结果 ----
        time.sleep(2.5)

        # ---- 7. 判定通过 ----
        if not is_slide_captcha_visible(d):
            logger.info(f"[slide] ✅ 第 {attempt} 轮破解成功！滑块拼图验证已通过")
            return True

        logger.warning(f"[slide] 第 {attempt} 轮未通过（验证框还在），自动刷新换题重试")
        _refresh_captcha_if_possible(d)
        time.sleep(2.0)

    logger.error(f"[slide] ❌ 超过 {SLIDE_CAPTCHA_MAX_ATTEMPTS} 轮仍未通过滑块拼图验证")
    return False


def _refresh_captcha_if_possible(d) -> bool:
    """尝试点弹窗右上角的刷新/重新加载按钮换一道新题。

    滑块拼图弹窗通常有个圆形刷新图标在背景图的右上角（位置大概
    在弹窗右边缘向内 5%，纵向 8%）。先试 desc 文本匹配，失败坐标兜底。
    """
    xml = _dump_xml(d)
    for w in ("刷新", "重新加载", "换一批", "重试"):
        if w in xml:
            try:
                objs = d(textContains=w)
                if objs.exists(timeout=1):
                    objs.click()
                    return True
            except Exception:
                pass
    # 坐标兜底：弹窗右上角刷新按钮（在弹窗里）
    try:
        w, h = d.info["displayWidth"], d.info["displayHeight"]
        # 先关弹窗重开（点击遮罩）
        d.click(int(w * 0.5), int(h * 0.5))
        time.sleep(0.5)
    except Exception:
        pass
    return False
