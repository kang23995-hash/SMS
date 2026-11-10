# -*- coding: utf-8 -*-
"""
kimi_login.py —— Kimi App 登出 / 登录 UI 自动化
================================================================

严格对应用户标注截图（①~⑩）与实机实测（1080x1920 竖屏）整理出的步骤：

  【登出】（前提：当前处于已登录状态）
    ② 点左上角「三横线」按钮（content-desc=导航按钮）打开左侧抽屉
    ① 点抽屉左上角的「头像 + 昵称」进入设置页
    ④ 在设置页往下滑到底，点「退出登录」
    ⑤ 弹窗中再点「退出登录」确认
    ③ （若本来就没登录）主页点「去登录」进入登录落地页

  【登录】
    ⑥ 登录落地页点「手机号登录」
    ⑦ 「用户协议及隐私协议」弹窗点「同意并继续」
    ⑧ 手机号输入页填入手机号（前缀 +86，hint「请输入手机号」）
    ⑨ 点「验证并登录」→ 短信直接发出（**Kimi 没有图形人机验证**）
    ⑩ 验证码页（标题「输入 6 位验证码」）填入轮询到的 6 位短信验证码

设计原则：
  * 查找一律「content-desc / text 优先，比例坐标兜底」——Kimi 是 Jetpack Compose
    应用，几乎没有 resource-id，content-desc 是唯一稳定的语义锚点；
  * **每一步点击后都要校验结果**，失败自动重试（对用户「登出/登录要稳」的要求）；
  * Kimi 无人机验证 → 不引入 captcha_solver / 千问大模型。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Optional, Sequence

from config import (
    KIMI_PACKAGE_CANDIDATES,
    KIMI_NAV_DESC,
    KIMI_DRAWER_AVATAR_DESC,
    KIMI_DRAWER_SEARCH_WORD,
    KIMI_SETTINGS_TITLE,
    KIMI_LOGOUT_WORD,
    KIMI_LOGOUT_DIALOG_TEXT,
    KIMI_LOGIN_ENTRY_WORDS,
    KIMI_LANDING_WORDS,
    KIMI_PHONE_LOGIN_WORD,
    KIMI_AGREE_WORDS,
    KIMI_AGREE_DIALOG_TITLE,
    KIMI_PHONE_INPUT_HINT,
    KIMI_VERIFY_BTN_WORD,
    KIMI_VCODE_TITLE,
    KIMI_VCODE_SUBTITLE_PREFIX,
    KIMI_VCODE_RESEND_PATTERN,
    KIMI_LOGGED_IN_HINTS,
)

logger = logging.getLogger(__name__)

# 运行时探测到的真实包名（首次连接设备后填充，见 kimi_package()）
_DETECTED_PACKAGE: Optional[str] = None

# 登录后常见干扰弹窗按钮（通知权限 / 更新提示 / 引导等），统一关闭
DISMISS_WORDS = ("以后再说", "暂不", "跳过", "我知道了", "稍后再说",
                 "不允许", "取消", "关闭")


# =====================================================================
# 基础工具
# =====================================================================

def run_adb(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """调用 adb 命令。如果已知设备序列号且命令不是 devices/connect，自动加 -s。"""
    no_device_cmds = {"devices", "connect", "disconnect", "start-server", "kill-server"}
    first_arg = str(args[0]) if args else ""
    if first_arg not in no_device_cmds and _device_serial:
        cmd = ["adb", "-s", _device_serial, *map(str, args)]
    else:
        cmd = ["adb", *map(str, args)]
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
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
    devices = []
    for line in result.stdout.decode("utf-8", errors="ignore").splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    if not devices:
        raise RuntimeError("没连到任何 Android 设备 / 模拟器")

    # 优先匹配配置的端口（"5554" in serial 同时命中 emulator-5554 和 127.0.0.1:5554）
    if port and port > 0:
        target = str(port)
        for serial in devices:
            if target in serial:
                logger.info(f"匹配到端口 {port} 的设备: {serial}")
                _device_serial = serial
                return serial
        logger.warning(f"没找到端口 {port} 对应的设备，回退自动取第一台")

    serial = devices[0]
    logger.info(f"检测到设备: {serial}")
    _device_serial = serial
    return serial


def connect_phone():
    """uiautomator2 连接设备。"""
    import uiautomator2 as u2
    serial = get_device_serial()
    logger.info(f"uiautomator2 连接: {serial}")
    d = u2.connect(serial)
    info = d.info
    logger.info(f"已连接: {info.get('productName') or ''} "
                f"{info.get('displayWidth')}x{info.get('displayHeight')}")
    return d


def dump_xml(d) -> str:
    """导出当前界面 XML。

    ⚠️ Kimi 是 Compose 应用，界面常驻光标闪烁 / 动画，`adb shell uiautomator dump`
    会频繁报 "could not get idle state"；而 uiautomator2 的 dump_hierarchy
    对此更宽容，因此统一走 uiautomator2，并带 3 次重试。
    """
    for attempt in range(3):
        try:
            xml = d.dump_hierarchy(compressed=False)
            if xml:
                return xml
        except Exception as exc:
            logger.debug(f"dump_hierarchy 第 {attempt + 1} 次失败: {exc}")
        time.sleep(0.6)
    return ""


def kimi_package() -> str:
    """探测 Kimi 真实包名（候选列表 + 当前前台包名兜底）。"""
    global _DETECTED_PACKAGE
    if _DETECTED_PACKAGE:
        return _DETECTED_PACKAGE
    for pkg in KIMI_PACKAGE_CANDIDATES:
        try:
            out = run_adb("shell", "pm", "list", "packages", pkg, timeout=15)
            if pkg in out.stdout.decode("utf-8", errors="ignore"):
                _DETECTED_PACKAGE = pkg
                logger.info(f"检测到 Kimi 包名: {pkg}")
                return pkg
        except Exception:
            continue
    logger.warning(f"未在候选列表中找到 Kimi，使用默认包名 {KIMI_PACKAGE_CANDIDATES[0]}")
    _DETECTED_PACKAGE = KIMI_PACKAGE_CANDIDATES[0]
    return _DETECTED_PACKAGE


def current_is_kimi(d) -> bool:
    try:
        cur = d.app_current().get("package", "")
        return cur in KIMI_PACKAGE_CANDIDATES
    except Exception:
        return False


def start_kimi(d, wait=5, retries=3) -> None:
    """把 Kimi 拉到前台（不杀进程）。

    ⚠️ 不能只判断一次前台：App 冷启动 / 从后台恢复时会有空窗期，
    必须轮询并在 **连续两次** 确认前台后才返回。
    """
    pkg = kimi_package()
    logger.info(f"拉起 Kimi App（{pkg}）")
    for attempt in range(retries):
        try:
            d.app_start(pkg, stop=False)
        except Exception as exc:
            logger.warning(f"app_start 异常: {exc}")
        deadline = time.time() + wait
        while time.time() < deadline:
            if current_is_kimi(d):
                time.sleep(1.2)
                if current_is_kimi(d):
                    return
            time.sleep(0.8)
        logger.warning(f"第 {attempt + 1} 次拉起 Kimi 未确认前台，重试")
    raise RuntimeError("Kimi 没有进入前台")


def force_reset(d) -> None:
    """杀进程（登出 / 重登前的彻底复位）。"""
    try:
        d.app_stop(kimi_package())
        time.sleep(2)
    except Exception:
        pass


force_logout_if_needed = force_reset   # 与其它项目接口名保持一致


# =====================================================================
# 节点解析 / 查找 / 点击
# =====================================================================

def _bounds_to_tuple(bounds: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    return tuple(map(int, m.groups()))


def _parse_nodes(xml: str) -> list:
    """把 XML 解析成节点列表（只保留位置/文本/desc/可点击性）。"""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter("node"):
        b = _bounds_to_tuple(node.attrib.get("bounds", ""))
        if not b:
            continue
        out.append({
            "text": (node.attrib.get("text") or "").strip(),
            "desc": (node.attrib.get("content-desc") or "").strip(),
            "rid": node.attrib.get("resource-id", ""),
            "class": node.attrib.get("class", ""),
            "bounds": b,
            "clickable": node.attrib.get("clickable") == "true",
            "enabled": node.attrib.get("enabled", "true") != "false",
            "visible": node.attrib.get("visible-to-user", "true") != "false",
            "scrollable": node.attrib.get("scrollable") == "true",
        })
    return out


def _screen_size(d) -> tuple:
    try:
        return int(d.info["displayWidth"]), int(d.info["displayHeight"])
    except Exception:
        return 1080, 1920


def _find_node(nodes: list, *, text=None, desc=None, text_contains=None,
               desc_contains=None, require_clickable=False) -> Optional[dict]:
    """按 text / content-desc 精确或包含匹配查找节点，返回面积最小的候选。"""
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
        if require_clickable and not n["clickable"]:
            continue
        b = n["bounds"]
        cands.append(((b[2] - b[0]) * (b[3] - b[1]), n))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def _click_point_owner(d, nodes: list, point: tuple) -> bool:
    """点击坐标：优先点击「包含该坐标的最小可点击节点」的中心。

    Compose 里按钮常包在可点击父容器中，直接按子节点中心点击也能命中父容器，
    但显式找到可点击祖先更稳妥。
    """
    px, py = point
    owners = []
    for n in nodes:
        if not n["clickable"] or not n["enabled"]:
            continue
        x1, y1, x2, y2 = n["bounds"]
        if x1 <= px <= x2 and y1 <= py <= y2:
            owners.append(((x2 - x1) * (y2 - y1), n))
    if owners:
        owners.sort(key=lambda x: x[0])
        b = owners[0][1]["bounds"]
        d.click((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    else:
        d.click(px, py)
    return True


def _click_node(d, nodes: list, node: dict) -> bool:
    """点击节点：先尝试可点击祖先，再退回节点中心坐标。"""
    x1, y1, x2, y2 = node["bounds"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    try:
        if node["clickable"]:
            d.click(cx, cy)
        else:
            _click_point_owner(d, nodes, (cx, cy))
        return True
    except Exception as exc:
        logger.debug(f"点击节点失败: {exc}")
        return False


def _click_text(d, words: Sequence[str], *, exact=True, wait=0.8) -> bool:
    """依次按 text 精确/包含匹配一组候选词点击。"""
    nodes = _parse_nodes(dump_xml(d))
    for w in words:
        n = _find_node(nodes, text=w) if exact else _find_node(nodes, text_contains=w)
        if n and _click_node(d, nodes, n):
            time.sleep(wait)
            logger.info(f"已点击 text={w!r}")
            return True
    return False


def _click_desc(d, desc: str, *, contains=False, wait=0.8) -> bool:
    """按 content-desc 精确/包含匹配点击。"""
    nodes = _parse_nodes(dump_xml(d))
    n = (_find_node(nodes, desc_contains=desc) if contains
         else _find_node(nodes, desc=desc))
    if n and _click_node(d, nodes, n):
        time.sleep(wait)
        logger.info(f"已点击 desc={desc!r}")
        return True
    return False


def _click_xy(d, x: int, y: int, wait=0.8) -> bool:
    try:
        d.click(int(x), int(y))
        time.sleep(wait)
        return True
    except Exception:
        return False


def _swipe_up(d, duration=0.4):
    w, h = _screen_size(d)
    d.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.28), duration=duration)
    time.sleep(0.5)


def _retry(fn, *, attempts=3, wait=1.0, name="") -> bool:
    """把「点击 + 校验」当成一个原子动作重试，直到成功或次数耗尽。"""
    for i in range(attempts):
        try:
            if fn():
                return True
        except Exception as exc:
            logger.debug(f"{name} 第 {i + 1} 次异常: {exc}")
        time.sleep(wait)
    logger.warning(f"{name} 重试 {attempts} 次仍未成功")
    return False


def _dismiss_popups(d, rounds=2) -> None:
    """登录后常见干扰弹窗（通知权限 / 更新提示等）统一关闭。"""
    import time as _t
    for _ in range(rounds):
        xml = dump_xml(d)
        hit = False
        for w in DISMISS_WORDS:
            if w in xml and _click_text(d, (w,)):
                hit = True
                break
        if not hit:
            break
        _t.sleep(0.8)


# =====================================================================
# 登录状态 / 抽屉 判断
# =====================================================================

def is_logged_in(d) -> bool:
    """已登录判定。

    关键陷阱：Kimi **未登录主页**上也有「导航按钮」(content-desc) 和
    「尽管问，带图也行」(输入框 hint)，恰好命中 KIMI_LOGGED_IN_HINTS。
    所以必须用「去登录」按钮作为**硬性排除条件**——只要有「去登录」，
    一定是未登录状态，不管其他特征词有没有命中。
    """
    xml = dump_xml(d)
    if not xml:
        return False
    # 硬排除：有「去登录」按钮 → 100% 未登录主页
    if any(w in xml for w in KIMI_LOGIN_ENTRY_WORDS):
        return False
    # 否则看已登录主界面特征
    return any(h in xml for h in KIMI_LOGGED_IN_HINTS)


def is_on_login_entry(d) -> bool:
    """未登录主页：有「去登录」按钮，且没有登录主界面特征。"""
    xml = dump_xml(d)
    return any(w in xml for w in KIMI_LOGIN_ENTRY_WORDS) and KIMI_VERIFY_BTN_WORD not in xml


def is_on_landing(d) -> bool:
    """登录落地页：同时出现「微信登录」和「手机号登录」两个入口。"""
    xml = dump_xml(d)
    return all(w in xml for w in KIMI_LANDING_WORDS)


def is_on_phone_page(d) -> bool:
    """手机号输入页：标题「手机号登录」+ 按钮「验证并登录」。"""
    xml = dump_xml(d)
    return KIMI_VERIFY_BTN_WORD in xml and KIMI_PHONE_INPUT_HINT in xml


def is_on_vcode_page(d) -> bool:
    """验证码页判定。

    标题 / 副标题 / 重新发送倒计时 任一命中即算在验证码页。
    放宽判据避免关键词不匹配导致「明明在验证码页却被判不在」
    （进而让 _input_vcode 的 _entered() 误返回 True）。

    ⚠️ 排除滑块拼图页（也含"验证码"字样，如"易盾验证码"/"验证码滑块"），
       否则会把滑块页误判为验证码页。
    """
    xml = dump_xml(d)
    if not xml:
        return False
    # 滑块页硬性排除
    if any(kw in xml for kw in ("易盾", "滑块", "拼图", "拖动滑块", "安全验证")):
        return False
    return any(kw in xml for kw in (
        KIMI_VCODE_TITLE,
        KIMI_VCODE_SUBTITLE_PREFIX,
        "重新发送",
        "后重新发送",
        "输入验证码",
        "6位验证码",
    ))


def is_drawer_open(d) -> bool:
    """左侧抽屉是否张开。

    判定锚点：抽屉里才有「用户头像」，以及底部的「搜索」框。
    主界面（首页）没有「用户头像」这个 content-desc，因此最稳。
    """
    xml = dump_xml(d)
    return KIMI_DRAWER_AVATAR_DESC in xml and KIMI_DRAWER_SEARCH_WORD in xml


def close_drawer_if_open(d) -> bool:
    """若左侧抽屉张开则关闭它（点抽屉右侧的空白遮罩），未张开直接返回 True。"""
    if not is_drawer_open(d):
        return True
    w, h = _screen_size(d)
    for _ in range(3):
        # 抽屉面板实测占 [0,0][918,1920]，右侧遮罩区点一下即可关闭
        _click_xy(d, int(w * 0.96), int(h * 0.5), wait=0.9)
        if not is_drawer_open(d):
            logger.info("✅ 左侧抽屉已关闭")
            return True
    logger.warning("抽屉未能关闭（不影响主流程，继续）")
    return False


# =====================================================================
# 登出（②①④⑤）
# =====================================================================

def _open_drawer(d) -> bool:
    """② 点左上角「导航按钮」打开左侧抽屉。"""
    if is_drawer_open(d):
        return True
    nodes = _parse_nodes(dump_xml(d))
    n = _find_node(nodes, desc=KIMI_NAV_DESC)
    if n and _click_node(d, nodes, n):
        time.sleep(1.2)
    else:
        # 坐标兜底：实测 1080x1920 下 (114,138)
        w, h = _screen_size(d)
        _click_xy(d, int(w * 0.105), int(h * 0.072), wait=1.2)
        logger.info("已按坐标点击左上角导航按钮")
    return is_drawer_open(d)


# 设置页一进入就有的条目（用来验证是否真的在设置页，不依赖底部的退出登录）
_SETTINGS_PRIME_WORDS = ("账号安全", "通知", "外观", "语音播报", "功能管理",
                         "我的订阅", "额度和发票", "礼品卡")


def _enter_settings(d) -> bool:
    """① 点抽屉左上角「头像 + 昵称」进入设置页。

    头像 content-desc=用户头像（bounds [48,114][168,234]），它的可点击父容器
    覆盖左上角一块区域，因此可以直接点头像中心（触达父容器）。

    验证条件：**只看设置页首屏特征词**（不依赖底部的退出登录是否可见），
    避免"页面已经在设置页但验证条件不满足导致反复重试点头像"的问题。
    """
    nodes = _parse_nodes(dump_xml(d))
    n = _find_node(nodes, desc=KIMI_DRAWER_AVATAR_DESC)
    if n:
        _click_node(d, nodes, n)
    else:
        # 坐标兜底：头像区中心 (108,174) / 放大到父容器 (300,174)
        w, h = _screen_size(d)
        _click_xy(d, int(w * 0.28), int(h * 0.091), wait=1.5)
        logger.info("已按坐标点击抽屉头像区")
    time.sleep(1.8)
    xml = dump_xml(d)
    # 设置页验证：标题 "设置" + 至少一个首屏条目（退出登录在底部，首屏没有）
    if KIMI_SETTINGS_TITLE not in xml:
        return False
    return any(w in xml for w in _SETTINGS_PRIME_WORDS)


def _find_logout_on_settings(d) -> bool:
    """在设置页下滑到底部，找到并点击「退出登录」。

    流程：
      1. 先等设置页稳定加载（标题 + 首屏特征词出现）
      2. 先直接尝试点击（万一退出登录就在首屏）
      3. 循环上滑，每滑一次就尝试点击 + 检测是否已到底
      4. 滑到底判定：连续 3 次 XML 长度变化 < 5
    """
    # --- 1. 等待设置页稳定加载 ---
    for _ in range(5):
        xml = dump_xml(d)
        if KIMI_SETTINGS_TITLE in xml and any(w in xml for w in _SETTINGS_PRIME_WORDS):
            break
        time.sleep(0.5)

    # --- 2. 先直接尝试点击 ---
    if _click_text(d, (KIMI_LOGOUT_WORD,), wait=1.5):
        logger.info("设置页首屏已找到并点击「退出登录」")
        return True

    # --- 3. 循环上滑 + 滑到底检测 ---
    same_rounds = 0
    prev_xml_len = len(dump_xml(d))
    for i in range(15):
        _swipe_up(d, duration=0.45)
        time.sleep(0.7)

        if _click_text(d, (KIMI_LOGOUT_WORD,), wait=1.5):
            logger.info(f"第 {i + 1} 次下滑后点击「退出登录」成功")
            return True

        # 检测是否滑到底（XML 长度连续变化很小 → 列表已无新项）
        cur_xml_len = len(dump_xml(d))
        if abs(cur_xml_len - prev_xml_len) < 5:
            same_rounds += 1
            if same_rounds >= 3:
                logger.warning(f"已滑到底部仍未找到「退出登录」（共下滑 {i + 1} 次）")
                break
        else:
            same_rounds = 0
        prev_xml_len = cur_xml_len

    # --- 4. 兜底：滑到底后再试一次（避免刚到边界差了一下）---
    if _click_text(d, (KIMI_LOGOUT_WORD,), wait=1.5):
        logger.info("滑到底部后最终一次点击「退出登录」成功")
        return True

    logger.error("设置页滑到底部仍未找到「退出登录」按钮")
    return False


def _confirm_logout_dialog(d) -> bool:
    """⑤ 弹出确认框后点其中的「退出登录」。

    弹窗里同一屏有两个「退出登录」文案（页面底部按钮 + 弹窗按钮），
    必须点弹窗里的那个。锚点策略：找到弹窗里的「取消」按钮，
    与它同一行且位于其左侧的「退出登录」即为弹窗按钮。
    """
    nodes = _parse_nodes(dump_xml(d))
    cancel = _find_node(nodes, text="取消")
    logout_nodes = [n for n in nodes
                    if n["text"] == KIMI_LOGOUT_WORD]
    if cancel and logout_nodes:
        cy = (cancel["bounds"][1] + cancel["bounds"][3]) // 2
        same_row = [n for n in logout_nodes
                    if abs(((n["bounds"][1] + n["bounds"][3]) // 2) - cy) <= 60
                    and n["bounds"][0] < cancel["bounds"][0]]
        if same_row:
            same_row.sort(key=lambda n: -n["bounds"][0])   # 取最靠右的（紧邻取消）
            _click_node(d, nodes, same_row[0])
            time.sleep(1.5)
            logger.info("已点击弹窗中的「退出登录」")
            return True
    # 兜底 1：弹窗按钮比页面底部按钮「层级更靠上」，取面积最小的那个
    if logout_nodes:
        logout_nodes.sort(key=lambda n: (n["bounds"][2] - n["bounds"][0]) *
                                        (n["bounds"][3] - n["bounds"][1]))
        _click_node(d, nodes, logout_nodes[0])
        time.sleep(1.5)
        return True
    # 兜底 2：直接按「取消」左侧点击
    if cancel:
        x1, y1, x2, y2 = cancel["bounds"]
        _click_xy(d, (x1 + x2) // 2 - int((x2 - x1) * 1.6), (y1 + y2) // 2, wait=1.5)
        return True
    return False


def logout(d) -> bool:
    """退出登录全流程（② → ① → ④ → ⑤），每一步点击后校验，失败自动重试。"""
    logger.info("===== 开始登出 Kimi =====")

    start_kimi(d)
    if not is_logged_in(d):
        logger.info("当前已是未登录状态，无需登出")
        return True

    # ---- ② 打开左侧抽屉 ----
    if not _retry(lambda: _open_drawer(d), attempts=3, wait=1.5, name="打开抽屉"):
        raise RuntimeError("无法打开左侧抽屉（导航按钮）")

    # ---- ① 点头像进设置页 ----
    if not _retry(lambda: _enter_settings(d), attempts=3, wait=1.5, name="进入设置页"):
        raise RuntimeError("无法从抽屉头像进入设置页")

    # ---- ④ 下滑找并点击「退出登录」----
    if not _retry(lambda: _find_logout_on_settings(d), attempts=3, wait=1.5,
                  name="点击设置页退出登录"):
        raise RuntimeError("设置页找不到「退出登录」")

    # ---- ⑤ 弹窗确认 ----
    for _ in range(6):
        xml = dump_xml(d)
        if KIMI_LOGOUT_DIALOG_TEXT in xml or "确认" in xml:
            _confirm_logout_dialog(d)
            time.sleep(1.5)
        # 弹窗点击后校验结果：已退出登录 → 回到未登录主页
        if not is_logged_in(d):
            logger.info("✅ 已退出登录")
            return True
        _confirm_logout_dialog(d)
        time.sleep(1.0)

    if not is_logged_in(d):
        logger.info("✅ 已退出登录")
        return True
    logger.warning("登出流程跑完但仍检测为已登录")
    return False


# =====================================================================
# 登录（③⑥⑦⑧⑨⑩）
# =====================================================================

def _goto_login_entry(d) -> bool:
    """③ 未登录主页点「去登录」，或在落地页/手机号页时直接视为已到达。"""
    if is_on_landing(d) or is_on_phone_page(d) or is_on_vcode_page(d):
        return True
    if _click_text(d, KIMI_LOGIN_ENTRY_WORDS, wait=1.5):
        logger.info("已点击主页「去登录」")
        return True
    return is_on_landing(d) or is_on_phone_page(d)


def _click_phone_login(d) -> bool:
    """⑥ 落地页点「手机号登录」→ ⑦ 协议弹窗点「同意并继续」。

    点完「手机号登录」后可能直接进手机号输入页，也可能先弹协议框；
    两种情况都在这里处理并校验最终到达手机号输入页。
    """
    if is_on_phone_page(d):
        return True
    if _click_text(d, (KIMI_PHONE_LOGIN_WORD,), wait=1.5):
        logger.info("已点击「手机号登录」")
    time.sleep(1.0)
    # ⑦ 协议弹窗
    xml = dump_xml(d)
    if KIMI_AGREE_DIALOG_TITLE in xml or "已阅读同意" in xml:
        if _click_text(d, KIMI_AGREE_WORDS, wait=1.5):
            logger.info("已点击协议弹窗「同意并继续」")
        else:
            logger.warning("协议弹窗出现但没找到「同意并继续」")
    time.sleep(1.2)
    return is_on_phone_page(d)


def _input_phone(d, phone: str) -> bool:
    """⑧ 手机号输入页填入手机号。"""
    nodes = _parse_nodes(dump_xml(d))
    target = None
    # 优先 hint="请输入手机号" 的 EditText
    for n in nodes:
        if "EditText" in n["class"] and KIMI_PHONE_INPUT_HINT in n["text"]:
            target = n
            break
    if target is None:
        for n in nodes:
            if "EditText" in n["class"]:
                target = n
                break
    if target is None:
        return False

    _click_node(d, nodes, target)
    time.sleep(0.4)
    try:
        obj = d(className="android.widget.EditText")
        if obj.exists(timeout=1):
            try:
                obj.clear_text()
            except Exception:
                pass
            obj.set_text(str(phone))
            logger.info(f"已填入手机号 {phone}")
            return _verify_phone_filled(d, phone)
    except Exception as exc:
        logger.debug(f"set_text 填手机号失败: {exc}")

    # 兜底：走 adb 直接输入
    try:
        d.set_fastinput_ime(True)
        d.send_keys(str(phone), clear=True)
        logger.info(f"已用 fastinput 填入手机号 {phone}")
    finally:
        try:
            d.set_fastinput_ime(False)
        except Exception:
            pass
    return _verify_phone_filled(d, phone)


def _verify_phone_filled(d, phone: str) -> bool:
    """校验手机号已真正写入输入框。

    Compose 的 EditText 有时不把 text 属性正确 dump 到 XML，
    所以不能只靠 `phone in xml`。改用双重判据：
      a) XML 里 hint「请输入手机号」消失（说明输入框已有内容）
      b) 有且只有一个 EditText，且它的 bounds 发生了变化（聚焦后输入框会变大）
    """
    xml = dump_xml(d)
    # 判据 a：hint 消失 → 大概率已填入
    if KIMI_PHONE_INPUT_HINT not in xml:
        logger.info("输入框 hint 已消失，判定手机号已填入")
        return True
    # 判据 b：兜底，set_text 写入后 XML 里确实能搜到手机号
    if phone in xml:
        logger.info("XML 中搜到手机号字符串，判定已填入")
        return True
    return False


def _click_verify_and_login(d) -> bool:
    """⑨ 点「验证并登录」发送验证码。

    ⚠️ Compose 按钮的 XML 属性全不可靠（clickable=false / enabled=true 假阳性），
    所以**不依赖 clickable/enabled**，直接按 bounds 像素中心点击。

    ⚠️ 关键判据：点击后「验证并登录」按钮从 UI 消失 = 点击成功。
    之前的代码把"按钮消失"误判成"未找到按钮→重试"，导致明明点击成功
    （进入了滑块/验证码页）却被报错"无响应"。
    """
    for attempt in range(3):
        xml = dump_xml(d)
        nodes = _parse_nodes(xml)
        btn = _find_node(nodes, text=KIMI_VERIFY_BTN_WORD)

        # ★ 按钮已经不在 UI 上了 = 上一次点击成功了（进入了下一步）
        if not btn:
            if attempt > 0:
                logger.info("✓ 「验证并登录」按钮已消失，判定点击成功")
                return True
            # 第一次循环就没按钮，可能页面还在加载，等一下
            logger.warning("未找到「验证并登录」按钮，等待后重试")
            time.sleep(1.5)
            continue

        x1, y1, x2, y2 = btn["bounds"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        logger.info(f"点击「验证并登录」bounds=[{x1},{y1}][{x2},{y2}] → 中心 ({cx},{cy})")
        d.click(cx, cy)
        time.sleep(2.0)

        # ★ 点击后再查一次：按钮消失 OR 出现验证码/滑块关键词 = 成功
        xml_after = dump_xml(d)
        nodes_after = _parse_nodes(xml_after)
        btn_after = _find_node(nodes_after, text=KIMI_VERIFY_BTN_WORD)
        if not btn_after:
            logger.info("✓ 点击后按钮消失，判定成功")
            return True
        if any(kw in xml_after for kw in
               ("验证码", "vcode", "code", "易盾", "滑块", "拼图", "安全验证")):
            logger.info("✓ 点击后进入验证码/滑块页，判定成功")
            return True

        logger.info(f"第 {attempt + 1} 次点击后按钮仍在且无新页面，再试一次")

    logger.error("3 次点击「验证并登录」均无响应")
    return False


def _wait_vcode_page(d, timeout=20) -> bool:
    """⑩ 等验证码页出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_on_vcode_page(d):
            return True
        time.sleep(0.8)
    return False


