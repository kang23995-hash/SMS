# -*- coding: utf-8 -*-
"""
豆包 App 登录 / 登出 UI 自动化 —— 基于真实 dump XML 的稳定版。

所有 resource-id / content-desc 都来自雷电模拟器 900x1600 真实 dump，
不再瞎猜。核心元素一览：

    汉堡菜单  com.larus.nova:id/back_icon                 desc="对话列表"
    齿轮      com.larus.nova:id/layout_root               desc="设置页"
    新对话    com.larus.nova:id/larus_chat_top_left_create_new_cvs  desc="创建新对话"
    输入框    com.larus.nova:id/input_text
    消息列表  com.larus.nova:id/message_list
    用户名    com.larus.nova:id/tv_username               text="男大"
    侧栏      com.larus.nova:id/side_bar_container
"""

from __future__ import annotations

import logging
import re
import time
import subprocess

logger = logging.getLogger(__name__)

DOUBAO_PACKAGE = "com.larus.nova"

# ============ 真实 resource-id / content-desc（来自 dump）============
R_ID_BACK_ICON = "com.larus.nova:id/back_icon"
R_ID_NEW_CHAT = "com.larus.nova:id/larus_chat_top_left_create_new_cvs"
R_ID_INPUT = "com.larus.nova:id/input_text"
R_ID_MESSAGE_LIST = "com.larus.nova:id/message_list"
R_ID_MSG_LIST_PARENT = "com.larus.nova:id/message_list_parent"
R_ID_PROFILE_CONTAINER = "com.larus.nova:id/profile_container"

# content-desc 常量
DESC_BACK_ICON = "对话列表"
DESC_GEAR = "设置页"            # 齿轮按钮在 action_container 里，desc 是 "设置页"
DESC_NEW_CHAT = "创建新对话"


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
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    return result


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
    lines = result.stdout.decode("utf-8", errors="ignore").splitlines()
    devices = []
    for line in lines[1:]:
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
    import uiautomator2 as u2
    serial = get_device_serial()
    logger.info(f"uiautomator2 连接: {serial}")
    d = u2.connect(serial)
    info = d.info
    logger.info(f"已连接: {info.get('productName') or ''} "
                f"{info.get('displayWidth')}x{info.get('displayHeight')}")
    return d


def dump_xml(d) -> str:
    return d.dump_hierarchy(compressed=False)


def _adb_tap(x: int, y: int) -> bool:
    try:
        return run_adb("shell", "input", "tap", str(x), str(y), timeout=10).returncode == 0
    except Exception:
        return False


def current_is_doubao(d) -> bool:
    try:
        return d.app_current().get("package", "") == DOUBAO_PACKAGE
    except Exception:
        return False


def start_doubao(d, wait=4):
    logger.info("拉起豆包 App")
    d.app_start(DOUBAO_PACKAGE, stop=False)
    time.sleep(wait)
    if not current_is_doubao(d):
        d.app_start(DOUBAO_PACKAGE, stop=False)
        time.sleep(wait)


# =====================================================================
#  通用点击辅助
# =====================================================================

def _try_click(d, **kwargs) -> bool:
    """按任意 uiautomator2 选择器点击（resourceId= / text= / description= ...）。"""
    try:
        obj = d(**kwargs)
        if obj.exists(timeout=3):
            obj.click()
            time.sleep(0.8)
            logger.info(f"已点击 {kwargs}")
            return True
    except Exception:
        pass
    return False


def _swipe_up(d, duration=0.35):
    try:
        w, h = d.info["displayWidth"], d.info["displayHeight"]
    except Exception:
        w, h = 900, 1600
    cx = w // 2
    d.swipe(cx, int(h * 0.75), cx, int(h * 0.25), duration=duration)


def _safe_dump(d) -> str:
    """dump 失败时返回空串，避免设备忙时直接把流程打断。"""
    try:
        return dump_xml(d)
    except Exception as e:
        logger.debug(f"dump 失败: {e}")
        return ""


