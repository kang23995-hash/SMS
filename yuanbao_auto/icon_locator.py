# -*- coding: utf-8 -*-
"""
icon_locator.py —— 基于 OpenCV 模板匹配的图标定位工具

背景：元宝 App 里有些按钮是纯图标（左上角抽屉按钮、抽屉右下角的设置齿轮），
没有 text / content-desc，靠 XML 语义或坐标比例定位都不稳。
本模块直接在屏幕截图上做模板匹配，找出图标像素中心坐标与置信度，
交给 yuanbao_login.py 精准点击。

核心：cv2.matchTemplate(TM_CCOEFF_NORMED) + 多尺度扫描
（图标模板若来自不同分辨率截图，0.7~1.2 逐档缩放取最优）。

用法：
    from icon_locator import find_icon, OPEN_DRAWER_ICON
    hit = find_icon(d, OPEN_DRAWER_ICON, roi=(0, 0, 0.3, 0.12))
    if hit:
        x, y, score = hit
        d.click(x, y)
"""

from __future__ import annotations

import logging
import os

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 图标模板目录（用户标注图中裁剪出的纯图标，无红圈/标注残留）
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuanbao_button_icons")
OPEN_DRAWER_ICON = os.path.join(ICON_DIR, "open_left_drawer_icon.png")   # 左上角抽屉按钮
SETTING_ICON = os.path.join(ICON_DIR, "the_setting_icon.png")            # 抽屉内设置齿轮

# 默认参数
DEFAULT_THRESHOLD = 0.80                      # 归一化相关置信度阈值
DEFAULT_SCALES = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2)


def _screen_gray(screenshot) -> np.ndarray:
    """PIL.Image / np.ndarray -> 灰度 np.ndarray。"""
    if hasattr(screenshot, "convert"):          # PIL.Image
        screenshot = np.array(screenshot.convert("RGB"))
    arr = np.asarray(screenshot)
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return arr


def _crop_roi(gray: np.ndarray, roi):
    """按比例裁剪搜索区域 roi=(x1,y1,x2,y2)；返回 (裁剪图, 偏移x, 偏移y)。"""
    if not roi:
        return gray, 0, 0
    h, w = gray.shape[:2]
    x1 = max(0, min(int(w * roi[0]), w - 1))
    y1 = max(0, min(int(h * roi[1]), h - 1))
    x2 = max(x1 + 1, min(int(w * roi[2]), w))
    y2 = max(y1 + 1, min(int(h * roi[3]), h))
    return gray[y1:y2, x1:x2], x1, y1


def locate_icons(d, template_path, threshold=DEFAULT_THRESHOLD,
                 scales=DEFAULT_SCALES, roi=None, screenshot=None, topk=3) -> list:
    """多尺度模板匹配，返回候选列表 [{'x','y','score','scale','w','h'}]（按置信度降序）。

    参数：
      d              uiautomator2 设备（screenshot=None 时用它取图）
      template_path  图标模板路径
      threshold      置信度阈值，低于此值的候选直接丢弃
      scales         缩放档位（模板分辨率与屏幕不符时逐档尝试）
      roi            限定搜索区域 (x1,y1,x2,y2) 比例值，如 (0,0,0.3,0.12)；
                     限制区域能显著减少误匹配（抽屉按钮只在左上角）
      screenshot     复用外部截图（避免重复取图）
      topk           最多返回的候选个数（各尺度合并后去重）
    """
    tpl0 = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE) if os.path.exists(template_path) else None
    if tpl0 is None:
        logger.error(f"图标模板缺失或不可读: {template_path}")
        return []

    if screenshot is None:
        screenshot = d.screenshot()
    gray = _screen_gray(screenshot)
    view, ox, oy = _crop_roi(gray, roi)
    vh, vw = view.shape[:2]

    cands = []
    for s in scales:
        tw = max(8, int(round(tpl0.shape[1] * s)))
        th = max(8, int(round(tpl0.shape[0] * s)))
        if th > vh or tw > vw:
            continue
        tpl = tpl0 if (tw, th) == (tpl0.shape[1], tpl0.shape[0]) else \
            cv2.resize(tpl0, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(view, tpl, cv2.TM_CCOEFF_NORMED)

        # 逐次取峰值，并把邻域压掉以便找次峰（一个尺度最多取 topk 个候选）
        for _ in range(min(topk, int(res.size))):
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val < threshold:
                break
            cands.append({
                "x": ox + max_loc[0] + tw // 2,
                "y": oy + max_loc[1] + th // 2,
                "score": float(max_val),
                "scale": s,
                "w": tw,
                "h": th,
            })
            x0 = max(0, max_loc[0] - tw // 2)
            y0 = max(0, max_loc[1] - th // 2)
            x1 = min(res.shape[1], max_loc[0] + tw // 2 + 1)
            y1 = min(res.shape[0], max_loc[1] + th // 2 + 1)
            res[y0:y1, x0:x1] = -1.0

    cands.sort(key=lambda c: c["score"], reverse=True)

    # 跨尺度去重：中心距离小于半个模板尺寸的视为同一目标
    dedup: list = []
    for c in cands:
        dup = False
        for k in dedup:
            r = 0.5 * max(c["w"], k["w"])
            if (c["x"] - k["x"]) ** 2 + (c["y"] - k["y"]) ** 2 <= r * r:
                dup = True
                break
        if not dup:
            dedup.append(c)
        if len(dedup) >= topk:
            break

    for i, c in enumerate(dedup, 1):
        logger.debug(f"候选{i}: ({c['x']},{c['y']}) score={c['score']:.3f} scale={c['scale']}")
    return dedup


def find_icon(d, template_path, threshold=DEFAULT_THRESHOLD, scales=DEFAULT_SCALES,
              roi=None, screenshot=None):
    """取置信度最高的匹配，返回 (x, y, score)；无命中返回 None。"""
    cands = locate_icons(d, template_path, threshold=threshold, scales=scales,
                         roi=roi, screenshot=screenshot, topk=1)
    if not cands:
        return None
    best = cands[0]
    return best["x"], best["y"], best["score"]


def save_match_debug(screenshot, matches, out_path) -> str:
    """把匹配框画在截图上存盘，便于人工核对（matches 为 locate_icons 结果）。"""
    if hasattr(screenshot, "convert"):
        screenshot = np.array(screenshot.convert("RGB"))
    img = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2BGR).copy()
    for i, m in enumerate(matches, 1):
        x0 = m["x"] - m["w"] // 2
        y0 = m["y"] - m["h"] // 2
        x1 = m["x"] + m["w"] // 2
        y1 = m["y"] + m["h"] // 2
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 255), 2)
        cv2.putText(img, f"{i}:{m['score']:.3f}", (x0, max(12, y0 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        cv2.circle(img, (m["x"], m["y"]), 4, (0, 255, 0), -1)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img)
    logger.info(f"匹配调试图已保存: {out_path}")
    return out_path
