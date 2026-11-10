# -*- coding: utf-8 -*-
"""
captcha_solver.py —— DeepSeek 登录人机验证破解器（v4.0 OpenCV+OCR 分工版）
=============================================================================

v3.0 失败根因：
  1. Prompt 让模型"抄题目"，模型理解成"描述图片" → 三轮分别答"黄色六棱柱"/
     "红色长方体"/"红色三棱柱"，完全没理会题目问的是"绿色球体"
  2. 坐标偶尔溢出裁剪图范围，兜底点弹窗中心 → 点错
  3. 千问 API 单次 4~40s，3 轮 15~20s，成功率低

v4.0 新思路（OpenCV 做眼睛，大模型做大脑，分工明确）：
  ┌────────────────────────────────────────────┐
  │  OpenCV 颜色分割（毫秒级）                    │
  │  - HSV 阈值提取红/绿/蓝/黄等色块              │
  │  - 轮廓检测 → 计算每个候选物体的面积/质心      │
  │  - 输出候选列表：[{"color":"绿","shape":"球体",  │
  │    "cx":450,"cy":320,"area":1200}, ...]      │
  └────────────────────────────────────────────┘
                   ↓
  ┌────────────────────────────────────────────┐
  │  OCR 读题（本地 easyocr，毫秒级）             │
  │  - 裁剪弹窗底部文字区                         │
  │  - 输出："点击图中最小的绿色球体"              │
  │  - easyocr 不可用时回退到大模型读题            │
  └────────────────────────────────────────────┘
                   ↓
  ┌────────────────────────────────────────────┐
  │  千问 VL（只看小图 + 选择题模式）              │
  │  输入：验证图 + OCR题目 + OpenCV候选列表      │
  │  Prompt："题目是【点击图中最小的绿色球体】。    │
  │    候选：1.绿球体(450,320)面积小 2.蓝球体...  │
  │    请回答是第几个？"                          │
  │  输出：{"index":1,"x":450,"y":320}           │
  └────────────────────────────────────────────┘
                   ↓
  ┌────────────────────────────────────────────┐
  │  OpenCV 校验 + adb 精准点击                   │
  │  - 模型坐标必须在某个候选物体质心附近           │
  │  - 偏差过大时用 OpenCV 计算的质心校正          │
  │  - adb shell input tap 精准点击               │
  └────────────────────────────────────────────┘
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from PIL import Image, ImageDraw

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
    CAPTCHA_USE_OPENCV,
    CAPTCHA_MIN_AREA,
    CAPTCHA_QWEN_MAX_SIDE,
)

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(SCREENSHOT_DIR) / "captcha_debug"
if CAPTCHA_DEBUG_KEEP:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

CAPTCHA_WEBVIEW_HINTS = ("智能验证码", "数美")

# OpenCV 可用性检测
try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    _CV2_AVAILABLE = False
    logger.warning("[captcha] OpenCV 未安装，颜色分割功能不可用，将回退到纯大模型方案")

# easyocr 已移除：用户明确要求用千问 API 读题，不用本地 OCR


# =====================================================================
#  1) XML：弹窗检测 + WebView bounds（毫秒级，最准）
# =====================================================================

def _get_xml(d) -> str:
    return d.dump_hierarchy(compressed=False)


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def _captcha_info_xml(d) -> Optional[dict]:
    """从 XML 里拿人机验证弹窗的 WebView bounds。"""
    try:
        xml = _get_xml(d)
    except Exception as exc:
        logger.debug(f"dump XML 失败: {exc}")
        return None

    try:
        root = ET.fromstring(xml)
    except Exception as exc:
        logger.debug(f"XML 解析失败: {exc}")
        return None

    for node in root.iter("node"):
        text = node.attrib.get("text") or ""
        cls = node.attrib.get("class", "")
        focused = node.attrib.get("focused") == "true"
        if any(h in text for h in CAPTCHA_WEBVIEW_HINTS) or (
                "WebView" in cls and focused):
            b = _parse_bounds(node.attrib.get("bounds", ""))
            if b:
                x1, y1, x2, y2 = b
                return {
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
                    "bounds": [x1, y1, x2, y2],
                    "text": text, "class": cls,
                }
    return None


def _captcha_is_present_xml(d) -> bool:
    return _captcha_info_xml(d) is not None


# =====================================================================
#  2) 截图（adb -s 绑设备）
# =====================================================================

def _get_serial() -> str:
    from config import EMULATOR_ADB_PORT
    port = EMULATOR_ADB_PORT
    r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=15)
    devs = [l.split()[0] for l in r.stdout.splitlines()[1:]
            if "device" in l and "*" not in l and l.strip()]
    if not devs:
        raise RuntimeError("没 adb 设备")
    if port and port > 0:
        for s in devs:
            if str(port) in s:
                return s
    return devs[0]


def _screen(d) -> Image.Image:
    serial = getattr(d, "serial", None) or getattr(d, "_serial", None) or _get_serial()
    r = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    if r.returncode != 0:
        shot = d.screenshot()
        if isinstance(shot, Image.Image):
            return shot.convert("RGB")
        return Image.open(BytesIO(shot)).convert("RGB")
    return Image.open(BytesIO(r.stdout)).convert("RGB")


# =====================================================================
#  3) OpenCV 颜色分割：提取候选物体
# =====================================================================

# HSV 颜色阈值表（OpenCV HSV: H 0-179, S 0-255, V 0-255）
_COLOR_RANGES = {
    "红": [((0, 100, 100), (10, 255, 255)), ((160, 100, 100), (179, 255, 255))],
    "绿": [((35, 80, 80), (85, 255, 255))],
    "蓝": [((90, 80, 80), (130, 255, 255))],
    "黄": [((15, 100, 100), (35, 255, 255))],
    "橙": [((5, 100, 100), (15, 255, 255))],
    "紫": [((125, 80, 80), (160, 255, 255))],
}


def _detect_color_blobs(img: Image.Image) -> List[dict]:
    """对验证图做颜色分割，返回候选物体列表。

    每个候选: {"color": "绿", "cx": 450, "cy": 320, "area": 1200, "bounds": (x1,y1,x2,y2)}
    """
    if not _CV2_AVAILABLE or not CAPTCHA_USE_OPENCV:
        return []

    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2HSV)
    candidates = []

    for color_name, ranges in _COLOR_RANGES.items():
        mask = None
        for lower, upper in ranges:
            m = cv2.inRange(arr, np.array(lower), np.array(upper))
            mask = m if mask is None else cv2.bitwise_or(mask, m)

        # 形态学去噪
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < CAPTCHA_MIN_AREA:
                continue
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            x, y, w, h = cv2.boundingRect(cnt)
            candidates.append({
                "color": color_name,
                "cx": cx, "cy": cy,
                "area": int(area),
                "bounds": (x, y, x + w, y + h),
            })

    # 按面积排序，标注面积排名（1=最小）
    candidates.sort(key=lambda c: c["area"])
    for i, c in enumerate(candidates):
        c["size_rank"] = i + 1  # 1 = 最小

    return candidates


# =====================================================================
#  4) 千问 VL：读题 + 选择题模式（OpenCV 候选 + 千问读题）
# =====================================================================

def _img_to_b64(img: Image.Image, max_side: int = 512) -> str:
    buf = BytesIO()
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.LANCZOS)
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_prompt(candidates: List[dict]) -> str:
    """构建读题 + 选择题式 Prompt。

    千问的任务：
      1. 先读弹窗底部的题目文字（比如"点击图中最小的绿色球体"）
      2. 从 OpenCV 提取的候选物体列表中，选出最符合题目描述的一个
      3. 返回该候选的质心坐标
    """
    lines = [
        "这是一个 Android App 人机验证弹窗的裁剪图。",
        "图片上半部分是验证图（彩色 3D 几何体），下半部分有一行中文题目文字。",
        "",
        "【你的任务】",
        "第一步：仔细阅读图片底部的那行中文题目文字（比如\"点击图中最小的XX色XX体\"）。",
        "第二步：根据题目要求，从下面候选物体列表中选出正确的一个。",
        "",
    ]

    if candidates:
        lines.append("【候选物体列表】（已按面积从小到大排序）")
        for i, c in enumerate(candidates, 1):
            size_desc = "最小" if c["size_rank"] == 1 else \
                        "较小" if c["size_rank"] <= 3 else \
                        "中等" if c["size_rank"] <= 6 else "较大"
            lines.append(
                f"{i}. {c['color']}色物体，中心相对坐标({c['cx']},{c['cy']})，"
                f"面积排名: 第{c['size_rank']}小（{size_desc}）"
            )
        lines.append("")
        lines.append("请从上面候选列表中选择一个最符合题目描述的物体。")
    else:
        lines.append("图中没有检测到明显的候选物体，请直接在验证图中寻找目标。")

    lines.extend([
        "",
        "只输出 JSON，不要其他文字：",
        '{"question": "你读到的题目文字", "index": 候选编号或0, "x": 整数, "y": 整数}',
        "",
        "注意：",
        "- question 字段必须原样抄下你读到的题目文字",
        "- 坐标是相对于这张裁剪图左上角 (0,0) 的像素坐标",
        "- 如果从候选列表选择，index 填候选编号；否则填 0",
        "- 坐标 x,y 必须对应你选择的那个候选物体的中心",
    ])
    return "\n".join(lines)


def _call_qwen(img: Image.Image, candidates: List[dict]) -> dict:
    """裁剪后的弹窗图 → 千问 VL → 返回 {question, x, y, index}（坐标是裁剪图内的）"""
    if not QWEN_API_KEY:
        raise RuntimeError("未配置 QWEN_API_KEY")

    b64 = _img_to_b64(img, max_side=CAPTCHA_QWEN_MAX_SIDE)
    cw, ch = img.size

    prompt = _build_prompt(candidates)

    payload = {
        "model": QWEN_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0.0,
        "max_tokens": 200,
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
                logger.warning(f"[captcha] 千问 HTTP {resp.status_code} "
                               f"（第 {attempt} 次）: {resp.text[:200]}")
                continue
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            break
        except Exception as exc:
            logger.warning(f"[captcha] 千问请求异常（第 {attempt} 次）: {exc}")
    if not content:
        raise RuntimeError(f"千问 VL 请求失败: {content[:200] or '无返回'}")

    logger.info(f"[captcha] 千问原始返回: {content[:300]}")
    return _parse_qwen_reply(content, cw, ch, candidates)


def _parse_qwen_reply(content: str, crop_w: int, crop_h: int,
                      candidates: List[dict]) -> dict:
    """解析千问返回的 JSON。坐标做归一化校正 + 候选区域校验。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", content).strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                raise RuntimeError(f"无法解析: {content[:200]}")
        else:
            raise RuntimeError(f"无法解析: {content[:200]}")

    question = str(data.get("question", "") or "")
    raw_x, raw_y = data.get("x", 0), data.get("y", 0)
    index = data.get("index", 0)

    x, y = 0, 0
    if isinstance(raw_x, (int, float)) and isinstance(raw_y, (int, float)):
        x, y = int(raw_x), int(raw_y)
        # 归一化校正（偶尔千问返回 0~1000）
        if 0 < x <= 1000 and 0 < y <= 1000:
            if x < crop_w * 0.2 and y < crop_h * 0.2:
                x, y = int(x / 1000 * crop_w), int(y / 1000 * crop_h)

    # 如果模型选了候选编号，用候选的质心坐标校正
    if isinstance(index, int) and 1 <= index <= len(candidates):
        cand = candidates[index - 1]
        # 模型坐标和候选质心偏差太大时，用候选质心
        dist = ((x - cand["cx"]) ** 2 + (y - cand["cy"]) ** 2) ** 0.5
        if dist > 50:  # 偏差超过 50px 就校正
            logger.info(f"[captcha] 模型坐标({x},{y})偏离候选{index}质心"
                        f"({cand['cx']},{cand['cy']}) {dist:.0f}px，已校正")
            x, y = cand["cx"], cand["cy"]
        else:
            logger.info(f"[captcha] 模型选择候选{index}，坐标({x},{y})在合理范围内")

    logger.info(f"[captcha] 解析: question=\"{question}\" "
                f"x_crop={x} y_crop={y} (裁剪图 {crop_w}x{crop_h})")
    return {"question": question, "x_crop": x, "y_crop": y, "index": index}


