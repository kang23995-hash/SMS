# -*- coding: utf-8 -*-
"""
yuanbao_login.py —— 腾讯元宝 App 登出 / 登录 UI 自动化
================================================================

对应用户标注图片的 ①~⑨ 步骤：

  【登出】
    ① 点左上角按钮打开侧边抽屉
    ② 点抽屉右下角的「设置」图标按钮
    ③ 设置页往下划到底部，点「退出登录」
    ④ 弹窗点「退出登录」确认

  【登录】
    ⑤ 落地页出现「手机登录」按钮（或先点右上角「登录」），点击
    ⑥ 「服务协议及隐私保护」弹窗点「同意」
    ⑦ 手机号登录页输入手机号 → 点「下一步」→ 短信自动发出
    ⑧ 输入 6 位短信验证码（轮询 FastAPI 拿码）
    ⑨ 弹出人机验证 → captcha_solver 用千问大模型选图 → 点「确定」
       点错会自动换新题，重新分析重新点，直到验证通过进入 App
    ⑩ 关闭「无水印保存功能」弹窗（我知道了）；
       默认「按住 说话」时点键盘图标切换为文字输入

所有查找均「XML 文本优先 + 比例坐标兜底」，不假设具体 resource-id
（元宝版本更新可能改 id，文本更稳）。横竖屏均按运行时实际分辨率适配。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
import hashlib
import os

logger = logging.getLogger(__name__)


class StuckAfterVcode(RuntimeError):
    """验证码已提交，但页面持续卡死不切换到下一步。

    触发场景：
      - 模拟器 WebView 渲染故障，人机验证弹窗白屏
      - 元宝 App 卡在"验证码输入成功等待验证"状态
      - 页面完全停止变化（XML 哈希持续不变、既不登录也不弹验证）
    外层 PhoneSessionManager 捕获后会 force_reset + 重启 + 重走登录流程。
    """
    def __init__(self, reason="unknown", stuck_for_secs=0, snapshot_dir=None):
        self.reason = reason
        self.stuck_for_secs = stuck_for_secs
        self.snapshot_dir = snapshot_dir
        msg = f"StuckAfterVcode({reason!r}) for {stuck_for_secs}s"
        if snapshot_dir:
            msg += f" snapshot={snapshot_dir}"
        super().__init__(msg)

# OpenCV 图标模板匹配（可选依赖：没有 cv2 时自动退化成 XML/坐标定位）
try:
    from icon_locator import locate_icons, OPEN_DRAWER_ICON, SETTING_ICON
    _ICON_MATCH_OK = True
except Exception as _icon_err:      # noqa: BLE001
    locate_icons = None
    OPEN_DRAWER_ICON = SETTING_ICON = ""
    _ICON_MATCH_OK = False
    logger.warning(f"图标模板匹配不可用（{_icon_err}），改用 XML/坐标定位")

# 元宝包名候选（版本更新可能改包名，运行时自动探测）
YUANBAO_PACKAGE_CANDIDATES = (
    "com.tencent.hunyuan.app.chat",
    "com.tencent.hunyuan.app",
    "com.tencent.hunyuan",
    "com.tencent.yuanbao",
    "com.tencent.mobileqq.yuanbao",
)


# =====================================================================
#  界面特征词（用于状态判断与元素查找）
# =====================================================================

# 主界面（已登录）特征
LOGGED_IN_HINTS = (
    "发消息或按住说话", "发消息", "按住 说话", "按住说话",
    "Hi，用户", "Hi, 用户", "快速思考",
)
# 落地页 / 未登录特征
LANDING_HINTS = ("登录即可体验更多功能", "手机登录", "登录后创建全新对话")
# 左上角抽屉按钮候选 desc/text（元宝的菜单图标可能没有文字，需多备几个）
DRAWER_OPEN_WORDS = (
    "打开侧边栏", "侧边栏", "菜单", "导航", "更多",
    "打开菜单", "打开抽屉", "展开侧边栏", "收起侧边栏",
    "返回", "back", "menu",
)
# 抽屉打开特征（实测只有这两个文案是抽屉独有的）
DRAWER_HINTS = ("新建对话", "福利中心")
# 登录后可能出现的干扰弹窗按钮
DISMISS_WORDS = ("我知道了", "以后再说", "暂不", "跳过", "取消", "稍后再说",
                 "不允许", "关闭")


# =====================================================================
#  基础工具
# =====================================================================

def run_adb(*args, timeout=30):
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


def get_device_serial():
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
    try:
        return d.dump_hierarchy(compressed=False)
    except Exception:
        return ""


def _launch_package(d, pkg: str) -> bool:
    """最可靠的启动方式：优先 adb shell monkey（绕过 u2 的 app_start 缓存）。"""
    try:
        d.shell(["monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER",
                 "1"])
        time.sleep(2.0)
        return d.app_current().get("package") == pkg
    except Exception:
        pass
    try:
        d.app_start(pkg, use_monkey=True)
        time.sleep(2.0)
        return d.app_current().get("package") == pkg
    except Exception:
        pass
    return False


def resolve_yuanbao_package(d) -> str:
    """自动探测元宝真实包名（依次尝试候选列表，每个最多 retry 2 次）。"""
    try:
        cur = d.app_current().get("package") or ""
        if cur in YUANBAO_PACKAGE_CANDIDATES:
            return cur
    except Exception:
        pass
    for pkg in YUANBAO_PACKAGE_CANDIDATES:
        for try_n in range(1, 3):   # 每个候选最多 2 次
            try:
                logger.info(f"尝试启动 {pkg}（第 {try_n}/2 次）")
                if _launch_package(d, pkg):
                    logger.info(f"元宝包名确认：{pkg}")
                    return pkg
            except Exception:
                time.sleep(1.0)
    raise RuntimeError(
        "无法启动腾讯元宝 App，请确认已安装且包名在 "
        f"{YUANBAO_PACKAGE_CANDIDATES} 中。"
    )


def current_is_yuanbao(d) -> bool:
    try:
        return d.app_current().get("package", "") in YUANBAO_PACKAGE_CANDIDATES
    except Exception:
        return False


def start_yuanbao(d, wait=4):
    """拉起元宝到前台（不杀进程），返回真实包名。

    先用 resolve_yuanbao_package（内部用 monkey + retry），
    拿到包名后轮询确认稳定在前台（连续 2 次 check 通过）。
    最终如果元宝卡在启动页（包名对但 activity 不对），
    也能 return pkg 让上层继续后续流程。
    """
    logger.info("拉起腾讯元宝 App")
    try:
        pkg = resolve_yuanbao_package(d)
    except RuntimeError:
        # 包名探测都失败了？再强启一次（直接 monkey 最可能的候选）
        logger.warning("resolve_yuanbao_package 失败，monkey 强启兜底...")
        for pkg in YUANBAO_PACKAGE_CANDIDATES:
            if _launch_package(d, pkg):
                logger.info(f"兜底启动成功：{pkg}")
                break
        else:
            raise

    deadline = time.time() + max(wait, 12)
    stable_cnt = 0
    while time.time() < deadline:
        if current_is_yuanbao(d):
            stable_cnt += 1
            if stable_cnt >= 2:      # 连续两次确认稳定
                return pkg
        else:
            stable_cnt = 0
            try:
                _launch_package(d, pkg)
            except Exception:
                pass
        time.sleep(1.5)
    logger.warning("元宝未确认稳定在前台，继续后续流程")
    return pkg


def force_reset(d):
    """彻底杀进程：先 d.app_stop（SIGTERM），不行再 adb am force-stop（硬杀），
    最后 kill -9 兜底。元宝 WebView 卡死/状态损坏时必须 force-stop 才能恢复。"""
    killed = False
    for pkg in YUANBAO_PACKAGE_CANDIDATES:
        try:
            pid_info = d.shell(["pidof", pkg])
            if not pid_info.strip():
                continue   # 进程本来就没在
            # 1) SIGTERM
            try:
                d.app_stop(pkg)
            except Exception:
                pass
            time.sleep(1.5)
            # 2) adb force-stop（硬杀 Activity + Service）
            try:
                d.shell(["am", "force-stop", pkg])
            except Exception:
                pass
            # 3) kill -9 兜底
            try:
                pids = d.shell(["pidof", pkg]).strip()
                if pids:
                    for pid in pids.split():
                        d.shell(["kill", "-9", pid])
            except Exception:
                pass
            killed = True
            break
        except Exception:
            continue
    # 最后清一下 WebView 缓存目录（腾讯 TBS 内核有时缓存脏导致白屏）
    if killed:
        time.sleep(1.0)
        try:
            for pkg in YUANBAO_PACKAGE_CANDIDATES:
                d.shell(["rm", "-rf", f"/data/data/{pkg}/cache/webviewCache"])
                d.shell(["rm", "-rf", f"/data/data/{pkg}/app_webview"])
        except Exception:
            pass
        logger.info("force_reset 完成（已清 WebView 缓存）")


force_logout_if_needed = force_reset  # 与其他 *_login 接口名保持一致


def _screen_size(d):
    try:
        return int(d.info["displayWidth"]), int(d.info["displayHeight"])
    except Exception:
        return 900, 1600


def _try_click(d, **kwargs) -> bool:
    """按 uiautomator2 选择器点击（resourceId=/text=/description=...）。"""
    try:
        obj = d(**kwargs)
        if obj.exists(timeout=2):
            obj.click()
            time.sleep(0.8)
            logger.info(f"已点击 {kwargs}")
            return True
    except Exception:
        pass
    return False


def _click_words(d, words, timeout_each=1.2) -> bool:
    """依次按 text / description 精确匹配一组候选词点击。"""
    for w in words:
        if _try_click(d, text=w):
            return True
    for w in words:
        try:
            obj = d(description=w)
            if obj.exists(timeout=timeout_each):
                obj.click()
                time.sleep(0.8)
                logger.info(f"已点击 desc={w}")
                return True
        except Exception:
            pass
    for w in words:
        if _try_click(d, textContains=w):
            return True
    return False


def _swipe_up(d, duration=0.35):
    w, h = _screen_size(d)
    d.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.25), duration=duration)


def _back(d, wait=0.8):
    """按返回键（忽略异常），用于策略失败后的回退。"""
    try:
        d.press("back")
        time.sleep(wait)
    except Exception:
        pass


def _parse_nodes(xml: str) -> list:
    """解析 XML hierarchy 为节点 dict 列表（登出定位用）。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    nodes = []
    for node in root.iter("node"):
        nodes.append({
            "text": (node.attrib.get("text") or "").strip(),
            "desc": (node.attrib.get("content-desc") or "").strip(),
            "rid": node.attrib.get("resource-id", ""),
            "class": node.attrib.get("class", ""),
            "bounds": node.attrib.get("bounds", ""),
            "clickable": node.attrib.get("clickable") == "true",
        })
    return nodes


