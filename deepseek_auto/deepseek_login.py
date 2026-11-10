# -*- coding: utf-8 -*-
"""
deepseek_login.py —— DeepSeek App 登出 / 登录 UI 自动化
================================================================

对应用户标注图片的 ①~⑩ 步骤：

  【登出】
    ① 点左上角按钮打开左侧抽屉
    ② 点抽屉右下角的「三个点」
    ③ 设置页往下划到底部，点「退出登录」
    ④ 弹窗点「退出登录」确认

  【登录】
    ⑤ 稍等几秒，落地页出现「手机验证码登录」按钮，点击
    ⑥ 「用户协议及隐私政策」弹窗点「同意」
    ⑦ 验证码登录页输入手机号
    ⑧ 点「发送验证码」→ 弹人机验证 → captcha_solver 破解 → 短信发出
    ⑨ 填入轮询到的短信验证码
    ⑩ 点「登录」→ 进入主界面

所有查找均「XML 文本/desc 优先 + 比例坐标兜底」，不假设具体 resource-id
（DeepSeek 版本更新可能改 id，文本更稳）。横竖屏均按运行时实际分辨率适配。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

logger = logging.getLogger(__name__)

DEEPSEEK_PACKAGE = "com.deepseek.chat"

# =====================================================================
#  界面特征词（用于状态判断与元素查找）
# =====================================================================

# 主界面（已登录）特征
LOGGED_IN_HINTS = ("发消息或按住说话", "发消息", "深度思考", "智能搜索")
# 落地页（未登录）特征
LANDING_HINTS = ("手机验证码登录", "密码登录")
# 抽屉「三个点」候选 desc/text
DRAWER_MORE_WORDS = ("更多", "更多操作", "更多选项", "更多功能", "设置", "···", "…")
# 左上角抽屉按钮候选 desc/text
DRAWER_OPEN_WORDS = ("打开侧边栏", "侧边栏", "打开抽屉", "对话历史", "历史记录", "菜单")
# 登录后可能出现的干扰弹窗按钮
DISMISS_WORDS = ("以后再说", "暂不", "跳过", "取消", "我知道了", "稍后再说", "不允许")


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

    # 优先匹配配置的端口（"5556" in serial 同时命中 emulator-5556 和 127.0.0.1:5556）
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


def current_is_deepseek(d) -> bool:
    try:
        return d.app_current().get("package", "") == DEEPSEEK_PACKAGE
    except Exception:
        return False


def start_deepseek(d, wait=4):
    """拉起 DeepSeek 到前台（不杀进程）。"""
    logger.info("拉起 DeepSeek App")
    d.app_start(DEEPSEEK_PACKAGE, stop=False)
    time.sleep(wait)
    if not current_is_deepseek(d):
        d.app_start(DEEPSEEK_PACKAGE, stop=False)
        time.sleep(wait)


def force_reset(d):
    """杀进程（登出/重登前的彻底复位）。"""
    try:
        d.app_stop(DEEPSEEK_PACKAGE)
        time.sleep(2)
    except Exception:
        pass


force_logout_if_needed = force_reset  # 与 doubao_login 接口名保持一致


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
#  左侧抽屉状态检测与关闭
# =====================================================================

# 抽屉关闭按钮候选 desc/text（版本更新可能改文案，多备几个）
DRAWER_CLOSE_WORDS = ("关闭侧边栏", "收起侧边栏", "折叠侧边栏", "收起", "关闭抽屉")


def is_drawer_open(d) -> bool:
    """左侧抽屉是否张开。

    判定依据：「搜索对话内容」搜索框只存在于抽屉内，
    主界面其它任何地方都没有这个文案，是最稳的锚点。
    """
    return "搜索对话内容" in dump_xml(d)


def close_drawer_if_open(d) -> bool:
    """若左侧抽屉张开则点按钮关闭；未张开直接返回。

    关闭按钮定位三级策略：
      1. 语义词：desc/text 匹配「关闭侧边栏/收起」等
      2. XML 锚定坐标：找到「搜索对话内容」框，点它同行右侧的按钮
         （真实布局：按钮紧贴搜索框右缘，大小约等于搜索框高度）
      3. 比例兜底：抽屉右缘顶部区域（横竖屏按宽高比区分）

    :return: True=抽屉已关闭（或本来就关着）；False=尝试后仍张开
    """
    if not is_drawer_open(d):
        logger.info("左侧抽屉未张开，无需关闭")
        return True

    logger.info("检测到左侧抽屉张开，开始关闭")

    for attempt in range(3):
        # ---- 策略 1：语义词点击 ----
        if _click_words(d, DRAWER_CLOSE_WORDS):
            if not is_drawer_open(d):
                logger.info("✅ 左侧抽屉已关闭（语义词命中）")
                return True

        # ---- 策略 2：以搜索框为锚点，点它右侧的关闭按钮 ----
        try:
            obj = d(textContains="搜索对话内容")
            if obj.exists(timeout=1):
                b = obj.info["bounds"]   # {'left','top','right','bottom'} 设备像素
                box_h = b["bottom"] - b["top"]
                click_x = b["right"] + int(box_h * 0.85)   # 按钮中心≈搜索框右缘+0.85倍框高
                click_y = (b["top"] + b["bottom"]) // 2
                logger.info(f"按搜索框锚点点击关闭按钮 ({click_x},{click_y})")
                d.click(click_x, click_y)
                time.sleep(0.8)
                if not is_drawer_open(d):
                    logger.info("✅ 左侧抽屉已关闭（锚点坐标命中）")
                    return True
        except Exception as exc:
            logger.debug(f"锚点点击异常: {exc}")

        # ---- 策略 3：比例兜底 ----
        w, h = _screen_size(d)
        fx = 0.30 if w > h else 0.88   # 横屏抽屉窄（约 28% 宽）/ 竖屏抽屉宽（约 85%）
        d.click(int(w * fx), int(h * 0.075))
        time.sleep(0.8)
        if not is_drawer_open(d):
            logger.info("✅ 左侧抽屉已关闭（比例兜底命中）")
            return True

        logger.warning(f"第 {attempt + 1}/3 次关闭抽屉未成功，重试")

    logger.warning("多次尝试后左侧抽屉仍张开（不影响主流程，继续）")
    return False


# =====================================================================
#  登录状态判断
# =====================================================================

def is_logged_in(d) -> bool:
    """已登录 = 主聊天界面可见（输入框/深度思考按钮），且无落地页特征。"""
    xml = dump_xml(d)
    if any(h in xml for h in LANDING_HINTS) and "确认退出登录" not in xml:
        return False
    return any(h in xml for h in LOGGED_IN_HINTS)


# =====================================================================
#  登出（图片步骤 ①~④）
# =====================================================================

def logout(d) -> bool:
    """退出登录全流程。

    ① 点左上角开抽屉 → ② 点抽屉右下角三个点 → （如出现菜单则点「设置」）
    → ③ 设置页划到底点「退出登录」→ ④ 弹窗确认「退出登录」
    """
    logger.info("===== 开始登出 DeepSeek =====")

    # 不在主界面先回主界面
    start_deepseek(d)
    if not is_logged_in(d):
        logger.info("当前已是未登录状态，无需登出")
        return True

    # ---- ① 点左上角按钮打开左侧抽屉 ----
    opened = _click_words(d, DRAWER_OPEN_WORDS)
    if not opened:
        # 坐标兜底：左上角区域（横竖屏通用比例）
        w, h = _screen_size(d)
        d.click(int(w * 0.07), int(h * 0.055))
        time.sleep(1.0)
        logger.info("已按坐标点击左上角抽屉按钮")

    # 确认抽屉打开：出现「搜索对话内容」输入框或用户手机号区域
    for _ in range(4):
        xml = dump_xml(d)
        if "搜索对话内容" in xml or re.search(r"\d{3}\*{4,}\d{2}", xml):
            break
        time.sleep(0.8)

    # ---- ② 点抽屉右下角「三个点」----
    dots_ok = _click_words(d, DRAWER_MORE_WORDS)
    if not dots_ok:
        # 坐标兜底：抽屉底部右侧。横屏抽屉窄（约 25% 宽），竖屏抽屉宽（约 85%）
        w, h = _screen_size(d)
        fx = 0.24 if w > h else 0.82   # 横屏 / 竖屏
        d.click(int(w * fx), int(h * 0.955))
        time.sleep(1.0)
        logger.info("已按坐标点击抽屉右下角三个点")

    # 三个点可能直接进设置页，也可能先弹小菜单 → 有「设置」就点
    time.sleep(1.0)
    xml = dump_xml(d)
    if "设置" in xml and "退出登录" not in xml:
        _try_click(d, text="设置")
        time.sleep(1.2)

    # ---- ③ 设置页划到底部点「退出登录」----
    logout_clicked = _try_click(d, text="退出登录")
    if not logout_clicked:
        for _ in range(10):
            _swipe_up(d)
            time.sleep(0.5)
            if _try_click(d, text="退出登录"):
                logout_clicked = True
                break
    if not logout_clicked:
        raise RuntimeError("设置页翻到底都没找到「退出登录」")
    time.sleep(1.0)

    # ---- ④ 弹窗确认「退出登录」（标题「确认退出登录?」）----
    for _ in range(5):
        xml = dump_xml(d)
        if "确认退出登录" in xml or "退出登录不会丢失" in xml:
            if _try_click(d, text="退出登录"):
                time.sleep(1.5)
                if not is_logged_in(d):
                    logger.info("✅ 已退出登录")
                    return True
        else:
            # 弹窗可能已点完消失
            if not is_logged_in(d):
                logger.info("✅ 已退出登录")
                return True
            _try_click(d, text="退出登录")
        time.sleep(0.8)

    if not is_logged_in(d):
        logger.info("✅ 已退出登录")
        return True
    logger.warning("登出流程跑完但仍检测为已登录")
    return False


# =====================================================================
#  登录（图片步骤 ⑤~⑩）
# =====================================================================

def _wait_landing(d, timeout=15) -> bool:
    """⑤ 等落地页「手机验证码登录」按钮出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = dump_xml(d)
        if "手机验证码登录" in xml:
            return True
        time.sleep(0.8)
    return False


