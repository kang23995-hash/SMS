# -*- coding: utf-8 -*-
"""
qianwen_login.py —— 千问 App 登出 / 登录 UI 自动化
======================================================

对应用户标注图片的 ①~⑧ 步骤：

  【登出】
    ① 点左上角三条横线按钮打开左侧抽屉
    ② 点抽屉左下角"我的"头像（图片显示是个圆形头像+我的+）进入设置
    ③ 我的页面往下滑到底部，点"退出登录"
    ④ 弹窗点"退出登录"确认

  【登录】（常规流程点"下一步"直接发短信；偶发「发送短信码」人机验证）
    ⑤ 未登录主界面有"立即登录，解锁更多功能"按钮（或直接点左上角抽屉后的某个入口）
       或者 App 已自动进入登录页面（输入手机号页面有千问 Logo + 输入手机号 + 下一步）
    ⑥ 输入手机号 → 点"下一步"
    ⑦ 可能先弹"用户协议及隐私协议" → 点"同意"（协议已勾选就直接过）
       ⚠️ 偶然会弹「身份验证 / 发送短信码」页：要求用当前登录手机号给阿里
       服务号发指定内容的短信，交给局域网 send_sms 手机代发（见 sms_verify.py）
    ⑧ 输入短信验证码（6 位）→ 点"登录" / 或自动登录

所有查找均「XML 文本/desc 优先 + 比例坐标兜底」，不假设具体 resource-id。
横竖屏均按运行时实际分辨率适配。
"""

from __future__ import annotations

import logging
import random
import re
import subprocess
import time

logger = logging.getLogger(__name__)

from config import (
    QIANWEN_PACKAGE, QIANWEN_LAUNCHER_ACTIVITY, QIANWEN_COMPONENT,
    QIANWEN_PACKAGE_CANDIDATES,
)

# =====================================================================
#  界面特征词（用于状态判断与元素查找）
# =====================================================================

# 主界面（已登录）特征
# ★ 新版千问欢迎页核心特征："我是千问" + "为你答疑" —— 必须包含！
#   否则 is_logged_in 会假阴性，导致 logout 跳过 force-stop，残留会话被错误使用
LOGGED_IN_HINTS = (
    "我是千问", "为你答疑", "随时找我聊天",   # 新版欢迎页
    "发消息", "按住说话", "向千问提问", "千问办事", "AI 生图",  # 经典版
)
# 落地页（未登录）特征
LANDING_HINTS = ("立即登录", "手机验证码", "密码登录")
# 登录页（输入手机号）特征 —— 注意不要包含 "千问"（主界面 WebView 也可能带）
LOGIN_PAGE_HINTS = ("输入手机号", "下一步")
# 抽屉打开的 Hallmark 特征 —— 侧栏展开后这些词必然出现（主界面不会同时出现）
# 统一和 worker.py 一致的词表，确保两个文件的抽屉检测逻辑完全相同
DRAWER_OPEN_HINTS = (
    "云空间", "收藏", "档案", "定时任务", "对话历史",
    "新建对话", "新对话", "我的空间", "智能体",
)
# 左上角抽屉按钮候选 desc/text
DRAWER_OPEN_WORDS = ("打开侧边栏", "侧边栏", "打开抽屉", "对话历史", "历史记录",
                     "菜单", "返回", "Back", "Menu")
# 我的/设置入口候选
MY_ENTRY_WORDS = ("我的", "个人中心", "个人主页", "Profile")
# 登录后可能出现的干扰弹窗按钮
DISMISS_WORDS = ("以后再说", "暂不", "跳过", "取消", "我知道了", "稍后再说", "不允许")


# =====================================================================
#  基础工具
# =====================================================================

_device_serial: str | None = None   # 进程级缓存，只取一次


def run_adb(*args, timeout=30, serial=None):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(map(str, args))
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )


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
    try:
        return d.dump_hierarchy(compressed=False)
    except Exception:
        return ""


def _current_package(d) -> str:
    try:
        return d.app_current().get("package", "")
    except Exception:
        return ""


# =====================================================================
#  ★ 模拟器健康检查 & 白屏恢复
# =====================================================================

def _ensure_emulator_healthy(d, max_recover=2) -> bool:
    """force-stop 后雷电模拟器 SurfaceFlinger 可能死锁 → 整个窗口白屏。

    检测方法：dump_xml 连续 3 次只有空 FrameLayout（没有任何特征词）。
    恢复方法：发 HOME 键回桌面 → 等 SurfaceFlinger 有时间清层。
    """
    serial = get_device_serial()
    ready_hints = ("千问", "发消息", "按住说话", "立即登录", "输入手机号",
                   "我是千问", "深度思考", "下一步", "Settings", "Settings")

    for attempt in range(max_recover + 1):
        xml = dump_xml(d)
        if xml and any(h in xml for h in ready_hints):
            return True  # 健康！

        if attempt == max_recover:
            break

        logger.warning(f"⚠ XML 无特征词（疑似白屏），HOME + 等待恢复（第 {attempt+1} 次）")
        # HOME 键回桌面，让 SurfaceFlinger 处理残留 layer
        run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
        time.sleep(2.0)

        # 确认 HOME 生效
        xml = dump_xml(d)
        if xml and ("Settings" in xml or "Launcher" in xml or "Camera" in xml
                    or "Launcher" in _current_package(d) or xml.count("<node") < 50):
            logger.info("已回到桌面，等待 SurfaceFlinger 恢复 3s")
            time.sleep(3.0)
        else:
            time.sleep(2.0)

    return False


def _random_human_pause(min_ms=150, max_ms=350) -> None:
    """微随机延迟，模拟人类操作节奏（低成本 + 心理安慰）。"""
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


def _human_click_coords(d, x, y, jitter=5):
    """坐标点击加 ±jitter 像素随机偏移，模拟人类手指不精准。"""
    jx = x + random.randint(-jitter, jitter)
    jy = y + random.randint(-jitter, jitter)
    jx = max(0, jx)
    jy = max(0, jy)
    try:
        d.click(jx, jy)
    except Exception:
        d.click(x, y)