def _bounds(bounds_str: str):
    """'[10,20][30,40]' -> (10, 20, 30, 40)；解析失败返回 None。"""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str or "")
    if not m:
        return None
    return tuple(map(int, m.groups()))


def _node_center(node: dict):
    b = _bounds(node.get("bounds", ""))
    if not b:
        return None
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


# ---------------------------------------------------------------------
#  OpenCV 图标模板匹配（纯图标按钮的精准定位）
#  实测（900x1600 雷电模拟器）：同分辨率模板命中置信度 ≈ 1.000，
#  限定 ROI 后无同分干扰，阈值 0.85 可稳定区分真伪。
# ---------------------------------------------------------------------

ICON_MATCH_THRESHOLD = 0.85                  # 置信度阈值
ICON_MATCH_SCALES = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3)   # 兼容不同 DPI
DRAWER_ICON_ROI = (0.0, 0.0, 0.25, 0.12)     # 抽屉按钮只在左上角
SETTING_ICON_ROI = (0.0, 0.80, 0.65, 1.0)    # 设置齿轮在抽屉底部


def _click_icon(d, template_path, roi=None, tag="") -> bool:
    """用 OpenCV 模板匹配定位图标并点击，返回是否真的发出了点击。

    置信度低于阈值时**不点击**（避免误点乱跳），交给后续 XML/锚点策略。
    """
    if not _ICON_MATCH_OK:
        return False
    try:
        cds = locate_icons(d, template_path, threshold=ICON_MATCH_THRESHOLD,
                           scales=ICON_MATCH_SCALES, roi=roi, topk=1)
    except Exception as e:      # noqa: BLE001
        logger.warning(f"图标匹配异常({tag}): {e}")
        return False
    if not cds:
        logger.info(f"图标未命中: {tag}（置信度 < {ICON_MATCH_THRESHOLD}）")
        return False
    m = cds[0]
    logger.info(f"图标命中: {tag} 中心=({m['x']},{m['y']}) "
                f"置信度={m['score']:.3f} 尺度={m['scale']}")
    d.click(m["x"], m["y"])
    return True