# =====================================================================
#  5) 主流程：检测 → 裁剪 → OpenCV+OCR → 问千问 → 校验点击
# =====================================================================

def wait_captcha_and_solve(d, appear_timeout: float = 60.0,
                           max_attempts: int = CAPTCHA_MAX_ATTEMPTS) -> bool:
    """点完「发送验证码」后调用。

    流程：
      1) 阶段1 —— XML 快速检测弹窗出现，或检测倒计时（短信已直接发出）
      2) 阶段2 —— 弹窗已在屏上 → OpenCV+OCR → 千问 → 校验点击 → XML 确认
    """
    logger.info(f"[captcha] 开始等待人机验证弹窗（超时 {appear_timeout}s，XML 毫秒级检测）...")
    deadline = time.time() + appear_timeout
    tick = 0
    cached_wv = None

    # 阶段 1：XML 快速检测弹窗出现
    while time.time() < deadline:
        tick += 1

        # 优先检测：是不是已经手动过掉了？弹窗消失 + 倒计时出现
        if _check_pass_by_countdown(d):
            logger.info("[captcha] ✅ 检测到倒计时 —— 人机验证已通过（可能手动过掉的）")
            return True

        wv = _captcha_info_xml(d)
        if wv:
            cached_wv = wv
            logger.info(f"[captcha] ✅ XML 检测到弹窗！bounds={wv['bounds']}")
            break

        # 真没弹窗：检查倒计时（无弹窗直接通过的情况）
        try:
            xml = _get_xml(d)
            if re.search(r"重新发送|\d+\s*s\b|秒后", xml):
                logger.info("[captcha] 发送按钮已进入倒计时 → 短信已直接发出（无弹窗）")
                return True
        except Exception:
            pass

        if tick % 10 == 0:
            logger.info(f"[captcha] 已等 {time.time()-deadline+appear_timeout:.0f}s，XML 仍未检出弹窗")
        time.sleep(0.5)

    if not cached_wv:
        logger.warning(f"[captcha] 等待 {appear_timeout}s 超时，弹窗未出现，留场继续")
        _save_debug(d, "wait_timeout")
        return True

    # 阶段 2：裁剪弹窗 → OpenCV+OCR → 问千问 → 校验点击
    return _solve_loop(d, cached_wv, max_attempts)


