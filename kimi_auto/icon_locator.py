# -*- coding: utf-8 -*-
"""
icon_locator.py —— 纯视觉全屏 OpenCV 模板匹配
==================================================

Kimi Auto 的图标定位方案（按用户明确要求）：
  - **全屏幕扫描**（无 ROI、无坐标预估值）
  - **多尺度匹配**（解决模板与实际渲染尺寸差异）
  - **灰度 TM_CCOEFF_NORMED**（抗色偏/对比度变化）
  - **取 score 最高命中的像素中心直接点击**

用法：
    from icon_locator import find_icon, NEW_CONVERSATION_ICON
    hit = find_icon(d, NEW_CONVERSATION_ICON, threshold=0.75)
    if hit:
        x, y, score = hit
        d.click(x, y)

⚠️ 没有 opencv-python 时自动返回 None（不崩主流程）。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模板目录（用户提供的图标截图，从真机原生分辨率裁剪）
# ---------------------------------------------------------------------------
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kimi_button_icons")

# ---- 模板常量（kimi_worker 直接 import）----
# 抽屉展开后右上角的圆形 + 号（新建对话）—— 由用户从真机截图裁剪提供
NEW_CONVERSATION_ICON = os.path.join(ICON_DIR, "new_conversation_icon.png")

# 默认多尺度档位（覆盖常见渲染差异：模板 44×44 在真机 1080×1920 上实测最佳 scale=1.0）
DEFAULT_SCALES = (0.7, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.3)

# ---------------------------------------------------------------------------
# OpenCV 可用性检测（延迟 import 防启动崩）
# ---------------------------------------------------------------------------
try:
    import cv2                        # type: ignore[import-untyped]
    import numpy as np                # type: ignore[import-untyped]
    _CV_OK = True
except Exception as _cv_err:         # noqa: BLE001
    cv2 = None                        # type: ignore[assignment]
    np = None                         # type: ignore[assignment]
    _CV_OK = False
    logger.warning(
        f"OpenCV 不可用（{_cv_err}），图标模板匹配跳过。pip install opencv-python"
    )


# =====================================================================
#  核心：纯视觉全屏扫描 → 取 score 最高命中
# =====================================================================

def find_icon(d, template_path, threshold=0.75, scales=DEFAULT_SCALES,
              screenshot=None):
    """全屏幕多尺度模板匹配，返回 **最高分命中** 的像素中心 (x, y, score)；
    无命中 / OpenCV 不可用 / 模板缺失均返回 None。

    参数：
      d              uiautomator2 设备
      template_path  模板图路径
      threshold      置信度阈值（TM_CCOEFF_NORMED 范围 [0,1]，低于此值丢弃）
      scales         多尺度档位（模板渲染尺寸差异的容错）
      screenshot     复用外部截图（避免重复取图）

    实机验证数据（Kimi 抽屉 + 号，模板 44×44）：
      真实按钮在 (788, 80)，scale=1.0 匹配分数 1.0000
      阈值 0.75 足够过滤噪声，也能容忍小幅渲染差异
    """
    if not _CV_OK:
        logger.debug("OpenCV 不可用，跳过图标匹配")
        return None
    if not os.path.exists(template_path):
        logger.error(f"模板文件缺失: {template_path}")
        return None

    # ---- 读模板 ----
    tpl0 = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
    if tpl0 is None:
        logger.error(f"模板不可读: {template_path}")
        return None

    # ---- 取屏幕截图 ----
    if screenshot is None:
        try:
            screenshot = d.screenshot()
        except Exception as exc:         # noqa: BLE001
            logger.error(f"取屏幕失败: {exc}")
            return None

    # ---- 转灰度 ----
    if hasattr(screenshot, "convert"):
        arr = np.array(screenshot.convert("RGB"))
    else:
        arr = np.asarray(screenshot)
    if arr.ndim == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    else:
        gray = arr
    sh, sw = gray.shape[:2]

    # ---- 多尺度匹配 ----
    best = None   # (score, x, y)
    for s in scales:
        tw = max(8, int(round(tpl0.shape[1] * s)))
        th = max(8, int(round(tpl0.shape[0] * s)))
        if th > sh or tw > sw:
            continue
        tpl = tpl0 if (tw, th) == (tpl0.shape[1], tpl0.shape[0]) else \
            cv2.resize(tpl0, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)

        if max_val >= threshold:
            cx = max_loc[0] + tw // 2
            cy = max_loc[1] + th // 2
            logger.debug(f"  scale={s:.2f}  score={max_val:.4f}  pos=({cx},{cy})")
            if best is None or max_val > best[0]:
                best = (float(max_val), cx, cy)

    if best is None:
        logger.debug(f"阈值 {threshold} 下未命中任何候选")
        return None

    score, x, y = best
    return x, y, score


def find_all_icons(d, template_path, threshold=0.75, scales=DEFAULT_SCALES,
                   screenshot=None, topk=5):
    """取 score 最高的 topk 个候选（跨尺度去重后）。

    返回列表 [(x, y, score), ...]，按 score 降序。
    场景：需要定位多个同名按钮时（比如主界面多个相同图标）。
    """
    if not _CV_OK:
        return []
    if not os.path.exists(template_path):
        return []

    tpl0 = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
    if tpl0 is None:
        return []

    if screenshot is None:
        try:
            screenshot = d.screenshot()
        except Exception:
            return []

    if hasattr(screenshot, "convert"):
        arr = np.array(screenshot.convert("RGB"))
    else:
        arr = np.asarray(screenshot)
    if arr.ndim == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    else:
        gray = arr
    sh, sw = gray.shape[:2]

    raw = []
    for s in scales:
        tw = max(8, int(round(tpl0.shape[1] * s)))
        th = max(8, int(round(tpl0.shape[0] * s)))
        if th > sh or tw > sw: continue
        tpl = tpl0 if (tw, th) == (tpl0.shape[1], tpl0.shape[0]) else \
            cv2.resize(tpl0, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        # 每个尺度取 topk 个（每次取峰后压周围区域）
        res_copy = res.copy()
        for _ in range(topk):
            _, v, _, loc = cv2.minMaxLoc(res_copy)
            if v < threshold: break
            cx, cy = loc[0] + tw//2, loc[1] + th//2
            raw.append((float(v), cx, cy))
            res_copy[max(0,loc[1]-th//2):min(sh,loc[1]+th//2+1),
                     max(0,loc[0]-tw//2):min(sw,loc[0]+tw//2+1)] = -1.0

    # 跨尺度去重（中心距离 < 模板宽度的一半视为同一目标）
    raw.sort(reverse=True)
    dedup = []
    for v, x, y in raw:
        dup = any(abs(x - dx) + abs(y - dy) < tpl0.shape[1]
                  for _, dx, dy in dedup)
        if not dup:
            dedup.append((x, y, v))
        if len(dedup) >= topk: break

    return dedup


def save_match_debug(screenshot, matches, out_path) -> str:
    """把匹配框画在截图上存盘，便于人工核对。"""
    if not _CV_OK:
        return ""
    if hasattr(screenshot, "convert"):
        screenshot = np.array(screenshot.convert("RGB"))
    img = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2BGR).copy()
    for i, (x, y, score) in enumerate(matches, 1):
        # 估算框大小（40px，近似真实按钮尺寸）
        s = 25
        cv2.rectangle(img, (x-s, y-s), (x+s, y+s), (0, 0, 255), 2)
        cv2.circle(img, (x, y), 4, (0, 255, 0), -1)
        cv2.putText(img, f"#{i}:{score:.3f}", (x-s, max(12, y-s-4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img)
    return out_path