def _dismiss_popups(d, rounds=3):
    """登录后常见干扰弹窗（无水印保存功能/通知权限/更新提示等）统一关闭。"""
    for _ in range(rounds):
        xml = dump_xml(d)
        hit = False
        for w in DISMISS_WORDS:
            if w in xml:
                if _try_click(d, text=w):
                    hit = True
                    break
        if not hit:
            break
        time.sleep(0.8)


# =====================================================================
#  卡死页面检测（验证码提交后 WebView 白屏 / App 停止响应 等）
# =====================================================================

# 正常页面的 XML 长度范围（低于下限意味着页面异常空）
_XML_LEN_MAIN_MIN = 35000      # 主界面 XML 正常 ≥ 60k，留够缓冲
_XML_LEN_CAPTCHA_MIN = 25000   # 人机验证即使 XML 不全 ≥ 40k
_XML_LEN_VCODE_MAX = 35000     # 正常"验证码输入页"XML ≤ 25k

# 白像素率判定（> 0.92 近似整屏白 = 异常空或纯白背景页面）
_WHITE_RATIO_HIGH = 0.92


def _xml_hash(xml: str) -> str:
    return hashlib.md5((xml or "").encode("utf-8")).hexdigest()[:10]


def _page_statistics(d, shot=None):
    """采集当前页面的诊断指标，返回 dict；任一指标采样失败用 None 占位。"""
    stat = {"xml": "", "xml_len": 0, "xml_hash": "",
            "white_ratio": None, "has_webview": False,
            "is_logged_in": False, "has_captcha": False}
    try:
        xml = dump_xml(d)
        stat["xml"] = xml
        stat["xml_len"] = len(xml)
        stat["xml_hash"] = _xml_hash(xml)
        stat["has_webview"] = bool(re.search(r'WebView|webkit', xml))
        stat["is_logged_in"] = is_logged_in(d)
        stat["has_captcha"] = any(m in xml for m in (
            "选择最符合描述的图片", "换一批", "人机验证", "安全验证", "完成验证",
        ))
    except Exception as e:
        logger.debug(f"页面统计异常: {e}")

    try:
        from PIL import Image
        import numpy as np
        if shot is None:
            shot = d.screenshot()
        arr = np.array(shot.convert("RGB"))
        stat["white_ratio"] = float(np.mean(np.all(arr > 240, axis=2)))
    except Exception as e:
        logger.debug(f"白像素率计算异常: {e}")

    return stat


def _looks_like_blank_or_stuck(stat: dict) -> bool:
    """根据一组统计判断页面是否像"白屏卡死"。

    典型卡死（探针复现）：
      is_logged_in=False + has_captcha=False + xml_len 低 + white_ratio 高
    正常页面（对照）：
      - 主界面：xml_len ≥ 60k，is_logged_in=True
      - 人机验证：xml_len ≥ 40k，has_captcha=True
      - 登录页/验证码页：xml_len ≈ 22-25k（短暂等待状态，不应超过 2 轮）
    """
    # 已经登录或真在验证 → 不是卡死
    if stat.get("is_logged_in") or stat.get("has_captcha"):
        return False
    xml_len = stat.get("xml_len", 0)
    wr = stat.get("white_ratio") or 0.0
    # 条件 A：XML 过短（≈"验证码输入页"或空白页），且白像素率高
    if xml_len < _XML_LEN_VCODE_MAX and wr > _WHITE_RATIO_HIGH:
        return True
    # 条件 B：XML 过短但白像素率没采集到 → 保守判断为卡死
    if xml_len < _XML_LEN_VCODE_MAX and stat.get("white_ratio") is None:
        return True
    # 条件 C：XML 长度正常但既没登录也没验证（可能卡在"加载中"）
    #   —— 这种情况可能是正常页面在切换，不放卡死判定，交给外层 deadline
    return False