def _solve_loop(d, webview: dict, max_attempts: int) -> bool:
    """弹窗已在屏上 → 裁剪 → OpenCV+OCR → 千问 → 校验点击 → XML 确认。"""
    wv = webview

    # 进入求解前再检查一次：你可能手动过掉了弹窗
    if not _captcha_is_present_xml(d) and _check_pass_by_countdown(d):
        logger.info("[captcha] ✅ 弹窗已消失 + 倒计时出现 —— 已通过（可能手动过掉的）")
        return True

    for attempt in range(1, max_attempts + 1):
        logger.info(f"[captcha] ===== 第 {attempt}/{max_attempts} 轮求解 =====")

        # 每轮开头也检查：千问 API 调用期间你可能手动过掉了
        if not _captcha_is_present_xml(d) and _check_pass_by_countdown(d):
            logger.info(f"[captcha] ✅ 第 {attempt} 轮前检测到弹窗消失 + 倒计时 —— 已通过（可能手动过掉的）")
            return True

        # 1) 先确认弹窗还在（XML）
        wv = _captcha_info_xml(d) or wv
        if not _captcha_is_present_xml(d):
            logger.warning("[captcha] XML 显示弹窗已消失")
            if _check_pass_by_countdown(d):
                return True
            if not _wait_popup_back(d, timeout=20):
                logger.error("[captcha] 弹窗消失后没回来，放弃")
                _save_debug(d, "popup_gone")
                return True
            wv = _captcha_info_xml(d) or wv

        # 2) 整屏截图 + 裁剪弹窗区域
        try:
            full = _screen(d)
        except Exception as exc:
            logger.warning(f"[captcha] 整屏截图失败: {exc}")
            time.sleep(1)
            continue

        # 裁剪：webview bounds 加 padding 把整个弹窗卡片框住
        pad_left, pad_top, pad_right, pad_bottom = 20, 30, 20, 30
        x1 = max(0, wv["x1"] - pad_left)
        y1 = max(0, wv["y1"] - pad_top)
        x2 = min(full.width, wv["x2"] + pad_right)
        y2 = min(full.height, wv["y2"] + pad_bottom)

        crop = full.crop((x1, y1, x2, y2))
        logger.info(f"[captcha] 裁剪弹窗区域: ({x1},{y1})-({x2},{y2}) "
                    f"裁剪图尺寸: {crop.size}")

        # 留存裁剪图
        try:
            _save_crop(crop, attempt)
        except Exception:
            pass

        # 3) OpenCV 颜色分割提取候选物体
        candidates = []
        if _CV2_AVAILABLE and CAPTCHA_USE_OPENCV:
            try:
                # 只取弹窗上半部分（验证图区域）做颜色分割
                # 弹窗布局：上面是图，下面是文字按钮，取上面 60% 作为验证图
                img_area = crop.crop((0, 0, crop.width, int(crop.height * 0.65)))
                candidates = _detect_color_blobs(img_area)
                logger.info(f"[captcha] OpenCV 检测到 {len(candidates)} 个候选物体")
                for c in candidates:
                    logger.debug(f"  候选: {c['color']}色 area={c['area']} "
                                 f"质心=({c['cx']},{c['cy']}) rank={c['size_rank']}")
            except Exception as e:
                logger.warning(f"[captcha] OpenCV 颜色分割失败: {e}")

        # 4) 发给千问 VL（读题 + 选择题模式，千问一次调用完成）
        try:
            t0 = time.time()
            qres = _call_qwen(crop, candidates)
            elapsed = time.time() - t0
            logger.info(f"[captcha] 千问耗时 {elapsed:.1f}s → question=\"{qres['question']}\" "
                        f"x_crop={qres['x_crop']} y_crop={qres['y_crop']} index={qres.get('index',0)}")
        except Exception as exc:
            logger.warning(f"[captcha] 千问调用失败: {exc}")
            time.sleep(1)
            continue

        # 6) 加偏移量还原成全屏绝对坐标
        x_full = qres["x_crop"] + x1
        y_full = qres["y_crop"] + y1

        # 7) 校验：全屏坐标应该在弹窗卡片范围内
        if not (wv["x1"] - 10 <= x_full <= wv["x2"] + 10
                and wv["y1"] - 10 <= y_full <= wv["y2"] + 10):
            logger.warning(f"[captcha] ⚠️ 还原坐标 ({x_full},{y_full}) 不在弹窗 bounds "
                           f"[{wv['x1']},{wv['y1']}]-[{wv['x2']},{wv['y2']}] 内")
            # 兜底：如果有候选物体，用最优候选的质心
            if candidates:
                best = _pick_best_candidate(candidates, qres.get("question", ""))
                # img_area 从 crop 的 (0,0) 开始，候选坐标已在 crop 坐标系中
                x_full = best["cx"] + x1
                y_full = best["cy"] + y1
                logger.warning(f"[captcha] → 用 OpenCV 最优候选校正为 ({x_full},{y_full})")
            else:
                x_full, y_full = wv["cx"], wv["cy"]
                logger.warning(f"[captcha] → 兜底点弹窗中心 ({x_full},{y_full})")

        logger.info(f"[captcha] 问题: \"{qres['question']}\" → 全屏点击 ({x_full},{y_full})")

        # 8) 留存带红点的整屏标记图
        try:
            _save_marked(full, x_full, y_full, f"a{attempt}_click")
        except Exception:
            pass

        # 9) adb 精准点击
        if not _adb_tap(x_full, y_full):
            try:
                d.click(x_full, y_full)
            except Exception as exc:
                logger.error(f"[captcha] 点击失败: {exc}")
                time.sleep(1)
                continue

        # 10) 等弹窗反应
        time.sleep(CAPTCHA_CLICK_WAIT)

        # 11) XML 判定弹窗消失
        if not _captcha_is_present_xml(d):
            time.sleep(2)
            if _check_pass_by_countdown(d):
                logger.info(f"[captcha] ✅ 第 {attempt} 轮后弹窗消失 + 倒计时 = 真的通过！")
                return True
            logger.warning(f"[captcha] 第 {attempt} 轮后弹窗消失但没倒计时，等弹窗再来...")
            if not _wait_popup_back(d, timeout=20):
                logger.warning("[captcha] 没等到新弹窗")
                _save_debug(d, "gone_no_new")
                return True
            continue

        # 弹窗还在 → 可能刷了新题或点错了
        logger.warning(f"[captcha] 第 {attempt} 轮后弹窗还在 → 未通过")

    logger.error(f"[captcha] ❌ 连续 {max_attempts} 轮未通过人机验证")
    _save_debug(d, "giveup")
    return False