# =====================================================================
#  侧栏（抽屉）与齿轮定位 —— 多级兜底
# =====================================================================
#
# 背景：豆包侧栏底部是「用户名 + 扫一扫 + 通知 + 设置(齿轮)」一排图标，
# 齿轮在最右侧。不同版本里齿轮的 content-desc 不一定是「设置页」，
# 甚至完全没有 desc —— 只靠 description="设置页" 选择器必然找不到。
# 因此这里做多级兜底：选择器 → XML 关键词 → 用户名锚点定位 → adb 坐标点击。

# 齿轮可能出现的 content-desc / text 关键词（兼容多版本措辞）
GEAR_KEYWORDS = ("设置页", "设置", "齿轮", "settings", "setting", "gear")
_GEAR_KEYWORDS_LOWER = {k.lower() for k in GEAR_KEYWORDS}
# 齿轮可能出现的 resource-id 关键词
GEAR_RID_KEYWORDS = ("setting", "gear", "config")


def _iter_nodes(xml: str):
    """把 dump 出来的 XML 拆成节点列表，并解析好 bounds / center。

    返回 [{...原始属性..., "_bounds": (x1,y1,x2,y2), "_center": (cx,cy)}, ...]
    """
    nodes = []
    for m in re.finditer(r"<node\b[^>]*?/?>", xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(0)))
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not bm:
            continue
        x1, y1, x2, y2 = (int(v) for v in bm.groups())
        attrs["_bounds"] = (x1, y1, x2, y2)
        attrs["_center"] = ((x1 + x2) // 2, (y1 + y2) // 2)
        nodes.append(attrs)
    return nodes


def _gear_node_exact(xml: str):
    """严格匹配齿轮：content-desc 恰为「设置页/设置」等，或 resource-id 命中设置。

    之所以先严格匹配，是因为首页顶栏有个 content-desc="豆包，打开设置页" 的
    干扰节点（包含「设置页」字样但不是齿轮），contains 匹配会误选它。
    """
    if not xml:
        return None
    nodes = _iter_nodes(xml)

    # A. content-desc 恰好等于某个关键词
    for n in nodes:
        if (n.get("content-desc") or "").strip().lower() in _GEAR_KEYWORDS_LOWER:
            return n

    # B. resource-id 命中
    for n in nodes:
        rid = (n.get("resource-id") or "").lower()
        if any(k in rid for k in GEAR_RID_KEYWORDS):
            return n
    return None


def _find_gear_node(xml: str):
    """在 dump 里找齿轮节点：先严格匹配，再退化为关键词包含匹配。"""
    if not xml:
        return None
    node = _gear_node_exact(xml)
    if node is not None:
        return node

    # 退化：content-desc / text 包含关键词（如顶栏的「豆包，打开设置页」也能进设置）
    for kw in GEAR_KEYWORDS:
        k = kw.lower()
        for n in _iter_nodes(xml):
            if k in (n.get("content-desc") or "").lower() \
                    or k in (n.get("text") or "").lower():
                return n
    return None


def _sidebar_right_edge(nodes):
    """若 dump 里有侧栏容器，返回其右边界 x 坐标；没有则返回 None。"""
    for n in nodes:
        if "side_bar_container" in (n.get("resource-id") or ""):
            return n["_bounds"][2]
    return None


def _find_gear_by_position(xml: str):
    """位置兜底：以「用户名」为锚点，取同一行里用户名右侧最右的那个图标。

    豆包侧栏底部这一排 = 用户名 + 扫一扫 + 通知 + 设置(齿轮)，齿轮永远最右。
    注意：侧栏不一定铺满整个屏幕，右侧还露着主页面，所以要把候选限制在侧栏范围内，
    否则会误选到主页面底部的输入区按钮。
    """
    if not xml:
        return None
    nodes = _iter_nodes(xml)

    # 找用户名锚点：text 形如「用户470037」或 resource-id 含 username
    anchors = []
    for n in nodes:
        text = (n.get("text") or "").strip()
        rid = (n.get("resource-id") or "").lower()
        if (text.startswith("用户") and re.search(r"\d", text) and "协议" not in text) \
                or "username" in rid:
            anchors.append(n)
    if not anchors:
        return None
    anchor = max(anchors, key=lambda n: n["_bounds"][3])  # 取最靠下的

    _, ay1, ax2, ay2 = anchor["_bounds"]
    cy = (ay1 + ay2) // 2
    tol = max(24, (ay2 - ay1) * 2)  # 纵向容差
    sx2 = _sidebar_right_edge(nodes)  # 侧栏右边界

    row = []
    for n in nodes:
        bx1, by1, bx2, by2 = n["_bounds"]
        bw, bh = bx2 - bx1, by2 - by1
        # 只要图标尺寸的节点（过滤细线 / 整行大容器）
        if bw < 16 or bh < 16 or bw > 260 or bh > 260:
            continue
        # 限制在侧栏范围内，排除主页面（侧栏右侧）的元素
        if sx2 is not None and bx2 > sx2 + 5:
            continue
        # 同一行、且在用户名右侧
        if abs((by1 + by2) // 2 - cy) <= tol and bx1 >= ax2 - 10:
            row.append(n)
    if not row:
        return None
    # 优先「可点击」的节点——排除 action_container 这种整行不可点的容器，
    # 否则它会因为最宽（x2 最大）被误当成齿轮。
    clickable = [n for n in row if n.get("clickable") == "true"]
    pool = clickable or row
    return max(pool, key=lambda n: n["_bounds"][2])  # 最右 = 齿轮


def _find_gear_bottom_band(xml: str, w: int, h: int):
    """最后兜底：屏幕底部 15% 区域（且限制在侧栏宽度内）里最右侧的图标。"""
    if not xml:
        return None
    nodes = _iter_nodes(xml)
    band_top = int(h * 0.85)
    sx2 = _sidebar_right_edge(nodes)
    best = None
    for n in nodes:
        x1, y1, x2, y2 = n["_bounds"]
        bw, bh = x2 - x1, y2 - y1
        if y1 < band_top or bw < 24 or bh < 24 or bw > 200 or bh > 200:
            continue
        if sx2 is not None and x2 > sx2 + 5:
            continue
        if n.get("clickable") != "true" and "Image" not in (n.get("class") or ""):
            continue
        if best is None or x2 > best["_bounds"][2]:
            best = n
    return best


def _sidebar_is_open(xml: str) -> bool:
    """粗判侧栏是否已展开（存在侧栏容器 / 用户名 / 齿轮）。

    注意这里用严格齿轮匹配，避免把首页顶栏的「豆包，打开设置页」误判成侧栏已开，
    否则侧栏明明是关的却不再点汉堡菜单，导致后面找不到齿轮。
    """
    if not xml:
        return False
    if "side_bar_container" in xml:
        return True
    if _gear_node_exact(xml) is not None:
        return True
    for n in _iter_nodes(xml):
        rid = (n.get("resource-id") or "").lower()
        if "username_container" in rid or "tv_username" in rid:
            return True
        text = (n.get("text") or "").strip()
        if text.startswith("用户") and re.search(r"\d", text) and "协议" not in text:
            return True
    return False


def _open_sidebar(d) -> bool:
    """确保侧栏处于展开状态；已经开了就不再点，避免把手风琴菜单点关。"""
    for _ in range(4):
        if _sidebar_is_open(_safe_dump(d)):
            return True
        # 点汉堡菜单：resource-id=back_icon，desc="对话列表"
        clicked = (
            _try_click(d, resourceId=R_ID_BACK_ICON)
            or _try_click(d, description=DESC_BACK_ICON)
            or _try_click(d, descriptionContains="对话列表")
        )
        if not clicked:
            logger.warning("没点开汉堡菜单（可能本来就开着或当前不在首页）")
        time.sleep(1.0)
    return _sidebar_is_open(_safe_dump(d))


def _tap_bounds(d, cx: int, cy: int, tag: str = "") -> bool:
    """优先用 adb 点坐标；adb 失败再回退 uiautomator2 的 d.click。"""
    if _adb_tap(cx, cy):
        time.sleep(1.0)
        logger.info(f"adb 已点击坐标 ({cx},{cy}) {tag}")
        return True
    try:
        d.click(cx, cy)
        time.sleep(1.0)
        logger.info(f"uiautomator2 已点击坐标 ({cx},{cy}) {tag}")
        return True
    except Exception:
        return False


def _click_gear(d) -> bool:
    """点侧栏里的齿轮（设置页）。多级兜底：

      1) uiautomator2 选择器（content-desc / text / resource-id）
      2) 解析 XML 按关键词定位齿轮节点 → adb 点坐标
      3) 以用户名为锚点，取同一行最右图标（齿轮）→ adb 点坐标
      4) 底部工具条区域最右侧图标 → adb 点坐标
    """
    # ---------- 1) 选择器直取 ----------
    for kwargs in (
        {"description": DESC_GEAR},
        {"descriptionContains": "设置"},
        {"descriptionContains": "setting"},
        {"text": "设置页"},
    ):
        if _try_click(d, **kwargs):
            return True

    # ---------- 2)(3)(4) XML 定位 + adb 坐标点击 ----------
    xml = _safe_dump(d)
    node = _find_gear_node(xml) or _find_gear_by_position(xml)
    if node is None:
        try:
            w, h = d.info["displayWidth"], d.info["displayHeight"]
        except Exception:
            w, h = 720, 1280
        node = _find_gear_bottom_band(xml, w, h)

    if node is not None:
        cx, cy = node["_center"]
        tag = f"({node.get('content-desc') or node.get('resource-id') or 'bottom-bar'!r})"
        return _tap_bounds(d, cx, cy, tag)

    # 全部失败才报错，并把侧栏 XML 头部打出来方便排查
    logger.warning("齿轮定位失败，侧栏 XML 片段如下（供排查）：\n%s", xml[:2500])
    return False


# =====================================================================
#  判断登录状态
# =====================================================================

def is_logged_in(d) -> bool:
    """判断当前是否已登录。

    已登录 → 存在「新对话」按钮（resource-id 或 content-desc）
    未登录 → 显示「登录以解锁更多功能」或「手机号登录」
    """
    xml = dump_xml(d)

    # 标志 A：登录页特征
    if "登录以解锁更多功能" in xml or "手机号登录" in xml:
        # 但如果同时有「退出登录」弹窗，那是登出中
        if "确认退出登录" not in xml:
            return False

    # 标志 B：已登录 → 有新对话按钮
    if R_ID_NEW_CHAT in xml or DESC_NEW_CHAT in xml:
        return True

    # 标志 C：已登录 → 侧栏底部能看到用户名（tv_username / username_container）
    for n in _iter_nodes(xml):
        rid = (n.get("resource-id") or "").lower()
        if "username_container" in rid or "tv_username" in rid:
            return True
        t = (n.get("text") or "").strip()
        if t.startswith("用户") and re.search(r"\d", t) and "协议" not in t:
            return True

    # 标志 D：已登录 → 侧栏容器存在 / 侧栏里的齿轮（设置页）存在
    if "side_bar_container" in xml or _find_gear_node(xml) is not None:
        return True

    return False


# =====================================================================
#  登出
# =====================================================================

def logout(d) -> bool:
    """退出登录。基于真实 dump：

      1. 确保侧栏展开（已开就不再点，避免点关）
      2. 在侧栏里点齿轮（设置页）—— 多级兜底定位
      3. 设置页滚到底部点「退出登录」
      4. 弹窗点「退出登录」确认
    """
    logger.info("===== 开始登出 =====")

    # --- 第 1 步：确保侧栏展开 ---
    if not _open_sidebar(d):
        logger.warning("侧栏没能展开，按 back 归位后重试一次")
        try:
            d.press("back")
            time.sleep(1.0)
        except Exception:
            pass
        _open_sidebar(d)

    # --- 第 2 步：点齿轮（设置页）---
    # 齿轮在侧栏底部「用户名 + 扫一扫 + 通知 + 设置」这一排的最右，
    # 不同版本 content-desc 不一致甚至缺失，靠 _click_gear 多级兜底。
    if not _click_gear(d):
        raise RuntimeError("侧栏里找不到齿轮（设置页按钮）")

    time.sleep(1.0)

    # --- 第 3 步：设置页滚到底部 → 点「退出登录」---
    logout_btn_clicked = _try_click(d, text="退出登录")

    if not logout_btn_clicked:
        for _ in range(10):
            _swipe_up(d)
            time.sleep(0.5)
            if _try_click(d, text="退出登录"):
                logout_btn_clicked = True
                break

    if not logout_btn_clicked:
        raise RuntimeError("设置页翻到底都没找到「退出登录」")

    time.sleep(0.8)

    # --- 第 4 步：弹窗确认「退出登录」---
    # 弹窗标题 "确认退出登录?" + "取消" / "退出登录"
    for _ in range(5):
        if _try_click(d, text="退出登录"):
            time.sleep(1.0)
            if not is_logged_in(d):
                logger.info("✅ 已退出登录")
                return True
        time.sleep(0.5)

    # 兜底：按 Enter
    try:
        d.press("enter")
        time.sleep(1)
    except Exception:
        pass

    if not is_logged_in(d):
        logger.info("✅ 已退出登录")
        return True

    logger.warning("登出流程跑完但仍检测为已登录")
    return not is_logged_in(d)


# =====================================================================
#  登录
# =====================================================================

def login_with_phone(
    d,
    phone: str,
    vcode: str | None = None,
    poller=None,
) -> bool:
    """完整登录流程。

    1. 确保已到登录页（「手机号登录」可见）
    2. 勾「已阅读并同意」复选框
    3. 点「手机号登录」按钮
    4. 输入手机号 → 点「下一步」（豆包自动发验证码）
    5. 轮询服务端拿验证码 → 填入 → 等待登录成功
    """
    logger.info(f"===== 开始登录: {phone} =====")

    # 先检测是否已经在手机号输入页（force_relogin 可能已提前进入登录流程）
    xml = dump_xml(d)
    already_on_phone_input = "请输入手机号" in xml or "下一步" in xml or "获取验证码" in xml

    if already_on_phone_input:
        logger.info("已在手机号输入页，跳过 start_doubao 和登出逻辑")
    else:
        # 确保已拉起豆包
        start_doubao(d)

        # ---- 新流程：主页检测「立即登录，解锁更多功能」----
        # 如果主页有这个字样，说明当前未登录，直接点击去登录
        # 如果没有，说明已登录，按老流程登出
        xml = dump_xml(d)
        if "立即登录" in xml and "解锁更多功能" in xml:
            logger.info("🔔 检测到主页「立即登录，解锁更多功能」，直接点击去登录")
            # 点击主页的「立即登录，解锁更多功能」
            # 用 textContains 匹配（文字含箭头 →，精确匹配容易失败）
            clicked = False
            for text in ("立即登录，解锁更多功能", "立即登录", "解锁更多功能"):
                try:
                    obj = d(textContains=text)
                    if obj.exists(timeout=2):
                        obj.click()
                        clicked = True
                        logger.info(f"✅ 已点击「{text}」")
                        break
                except Exception:
                    pass
            if not clicked:
                # 坐标兜底：「立即登录」在主页顶部偏右，约 50% 屏宽、25% 屏高
                w, h = d.info["displayWidth"], d.info["displayHeight"]
                d.click(int(w * 0.50), int(h * 0.25))
                logger.info("✅ 已按坐标点击「立即登录」")
            time.sleep(2.0)  # 等登录弹窗加载

            # 点击弹窗中的「手机号登录」
            if not _try_click(d, text="手机号登录"):
                # 弹窗可能直接就是手机号登录页，跳过
                logger.info("未检测到「手机号登录」弹窗按钮，可能已直接进入登录页")
        else:
            # 已登录，按老流程登出
            if is_logged_in(d):
                logger.info("检测到已登录，先登出")
                logout(d)

            start_doubao(d)

            # --- 确保在登录页 ---
            for _ in range(4):
                xml = dump_xml(d)
                if "手机号登录" in xml or "登录以解锁更多功能" in xml:
                    break
                try:
                    d.press("back")
                    time.sleep(0.8)
                except Exception:
                    pass

            # 强制兜底：重启
            xml = dump_xml(d)
            if "手机号登录" not in xml and "登录以解锁更多功能" not in xml:
                try:
                    d.app_stop(DOUBAO_PACKAGE)
                except Exception:
                    pass
                start_doubao(d, wait=5)

    # --- 勾「已阅读并同意」复选框（仅在非手机号输入页时需要）---
    if not already_on_phone_input:
        agreed = False
        try:
            for kwargs in (
                {"className": "android.widget.CheckBox"},
                {"checkable": True, "className": "android.view.ViewGroup"},
            ):
                try:
                    obj = d(**kwargs)
                    if obj.exists(timeout=2):
                        info = obj.info
                        if not info.get("checked", False):
                            obj.click()
                            time.sleep(0.5)
                        agreed = True
                        break
                except Exception:
                    pass
        except Exception:
            pass

        if not agreed:
            # 按坐标兜底：复选框在屏幕左下偏左
            try:
                w, h = d.info["displayWidth"], d.info["displayHeight"]
                d.click(int(w * 0.08), int(h * 0.87))
                time.sleep(0.5)
                agreed = True
                logger.info("已按坐标勾协议复选框")
            except Exception:
                logger.warning("勾复选框失败 —— 可能已经勾着了")

        # --- 点「手机号登录」按钮 ---
        if not _try_click(d, text="手机号登录"):
            raise RuntimeError("登录页找不到「手机号登录」按钮")
        time.sleep(1.5)

    # --- 输入手机号 ---
    # 登录页的 EditText 没有已知 resource-id，用 hint 匹配
    phone_inputted = False
    try:
        for kwargs in (
            {"textContains": "请输入手机号", "className": "android.widget.EditText"},
            {"textContains": "请输入11位手机号", "className": "android.widget.EditText"},
            {"className": "android.widget.EditText"},
        ):
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=3):
                    obj.click()
                    time.sleep(0.3)
                    try:
                        obj.clear_text()
                    except Exception:
                        pass
                    obj.set_text(phone)
                    logger.info("已填入手机号")
                    phone_inputted = True
                    break
            except Exception:
                pass
    except Exception:
        pass

    if not phone_inputted:
        raise RuntimeError("找不到手机号输入框")
    time.sleep(0.8)

    # --- 点「下一步」（豆包自动发验证码）---
    # ⚠️ 关键点：点下一步之前先记下当前时间，轮询时只查这个时间之后的新码
    # 否则会拿到历史遗留的旧验证码（之前踩过这个坑）
    import time as _t
    vcode_send_ts = _t.time()

    if not _try_click(d, text="下一步"):
        if not _try_click(d, text="继续"):
            raise RuntimeError("找不到「下一步」按钮")
    time.sleep(3.0)  # 等短信真发出去 + 跳转到验证码页

    # --- 轮询拿验证码（只查 vcode_send_ts 之后才收到的新码）---
    if not vcode and poller is not None:
        try:
            try:
                # poller 新版签名：poller(after_ts)，把时间戳传进去
                vcode = poller(vcode_send_ts)
            except TypeError:
                # 兼容旧版 poller() 无参调用（测试用）
                vcode = poller()
        except Exception as exc:
            raise RuntimeError(f"轮询验证码失败: {exc}")

    if not vcode:
        raise RuntimeError("拿不到验证码")

    vcode = str(vcode).strip()
    logger.info(f"拿到验证码: {vcode}")

    # --- 填入验证码 ---
    # 验证码页 6 格输入框，set_text 进去 App 会自动分发
    vcode_inputted = False
    try:
        for kwargs in (
            {"className": "android.widget.EditText"},
        ):
            try:
                obj = d(**kwargs)
                if obj.exists(timeout=3):
                    obj.click()
                    time.sleep(0.3)
                    obj.set_text(vcode)
                    vcode_inputted = True
                    logger.info("已填入验证码")
                    break
            except Exception:
                pass
    except Exception:
        pass

    if not vcode_inputted:
        raise RuntimeError("找不到验证码输入框")

    time.sleep(3.0)

    # --- 确认登录成功 ---
    for _ in range(10):
        if is_logged_in(d):
            logger.info(f"✅ 手机号 {phone} 登录成功")
            return True
        time.sleep(1.0)

    raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")


# =====================================================================
#  入口别名（run.py 需要）
# =====================================================================

def force_reset(d):
    try:
        d.app_stop(DOUBAO_PACKAGE)
        time.sleep(2)
    except Exception:
        pass


force_logout_if_needed = force_reset