def _save_stuck_snapshot(d, stat: dict, tag="stuck") -> str:
    """卡死现场留存：截图 + XML + 统计 JSON。返回保存目录。"""
    try:
        from datetime import datetime
        from config import SCREENSHOT_DIR
        out_dir = SCREENSHOT_DIR / "stuck_snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = d.screenshot()
        shot.save(out_dir / f"{ts}_{tag}_screen.png")
        xml = stat.get("xml") or dump_xml(d)
        (out_dir / f"{ts}_{tag}_hierarchy.xml").write_text(xml, encoding="utf-8")
        import json
        snap = {k: v for k, v in stat.items() if k != "xml"}
        (out_dir / f"{ts}_{tag}_stat.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.warning(f"[stuck] 卡死现场已留存: {out_dir}\\{ts}_{tag}_*")
        return str(out_dir)
    except Exception as e:
        logger.warning(f"[stuck] 留存现场失败: {e}")
        return ""


def _wait_post_vcode(d, appear_timeout=30.0) -> None:
    """验证码提交后的等待循环。

    核心区分：
      - has_webview=True → 元宝已经弹出 WebView 人机验证容器（腾讯 TBS X5），
        哪怕全白也不是"卡死"，只是 WebView 还没渲染出验证内容——**继续等，不触发 stuck**
      - has_webview=False + 白屏 + 静态 → 真卡死（验证码页没触发任何跳转）

    卡死触发（二选一）：
      a) 无 WebView + 画像像白屏卡死 + 持续 5s 静态 → StuckAfterVcode(blank_or_static_page)
      b) 有 WebView 但 30s 内始终没渲染出 captcha + 没登录 → StuckAfterVcode(webview_never_rendered)
    """
    from collections import deque
    hashes = deque(maxlen=6)
    stuck_for = 0.0
    webview_seen_until = 0.0   # WebView 首次出现的时间戳

    deadline = time.time() + appear_timeout
    grace_until = time.time() + 3.0
    first_tick = True

    while time.time() < deadline:
        stat = _page_statistics(d)
        now = time.time()

        if stat["is_logged_in"]:
            logger.info(f"[post-vcode] ✅ 登录成功（用时 {now - (deadline - appear_timeout):.1f}s）")
            return
        if stat["has_captcha"]:
            logger.info(f"[post-vcode] 检测到人机验证，交给 captcha_solver 求解")
            return

        if stat["xml_hash"]:
            hashes.append(stat["xml_hash"])

        distinct_cnt = len(set(hashes)) if hashes else 99
        looks_static = distinct_cnt <= 3 and len(hashes) >= 3

        has_webview = stat.get("has_webview", False)
        if has_webview and webview_seen_until == 0.0:
            webview_seen_until = now
            logger.info("[post-vcode] 检测到 WebView 容器（人机验证 SDK 已弹出），等待渲染...")

        if has_webview:
            # 有 WebView → 可能在加载验证内容，**不触发 stuck 累加**
            stuck_for = 0.0
        elif now >= grace_until and looks_static and _looks_like_blank_or_stuck(stat):
            stuck_for += 1.0
        elif now >= grace_until and looks_static:
            stuck_for += 0.5
        else:
            stuck_for = 0.0

        # 触发卡死 a：无 WebView 的纯白屏/静态页
        if (not has_webview
                and now >= grace_until
                and stuck_for >= 5.0
                and looks_static
                and _looks_like_blank_or_stuck(stat)):
            snap_dir = _save_stuck_snapshot(d, stat, tag="post_vcode_no_webview")
            raise StuckAfterVcode(
                reason="blank_or_static_page_no_webview",
                stuck_for_secs=int(stuck_for),
                snapshot_dir=snap_dir,
            )

        # 日志（每秒一次心跳，卡住时额外一行）
        if first_tick or (int(stuck_for) > int(stuck_for - 1e-9) and stuck_for > 0) or has_webview:
            extra = "WEBVIEW" if has_webview else ""
            logger.info(
                f"[post-vcode] t={now - (deadline - appear_timeout):.0f}s "
                f"xml={stat['xml_len']} wh={stat['white_ratio']:.2f} "
                f"distinct_hash={distinct_cnt} stuck={stuck_for:.0f}s {extra}"
            )
        first_tick = False
        time.sleep(1.0)

    # 超时：有 WebView 但始终没渲染出 captcha → 真卡死
    stat = _page_statistics(d)
    if stat.get("has_webview"):
        snap_dir = _save_stuck_snapshot(d, stat, tag="post_vcode_webview_never_rendered")
        raise StuckAfterVcode(
            reason="webview_never_rendered",
            stuck_for_secs=int(appear_timeout),
            snapshot_dir=snap_dir,
        )
    snap_dir = _save_stuck_snapshot(d, stat, tag="post_vcode_timeout")
    raise StuckAfterVcode(
        reason="post_vcode_timeout",
        stuck_for_secs=int(appear_timeout),
        snapshot_dir=snap_dir,
    )


# =====================================================================
#  侧边抽屉状态检测与关闭
# =====================================================================

def is_drawer_open(d) -> bool:
    """侧边抽屉是否张开（判据见 DRAWER_HINTS，均为主界面不出现的文案）。"""
    xml = dump_xml(d)
    return any(h in xml for h in DRAWER_HINTS)


def close_drawer_if_open(d) -> bool:
    """若抽屉张开则关闭；未张开直接返回。"""
    if not is_drawer_open(d):
        logger.info("侧边抽屉未张开，无需关闭")
        return True

    logger.info("检测到侧边抽屉张开，开始关闭")
    w, h = _screen_size(d)
    for attempt in range(3):
        # 策略 1：点右上角区域（抽屉外）收起
        d.click(int(w * 0.85), int(h * 0.30))
        time.sleep(0.9)
        if not is_drawer_open(d):
            logger.info("✅ 侧边抽屉已关闭")
            return True
        # 策略 2：按返回键
        try:
            d.press("back")
            time.sleep(0.9)
        except Exception:
            pass
        if not is_drawer_open(d):
            logger.info("✅ 侧边抽屉已关闭（back）")
            return True

    logger.warning("多次尝试后抽屉仍张开（不影响主流程，继续）")
    return False


# =====================================================================
#  登录状态判断
# =====================================================================

def is_logged_in(d) -> bool:
    """已登录 = 主界面有输入区/用户问候特征，且无落地页特征、无验证弹窗。"""
    xml = dump_xml(d)
    # 人机验证在屏时绝不能认为已登录
    if "选择最符合描述的图片" in xml or "换一批" in xml:
        return False
    if any(h in xml for h in LANDING_HINTS):
        return False
    return any(h in xml for h in LOGGED_IN_HINTS) or (
        "android.widget.EditText" in xml and "元宝" in xml
    )


# =====================================================================
#  登出（图片步骤 ①~④）
# =====================================================================

def _open_drawer(d) -> bool:
    """① 点击左上角菜单按钮打开侧边抽屉，带验证与重试。

    定位策略（优先级从高到低）：
      0) OpenCV 模板匹配 open_left_drawer_icon.png（置信度≥0.85 才点，最精准）
      a) text/description 匹配 DRAWER_OPEN_WORDS
      b) 以「元宝」标题为锚点，点其左侧紧邻位置
      c) 比例兜底：左上角 (0.087, 0.073)
    每种策略点击后都验证抽屉是否真的打开，没打开就继续下一个策略/下一轮重试。
    """
    if is_drawer_open(d):
        logger.info("抽屉已张开，无需再开")
        return True

    w, h = _screen_size(d)
    for attempt in range(3):
        # 策略 0：模板匹配左上角抽屉图标
        if _click_icon(d, OPEN_DRAWER_ICON, roi=DRAWER_ICON_ROI, tag="抽屉按钮"):
            time.sleep(1.0)
            if is_drawer_open(d):
                logger.info("✅ 侧边抽屉已打开（模板匹配）")
                return True
            logger.warning("模板匹配点中抽屉按钮但抽屉未打开，改用其他策略")

        xml = dump_xml(d)
        nodes = _parse_nodes(xml)
        clicked = False

        # 策略 a：语义词点击
        if _click_words(d, DRAWER_OPEN_WORDS):
            clicked = True

        # 策略 b：以「元宝」标题为锚点，点其左侧按钮
        if not clicked:
            yuanbao_node = None
            for n in nodes:
                if n["text"] == "元宝" or n["desc"] == "元宝":
                    yuanbao_node = n
                    break
            if yuanbao_node:
                b = _bounds(yuanbao_node["bounds"])
                if b:
                    # 按钮在标题左边，中心约在标题左缘 - 标题高度的 0.6 倍处
                    btn_x = b[0] - int((b[3] - b[1]) * 0.9)
                    btn_y = (b[1] + b[3]) // 2
                    if btn_x > 0:
                        logger.info(f"以「元宝」标题为锚点点击抽屉按钮 ({btn_x},{btn_y})")
                        d.click(btn_x, btn_y)
                        clicked = True

        # 策略 c：比例兜底
        if not clicked:
            cx, cy = int(w * 0.087), int(h * 0.073)
            logger.info(f"比例兜底点击左上角抽屉按钮 ({cx},{cy})")
            d.click(cx, cy)

        time.sleep(1.0)
        if is_drawer_open(d):
            logger.info("✅ 侧边抽屉已打开")
            return True
        logger.warning(f"第 {attempt + 1}/3 次打开抽屉未成功，重试")

    logger.error("多次尝试后仍无法打开侧边抽屉")
    return False


def _click_settings_in_drawer(d) -> bool:
    """② 点击抽屉右下角的设置（齿轮）图标，带验证。

    定位策略（优先级从高到低）：
      0) XML content-desc/text="设置" 的精确 bounds（齿轮节点自身带 desc）
      1) OpenCV 模板匹配 the_setting_icon.png（置信度≥0.85 才点）
      a) text/description 精确匹配「设置」（限底部区域）
      b) 锚点推算：齿轮与用户行同高，横坐标 ≈ 抽屉右缘 × 0.75
      c) 比例兜底：抽屉右下角 (0.45, 0.92)
    每种策略点击后都用 _is_settings_page 验证，没进入设置页就回退重试。
    """
    w, h = _screen_size(d)
    for attempt in range(3):
        # 回退时用 back 可能把抽屉关掉，这里确保抽屉仍张开
        if attempt > 0 and not is_drawer_open(d):
            logger.info("抽屉被关掉了，重新打开")
            if not _open_drawer(d):
                continue

        xml = dump_xml(d)
        nodes = _parse_nodes(xml)

        # 策略 0：XML 精确节点（desc/text = 设置，且位于抽屉底部区域）
        for n in nodes:
            if n["desc"] == "设置" or n["text"] == "设置":
                b = _bounds(n["bounds"])
                if not b or b[1] < h * 0.80:
                    continue
                cx, cy = _node_center(n)
                logger.info(f"按节点「设置」精确点击齿轮 ({cx},{cy}) bounds={n['bounds']}")
                d.click(cx, cy)
                time.sleep(1.2)
                if _is_settings_page(d):
                    logger.info("✅ 已进入设置页（节点定位）")
                    return True
                _back(d)

        # 策略 1：模板匹配设置齿轮
        if _click_icon(d, SETTING_ICON, roi=SETTING_ICON_ROI, tag="设置齿轮"):
            time.sleep(1.2)
            if _is_settings_page(d):
                logger.info("✅ 已进入设置页（模板匹配）")
                return True
            _back(d)

        # 策略 a：语义词点击（只看底部区域，避免误点设置页内的条目）
        for n in nodes:
            if (n["text"] == "设置" or n["desc"] == "设置") and n["clickable"]:
                b = _bounds(n["bounds"])
                if b and b[1] > h * 0.70:
                    cx, cy = _node_center(n)
                    logger.info(f"点击抽屉底部设置入口 ({cx},{cy})")
                    d.click(cx, cy)
                    time.sleep(1.2)
                    if _is_settings_page(d):
                        logger.info("✅ 已进入设置页")
                        return True
                    _back(d)

        # 策略 b：锚点推算（齿轮与用户行同高，x ≈ 抽屉右缘 × 0.75）
        row_y = None
        for n in nodes:
            if n["desc"] == "头像" or n["text"].startswith("用户"):
                b = _bounds(n["bounds"])
                if b:
                    row_y = (b[1] + b[3]) // 2
                    break
        drawer_right = None
        for n in nodes:
            if n["desc"] == "抽屉页入口":
                b = _bounds(n["bounds"])
                if b:
                    drawer_right = b[2]
                    break
        if row_y is not None:
            dr = drawer_right or int(w * 0.63)
            gear_x = int(dr * 0.75)
            logger.info(f"锚点推算齿轮位置 ({gear_x},{row_y})（抽屉右缘={dr}）")
            d.click(gear_x, row_y)
            time.sleep(1.2)
            if _is_settings_page(d):
                logger.info("✅ 已进入设置页（锚点推算）")
                return True
            _back(d)

        # 策略 c：比例兜底
        cx, cy = int(w * 0.45), int(h * 0.92)
        logger.info(f"比例兜底点击抽屉右下角设置图标 ({cx},{cy})")
        d.click(cx, cy)
        time.sleep(1.5)
        if _is_settings_page(d):
            logger.info("✅ 已进入设置页")
            return True
        logger.warning(f"第 {attempt + 1}/3 次点击设置未进入设置页，重试")
        _back(d)

    logger.error("多次尝试后仍无法进入设置页")
    return False


def _is_settings_page(d) -> bool:
    """判断当前是否在设置页（标题「设置」且有「退出登录」/「关于元宝」等条目）。"""
    xml = dump_xml(d)
    if "退出登录" in xml:
        return True
    if "设置" in xml and ("关于元宝" in xml or "账号设置" in xml
                          or "数据与隐私" in xml):
        return True
    return False


def _click_logout_in_settings(d) -> bool:
    """③ 设置页滑到底部，点「退出登录」。"""
    # 先等设置页加载
    for _ in range(5):
        if _is_settings_page(d):
            break
        time.sleep(0.6)

    # 直接尝试点击（退出登录可能已在屏内）
    if _try_click(d, text="退出登录"):
        logger.info("已点击「退出登录」")
        return True

    # 不在屏内 → 多次上滑直到找到或滑到底
    same_rounds = 0
    prev_xml_len = len(dump_xml(d))
    for i in range(12):
        _swipe_up(d, duration=0.4)
        time.sleep(0.6)
        if _try_click(d, text="退出登录"):
            logger.info(f"第 {i + 1} 次滑动后点击「退出登录」成功")
            return True
        # 检测是否滑到底（XML 长度不再变化且没有新内容）
        cur_xml_len = len(dump_xml(d))
        if abs(cur_xml_len - prev_xml_len) < 5:
            same_rounds += 1
            if same_rounds >= 3:
                logger.warning("已滑到底部仍未找到「退出登录」")
                break
        else:
            same_rounds = 0
        prev_xml_len = cur_xml_len

    logger.error("设置页未找到「退出登录」按钮")
    return False


def _confirm_logout(d) -> bool:
    """④ 弹窗确认退出登录，直到检测到未登录状态。"""
    for _ in range(6):
        if not is_logged_in(d):
            logger.info("✅ 已退出登录")
            return True
        xml = dump_xml(d)
        if "退出登录" in xml:
            # 弹窗确认按钮优先「退出登录」，其次「确定」
            if _try_click(d, text="退出登录"):
                logger.info("已点击确认弹窗的「退出登录」")
            elif _try_click(d, text="确定"):
                logger.info("已点击确认弹窗的「确定」")
            time.sleep(1.5)
        else:
            # 没有弹窗但仍显示已登录，可能确认弹窗还没出来，再等等
            time.sleep(1.0)
    if not is_logged_in(d):
        logger.info("✅ 已退出登录")
        return True
    logger.warning("确认退出后仍检测为已登录")
    return False


def logout(d) -> bool:
    """退出登录全流程（图片步骤 ①~④），每步带验证，失败即抛异常。

    ① 点左上角菜单按钮开侧边抽屉
    ② 点抽屉右下角齿轮设置图标
    ③ 设置页划到底点「退出登录」
    ④ 弹窗确认「退出登录」
    """
    logger.info("===== 开始登出腾讯元宝 =====")

    start_yuanbao(d)
    if not is_logged_in(d):
        logger.info("当前已是未登录状态，无需登出")
        return True

    # ---- ① 打开侧边抽屉 ----
    if not _open_drawer(d):
        raise RuntimeError("无法打开侧边抽屉（步骤①失败）")

    # ---- ② 点击抽屉右下角设置图标 ----
    if not _click_settings_in_drawer(d):
        raise RuntimeError("无法进入设置页（步骤②失败）")

    # ---- ③ 设置页点「退出登录」----
    if not _click_logout_in_settings(d):
        raise RuntimeError("设置页未找到「退出登录」（步骤③失败）")
    time.sleep(1.0)

    # ---- ④ 弹窗确认 ----
    return _confirm_logout(d)


# =====================================================================
#  登录（图片步骤 ⑤~⑩）
# =====================================================================

def _wait_landing(d, timeout=15) -> bool:
    """⑤ 等落地页「手机登录」/「登录后创建全新对话」出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = dump_xml(d)
        if "手机登录" in xml or "登录后创建全新对话" in xml or "登录即可体验更多功能" in xml:
            return True
        time.sleep(0.8)
    return False


def _click_phone_login(d) -> bool:
    """⑤ 点击「手机登录」按钮；没有则先点右上角「登录」唤起登录面板。"""
    if _try_click(d, text="手机登录"):
        return True
    # 有时落地页只有右上角「登录」入口，先点它
    if _try_click(d, text="登录"):
        time.sleep(1.5)
        if _try_click(d, text="手机登录"):
            return True
    # 底部弹层里的「手机登录」可能是 desc
    try:
        obj = d(description="手机登录")
        if obj.exists(timeout=2):
            obj.click()
            logger.info("已点击 desc=手机登录")
            return True
    except Exception:
        pass
    return False


def _agree_protocol(d) -> None:
    """⑥ 「服务协议及隐私保护」弹窗点「同意」。

    ⚠️ 只能精确匹配 text=同意 —— 绝不能用 textContains，
    否则会误点「不同意」。
    """
    for _ in range(4):
        xml = dump_xml(d)
        if "服务协议及隐私保护" in xml or "我已阅读并同意" in xml:
            if _try_click(d, text="同意"):
                logger.info("✅ 已同意服务协议")
                return
        # 没弹窗（可能已勾过协议）说明已进入下一步
        if "请输入手机号" in xml or "下一步" in xml:
            return
        time.sleep(0.8)
    logger.info("未出现服务协议弹窗（可能已同意过），继续")


def _input_phone(d, phone: str) -> bool:
    """⑦ 手机号登录页输入手机号（输入框 hint「请输入手机号」）。"""
    for _ in range(3):
        if "下一步" in dump_xml(d) or "请输入手机号" in dump_xml(d):
            break
        time.sleep(0.8)
    for kwargs in (
        {"textContains": "请输入手机号", "className": "android.widget.EditText"},
        {"className": "android.widget.EditText"},   # 页面上第一个 EditText 即手机号
    ):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=2):
                obj.click()
                time.sleep(0.3)
                try:
                    obj.clear_text()
                except Exception:
                    pass
                obj.set_text(phone)
                time.sleep(0.5)
                # 部分版本 set_text 不触发按钮态刷新，补一次字符级输入
                try:
                    if (obj.info.get("text") or "") != phone:
                        obj.clear_text()
                        d.shell(["input", "text", phone])
                        time.sleep(0.5)
                except Exception:
                    pass
                logger.info(f"已填入手机号 {phone}")
                return True
        except Exception:
            pass
    return False


def _click_next(d) -> bool:
    """⑦ 点「下一步」（输入手机号后按钮才变可用）。"""
    for _ in range(3):
        try:
            obj = d(text="下一步")
            if obj.exists(timeout=2):
                info = obj.info
                if info.get("enabled", True) is False:
                    logger.info("「下一步」还不可用，等待输入生效")
                    time.sleep(1.0)
                    continue
                obj.click()
                time.sleep(1.2)
                logger.info("已点击「下一步」")
                return True
        except Exception:
            pass
        time.sleep(0.8)
    # 兜底：desc / contains
    return _click_words(d, ("下一步",))


def _vcode_filled(d, vcode: str) -> bool:
    """检查验证码格子是否已填入（XML 里出现完整码或 >=6 个纯数字文本节点）。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(dump_xml(d))
    except Exception:
        return False
    digit_cnt = 0
    for node in root.iter("node"):
        t = (node.attrib.get("text") or "").strip()
        if not t:
            continue
        if t.replace(" ", "") == vcode:
            return True
        if t.isdigit():
            digit_cnt += 1
    return digit_cnt >= len(vcode)


def _input_vcode(d, vcode: str) -> bool:
    """⑧ 输入 6 位短信验证码。

    元宝验证码页的 EditText 在 XML 里能直接 set_text（探针 30_filled_fake.xml 已确认：
    set_text('000000') 后 XML 里 text='000000' 且每个格子 TextView 都显示对应数字）。
    优先用 set_text（最可靠），失败再逐位 send_keys，最后坐标点首格 + adb input。
    """
    vcode = str(vcode).strip()
    import random
    import xml.etree.ElementTree as ET

    def _title_anchor_click() -> bool:
        """兜底：点「输入 6 位验证码」标题下方居中横排的第一个格子。"""
        try:
            w, h = _screen_size(d)
            root = ET.fromstring(dump_xml(d))
            title_y = None
            for node in root.iter("node"):
                if "验证码" in (node.attrib.get("text") or ""):
                    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                                 node.attrib.get("bounds", ""))
                    if m:
                        title_y = int(m.group(4))
                        break
            if title_y is None:
                return False
            cy = title_y + int(h * 0.09)
            cx = int(w * 0.11)
            d.click(cx, cy)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def _focus_edittext() -> bool:
        try:
            edits = d(className="android.widget.EditText")
            if edits.exists(timeout=1):
                edits[0].click()
                time.sleep(0.3)
                return True
        except Exception:
            pass
        return _title_anchor_click()

    # 路径 1（优先）：EditText → 清 → set_text → 等 → 检查
    if _focus_edittext():
        try:
            d(className="android.widget.EditText")[0].clear_text()
        except Exception:
            pass
        logger.info(f"set_text 填入验证码 {vcode}")
        try:
            d(className="android.widget.EditText")[0].set_text(vcode)
        except Exception:
            pass
        time.sleep(0.8)
        if _vcode_filled(d, vcode):
            logger.info("✅ 已填入验证码")
            time.sleep(1.2)   # 元宝填完 6 位自动触发校验
            return True
        logger.warning("set_text 未能确认，逐位 send_keys 再试")

        # 路径 2（set_text 失败时）：逐位 send_keys
        try:
            d(className="android.widget.EditText")[0].clear_text()
        except Exception:
            pass
        logger.info("逐位输入验证码")
        for i, ch in enumerate(vcode):
            try:
                d.send_keys(ch)
            except Exception:
                d.shell(["input", "text", ch])
            time.sleep(random.uniform(0.2, 0.35))
        if _vcode_filled(d, vcode):
            logger.info("✅ 已逐位填入验证码")
            time.sleep(1.2)
            return True

    # 路径 3（兜底）：坐标点首格 + adb input
    if _title_anchor_click():
        d.shell(["input", "text", vcode])
        time.sleep(0.8)
        if _vcode_filled(d, vcode):
            logger.info("✅ 已按坐标点击填入验证码")
            time.sleep(1.2)
            return True

    # 实在没法确认 → 不 return False 让上层 abort，继续让元宝自己飞一会儿
    # （可能元宝内部状态已被 set_text 写入，只是 XML 检测不到）
    logger.warning("验证码填入未能确认，但继续主流程观察")
    return True