def _pick_best_candidate(candidates: List[dict], question: str) -> dict:
    """根据题目文字从候选列表中挑最匹配的一个。

    简单规则：
      - 题目提到"最小" → 选 size_rank=1 的
      - 题目提到颜色 → 优先选该颜色的
    """
    if not candidates:
        raise ValueError("候选列表为空")

    # 先按颜色过滤
    color_map = {"红": "红", "绿": "绿", "蓝": "蓝", "黄": "黄", "橙": "橙", "紫": "紫"}
    matched = []
    for c in candidates:
        for kw, color in color_map.items():
            if kw in question and c["color"] == color:
                matched.append(c)
                break

    if not matched:
        matched = candidates

    # 题目说"最小"就选面积最小的
    if "最小" in question:
        best = min(matched, key=lambda c: c["size_rank"])
    elif "最大" in question:
        best = max(matched, key=lambda c: c["size_rank"])
    else:
        best = matched[0]

    logger.info(f"[captcha] OpenCV 智能挑选: \"{question}\" → {best['color']}色 "
                f"rank={best['size_rank']} 质心=({best['cx']},{best['cy']})")
    return best


def _adb_tap(x: int, y: int) -> bool:
    """用 adb shell input tap 精准点击坐标。"""
    try:
        serial = _get_serial()
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _check_pass_by_countdown(d) -> bool:
    """看发送验证码按钮是否进入倒计时（= 真的通过了）。"""
    try:
        xml = _get_xml(d)
        return bool(re.search(r"重新发送|\d+\s*s\b|秒后", xml))
    except Exception:
        return False


