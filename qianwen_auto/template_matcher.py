# -*- coding: utf-8 -*-
"""
template_matcher —— 基于 OpenCV 模板匹配的 UI 按钮点击。

★ 为什么用模板匹配？
  千问新版大量 UI 用 WebView 渲染，content-desc / resource-id 经常缺失，
  UIAutomator 的 text / description 选择器根本找不到。
  用截图 + 模板匹配能直接按「按钮长什么样」来定位，最靠谱。

★ 路径约定（完全不写死）：
  模板图统一放在项目根目录的 button_icons/ 下，由 config.BASE_DIR 动态推导：
      button_icons/open_left_drawer.png   ← 打开左侧抽屉（汉堡三条横线）
      button_icons/open_setting.png       ← 抽屉里的设置齿轮
      button_icons/open_new_chat.png      ← 顶部栏的新建对话（笔+方框）
      button_icons/switch_keyboard.png    ← 语音输入时的键盘切换图标
  项目搬到服务器部署时，只要把 button_icons/ 目录一起打包就行。

★ 用法：
  from template_matcher import click_template, find_template

  # 1) 只找，不点击
  pos = find_template(d, "open_left_drawer")

  # 2) 匹配 + 点击 + 可选验证（最常用）
  ok = click_template(d, "open_left_drawer", label="开抽屉")
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PIL import Image

# 从 config 动态拿项目根目录和 ADB 端口 —— 不写死！
from config import BASE_DIR

log = logging.getLogger("qianwen.template")

# 模板图目录（相对项目根，不写死绝对路径）
ICONS_DIR: Path = BASE_DIR / "button_icons"

# 匹配模式：TM_CCOEFF_NORMED 取值 [0, 1]，越大越匹配
_DEFAULT_THRESHOLD = 0.82
_MATCH_METHOD = cv2.TM_CCOEFF_NORMED

# 图标模板通常截自 720px 宽设备；其他分辨率按比例换算，再做邻域多尺度搜索。
# 这只是尺寸基准，不是路径；也可用环境变量覆盖。
_REFERENCE_SCREEN_WIDTH = int(os.getenv("QIANWEN_ICON_REFERENCE_WIDTH", "720"))
_SCALE_FACTORS = (1.0, 1.15, 0.85, 1.30, 0.70, 1.50)


# =====================================================================
#  内部：ADB 工具（自包含，不依赖 worker/login）
# =====================================================================

def _get_adb_prefix() -> list:
    """返回 adb 命令前缀 [adb, -s, serial]。"""
    try:
        from config import EMULATOR_ADB_PORT
        return ["adb", "-s", f"emulator-{EMULATOR_ADB_PORT}"]
    except Exception:
        return ["adb"]


def _adb_screenshot_gray() -> Optional[np.ndarray]:
    """adb 截图 → numpy 灰度图。失败返回 None。"""
    try:
        proc = subprocess.run(
            _get_adb_prefix() + ["exec-out", "screencap", "-p"],
            capture_output=True, timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        # PNG → PIL → numpy 灰度
        img = Image.open(BytesIO(proc.stdout)).convert("RGB")
        arr = np.array(img)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    except Exception as exc:
        log.debug("adb 截图失败：%s", exc)
        return None


def _adb_tap(x: int, y: int) -> bool:
    try:
        proc = subprocess.run(
            _get_adb_prefix() + ["shell", "input", "tap", str(int(x)), str(int(y))],
            capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception as exc:
        log.debug("adb tap 失败：%s", exc)
        return False


def _screen_size() -> Tuple[int, int]:
    """adb 获取屏幕宽高。"""
    try:
        proc = subprocess.run(
            _get_adb_prefix() + ["shell", "wm", "size"],
            capture_output=True, text=True, timeout=10,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # 形如 "Physical size: 720x1280"
        import re
        m = re.search(r"(\d+)\s*[x×]\s*(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    # 兜底：取一次截图算尺寸
    gray = _adb_screenshot_gray()
    if gray is not None:
        return gray.shape[1], gray.shape[0]
    return 720, 1280  # 最终兜底


# =====================================================================
#  内部：模板加载（带缓存 + 尺寸修正）
# =====================================================================

@lru_cache(maxsize=16)
def _load_template_gray(name: str) -> Optional[np.ndarray]:
    """加载 button_icons/<name>.png 为灰度图，保留图标原始像素尺寸。"""
    p = Path(name)
    if p.suffix:
        path = p if p.is_absolute() else (ICONS_DIR / p.name)
    else:
        path = ICONS_DIR / f"{name}.png"

    if not path.exists():
        log.warning("模板文件不存在：%s", path)
        return None
    try:
        img = Image.open(path).convert("RGB")
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    except Exception as exc:
        log.warning("加载模板 %s 失败：%s", path, exc)
        return None


def _candidate_scales(screen_w: int) -> Tuple[float, ...]:
    """根据屏幕宽度生成候选缩放比例，不根据图标宽度推算屏幕宽度。"""
    base = max(0.5, screen_w / _REFERENCE_SCREEN_WIDTH)
    values = []
    for factor in _SCALE_FACTORS:
        scale = round(base * factor, 4)
        if scale not in values:
            values.append(scale)
    return tuple(values)


# =====================================================================
#  核心 API
# =====================================================================

def find_template(
    template_name: str,
    threshold: float = _DEFAULT_THRESHOLD,
    region: Optional[Tuple[float, float, float, float]] = None,
    screen_gray: Optional[np.ndarray] = None,
    save_fail: bool = True,
) -> Optional[Tuple[int, int, float]]:
    """在当前屏幕上找模板图，返回 (center_x, center_y, max_score) 或 None。

    Args:
        template_name: 模板文件名（在 button_icons/ 下，可不写 .png）
        threshold:     匹配阈值（0~1）。默认 0.82，越高越严格。
        region:        可选搜索区域 (x1_ratio, y1_ratio, x2_ratio, y2_ratio)
                       比如 (0.0, 0.0, 1.0, 0.20) 只搜顶部 20%。
                       传 region 能大幅加速 + 减少误点。
        screen_gray:   可选 —— 调用方已有的灰度图（避免重复截图）。
        save_fail:     匹配失败时是否把屏幕存到 screenshots/ 方便调试。

    Returns:
        (cx, cy, score)   匹配到了，score 是 0~1 之间的相似度
        None              没匹配到（score 低于阈值）
    """
    tpl_raw = _load_template_gray(template_name)
    if tpl_raw is None:
        return None

    if screen_gray is None:
        screen_gray = _adb_screenshot_gray()
    if screen_gray is None:
        log.warning("find_template 截图失败")
        return None

    sh, sw = screen_gray.shape[:2]

    # 先裁剪搜索区域，再对每个尺度分别判断大小；某个尺度过大不影响其他尺度。
    sx, sy = 0, 0
    if region is not None:
        rx1, ry1, rx2, ry2 = region
        sx = max(0, int(rx1 * sw))
        sy = max(0, int(ry1 * sh))
        ex = min(sw, int(rx2 * sw))
        ey = min(sh, int(ry2 * sh))
        screen_roi = screen_gray[sy:ey, sx:ex]
    else:
        screen_roi = screen_gray

    rh, rw = screen_roi.shape[:2]
    best = None  # (score, left, top, width, height, scale)
    for scale in _candidate_scales(sw):
        tw = max(4, round(tpl_raw.shape[1] * scale))
        th = max(4, round(tpl_raw.shape[0] * scale))
        if tw > rw or th > rh:
            continue
        tpl = tpl_raw if (tw, th) == (tpl_raw.shape[1], tpl_raw.shape[0]) else cv2.resize(
            tpl_raw, (tw, th), interpolation=cv2.INTER_AREA
        )
        res = cv2.matchTemplate(screen_roi, tpl, _MATCH_METHOD)
        _, score, _, tl = cv2.minMaxLoc(res)
        if best is None or score > best[0]:
            best = (float(score), tl[0], tl[1], tw, th, scale)

    if best is None:
        log.warning("[模板] %s 所有尺度均大于搜索区域，无法匹配", template_name)
        return None

    score, left, top, tw, th, scale = best
    if score < threshold:
        log.info("[模板] %s 未命中：best=%.3f scale=%.2f threshold=%.2f",
                 template_name, score, scale, threshold)
        if save_fail:
            _save_fail_debug(screen_gray, template_name, score, threshold)
        return None

    cx = int(left + sx + tw / 2)
    cy = int(top + sy + th / 2)
    log.info("[模板] %s 命中：score=%.3f scale=%.2f center=(%d,%d)",
             template_name, score, scale, cx, cy)
    return cx, cy, score


def click_template(
    template_name: str,
    threshold: float = _DEFAULT_THRESHOLD,
    region: Optional[Tuple[float, float, float, float]] = None,
    sleep: float = 0.5,
    label: str = "",
    screen_gray: Optional[np.ndarray] = None,
) -> Optional[Tuple[int, int, float]]:
    """模板匹配 + adb tap。返回 (cx, cy, score) 或 None。"""
    pos = find_template(
        template_name, threshold=threshold, region=region,
        screen_gray=screen_gray,
    )
    if pos is None:
        return None
    cx, cy, score = pos
    ok = _adb_tap(cx, cy)
    if not ok:
        # adb tap 失败（少见），再试一次
        time.sleep(0.15)
        ok = _adb_tap(cx, cy)
    if ok:
        log.info("[模板✓] %s 匹配 score=%.3f → 点击 (%d,%d) %s",
                 template_name, score, cx, cy, f"[{label}]" if label else "")
        if sleep > 0:
            time.sleep(sleep)
        return pos
    log.warning("[模板] %s 匹配到 (%d,%d) 但 adb tap 失败", template_name, cx, cy)
    return None


# =====================================================================
#  便捷：四个常用按钮的 region 预设（直接用，省得每次传）
# =====================================================================

# 顶部栏（汉堡、新建对话、模式选择等都在这里）
TOP_BAR_REGION = (0.00, 0.03, 0.45, 0.22)

# 底部工具栏（键盘切换图标、语音输入等都在这里）
# ★ 之前设 (0.75, 0.98) 在 720×1280 上只有 294px 高，模板图稍大就被裁没了
BOTTOM_BAR_REGION = (0.00, 0.60, 1.00, 1.00)

# 抽屉区域（齿轮、退出登录等在抽屉内；抽屉实际宽约屏幕 88%，不能裁到 70%）
DRAWER_REGION = (0.00, 0.05, 0.90, 0.85)

# 常用按钮的一键点击封装
def click_open_drawer(label: str = "开抽屉") -> Optional[Tuple[int, int, float]]:
    """开左侧抽屉（汉堡按钮）—— 优先顶部栏左半区匹配。"""
    return click_template(
        "open_left_drawer", threshold=0.80,
        region=TOP_BAR_REGION, sleep=0.55, label=label,
    )

def click_open_new_chat(label: str = "新建对话") -> Optional[Tuple[int, int, float]]:
    """新建对话（顶部栏笔+方框图标）—— 顶部栏左半区。"""
    return click_template(
        "open_new_chat", threshold=0.80,
        region=TOP_BAR_REGION, sleep=0.55, label=label,
    )

def click_open_setting(label: str = "打开设置") -> Optional[Tuple[int, int, float]]:
    """设置齿轮 —— 必须抽屉已开，在左半屏搜。"""
    return click_template(
        "open_setting", threshold=0.80,
        region=DRAWER_REGION, sleep=0.55, label=label,
    )

def click_switch_keyboard(label: str = "切键盘") -> Optional[Tuple[int, int, float]]:
    """语音→键盘切换 —— 底部工具栏。"""
    return click_template(
        "switch_keyboard", threshold=0.80,
        region=BOTTOM_BAR_REGION, sleep=0.45, label=label,
    )


# =====================================================================
#  调试辅助
# =====================================================================

def _save_fail_debug(screen_gray: np.ndarray, name: str, score: float,
                     threshold: float) -> None:
    """匹配失败时把屏幕存到 screenshots/ 方便调试。"""
    try:
        from config import SCREENSHOT_DIR
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%H%M%S")
        out = SCREENSHOT_DIR / f"_tm_fail_{stamp}_{name}_s{score:.2f}_t{threshold:.2f}.png"
        cv2.imwrite(str(out), screen_gray)
        log.debug("调试截图已存：%s", out.name)
    except Exception:
        pass


def reset_cache() -> None:
    """模板图更新后调用此函数清缓存（或等进程重启）。"""
    _load_template_gray.cache_clear()
    log.info("模板缓存已清空")