def _adb_tap(x: int, y: int) -> bool:
    """用 adb shell input tap 精准点击坐标。
    uiautomator2 的 d.click 在 clickable=false 时会失灵，adb tap 更可靠。
    """
    serial = get_device_serial()
    try:
        return run_adb("shell", "input", "tap", str(int(x)), str(int(y)),
                        timeout=10, serial=serial).returncode == 0
    except Exception:
        return False


def _tap_bounds(d, cx: int, cy: int, tag: str = "") -> bool:
    """优先 adb tap，失败再回退 uiautomator2 d.click。"""
    if _adb_tap(cx, cy):
        time.sleep(0.40)
        logger.info(f"adb 已点击坐标 ({cx},{cy}) {tag}")
        return True
    try:
        d.click(cx, cy)
        time.sleep(0.40)
        logger.info(f"uiautomator2 已点击坐标 ({cx},{cy}) {tag}")
        return True
    except Exception:
        return False


def _iter_nodes(xml: str):
    """正则方式快速 dump XML 拆成节点列表，并预解析好 bounds/center。
    返回 [{...原始属性..., "_bounds": (x1,y1,x2,y2), "_center": (cx,cy)}, ...]
    比 ElementTree 轻量，适合频繁解析的热路径（齿轮定位 / 侧栏检测）。
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


def _safe_dump(d) -> str:
    """dump 失败时返回空串，避免设备忙时直接把流程打断。"""
    try:
        return dump_xml(d)
    except Exception as e:
        logger.debug(f"dump 失败: {e}")
        return ""


# =====================================================================
#  侧栏抽屉 & 设置齿轮定位 —— 多级兜底（吸收 doubao_auto 方案）
# =====================================================================
#
# 千问侧栏布局（来自真实截图）：
#   顶部：用户头像 + 用户名（如 Qwen3401）
#   图标栏：云空间 | 收藏 | 档案 | 设置(齿轮)   ← 齿轮在最右
#   中间：定时任务 / 对话历史 / 历史会话列表
#   底部：新建对话按钮
#
# 齿轮按钮定位策略（四级兜底）：
#   1) 严格匹配 content-desc / text / resource-id
#   2) XML 关键词包含匹配
#   3) 侧栏图标栏最右侧位置兜底
#   4) adb 固定比例坐标兜底

# 齿轮可能的 content-desc / text / resource-id 关键词
GEAR_DESC_KEYWORDS = ("设置", "设置页", "齿轮", "settings", "setting", "gear",
                      "Setting", "Settings")
_GEAR_DESC_LOWER = {k.lower() for k in GEAR_DESC_KEYWORDS}
GEAR_RID_KEYWORDS = ("setting", "gear", "config")


def _gear_node_exact(xml: str):
    """严格匹配齿轮节点：content-desc 恰好等于关键词，或 resource-id 命中设置。

    先严格匹配是因为顶栏可能有「豆包，打开设置页」这类包含关键词但不是齿轮的
    干扰节点（千问首页顶栏也有类似的 "打开侧边栏" desc）。
    """
    if not xml:
        return None
    nodes = _iter_nodes(xml)

    # A. content-desc 恰好等于某个关键词
    for n in nodes:
        desc = (n.get("content-desc") or "").strip().lower()
        if desc in _GEAR_DESC_LOWER:
            return n

    # B. text 恰好等于某个关键词
    for n in nodes:
        text = (n.get("text") or "").strip().lower()
        if text in _GEAR_DESC_LOWER:
            return n

    # C. resource-id 命中
    for n in nodes:
        rid = (n.get("resource-id") or "").lower()
        if any(k in rid for k in GEAR_RID_KEYWORDS):
            return n
    return None


def _find_gear_node(xml: str):
    """先严格匹配齿轮节点，找不到再退化到关键词包含匹配。"""
    if not xml:
        return None
    node = _gear_node_exact(xml)
    if node is not None:
        return node

    # 退化：content-desc / text / resource-id 包含关键词
    for kw in GEAR_DESC_KEYWORDS:
        k = kw.lower()
        for n in _iter_nodes(xml):
            if k in (n.get("content-desc") or "").lower() \
                    or k in (n.get("text") or "").lower():
                return n
    return None


def _find_gear_by_sidebar_icons(xml: str, w: int, h: int):
    """位置兜底：侧栏图标栏最右侧的那个图标 = 设置齿轮。

    千问侧栏中部有一排图标：云空间 / 收藏 / 档案 / 设置，
    齿轮永远是最右侧那个可点击的图标节点。
    """
    if not xml:
        return None
    nodes = _iter_nodes(xml)

    # 找所有符合「图标尺寸」的节点（排除细线 / 文字容器）
    icon_candidates = []
    for n in nodes:
        x1, y1, x2, y2 = n["_bounds"]
        bw, bh = x2 - x1, y2 - y1
        # 图标一般在 40~120px 之间，太细或太大都不是
        if bw < 30 or bh < 30 or bw > 200 or bh > 200:
            continue
        # 纵向限制在屏幕顶部 45% 区域内（侧栏图标栏在顶部）
        if y1 > h * 0.45:
            continue
        # 横向限制在屏幕左侧 55%（抽屉不可能铺满整个屏幕）
        if x2 > w * 0.55:
            continue
        # 必须可点击或是 ImageView
        if n.get("clickable") != "true" and "Image" not in (n.get("class") or ""):
            continue
        icon_candidates.append(n)

    if not icon_candidates:
        return None

    # 最右侧 = x2 最大
    return max(icon_candidates, key=lambda n: n["_bounds"][2])


# =====================================================================
#  ★ 启动 / 停止（重写！）
# =====================================================================

def _cold_start_qianwen_with_adb() -> None:
    """非阻塞冷启动千问（**不带 -W**！）。

    为什么不能用 am start -W：
      -W 会阻塞直到 ActivityManager 发出「Window Focused」信号
      force-stop 后立即启动 → 进程 attach 竞态 → 进程挂掉 →
      ActivityManager 永远不会发 Focused → -W 阻塞 45s →
      更糟的是雷电模拟器 SurfaceFlinger 卡死 → 整个窗口白屏。
    所以我们非阻塞发 am start，然后靠自己的 _wait_qianwen_ready poll。
    """
    serial = get_device_serial()

    # 先 HOME 一次 —— SurfaceFlinger 残留清层
    run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
    time.sleep(1.5)

    logger.info(f"[ADB] am start (non-blocking) -n {QIANWEN_COMPONENT}")
    result = run_adb(
        "shell", "am", "start", "-n", QIANWEN_COMPONENT,
        timeout=15, serial=serial,
    )
    out = result.stdout.decode("utf-8", errors="ignore").strip()
    err = result.stderr.decode("utf-8", errors="ignore").strip()
    logger.info(f"[ADB] am start out={out[:150]} | err={err[:80]}")

    if "Error" in out or "does not exist" in out:
        raise RuntimeError(f"千问启动失败: {out}")

    # 启动后等 SurfaceFlinger 开始渲染
    time.sleep(2.0)


def _wait_qianwen_ready(d, timeout=20) -> bool:
    """启动后轮询 dump_xml，直到出现特征词。"""
    ready_hints = ("千问", "发消息", "按住说话", "立即登录", "输入手机号",
                   "我是千问", "深度思考", "下一步")
    deadline = time.time() + timeout
    start = time.time()
    rounds = 0
    last_empty = 0
    while time.time() < deadline:
        rounds += 1
        xml = dump_xml(d)
        if xml and any(h in xml for h in ready_hints):
            elapsed = time.time() - start
            logger.info(f"✓ 千问加载完成（第 {rounds} 轮，耗时 {elapsed:.1f}s）")
            return True
        if not xml or xml.count("<node") < 3:
            last_empty += 1
            if last_empty >= 4:
                # 连续空 XML —— 可能白屏，HOME 恢复
                logger.warning(f"连续 {last_empty} 轮 XML 空（疑似白屏），HOME 恢复")
                serial = get_device_serial()
                run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
                time.sleep(2.0)
                last_empty = 0
        time.sleep(1.0)
    logger.warning(f"千问加载超时（{timeout}s，{rounds} 轮）")
    return False


def resolve_qianwen_package(d) -> str:
    """确认千问包名（仅 com.aliyun.tongyi 有效）。"""
    pkg = _current_package(d)
    if pkg == QIANWEN_PACKAGE:
        return pkg
    # 用 adb 非阻塞启动一次
    try:
        _cold_start_qianwen_with_adb()
        if _wait_qianwen_ready(d, timeout=20):
            if _current_package(d) == QIANWEN_PACKAGE:
                logger.info(f"千问包名确认：{QIANWEN_PACKAGE}")
                return QIANWEN_PACKAGE
    except Exception as exc:
        logger.warning("resolve_qianwen_package 启动异常：%s", exc)
    raise RuntimeError(
        f"无法启动千问 App（{QIANWEN_COMPONENT}），"
        "请确认已安装并能在模拟器中手动打开。"
    )


def start_qianwen(d, wait=4) -> str:
    """拉起千问到前台。

    策略：
      - 当前已经是千问 → 确认一下不是白屏（dump 有特征词）就直接返回
      - 否则 HOME + 非阻塞 am start + 轮询等待
    """
    cur = _current_package(d)
    if cur == QIANWEN_PACKAGE:
        if _ensure_emulator_healthy(d):
            logger.info("千问已在前台，无需重启")
            return QIANWEN_PACKAGE

    logger.info("拉起千问 App（HOME + 非阻塞 am start -n 正确 Activity）")

    # 先 HOME 回桌面，确保 SurfaceFlinger 干净
    serial = get_device_serial()
    run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
    time.sleep(1.0)

    try:
        _cold_start_qianwen_with_adb()
    except Exception as exc:
        logger.warning(f"am start 异常：{exc}")
        # monkey 1 次兜底启动
        logger.info("monkey -p ... 1 兜底启动")
        run_adb("shell", "monkey", "-p", QIANWEN_PACKAGE, "-c",
                "android.intent.category.LAUNCHER", "1",
                timeout=15, serial=serial)
        time.sleep(2.0)

    # 等待加载
    _wait_qianwen_ready(d, timeout=20)
    _ensure_emulator_healthy(d)
    return QIANWEN_PACKAGE


def force_reset(d=None) -> None:
    """杀千问进程 —— 只在万不得已时用。

    ⚠ force-stop 后**必须** HOME 一次让 SurfaceFlinger 清层，
    否则雷电模拟器可能白屏。不再立即 start，由调用方决定何时重启。
    """
    serial = get_device_serial()
    logger.info(f"[ADB] force-stop {QIANWEN_PACKAGE}")
    run_adb("shell", "am", "force-stop", QIANWEN_PACKAGE, timeout=15, serial=serial)
    # 关键：杀完进程后立即 HOME —— 让 SurfaceFlinger 清掉所有千问 layer
    run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
    time.sleep(2.5)


force_logout_if_needed = force_reset  # 兼容接口名


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


def _swipe_down(d, duration=0.35):
    w, h = _screen_size(d)
    d.swipe(w // 2, int(h * 0.25), w // 2, int(h * 0.75), duration=duration)


def _dismiss_popups(d, rounds=2):
    """登录后常见干扰弹窗（通知权限/更新提示等）统一关闭。"""
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
#  抽屉状态检测
# =====================================================================

def _drawer_is_open(d) -> bool:
    """左侧抽屉是否张开。"""
    xml = dump_xml(d)
    return any(h in xml for h in DRAWER_OPEN_HINTS)


def close_drawer_if_open(d) -> bool:
    """若左侧抽屉张开则按 back 关闭。"""
    if not _drawer_is_open(d):
        return True
    try:
        d.press("back")
        time.sleep(0.8)
        if not _drawer_is_open(d):
            logger.info("✅ 已关闭左侧抽屉")
            return True
    except Exception:
        pass
    return False


# =====================================================================
#  登录状态判断
# =====================================================================

def is_logged_in(d, retries=3, interval=2.0) -> bool:
    """已登录 = 主聊天界面可见（输入框/深度思考等），且无登录页特征。

    ★ 加 retries：千问冷启动时 XML 可能先是空的 FrameLayout（App 还在加载），
      单次 dump 容易误判成"未登录"导致触发不必要的 force_reset + 重登链。
      默认重试 3 次、每次隔 2s —— 冷启动 6s 内足够完成加载。
    """
    last_result = False
    for attempt in range(1, retries + 1):
        xml = dump_xml(d)
        if not xml:
            logger.debug(f"is_logged_in 第 {attempt} 轮：XML 空（App 未就绪）")
            time.sleep(interval)
            continue

        # 先排"明确未登录"信号
        landing = any(h in xml for h in LANDING_HINTS)   # 立即登录/手机验证码/密码登录
        loginpg = any(h in xml for h in LOGIN_PAGE_HINTS)  # 输入手机号/下一步/千问
        loggedin = any(h in xml for h in LOGGED_IN_HINTS)  # 发消息/按住说话/AI 生图

        if landing and not loggedin:
            last_result = False
        elif loginpg and "输入手机号" in xml and not loggedin:
            last_result = False
        elif loggedin:
            last_result = True
        else:
            # 都没命中，说明 App 可能还在加载
            last_result = False

        # 第一次直接返回如果明确命中
        if attempt == 1 and loggedin and not landing and not loginpg:
            logger.debug(f"is_logged_in 第 {attempt} 轮：✓ 已明确是已登录")
            return True
        if attempt == 1 and last_result is False and (landing or loginpg):
            logger.debug(f"is_logged_in 第 {attempt} 轮：明确未登录 ({landing=}, {loginpg=})")
            return False

        # 后续轮继续等
        logger.debug(f"is_logged_in 第 {attempt}/{retries} 轮：result={last_result} "
                      f"landing={landing} loginpg={loginpg} loggedin={loggedin}")
        time.sleep(interval)

    logger.debug(f"is_logged_in 最终返回 {last_result}")
    return last_result


# =====================================================================
#  登出（图片步骤 ①~④）—— 吸收 doubao_auto 多级兜底方案
# =====================================================================
#
# 千问 App 退出登录完整流程（来自真实截图）：
#   ① 点左上角三条横线（汉堡菜单）→ 打开左侧抽屉
#   ② 在抽屉图标栏点「设置」齿轮按钮 → 进入设置页面
#   ③ 设置页面滚到底部 → 点「退出登录」
#   ④ 弹窗确认「退出登录」

# 左上角汉堡按钮可能的 content-desc / text 关键词
HAMBURGER_DESC_KEYWORDS = ("打开侧边栏", "侧边栏", "打开抽屉", "对话列表",
                           "菜单", "Menu", "menu", "导航", "Navigation", "navigation")
HAMBURGER_RID_KEYWORDS = ("menu", "hamburger", "navigation", "drawer", "back_icon")


def _drawer_is_open_by_xml(xml: str, w: int, h: int) -> bool:
    """更精准的抽屉打开检测：
    侧栏里能找到齿轮（设置）或用户名字样，且节点限制在屏幕左半部分。
    """
    if not xml:
        return False
    # 齿轮严格匹配
    if _gear_node_exact(xml) is not None:
        return True
    nodes = _iter_nodes(xml)
    for n in nodes:
        value = (n.get("text") or n.get("content-desc") or "").strip()
        if not value:
            continue
        # 命中抽屉特征词，且节点在屏幕左侧
        if any(kw in value for kw in DRAWER_OPEN_HINTS):
            x1 = n["_bounds"][0]
            if x1 < w * 0.30:
                return True
    return False


def _drawer_open_evidence(d, w: int, h: int) -> bool:
    """抽屉打开证据：必须在抽屉区域匹配到设置图标。

    不再仅凭 XML 文案判断：WebView 可能把隐藏节点也导出，曾把主页误判为抽屉。
    """
    try:
        from template_matcher import DRAWER_REGION, find_template
        return find_template(
            "open_setting", threshold=0.75,
            region=DRAWER_REGION, save_fail=False,
        ) is not None
    except Exception:
        return False


def _open_drawer_precise(d) -> bool:
    """打开左侧抽屉：OpenCV 优先，所有成功返回都必须有设置图标视觉证据。"""
    w, h = _screen_size(d)

    if _drawer_open_evidence(d, w, h):
        logger.info("抽屉已打开（设置图标验证），无需重复点击")
        return True

    def wait_open(timeout: float = 2.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _drawer_open_evidence(d, w, h):
                return True
            time.sleep(0.30)
        return False

    # 策略0：OpenCV 模板匹配汉堡图标。
    try:
        from template_matcher import click_open_drawer
        if click_open_drawer(label="登出·模板开抽屉") is not None and wait_open():
            logger.info("✓ 抽屉已展开（模板点击 + 设置图标验证）")
            return True
    except Exception as exc:
        logger.warning("模板开抽屉异常，继续兜底：%s", exc)

    # 策略1：精确选择器。
    for desc in HAMBURGER_DESC_KEYWORDS:
        for kwargs in ({"description": desc}, {"descriptionContains": desc}):
            if _try_click(d, **kwargs) and wait_open():
                logger.info("✓ 抽屉已展开（选择器 + 设置图标验证）")
                return True

    # 策略2：XML 中明确的汉堡节点。
    xml = _safe_dump(d)
    if xml:
        for n in _iter_nodes(xml):
            desc = (n.get("content-desc") or "").strip()
            text = (n.get("text") or "").strip()
            rid = (n.get("resource-id") or "").lower()
            combined = (desc + text + rid).lower()
            bx1, _, _, by2 = n["_bounds"]
            if bx1 > w * 0.25 or by2 > h * 0.15 or n.get("clickable") != "true":
                continue
            matched = any(kw.lower() in combined for kw in HAMBURGER_DESC_KEYWORDS) or any(
                kw in rid for kw in HAMBURGER_RID_KEYWORDS
            )
            if matched:
                cx, cy = n["_center"]
                if _tap_bounds(d, cx, cy, f"汉堡按钮 XML={desc or rid}") and wait_open():
                    logger.info("✓ 抽屉已展开（XML 节点 + 设置图标验证）")
                    return True

    # 策略3：只尝试左上角汉堡区域，最后仍须视觉验证。
    logger.info("策略3：adb 坐标兜底点击左上角抽屉按钮")
    for x_ratio, y_ratio in ((0.055, 0.050), (0.065, 0.065), (0.075, 0.080), (0.045, 0.065)):
        if _tap_bounds(d, int(w * x_ratio), int(h * y_ratio), "抽屉坐标兜底") and wait_open():
            logger.info("✓ 抽屉已展开（坐标 + 设置图标验证）")
            return True

    logger.warning("抽屉未打开：所有方式均未通过设置图标验证")
    return False


def _click_gear_precise(d) -> bool:
    """点击设置：模板优先；只允许精确 XML 兜底，禁止按位置猜测齿轮。"""
    w, h = _screen_size(d)

    # 抽屉未通过视觉验证时绝不点击，防止在主页误点“快速”等按钮。
    if not _drawer_open_evidence(d, w, h):
        logger.warning("⚠ 抽屉未打开，取消齿轮点击（防止在主页误点顶部图标）")
        return False

    try:
        from template_matcher import click_open_setting
        pos = click_open_setting(label="模板齿轮")
        if pos is not None:
            logger.info(f"✓ 齿轮点击成功（模板匹配） score={pos[2]:.3f}")
            return True
    except Exception as exc:
        logger.warning("模板齿轮异常：%s", exc)

    # 仅在已通过设置图标视觉验证的抽屉里，允许精确选择器兜底。
    for kwargs in (
        {"description": "设置"},
        {"description": "设置页"},
        {"text": "设置"},
    ):
        if _try_click(d, **kwargs):
            return True

    xml = _safe_dump(d)
    node = _gear_node_exact(xml)
    if node is not None:
        cx, cy = node["_center"]
        return _tap_bounds(d, cx, cy, "设置 XML 精确节点")

    logger.warning("设置图标模板和精确 XML 均未命中，取消点击（不做位置猜测）")
    return False


def _in_settings_page(xml: str) -> bool:
    """严格判定当前是否千问「设置」页面。

    必须同时满足：
      - 含设置页特征（"退出登录" / "账号管理" / "隐私与安全" / "关于千问"）
      - 不含主页特征（"发消息或按住说话" / "我是千问" / "发消息" 输入框）
    防止把主页误判为设置页（真实事故：齿轮点空后主页被当成设置页）。
    """
    if not xml:
        return False
    # 主页防护：主页有这些文案就一定不是设置页
    HOME_HINTS = ("发消息或按住说话", "我是千问", "为你答疑 办事 创作",
                  "按住说话", "深度思考")
    if any(h in xml for h in HOME_HINTS):
        return False
    SETTINGS_HINTS = (
        "退出登录", "账号管理", "隐私与安全", "关于千问", "账号与安全", "清除缓存",
        # 新版千问设置页首屏可见项（退出登录需要下滑才出现）
        "声音设置", "形象设置", "字号大小", "主题色彩", "震动反馈",
        "开启视频卡片", "语言设置",
    )
    return any(h in xml for h in SETTINGS_HINTS)


def logout(d) -> bool:
    """退出登录全流程（优化版 —— 吸收 doubao_auto 多级兜底方案）。

    ① 点左上角三条横线（汉堡菜单）→ 打开左侧抽屉
    → ② 在抽屉图标栏点「设置」齿轮 → 进入设置页面
    → ③ 设置页滚到底点「退出登录」
    → ④ 弹窗确认「退出登录」
    """
    logger.info("===== 开始登出千问 =====")

    # 先确保回到千问主界面
    start_qianwen(d)
    if not is_logged_in(d):
        logger.info("当前已是未登录状态，无需登出")
        return True

    # ---- ① 打开左侧抽屉（多级兜底，必须确认打开，绝不乐观继续）----
    drawer_ok = _open_drawer_precise(d)
    if not drawer_ok:
        logger.warning("首次开抽屉失败，按 back 归位后重试")
        try:
            d.press("back")
            time.sleep(0.8)
        except Exception:
            pass
        drawer_ok = _open_drawer_precise(d)

    # 最终确认抽屉打开（轮询 4 次）
    w, h = _screen_size(d)
    confirmed = False
    for _ in range(4):
        if _drawer_open_evidence(d, w, h):
            confirmed = True
            break
        time.sleep(0.8)
    if not confirmed:
        raise RuntimeError("抽屉始终未能打开，中止登出（避免在主页误点）")

    # ---- ② 点设置齿轮按钮（_click_gear_precise 内部已做抽屉前置校验）----
    if not _click_gear_precise(d):
        raise RuntimeError("侧栏里找不到设置齿轮按钮")

    # 验证设置页面真的加载出来了
    _random_human_pause(500, 800)
    settings_loaded = False
    for _ in range(5):
        xml = _safe_dump(d)
        if _in_settings_page(xml):
            settings_loaded = True
            break
        time.sleep(0.7)

    if not settings_loaded:
        logger.warning("齿轮点击后未进入设置页，按 back 归位重试一次")
        try:
            d.press("back")
            time.sleep(0.8)
        except Exception:
            pass
        # 重试：back 可能把抽屉关了，先确保抽屉打开再点齿轮
        if not _drawer_open_evidence(d, w, h):
            logger.info("重试前抽屉已关闭，重新打开")
            _open_drawer_precise(d)
            time.sleep(0.6)
        if not _click_gear_precise(d):
            raise RuntimeError("重试时未能点击设置齿轮（抽屉可能已关闭）")
        _random_human_pause(500, 800)
        # ★ 重试后必须重新验证，不能再假成功
        for _ in range(5):
            xml = _safe_dump(d)
            if _in_settings_page(xml):
                settings_loaded = True
                break
            time.sleep(0.7)

    if not settings_loaded:
        raise RuntimeError("两次尝试均未进入设置页面，中止登出")

    logger.info("✓ 已进入设置页面（已验证）")

    # ---- ③ 设置页滚到底部点「退出登录」----
    logout_clicked = _try_click(d, text="退出登录")
    swipes = 0
    if not logout_clicked:
        for swipes in range(12):
            _swipe_up(d)
            time.sleep(0.5)
            if _try_click(d, text="退出登录"):
                logout_clicked = True
                break
    if not logout_clicked:
        raise RuntimeError(f"设置页翻到底都没找到「退出登录」（已滑 {swipes+1} 次）")
    time.sleep(0.8)

    # ---- ④ 弹窗确认「退出登录」----
    # 千问退出登录不是简单二次确认，它弹的是「选择退出登录的账号」对话框：
    #   标题：选择退出登录的账号
    #   选项：○ 仅本机账号 187****xxxx    （默认选中）
    #   按钮：[取消] [确定]               ← 点"确定"才真正退出！
    # 少数版本也可能弹传统二次确认弹窗：[取消] [退出登录]
    confirm_btn_words = ("确定", "确认", "退出登录", "好的", "OK", "Confirm", "Exit")
    cancel_btn_words = ("取消", "Cancel")

    for _ in range(8):
        xml = _safe_dump(d)

        # 检测千问特有的账号选择弹窗
        account_dialog = (
            "选择退出登录的账号" in xml
            or "仅本机账号" in xml
            or "选择账号" in xml
            or "确认退出" in xml
        )

        if account_dialog:
            logger.info("检测到退出登录确认弹窗，准备点'确定'")
            # 尝试各种可能的确认按钮文字
            clicked = False
            for w in confirm_btn_words:
                if _try_click(d, text=w):
                    clicked = True
                    time.sleep(1.5)
                    break
            if not clicked:
                # 兜底：弹窗底部右侧那个按钮
                w, h = _screen_size(d)
                for x_ratio in (0.82, 0.78, 0.74):
                    for y_ratio in (0.88, 0.90, 0.86):
                        if _tap_bounds(d, int(w * x_ratio), int(h * y_ratio),
                                        f"弹窗确定兜底({x_ratio},{y_ratio})"):
                            time.sleep(1.5)
                            break
                    else:
                        continue
                    break
        else:
            # 没弹窗 → 检查是否已退出
            if not is_logged_in(d):
                logger.info("✅ 已退出登录")
                return True

        # 确认登录状态
        if not is_logged_in(d):
            logger.info("✅ 已退出登录")
            return True

        time.sleep(0.8)

    # 兜底：先按 Enter，不行再按 ESC（某些弹窗 Enter 等同于点确定）
    try:
        d.press("enter")
        time.sleep(1.0)
    except Exception:
        pass

    if not is_logged_in(d):
        logger.info("✅ 已退出登录（Enter 兜底）")
        return True

    logger.warning("登出流程跑完但仍检测为已登录 — 可能弹窗未处理完")
    # 再做一次诊断：看看当前是不是还卡在 dialog
    xml = _safe_dump(d)
    if "选择退出登录的账号" in xml or "仅本机账号" in xml:
        logger.warning("⚠️ 仍卡在退出登录弹窗上，尝试直接 adb 点确定按钮区域")
        w, h = _screen_size(d)
        _tap_bounds(d, int(w * 0.85), int(h * 0.89), "最终兜底确定")
        time.sleep(1.5)
    return not is_logged_in(d)


# =====================================================================
#  辅助：XML 解析
# =====================================================================

def _parse_nodes(xml: str) -> list:
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
            "clickable": node.attrib.get("clickable") == "true",
            "enabled": node.attrib.get("enabled", "true") != "false",
            "visible": node.attrib.get("visible-to-user", "true") != "false",
        })
    return nodes


# =====================================================================
#  登录（图片步骤 ⑤~⑧）
# =====================================================================

def _wait_landing_or_login_page(d, timeout=15) -> str | None:
    """⑤ 等未登录页面出现（两种可能：
      a) 主界面顶部出现"立即登录，解锁更多功能"按钮
      b) App 已自动跳到"输入手机号"登录页
    返回："landing" | "login_page" | None
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = dump_xml(d)
        if any(h in xml for h in LOGIN_PAGE_HINTS):
            # 是登录页（输入手机号）
            if any(h in xml for h in LANDING_HINTS):
                return "landing"   # 但落地页也可能同时有这些词
            # 纯登录页有"输入手机号"和"下一步"
            if "输入手机号" in xml or "下一步" in xml:
                return "login_page"
        if any(h in xml for h in LANDING_HINTS):
            return "landing"
        # 未登录主界面可能出现"立即登录"按钮
        if "立即登录" in xml:
            return "landing"
        time.sleep(0.8)
    return None