def _switch_to_keyboard(d) -> bool:
    """⑩ 默认「按住 说话」时点键盘图标切换为文字输入。"""
    xml = dump_xml(d)
    if "按住" not in xml:
        return True   # 已经是键盘模式
    # 语义词：desc/text = 键盘 / 切换键盘 / 显示键盘
    for w in ("键盘", "切换键盘", "显示键盘", "keyboard"):
        for kwargs in ({"description": w}, {"descriptionContains": w},
                       {"text": w}):
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=1):
                    obj.click()
                    time.sleep(1.0)
                    logger.info(f"已点击键盘按钮（{kwargs}）")
                    return True
            except Exception:
                pass
    # 坐标兜底：输入条右侧的键盘图标（真实截图实测比例）
    w, h = _screen_size(d)
    d.click(int(w * 0.85), int(h * 0.95))
    time.sleep(1.0)
    logger.info("已按坐标点击键盘图标")
    return True


def login_with_phone(d, phone: str, vcode: str | None = None, poller=None) -> bool:
    """完整登录流程（步骤 ⑤~⑩）。

    :param phone:   手机号
    :param vcode:   直接指定验证码（测试用）；None 时走 poller 轮询
    :param poller:  轮询函数 poller(after_ts) -> vcode，
                    after_ts 为「短信实际发出」的时间戳，只查该时间之后的新码
    """
    from captcha_solver import wait_captcha_and_solve, is_captcha_present  # 延迟导入

    logger.info(f"===== 开始登录腾讯元宝: {phone} =====")
    start_yuanbao(d)

    # 已登录先登出
    if is_logged_in(d):
        logger.info("检测到已登录，先登出")
        logout(d)
        start_yuanbao(d)

    # ---- ⑤ 等落地页 → 点「手机登录」----
    if not _wait_landing(d, timeout=15):
        # 兜底：重启 App 再等一轮
        force_reset(d)
        start_yuanbao(d, wait=5)
        if not _wait_landing(d, timeout=10):
            raise RuntimeError("落地页一直没出现「手机登录」按钮")
    if not _click_phone_login(d):
        raise RuntimeError("找不到「手机登录」按钮")
    time.sleep(1.2)

    # ---- ⑥ 「服务协议及隐私保护」弹窗 → 点「同意」----
    _agree_protocol(d)
    time.sleep(1.0)

    # ---- ⑦ 输入手机号 → 点「下一步」→ 短信自动发出 ----
    if not _input_phone(d, phone):
        raise RuntimeError("找不到手机号输入框")
    time.sleep(0.8)

    # ⚠️ 关键：点「下一步」后短信才发出，时间戳必须记在点击之前，
    #    轮询时只查这之后的新码，防止拿到历史旧码
    vcode_send_ts = time.time()
    if not _click_next(d):
        raise RuntimeError("找不到「下一步」按钮")
    logger.info("已点击「下一步」，等待短信发出")

    # 点「下一步」后可能立即弹人机验证（先过验证才发码的版本）
    if is_captcha_present(d):
        logger.info("「下一步」后出现人机验证，先求解")
        if not wait_captcha_and_solve(d, appear_timeout=3.0):
            raise RuntimeError("人机验证多次尝试未通过")

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

    # ---- ⑧ 输入 6 位验证码 ----
    if not _input_vcode(d, vcode):
        raise RuntimeError("找不到验证码输入框")
    # 注：不再额外 sleep——元宝填完 6 位自动触发校验，
    #     后续 _wait_post_vcode 有 3 秒宽限期专门容忍这一步

    # ---- ⑨ 验证码提交后：等页面状态变化（登录 / 弹人机验证 / 卡死）----
    # 可能出现的路径：
    #   A) 直接登录成功（is_logged_in=True）→ _wait_post_vcode 正常返回
    #   B) 弹出人机验证 → _wait_post_vcode 正常返回 → 下面 if 分支接 captcha_solver
    #   C) 卡死（白屏/WebView 渲染失败/App 停止响应）→ _wait_post_vcode 抛 StuckAfterVcode
    _wait_post_vcode(d, appear_timeout=45.0)

    # ---- ⑩ 如果弹出了人机验证（captcha_solver 求解）----
    if is_captcha_present(d):
        logger.info("验证码提交后检测到人机验证，开始 AI 求解")
        if not wait_captcha_and_solve(d, appear_timeout=5.0):
            raise RuntimeError("人机验证多次尝试未通过")

    # ---- 确认登录成功 ----
    for _ in range(12):
        if is_logged_in(d):
            # ⑩ 关闭「无水印保存功能」等登录后弹窗
            _dismiss_popups(d)
            time.sleep(1.0)
            # 默认「按住 说话」→ 切换键盘输入
            try:
                _switch_to_keyboard(d)
            except Exception as exc:
                logger.debug(f"切换键盘输入异常（继续）: {exc}")
            _dismiss_popups(d)
            # 抽屉若张开，关闭避免遮挡主界面
            close_drawer_if_open(d)
            logger.info(f"✅ 手机号 {phone} 登录成功")
            return True
        time.sleep(1.0)

    raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")