def _input_phone(d, phone: str) -> bool:
    """⑦ 验证码登录页输入手机号。"""
    for kwargs in (
        {"textContains": "手机号", "className": "android.widget.EditText"},
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
                logger.info(f"已填入手机号 {phone}")
                return True
        except Exception:
            pass
    return False


def _input_vcode(d, vcode: str) -> bool:
    """⑨ 输入短信验证码（验证码输入框是页面上第二个 EditText，或带「验证码」hint）。"""
    try:
        obj = d(textContains="验证码", className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.click()
            time.sleep(0.3)
            obj.set_text(vcode)
            logger.info("已按 hint 填入验证码")
            return True
    except Exception:
        pass
    # 兜底：取页面所有 EditText 中不是手机号框（内容为空或长度<11）的那个
    try:
        edits = d(className="android.widget.EditText")
        for i in range(edits.count):
            e = edits[i]
            txt = (e.info.get("text") or "").strip()
            if len(txt) < 11:
                e.click()
                time.sleep(0.3)
                e.set_text(vcode)
                logger.info("已按序号填入验证码")
                return True
    except Exception:
        pass
    return False


def login_with_phone(d, phone: str, vcode: str | None = None, poller=None) -> bool:
    """完整登录流程（步骤 ⑤~⑩）。

    :param phone:   手机号
    :param vcode:   直接指定验证码（测试用）；None 时走 poller 轮询
    :param poller:  轮询函数 poller(after_ts) -> vcode，
                    after_ts 为「短信实际发出」的时间戳，只查该时间之后的新码
    """
    from captcha_solver import wait_captcha_and_solve  # 延迟导入，避免循环依赖

    logger.info(f"===== 开始登录 DeepSeek: {phone} =====")
    start_deepseek(d)

    # 已登录先登出
    if is_logged_in(d):
        logger.info("检测到已登录，先登出")
        logout(d)
        start_deepseek(d)

    # ---- ⑤ 等落地页 → 点「手机验证码登录」----
    if not _wait_landing(d, timeout=15):
        # 兜底：重启 App 再等一轮
        force_reset(d)
        start_deepseek(d, wait=5)
        if not _wait_landing(d, timeout=10):
            raise RuntimeError("落地页一直没出现「手机验证码登录」按钮")
    if not _try_click(d, text="手机验证码登录"):
        raise RuntimeError("找不到「手机验证码登录」按钮")
    time.sleep(1.2)

    # ---- ⑥ 「用户协议及隐私政策」弹窗 → 点「同意」----
    for _ in range(4):
        xml = dump_xml(d)
        if "用户协议及隐私政策" in xml or "我已阅读并同意" in xml:
            if _try_click(d, text="同意"):
                break
        # 没弹窗（可能已勾过协议）直接进下一步
        if "验证码登录" in xml and "发送验证码" in xml:
            break
        time.sleep(0.8)
    time.sleep(1.0)

    # ---- ⑦ 输入手机号 ----
    for _ in range(3):
        if "发送验证码" in dump_xml(d):
            break
        time.sleep(0.8)
    if not _input_phone(d, phone):
        raise RuntimeError("找不到手机号输入框")
    time.sleep(0.8)

    # ---- ⑧ 点「发送验证码」→ 人机验证 → 短信发出 ----
    if not _try_click(d, text="发送验证码"):
        raise RuntimeError("找不到「发送验证码」按钮")
    logger.info("已点击「发送验证码」，等待人机验证弹窗")

    # 破解人机验证（弹窗没出现也兼容：直接视为通过）
    if not wait_captcha_and_solve(d):
        raise RuntimeError("人机验证多次尝试未通过")

    # ⚠️ 关键：短信是验证通过后才发出的，时间戳必须记在这里，
    #    轮询时只查这之后的新码，防止拿到历史旧码
    vcode_send_ts = time.time()
    logger.info("等待短信发出，准备轮询验证码")

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

    # ---- ⑨ 填入验证码 ----
    if not _input_vcode(d, vcode):
        raise RuntimeError("找不到验证码输入框")
    time.sleep(1.0)

    # ---- ⑩ 点「登录」----
    if not _try_click(d, text="登录"):
        # 部分版本填完 6 位码自动登录，没有登录按钮也正常
        logger.info("没找到「登录」按钮（可能已自动提交），等待登录结果")
    time.sleep(3.0)

    # ---- 确认登录成功 ----
    for _ in range(12):
        if is_logged_in(d):
            _dismiss_popups(d)
            # 登录成功后左侧抽屉（历史会话列表）默认张开，
            # 会遮挡主界面并污染后续问答截图，先检测并关闭再走问答流程
            close_drawer_if_open(d)
            logger.info(f"✅ 手机号 {phone} 登录成功")
            return True
        time.sleep(1.0)

    raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")