def _enter_login_page(d) -> bool:
    """从落地页或未登录主界面进入登录页。
    返回 True 表示已在登录页（输入手机号界面）。
    """
    xml = dump_xml(d)

    # 已在登录页
    if "输入手机号" in xml and "下一步" in xml:
        logger.info("已在登录页")
        return True

    # 有"立即登录"按钮 → 点击
    if _try_click(d, textContains="立即登录"):
        time.sleep(1.5)
        xml = dump_xml(d)
        if "输入手机号" in xml or "下一步" in xml:
            return True

    # 直接 text=立即登录
    if _try_click(d, text="立即登录"):
        time.sleep(1.5)
        return True

    # 如果在抽屉里可能有登录相关入口，先关抽屉再试
    close_drawer_if_open(d)
    time.sleep(0.5)
    if _try_click(d, textContains="立即登录") or _try_click(d, text="立即登录"):
        time.sleep(1.5)
        return True

    # 兜底：点击屏幕中间偏上（登录按钮可能在那）
    w, h = _screen_size(d)
    d.click(w // 2, int(h * 0.17))
    time.sleep(1.5)

    # 再检查一遍
    xml = dump_xml(d)
    return "输入手机号" in xml or "下一步" in xml


def _dismiss_protocol_dialog(d) -> bool:
    """⑦ 用户协议弹窗 → 点"同意"。
    注意：协议可能先勾选 → 再点下一步时才弹。
    也可能是点"下一步"时才弹。
    """
    # 可能的弹窗文字
    agreement_hints = ("用户协议及隐私协议", "已阅读并同意", "用户协议", "隐私协议",
                       "协议及隐私", "同意并继续")
    agree_words = ("同意", "同意并继续", "好的", "我已同意")

    xml = dump_xml(d)
    has_dialog = any(h in xml for h in agreement_hints)

    if has_dialog:
        # 有弹窗
        for w in agree_words:
            if _try_click(d, text=w):
                time.sleep(1.0)
                logger.info("✓ 已点击同意协议")
                return True
        # 兜底：同意按钮一般在弹窗中间偏右，坐标大约屏幕 x=0.7, y=0.5
        w, h = _screen_size(d)
        d.click(int(w * 0.7), int(h * 0.5))
        time.sleep(1.0)
        return True

    # 没有弹窗（可能协议已勾选过）
    return True


def _check_agreement_checkbox(d) -> bool:
    """登录页底部有"未注册手机号验证通过后将自动注册，已勾选并同意"文字，
    前面有一个勾选框。需要确认它是勾选状态。

    如果 XML 能读到 checkbox 就点它；兜底直接点文字前的位置。
    """
    xml = dump_xml(d)
    hint = "未注册手机号验证通过后将自动注册"

    if hint in xml or "已勾选并同意" in xml or "已阅读并同意" in xml:
        # 可能已选中，但需要确认 checkbox 位置
        w, h = _screen_size(d)
        # 找到那段文字的 bounds
        try:
            nodes = _parse_nodes(xml)
            for n in nodes:
                value = (n.get("text") or "") + (n.get("desc") or "")
                if hint in value or "已勾选" in value or "已阅读" in value:
                    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
                    if m:
                        x1, y1, x2, y2 = map(int, m.groups())
                        # checkbox 在文字左侧
                        d.click(max(10, x1 - int((x2 - x1) * 0.15)),
                                (y1 + y2) // 2)
                        time.sleep(0.5)
                        return True
        except Exception:
            pass

    # 如果有 checkbox 元素
    try:
        obj = d(className="android.widget.CheckBox")
        if obj.exists(timeout=1):
            info = obj.info
            if info.get("checked") or info.get("selected"):
                logger.info("协议已勾选")
                return True
            obj.click()
            time.sleep(0.3)
            logger.info("✓ 已勾选协议")
            return True
    except Exception:
        pass

    # 兜底：点击屏幕底部左侧（checkbox 典型位置）
    w, h = _screen_size(d)
    d.click(int(w * 0.06), int(h * 0.95))
    time.sleep(0.5)
    return True


def _input_phone(d, phone: str) -> bool:
    """⑥ 登录页输入手机号。
    千问登录页有一个 EditText，前面有 +86 国家代码前缀。
    """
    # 策略1：找到包含"输入手机号" hint 的 EditText
    try:
        obj = d(textContains="输入手机号", className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.click()
            time.sleep(0.3)
            try:
                obj.clear_text()
            except Exception:
                pass
            obj.set_text(phone)
            logger.info(f"✓ 已填入手机号 {phone}")
            return True
    except Exception:
        pass

    # 策略2：页面上第一个 EditText（输入框前面会有 +86 标签）
    try:
        obj = d(className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.click()
            time.sleep(0.3)
            try:
                obj.clear_text()
            except Exception:
                pass
            obj.set_text(phone)
            logger.info(f"✓ 已填入手机号 {phone}")
            return True
    except Exception:
        pass

    # 策略3：坐标兜底（登录页输入框一般在屏幕中上部偏上）
    w, h = _screen_size(d)
    d.click(int(w * 0.25), int(h * 0.25))
    time.sleep(0.3)
    try:
        d.set_fastinput_ime(True)
        d.send_keys(phone, clear=True)
        d.set_fastinput_ime(False)
        logger.info(f"✓ 已填入手机号 {phone}（坐标兜底）")
        return True
    except Exception:
        pass

    return False


def _click_next_step(d) -> bool:
    """⑥ 点「下一步」按钮。"""
    if _try_click(d, text="下一步"):
        return True
    if _try_click(d, textContains="下一步"):
        return True
    # 兜底：按钮一般在输入框下方
    w, h = _screen_size(d)
    d.click(w // 2, int(h * 0.35))
    time.sleep(0.5)
    return True


def _input_vcode(d, vcode: str) -> bool:
    """⑧ 输入短信验证码。
    千问验证码页有 6 个独立的方格输入框（不是一个大 EditText）。
    直接 set_text 到第一个 EditText 可能会自动填充所有格。
    或者逐格点击输入。
    """
    vcode = str(vcode).strip()

    # 策略1：找到包含"验证码" hint 的 EditText 直接填入
    try:
        obj = d(textContains="验证码", className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.click()
            time.sleep(0.3)
            obj.set_text(vcode)
            logger.info("✓ 已填入验证码（hint匹配）")
            return True
    except Exception:
        pass

    # 策略2：取页面上所有 EditText，找第一个
    try:
        edits = d(className="android.widget.EditText")
        for i in range(edits.count):
            e = edits[i]
            txt = (e.info.get("text") or "").strip()
            if len(txt) == 0 or len(txt) < 11:  # 不是手机号框
                e.click()
                time.sleep(0.3)
                e.set_text(vcode)
                logger.info(f"✓ 已填入验证码（EditText#{i}）")
                return True
    except Exception:
        pass

    # 策略3：坐标 + 快速输入兜底
    w, h = _screen_size(d)
    d.click(w // 2, int(h * 0.30))
    time.sleep(0.3)
    try:
        d.set_fastinput_ime(True)
        d.send_keys(vcode, clear=True)
        d.set_fastinput_ime(False)
        logger.info("✓ 已填入验证码（坐标兜底）")
        return True
    except Exception:
        pass

    return False


def _handle_sms_verify(d, phone: str, stage: str, wait_s: float = 4.0) -> bool:
    """处理「发送短信码」人机验证（偶然弹窗）。

    页面上要求用**当前登录的这个手机号**给阿里服务号发一条指定内容的短信，
    本机做不了，交给局域网里持有该号的 send_sms 手机代发；设备回执成功后
    再点「已发送短信，下一步」。失败会抛 RuntimeError，由主循环重试。

    :return: True = 出现过并已处理；False = 当前没有这个页面。
    """
    try:
        from sms_verify import handle_identity_verification, wait_identity_page
    except Exception as exc:                # 模块缺失/依赖没装，不影响正常登录
        logger.warning("人机验证代发模块不可用（%s），跳过检测", exc)
        return False

    try:
        if not wait_identity_page(d, wait_s=wait_s):
            return False
        logger.warning("[人机验证] %s 检测到「发送短信码」页面，使用登录号 %s 代发", stage, phone)
        return handle_identity_verification(d, phone, stage=stage)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"人机验证处理异常（{stage}）：{exc}") from exc


def login_with_phone(d, phone: str, vcode: str | None = None, poller=None) -> bool:
    """完整登录流程（步骤 ⑤~⑧，含偶发「发送短信码」人机验证）。

    ⚠️ 不再 force_reset！UI 登出后 App 已在未登录界面，
    直接点"立即登录"就行，杀进程只会触发模拟器白屏。
    """
    logger.info(f"===== 开始登录千问: {phone} =====")
    start_qianwen(d)

    # 已登录先走 UI 登出（不 force_reset）
    if is_logged_in(d):
        logger.info("检测到已登录，先 UI 登出")
        logout(d)
        _random_human_pause(800, 1500)
    else:
        logger.info("未登录状态，直接进入登录流程")

    # ---- ⑤ 等落地页 / 登录页 ----
    result = _wait_landing_or_login_page(d, timeout=15)
    if result is None:
        # 兜底：HOME + 非阻塞 start，不 force_reset
        logger.info("首次没等到页面，HOME + am start 再试")
        serial = get_device_serial()
        run_adb("shell", "input", "keyevent", "3", timeout=10, serial=serial)
        time.sleep(1.0)
        _cold_start_qianwen_with_adb()
        _wait_qianwen_ready(d, timeout=15)
        result = _wait_landing_or_login_page(d, timeout=10)
        if result is None:
            raise RuntimeError("落地页/登录页一直没出现")

    logger.info(f"⑤ 检测到页面类型：{result}")

    # 如果在落地页（有"立即登录"按钮），需要先点进去
    if result == "landing":
        if not _enter_login_page(d):
            raise RuntimeError("无法进入登录页面（输入手机号界面）")
        logger.info("✓ 已进入登录页面")
    # 如果已在 login_page（输入手机号界面），直接继续

    # 等登录页完全加载
    for _ in range(5):
        xml = dump_xml(d)
        if "输入手机号" in xml:
            break
        time.sleep(0.8)

    # 可能先弹协议 → 确认
    _dismiss_protocol_dialog(d)

    # ---- ⑥ 输入手机号 ----
    if not _input_phone(d, phone):
        raise RuntimeError("找不到手机号输入框")
    time.sleep(0.8)

    # 确认协议勾选
    _check_agreement_checkbox(d)

    # ---- 点「下一步」 ----
    if not _click_next_step(d):
        raise RuntimeError("找不到「下一步」按钮")
    time.sleep(1.5)

    # ---- 可能的协议弹窗（点下一步时才弹）----
    _dismiss_protocol_dialog(d)

    # ---- 人机验证（偶然）：点「下一步」后可能弹「发送短信码」页 ----
    # 用当前登录的手机号把指定短信代发出去，设备回执后再点「已发送短信，下一步」
    if _handle_sms_verify(d, phone, "点「下一步」后"):
        time.sleep(1.0)
        _dismiss_protocol_dialog(d)

    # 短信应已发出，记录时间戳，防止轮询拿到历史旧码
    vcode_send_ts = time.time()
    logger.info("已点下一步，短信应已发出，准备轮询验证码")

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

    # 可能又弹一次协议
    _dismiss_protocol_dialog(d)

    # ---- ⑧ 填入验证码 ----
    if not _input_vcode(d, vcode):
        raise RuntimeError("找不到验证码输入框")
    time.sleep(1.0)

    # 有些版本填完 6 位自动登录；也可能需要点"登录"按钮
    for w in ("登录", "完成", "确定"):
        if _try_click(d, text=w):
            logger.info(f"已点击 {w}")
            break
    time.sleep(3.0)

    # ---- 人机验证（偶然）：提交验证码后也可能弹「发送短信码」页 ----
    if _handle_sms_verify(d, phone, "提交验证码后", wait_s=5.0):
        time.sleep(2.0)

    # ---- 确认登录成功 ----
    for _ in range(15):
        if is_logged_in(d):
            _dismiss_popups(d)
            close_drawer_if_open(d)
            logger.info(f"✅ 手机号 {phone} 登录成功")
            return True
        time.sleep(1.0)

    raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")