def _wait_popup_back(d, timeout: float = 20.0) -> bool:
    """弹窗消失后等它再出来。"""
    logger.info(f"[captcha] 等待新弹窗...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _captcha_info_xml(d):
            return True
        time.sleep(1)
    return False


# =====================================================================
#  调试留存
# =====================================================================

def _save_debug(d, tag: str) -> None:
    if not CAPTCHA_DEBUG_KEEP:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img = _screen(d)
        img.save(DEBUG_DIR / f"{ts}_{tag}_screen.png")
        xml = _get_xml(d)
        (DEBUG_DIR / f"{ts}_{tag}_hierarchy.xml").write_text(xml, encoding="utf-8")
        logger.info(f"[captcha] 留场: {ts}_{tag}_*")
    except Exception as exc:
        logger.debug(f"[captcha] 留场失败: {exc}")


def _save_marked(full_img, x: int, y: int, tag: str) -> None:
    if not CAPTCHA_DEBUG_KEEP:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        marked = full_img.copy()
        draw = ImageDraw.Draw(marked)
        draw.ellipse([x - 18, y - 18, x + 18, y + 18], outline=(255, 0, 0), width=5)
        draw.line([x - 35, y, x + 35, y], fill=(255, 0, 0), width=3)
        draw.line([x, y - 35, x, y + 35], fill=(255, 0, 0), width=3)
        draw.text((x + 25, y - 25), f"({x},{y})", fill=(255, 0, 0))
        marked.save(DEBUG_DIR / f"{ts}_{tag}_marked.png")
    except Exception as exc:
        logger.debug(f"[captcha] 存标记图失败: {exc}")


def _save_crop(crop_img, attempt: int) -> None:
    if not CAPTCHA_DEBUG_KEEP:
        return
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        crop_img.save(DEBUG_DIR / f"{ts}_a{attempt}_crop.png")
    except Exception as exc:
        logger.debug(f"[captcha] 存裁剪图失败: {exc}")