def _input_vcode(d, vcode: str) -> bool:
    """⑩ 填入 6 位短信验证码。

    Kimi 的验证码输入是「6 个独立方格」，底层通常挂一个隐藏 EditText；
    优先逐位模拟真人输入（每位间隔 0.25~0.45s）以降低风控概率，
    兜底一次性 set_text，最后兜底 adb `input text`。
    """
    code = str(vcode).strip()
    if not code:
        return False

    nodes = _parse_nodes(dump_xml(d))
    edit = None
    for n in nodes:
        if "EditText" in n["class"]:
            edit = n
            break

    # 每次输入后校验：验证码页消失（说明已提交并进入主界面）即算成功
    def _entered() -> bool:
        return not is_on_vcode_page(d)

    # ---- 方案 1：聚焦底层 EditText 后逐位输入 ----
    if edit is not None:
        try:
            _click_node(d, nodes, edit)
            time.sleep(0.4)
            obj = d(className="android.widget.EditText")
            for i, ch in enumerate(code):
                obj.set_text(code[: i + 1])   # 逐位「覆盖式」写入，模拟真人节奏
                time.sleep(0.45 if i < 3 else 0.3)
            logger.info("已逐位填入验证码")
            time.sleep(1.5)
            if _entered():
                return True
        except Exception as exc:
            logger.debug(f"逐位输入验证码失败: {exc}")

    # ---- 方案 2：直接点第 1 个方格，adb 输入整串 ----
    w, h = _screen_size(d)
    try:
        _click_xy(d, int(w * 0.115), int(h * 0.335), wait=0.5)
        d.set_fastinput_ime(True)
        d.send_keys(code, clear=True)
        d.set_fastinput_ime(False)
        logger.info("已用 fastinput 一次性填入验证码")
        time.sleep(1.5)
        if _entered():
            return True
    except Exception as exc:
        logger.debug(f"fastinput 输入验证码失败: {exc}")

    # ---- 方案 3：adb shell input text ----
    try:
        run_adb("shell", "input", "text", code, timeout=15)
        time.sleep(1.5)
        if _entered():
            logger.info("已用 adb input text 填入验证码")
            return True
    except Exception as exc:
        logger.debug(f"adb input 验证码失败: {exc}")

    return _entered()


def login_with_phone(d, phone: str, vcode: str | None = None, poller=None) -> bool:
    """完整登录流程（③⑥⑦⑧⑨⑩）。

    :param phone:   手机号
    :param vcode:   直接指定验证码（测试用）；None 时走 poller 轮询
    :param poller:  轮询函数 poller(after_ts) -> vcode，
                    after_ts 为「短信实际发出」的时间戳，只查该时间之后的新码
    """
    logger.info(f"===== 开始登录 Kimi: {phone} =====")
    start_kimi(d)

    # 已登录先登出
    if is_logged_in(d):
        logger.info("检测到已登录，先登出")
        logout(d)
        start_kimi(d)

    # ---- ③ 到登录入口 ----
    if not _retry(lambda: _goto_login_entry(d), attempts=3, wait=2.0,
                  name="进入登录入口"):
        # 兜底：重启 App 再试一次
        force_reset(d)
        start_kimi(d, wait=6)
        if not _retry(lambda: _goto_login_entry(d), attempts=2, wait=2.0,
                      name="重启后进入登录入口"):
            raise RuntimeError("无法进入登录页面（「去登录」没找到）")

    # ---- ⑥⑦ 点「手机号登录」→ 协议弹窗「同意并继续」 ----
    if not _retry(lambda: _click_phone_login(d), attempts=3, wait=1.5,
                  name="进入手机号输入页"):
        raise RuntimeError("点击「手机号登录」/「同意并继续」后仍未进入手机号输入页")

    # ---- ⑧ 输入手机号 ----
    if not _retry(lambda: _input_phone(d, phone), attempts=3, wait=1.0,
                  name="填入手机号"):
        raise RuntimeError("找不到手机号输入框 / 手机号没写入成功")

    # ---- ⑨ 点「验证并登录」→ 短信发出 ----
    if not _retry(lambda: _click_verify_and_login(d), attempts=3, wait=1.5,
                  name="点击验证并登录"):
        raise RuntimeError("找不到「验证并登录」按钮")

    # ⚠️ 关键：短信是点击「验证并登录」时才发出的，时间戳必须记在这里，
    #    轮询只查这之后的新码，避免拿到历史旧短信
    vcode_send_ts = time.time()
    logger.info("已点击「验证并登录」，等待验证码页出现")

    # ⭐ 风控滑块拼图人机验证（多次登出/登录后 Kimi 弹出）
    # 必须在等验证码页之前检测并破解，否则流程卡死在这里
    try:
        import slider_captcha_solver as _scs
        # 等弹窗完全渲染（有时弹窗出现有 1~2 秒延迟）
        for _ in range(3):
            if _scs.is_slide_captcha_visible(d):
                break
            time.sleep(1.0)
        if _scs.is_slide_captcha_visible(d):
            logger.warning("⚠️ 检测到滑块拼图人机验证！调用 AI + OpenCV 双引擎破解")
            ok = _scs.solve_slide_captcha(d)
            if ok:
                logger.info("✅ 滑块拼图人机验证已通过，继续登录流程")
            else:
                raise RuntimeError("滑块拼图人机验证破解失败（AI + OpenCV 双引擎都没通过）")
        else:
            logger.info("本次登录未触发滑块风控（正常情况）")
    except ImportError as exc:
        logger.error(f"slider_captcha_solver 无法导入: {exc}")
    except Exception as exc:           # noqa: BLE001
        logger.error(f"滑块拼图破解异常: {exc}")
        # 不 raise：破不了也继续走，让 _wait_vcode_page 判定（如果真有弹窗卡着会超时，
        # 上层会捕获这个异常并走异常恢复流程 → 重新登出登录）

    if not _wait_vcode_page(d, timeout=20):
        logger.warning("验证码页未出现，仍尝试轮询验证码")

    # ---- 轮询拿验证码 ----
    if not vcode and poller is not None:
        try:
            try:
                vcode = poller(vcode_send_ts)
            except TypeError:
                vcode = poller()   # 兼容无参旧签名
        except Exception as exc:
            raise RuntimeError(f"轮询验证码失败: {exc}")
    if not vcode:
        raise RuntimeError("拿不到验证码")
    vcode = str(vcode).strip()
    logger.info(f"拿到验证码: {vcode}")

    # ---- ⑩ 填入验证码 ----
    if not _retry(lambda: _input_vcode(d, vcode), attempts=3, wait=2.0,
                  name="填入验证码"):
        raise RuntimeError("验证码输入后未进入已登录状态")

    # ---- 确认登录成功 ----
    for _ in range(12):
        if is_logged_in(d):
            _dismiss_popups(d)
            # 登录成功后抽屉可能默认张开，会遮挡主界面并污染问答截图，先关掉
            close_drawer_if_open(d)
            logger.info(f"✅ 手机号 {phone} 登录成功")
            return True
        time.sleep(1.0)

    raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")
