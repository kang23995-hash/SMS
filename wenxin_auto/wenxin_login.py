# -*- coding: utf-8 -*-
"""
wenxin_login.py —— 文心 App 登出 / 登录 UI 自动化
================================================================

对应用户标注图片的 ①~⑨ 步骤：

  【登出】（图①~④）
    ① 主对话页点左上角「☰」按钮，打开左侧抽屉
    ② 点左侧抽屉右下角的「设置」齿轮按钮
    ③ 设置页往下滑动到底部，点「退出登录」
    ④ 底部弹层点「确认退出」

  【登录】（图⑤~⑨）
    ⑤ 登录页输入手机号码（+86 | 请输入手机号）
    ⑥ 点「验证码登录」后弹出「服务协议及隐私保护」弹窗 → 点「同意」
    ⑦ 点「验证码登录」按钮（短信随即下发；文心无滑块/点选等人机验证）
    ⑧ （短信下发，无需 App 内操作）
    ⑨ 在「输入短信验证码」页输入收到的 6 位验证码（输满一般自动提交登录）

⚠️ 与 deepseek_login.py 的关键差异：
  - 文心 App 所有 resource-id 均被混淆为 com.baidu.newapp:id/obfuscated，
    因此**完全不依赖 resource-id**，全部用「文本/desc 锚点 + 比例坐标兜底」；
  - 登录全程**没有人机验证**，短信在点「验证码登录」（并通过协议弹窗）后直接下发；
  - 每一步点击后都验证操作结果，失败自动重试，避免因 UI 定位偏差中断流程。
"""

from __future__ import annotations

import logging
import random
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

WENXIN_PACKAGE = "com.baidu.newapp"

# =====================================================================
#  界面特征词（用于状态判断与元素查找）
# =====================================================================

# 主界面（已登录）特征。
# ⚠️ 实机验证：文心输入框提示语「发消息或按住说话」是 Compose 内部绘制，
#    **不会出现在无障碍 XML 里**（EditText 节点 text 为空、无 desc）。
#    因此文本特征只能用「我是文心」（空对话页欢迎语）和「内容由AI生成」（页脚），
#    再叠加「底部 EditText」结构特征兜底（见 is_logged_in）。
LOGGED_IN_HINTS = ("我是文心", "Hi，我是文心", "内容由AI生成")
# 登录页（未登录）特征：手机号输入框 / 验证码登录按钮 / 稍后登录 / 切换登录方式
LANDING_HINTS = ("请输入手机号", "验证码登录", "稍后登录", "切换登录方式")
# 登录页【独有】特征（用于精确状态机判定）。
# ⚠️ 不能单用「验证码登录」——短信页标题是「短信验证码登录」，包含该子串；
#    实测「请输入手机号」「切换登录方式」「稍后登录」只在手机号登录页出现。
LANDING_UNIQUE_HINTS = ("请输入手机号", "切换登录方式", "稍后登录")
# 协议弹窗【独有】特征。
# ⚠️ 不能用「同意」——登录页底部协议行「请您阅读并同意 文心用户协议…」含该子串；
#    弹窗标题「服务协议及隐私保护」和「不同意」按钮只在弹窗上出现（实机 XML 证实）。
AGREEMENT_DIALOG_HINTS = ("服务协议及隐私保护", "不同意")
# 短信验证码页特征
SMS_PAGE_HINTS = ("输入短信验证码", "短信验证码至", "秒后重新发送", "重新发送")
# 协议相关文案（用于判断弹窗是否出现，标题「服务协议及隐私保护」为弹窗独有）
AGREEMENT_HINTS = ("服务协议及隐私保护", "已阅读并同意")
# 抽屉打开特征：抽屉内独有的「新建对话」「我的空间」入口
DRAWER_OPEN_HINTS = ("新建对话", "我的空间", "与文心的对话")
# 设置页特征
SETTINGS_HINTS = ("字号大小", "账号管理", "截图分享", "关于我们")
# 登录后可能出现的干扰弹窗按钮（通知权限等）
DISMISS_WORDS = ("知道了", "以后再说", "暂不", "跳过", "取消", "我知道了", "稍后再说", "不允许")


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
            r = run_adb("connect", f"127.0.0.1:{port}", timeout=15)
            out = r.stdout.decode("utf-8", errors="ignore").strip()
            if out:
                logger.debug(f"adb connect 127.0.0.1:{port} → {out}")
        except Exception as exc:
            logger.debug(f"adb connect 127.0.0.1:{port} 异常: {exc}")

    # 查所有在线设备
    result = run_adb("devices", timeout=15)
    raw = result.stdout.decode("utf-8", errors="ignore")
    logger.info(f"adb devices 原始输出: {raw!r}")
    devices = []
    for line in raw.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    if not devices:
        raise RuntimeError("没连到任何 Android 设备 / 模拟器")

    logger.info(f"解析到设备列表: {devices}")

    # 优先匹配配置的端口（"5560" in serial 同时命中 emulator-5560 和 127.0.0.1:5560）
    if port and port > 0:
        target = str(port)
        for serial in devices:
            if target in serial:
                logger.info(f"匹配到端口 {port} 的设备: {serial}")
                _device_serial = serial
                return serial
        logger.warning(f"没找到端口 {port} 对应的设备（当前设备列表: {devices}），回退自动取第一台")

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


def dump_xml(d, timeout: float = 10.0) -> str:
    """dump_hierarchy 带超时保护（核心防卡死机制）。

    uiautomator2 的 dump_hierarchy 在 App 闪屏、WebView 加载、无障碍树重建
    或 UI 线程卡顿时**没有内部超时**，会无限期阻塞，导致整个 worker 卡死、
    日志无任何输出（即用户遇到的"卡在拉起文心 App 之后不动"）。

    这里用守护线程 + Event 做超时兜底：
      - 超时返回空串并打 warning，让调用方能感知"dump 不可用"并走兜底逻辑；
      - 正常返回 dump 出来的 XML 字符串。
    """
    result = {"xml": ""}
    done = threading.Event()

    def _do():
        try:
            result["xml"] = d.dump_hierarchy(compressed=False)
        except Exception as e:
            logger.debug(f"dump_hierarchy 异常: {e}")
        finally:
            done.set()

    t = threading.Thread(target=_do, daemon=True, name="dump-xml")
    t.start()
    if not done.wait(timeout):
        logger.warning(f"⚠️ dump_hierarchy 超时（>{timeout}s），返回空 XML —— "
                       f"App 可能正在闪屏/加载/ANR，调用方将走兜底逻辑")
        return ""
    return result["xml"]


def current_is_wenxin(d) -> bool:
    try:
        return d.app_current().get("package", "") == WENXIN_PACKAGE
    except Exception:
        return False


def start_wenxin(d, wait=4):
    """拉起文心 App 到前台（不杀进程），并等待 UI 就绪。

    就绪判定：能成功 dump 出非空 XML（说明无障碍树已建好，不再是闪屏/加载态）。
    这样调用方后续的 _back_to_main / is_logged_in 不会在闪屏页上 dump 超时。
    """
    logger.info("拉起文心 App")
    d.app_start(WENXIN_PACKAGE, stop=False)
    time.sleep(wait)
    if not current_is_wenxin(d):
        d.app_start(WENXIN_PACKAGE, stop=False)
        time.sleep(wait)

    # ---- 等待 UI 就绪：dump 成功才返回 ----
    deadline = time.time() + 15
    while time.time() < deadline:
        if dump_xml(d, timeout=6):
            return
        logger.info("文心 UI 尚未就绪（闪屏/加载中），1s 后重试...")
        time.sleep(1)
    logger.warning("文心拉起后 15s 内 UI 仍未就绪，继续后续流程（dump 可能返回空）")


def force_reset(d):
    """杀进程（登出/重登前的彻底复位）。"""
    try:
        d.app_stop(WENXIN_PACKAGE)
        time.sleep(2)
    except Exception:
        pass


force_logout_if_needed = force_reset  # 与其它 *_login 模块接口名保持一致


def _screen_size(d):
    try:
        return int(d.info["displayWidth"]), int(d.info["displayHeight"])
    except Exception:
        return 900, 1600


def _try_click(d, **kwargs) -> bool:
    """按 uiautomator2 选择器点击（text=/description=/className=...）。"""
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
#  登录状态判断
# =====================================================================

def is_logged_in(d) -> bool:
    """已登录 = 处于文心主聊天界面（空对话页或已有对话页）。

    判定顺序（先排除后确认，逐级兜底）：
      1. 命中登录落地页特征 → False
         （「短信验证码登录」按钮文案自带「验证码登录」子串，
          短信验证码页会在这里被一并拦截，不冲突）
      2. 命中短信验证码页特征 → False
      3. 命中已登录文本特征（我是文心 / 内容由AI生成）→ True
      4. 结构兜底：屏幕底部存在 EditText（bounds 下缘 > 屏高 70%）→ True
         —— 已有对话的历史页没有「我是文心」欢迎语，「内容由AI生成」页脚
            也可能被长对话顶出屏幕，但底部输入框（EditText, y≈1667）一定在，
            用它兜底可避免把已登录的对话页误判成未登录。
      5. 都不满足 → False
    """
    xml = dump_xml(d)
    if not xml:
        logger.warning("is_logged_in: dump 为空（App 可能未就绪），保守判定未登录")
        return False

    # ---- 1/2. 登录流程页面优先排除 ----
    landing_hit = [h for h in LANDING_HINTS if h in xml]
    if landing_hit:
        logger.info(f"is_logged_in=False（命中登录页特征: {landing_hit}）")
        return False
    sms_hit = [h for h in SMS_PAGE_HINTS if h in xml]
    if sms_hit:
        logger.info(f"is_logged_in=False（命中短信页特征: {sms_hit}）")
        return False

    # ---- 3. 文本特征命中 ----
    logged_hit = [h for h in LOGGED_IN_HINTS if h in xml]
    if logged_hit:
        logger.info(f"is_logged_in=True（命中已登录文本特征: {logged_hit}）")
        return True

    # ---- 4. 结构兜底：底部输入框（EditText）存在即视为已登录 ----
    try:
        h = int(d.info.get("displayHeight", 1920))
        bottom_limit = int(h * 0.70)
        for node in d(className="android.widget.EditText"):
            b = node.info.get("bounds", {})
            if b.get("bottom", 0) > bottom_limit:
                logger.info("is_logged_in=True（命中底部 EditText 结构兜底）")
                return True
    except Exception as exc:
        logger.debug(f"is_logged_in 结构兜底异常: {exc}")

    logger.info("is_logged_in=False（无任何已登录特征）")
    return False


def _back_to_main(d, max_press: int = 3) -> None:
    """把 App 退回到主界面（聊天页 / 登录落地页）。

    背景：App 可能被留在个人主页、设置页等子页面（例如误触抽屉里的
    用户资料卡）。这些页面既不是已登录聊天页也不是登录落地页，
    直接走 is_logged_in 会被误判为「未登录」而跳过登出。

    策略：每按一次返回键前先检查——
      - 出现已登录主界面特征（LOGGED_IN_HINTS）→ 到位，停止；
      - 出现登录落地页特征（LANDING_HINTS）→ 本来就是未登录，停止；
      - 否则继续按返回键（次数用尽后交调用方按当前状态处理）。

    注意：先检查后按键，保证停在主聊天页时一次返回键都不会多按
    （主聊天页上按返回键会直接退出到桌面）。
    """
    for i in range(max_press):
        xml = dump_xml(d)
        logged_hit = [h for h in LOGGED_IN_HINTS if h in xml]
        if logged_hit:
            logger.info(f"_back_to_main: 已在主界面（命中 {logged_hit}），停止回退")
            return
        landing_hit = [h for h in LANDING_HINTS if h in xml]
        if landing_hit:
            logger.info(f"_back_to_main: 已在登录落地页（命中 {landing_hit}），停止回退")
            return
        logger.info(f"_back_to_main: 第 {i + 1}/{max_press} 次按返回键退回主界面")
        d.press("back")
        time.sleep(0.8)
    logger.warning("_back_to_main: 达到最大返回次数仍未回到主界面，交由调用方处理")


def _on_landing_page(d) -> bool:
    """是否处于登录落地页（手机号输入页）。"""
    xml = dump_xml(d)
    return "请输入手机号" in xml or ("验证码登录" in xml and "切换登录方式" in xml)


def _on_sms_page(d) -> bool:
    """是否处于短信验证码输入页。"""
    xml = dump_xml(d)
    return any(h in xml for h in SMS_PAGE_HINTS)


def _login_phase(d) -> str:
    """登录流程统一状态机（实机 XML 证据驱动）。

    :return:
      'sms'        短信验证码页（输入短信验证码 / 60秒后重新发送）
      'agreement'  服务协议及隐私保护弹窗（含「不同意」按钮）
      'landing'    手机号登录页（请输入手机号 / 切换登录方式）
      'transition' 都不是——页面跳转过渡态（WebView 加载时特征词会全部消失
                   约 3 秒，实测 0.6~2.4s 内 XML 无任何登录相关文案）

    判定优先级：sms → agreement → landing → transition。
    特征词均用各页面【独有】文案，避免子串互相污染
    （短信页标题「短信验证码登录」含「验证码登录」；登录页协议行含「同意」）。
    """
    xml = dump_xml(d)
    if any(h in xml for h in SMS_PAGE_HINTS):
        return "sms"
    if any(h in xml for h in AGREEMENT_DIALOG_HINTS):
        return "agreement"
    if any(h in xml for h in LANDING_UNIQUE_HINTS):
        return "landing"
    return "transition"


_PHASE_CN = {"sms": "短信验证码页", "agreement": "协议弹窗",
             "landing": "手机号登录页", "transition": "过渡态（特征未渲染）"}


def _drawer_opened(d) -> bool:
    """左侧抽屉是否张开（抽屉内独有的「新建对话」入口为锚点）。"""
    xml = dump_xml(d)
    return any(h in xml for h in DRAWER_OPEN_HINTS)


def _on_settings_page(d) -> bool:
    """是否处于设置页。"""
    xml = dump_xml(d)
    return any(h in xml for h in SETTINGS_HINTS)


# =====================================================================
#  登出（图片步骤 ①~④）
# =====================================================================

def _step1_open_drawer(d) -> bool:
    """① 点左上角「☰」按钮打开左侧抽屉（带点击后验证 + 重试）。

    按钮是纯图标无文字，定位策略：
      1. 语义 desc 候选（部分版本有无障碍描述）
      2. 比例坐标：屏幕左上角区域（约 7% 宽、5.5% 高）
    验证：抽屉打开后出现「新建对话 / 我的空间」锚点。
    """
    for attempt in range(3):
        # 策略 1：语义 desc/text
        if _click_words(d, ("打开抽屉", "打开侧边栏", "侧边栏", "菜单", "历史记录", "对话历史")):
            time.sleep(1.2)
        else:
            # 策略 2：左上角比例坐标兜底
            w, h = _screen_size(d)
            d.click(int(w * 0.075), int(h * 0.062))
            time.sleep(1.2)
            logger.info("已按坐标点击左上角抽屉按钮")

        if _drawer_opened(d):
            logger.info("✅ ① 左侧抽屉已打开")
            return True
        logger.warning(f"第 {attempt + 1}/3 次打开抽屉未见特征，重试")
    return False


def _step2_open_settings(d) -> bool:
    """② 点左侧抽屉右下角的「设置」齿轮按钮（带验证 + 重试）。

    齿轮是纯图标。定位策略：
      1. 语义 desc/text「设置」
      2. 锚点坐标：抽屉底部是「头像+用户名 | Lv徽章 | 扫一扫 | 消息 | 设置」一行，
         齿轮是最右侧图标 → 抽屉底行最右端（约屏宽 76%、屏高 95% 处）
    验证：进入设置页（出现「字号大小 / 账号管理」等）。
    """
    for attempt in range(3):
        if not _drawer_opened(d):
            # 抽屉被意外关闭则重新打开
            if not _step1_open_drawer(d):
                continue

        if _click_words(d, ("设置", "设置按钮", "打开设置")):
            time.sleep(1.5)
        else:
            w, h = _screen_size(d)
            # 抽屉底行最右侧齿轮图标（竖屏抽屉宽约 85%，齿轮在其右端）
            d.click(int(w * 0.765), int(h * 0.952))
            time.sleep(1.5)
            logger.info("已按坐标点击抽屉右下角设置按钮")

        if _on_settings_page(d):
            logger.info("✅ ② 已进入设置页")
            return True
        logger.warning(f"第 {attempt + 1}/3 次进入设置页未见特征，重试")
    return False


def _step3_click_logout(d) -> bool:
    """③ 设置页下滑到底部点「退出登录」（带验证）。

    验证：点击后底部弹出「确认退出 / 取消」弹层。
    """
    # 「退出登录」一般在设置页底部，先直接找，找不到再滑
    for _ in range(8):
        if "退出登录" in dump_xml(d):
            if _try_click(d, text="退出登录"):
                time.sleep(1.5)
                xml = dump_xml(d)
                if "确认退出" in xml:
                    logger.info("✅ ③ 已点「退出登录」，确认弹层已出现")
                    return True
                # 点了但弹层没出现：可能点偏，继续找
        else:
            _swipe_up(d)
            time.sleep(0.6)
    logger.warning("设置页翻到底都没点到「退出登录」")
    return False


def _step4_confirm_logout(d) -> bool:
    """④ 底部弹层点「确认退出」（带验证）。

    验证：退出后进入未登录态（登录页特征出现，或主界面特征消失）。
    """
    for attempt in range(3):
        if _try_click(d, text="确认退出"):
            time.sleep(2.5)
        else:
            # 弹层可能已消失，直接检查状态
            time.sleep(1.0)

        if not is_logged_in(d):
            logger.info("✅ ④ 已退出登录")
            return True
        logger.warning(f"第 {attempt + 1}/3 次确认退出后仍检测为已登录，重试")
    return False


def logout(d) -> bool:
    """退出登录全流程（图片步骤 ①~④）。

    ① 左上角开抽屉 → ② 抽屉右下角设置 → ③ 设置页底部「退出登录」→ ④「确认退出」
    """
    logger.info("===== 开始登出文心 =====")

    # 不在主界面先回主界面
    start_wenxin(d)
    _back_to_main(d)   # 兼容停在个人主页/设置页等子页面的情况
    if not is_logged_in(d):
        logger.info("当前已是未登录状态，无需登出")
        return True

    if not _step1_open_drawer(d):
        logger.warning("① 打开抽屉失败，登出中止")
        return False
    if not _step2_open_settings(d):
        logger.warning("② 进入设置页失败，登出中止")
        return False
    if not _step3_click_logout(d):
        logger.warning("③ 点击「退出登录」失败，登出中止")
        return False
    if not _step4_confirm_logout(d):
        logger.warning("④ 确认退出失败")
        return False

    logger.info("===== 登出文心完成 =====")
    return True


# =====================================================================
#  登录（图片步骤 ⑤~⑨）
# =====================================================================

def _wait_landing(d, timeout=15) -> bool:
    """等登录落地页（手机号输入页）出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _on_landing_page(d):
            return True
        time.sleep(0.8)
    return False


def _step5_input_phone(d, phone: str) -> bool:
    """⑤ 登录页输入手机号码（带验证 + 重试）。

    手机号输入框是登录页上唯一的 EditText（+86 前缀右侧）。
    验证：输入后页面 XML 中出现该手机号文本。
    """
    for attempt in range(3):
        try:
            # 优先：带「手机号」hint 的输入框；兜底：页面第一个 EditText
            obj = d(textContains="手机号", className="android.widget.EditText")
            if not obj.exists(timeout=2):
                obj = d(className="android.widget.EditText")
            if obj.exists(timeout=2):
                obj.click()
                time.sleep(0.4)
                try:
                    obj.clear_text()
                except Exception:
                    pass
                obj.set_text(phone)
                time.sleep(0.8)
                if phone in dump_xml(d):
                    logger.info(f"✅ ⑤ 已填入手机号 {phone}")
                    return True
        except Exception as exc:
            logger.debug(f"输入手机号异常: {exc}")
        logger.warning(f"第 {attempt + 1}/3 次输入手机号未验证成功，重试")
    return False


def _tick_agreement_checkbox(d) -> bool:
    """（可选前置）勾选登录页底部的协议小圆点。

    小圆点在「请您阅读并同意 文心用户协议…」文本左侧，勾选后点「验证码登录」
    就不会再弹协议弹窗。找不到/点不中也不影响主流程（弹窗流程兜底）。
    """
    try:
        xml = dump_xml(d)
        if "请您阅读并同意" not in xml and "文心用户协议" not in xml:
            return False
        # 以协议文本为锚点，圆点在其左侧同一行
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        anchor = None
        for node in root.iter("node"):
            text = node.attrib.get("text", "") or ""
            if "请您阅读并同意" in text or "文心用户协议" in text:
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                             node.attrib.get("bounds", ""))
                if m:
                    anchor = tuple(map(int, m.groups()))
                    break
        if anchor:
            x1, y1, x2, y2 = anchor
            w, h = _screen_size(d)
            # 圆点中心 ≈ 文本左缘往左半个行高的位置
            cx = max(int(w * 0.045), x1 - (y2 - y1))
            cy = (y1 + y2) // 2
            d.click(cx, cy)
            time.sleep(0.6)
            logger.info(f"已按锚点勾选协议小圆点 ({cx},{cy})")
            return True
    except Exception as exc:
        logger.debug(f"勾选协议小圆点异常: {exc}")
    return False


def _step7_click_vcode_login(d) -> bool:
    """⑦ 点「验证码登录」按钮（状态机 + 轮询确认，幂等防误判）。

    实机证据：点击后页面有约 3 秒过渡态（WebView 加载，期间 XML 中所有
    登录相关特征词全部消失）。旧实现「固定 sleep 2s + 单次 dump」正好落在
    过渡态，会把**已经成功的点击误判为失败**，随后重试时页面已跳转、按钮
    消失，又报「未找到按钮」，最终整个登录被误杀。

    本实现三条铁律：
      1. 点击【前】先判状态：已在短信页/协议弹窗 → 直接成功，绝不重复点击；
      2. 点击【后】轮询 8s 等待目标状态，平滑吃掉过渡动画；
      3. 按钮找不到的瞬间先复查状态——页面已跳转就算成功；
         8s 后只要已离开登录页也放行（后续 _wait_sms_page 还有 15s 长等）。
    """
    for attempt in range(3):
        phase = _login_phase(d)

        # ---- 铁律 1：点击前幂等检查（已进入目标页直接成功）----
        if phase in ("sms", "agreement"):
            logger.info(f"✅ ⑦ 当前已在{_PHASE_CN[phase]}，点击此前已生效，无需重复点击")
            return True
        if phase == "transition":
            logger.info("⑦ 页面过渡中（登录特征未渲染），1s 后复查状态")
            time.sleep(1.0)
            continue

        # ---- phase == 'landing'：按钮应当存在，执行点击 ----
        if not _try_click(d, text="验证码登录"):
            # 铁律 3：按钮消失可能是点击瞬间页面刚好跳走，先复查再报错
            phase = _login_phase(d)
            if phase in ("sms", "agreement"):
                logger.info(f"✅ ⑦ 按钮消失时页面已进入{_PHASE_CN[phase]}，判定点击已生效")
                return True
            logger.warning(f"第 {attempt + 1}/3 次未找到「验证码登录」按钮"
                           f"（当前状态: {_PHASE_CN[phase]}），1s 后重试")
            time.sleep(1.0)
            continue

        # ---- 铁律 2：点击后轮询等待协议弹窗 / 短信验证码页 ----
        logger.info("⑦ 已点击「验证码登录」，轮询等待协议弹窗/短信验证码页...")
        deadline = time.time() + 8.0
        while time.time() < deadline:
            time.sleep(0.6)
            phase = _login_phase(d)
            if phase in ("sms", "agreement"):
                logger.info(f"✅ ⑦ 点击生效，已进入{_PHASE_CN[phase]}")
                return True

        # ---- 8s 未确认：已离开登录页即放行，交给后续 _wait_sms_page 兜底 ----
        phase = _login_phase(d)
        if phase != "landing":
            logger.warning(f"⑦ 8s 内未见目标页特征，但当前已是{_PHASE_CN[phase]}，"
                           f"放行并交由 _wait_sms_page 继续确认")
            return True
        logger.warning(f"第 {attempt + 1}/3 次点击后仍停留在登录页，重试")

    return False


def _step6_agree_agreement(d) -> bool:
    """⑥ 「服务协议及隐私保护」弹窗 → 点「同意」（状态机 + 轮询，幂等）。

    两种正常路径：
      - ⑦ 之前已勾选协议 → 不弹窗、直达短信页 → 本步骤自动跳过；
      - 未勾选 → 弹窗 → 点「同意」→ 约 3s 过渡态后进入短信页（短信下发）。

    ⚠️ 与 ⑦ 同样的坑：点「同意」后有 ~3s 过渡态，不能用固定 sleep + 单次 dump，
    否则会误判"同意失败"并在弹窗已消失后反复找不到「同意」按钮。
    """
    # ---- 先等弹窗（最多 4s）：期间若直达短信页则直接通过 ----
    dialog = False
    deadline = time.time() + 4.0
    while time.time() < deadline:
        phase = _login_phase(d)
        if phase == "sms":
            logger.info("协议弹窗未出现（协议此前已勾选），已直达短信页，跳过 ⑥")
            return True
        if phase == "agreement":
            dialog = True
            break
        time.sleep(0.5)
    if not dialog:
        logger.info("协议弹窗未出现，跳过 ⑥")
        return True

    # ---- 弹窗上点「同意」，轮询等短信页 ----
    for attempt in range(3):
        # 幂等：弹窗可能已被上一次点击关掉
        phase = _login_phase(d)
        if phase == "sms":
            logger.info("✅ ⑥ 协议弹窗已确认，已在短信验证码页（短信已下发）")
            return True
        if phase != "agreement":
            logger.info(f"⑥ 弹窗已消失（当前 {_PHASE_CN[phase]}），等待短信页...")
            return _wait_sms_page(d, timeout=10)

        if not _try_click(d, text="同意"):
            logger.warning("协议弹窗上未找到「同意」按钮，0.8s 后重试")
            time.sleep(0.8)
            continue

        logger.info("⑥ 已点「同意」，轮询等待短信验证码页...")
        if _wait_sms_page(d, timeout=10):
            logger.info("✅ ⑥ 已同意协议，进入短信验证码页（短信已下发）")
            return True
        logger.warning(f"第 {attempt + 1}/3 次同意后 10s 内未见短信页，复查弹窗状态")

    return _on_sms_page(d)


def _wait_sms_page(d, timeout=15) -> bool:
    """等短信验证码页出现（⑥⑦ 走完后的落地页）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _on_sms_page(d):
            return True
        time.sleep(0.8)
    return False


def _step9_input_vcode(d, vcode: str) -> bool:
    """⑨ 输入 6 位短信验证码。

    按项目通用规则：**优先逐位模拟真人输入**（每 0.25~0.45 秒一位，
    第 3 位后增加停顿），避免一次性输入触发风控；兜底用一次性文本输入。

    文心的验证码区是 6 个分格框，获得焦点后逐位键入会自动跳格，
    输满 6 位一般自动提交，无需再点按钮。
    """
    vcode = "".join(ch for ch in str(vcode) if ch.isdigit())
    if len(vcode) != 6:
        logger.warning(f"验证码位数异常: {vcode!r}，仍尝试输入")

    # ---- 先让验证码输入区获得焦点 ----
    focused = False
    try:
        obj = d(className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.click()
            focused = True
    except Exception:
        pass
    if not focused:
        # 坐标兜底：点第一个分格框（图⑨红框区域，约屏宽 12%、屏高 41%）
        w, h = _screen_size(d)
        d.click(int(w * 0.12), int(h * 0.41))
        time.sleep(0.5)
        logger.info("已按坐标点击验证码首格获取焦点")
    time.sleep(0.5)

    # ---- 逐位模拟真人输入（KEYCODE_0=7 … KEYCODE_9=16）----
    try:
        for i, ch in enumerate(vcode):
            d.press(7 + int(ch))
            # 第 3 位后多停一拍，更像真人节奏
            if i == 2:
                time.sleep(random.uniform(0.6, 0.9))
            else:
                time.sleep(random.uniform(0.25, 0.45))
        logger.info("✅ ⑨ 已逐位输入验证码")
        return True
    except Exception as exc:
        logger.warning(f"逐位输入异常（{exc}），改用一次性文本输入兜底")

    # ---- 兜底：一次性 set_text ----
    try:
        obj = d(className="android.widget.EditText")
        if obj.exists(timeout=2):
            obj.set_text(vcode)
            logger.info("✅ ⑨ 已一次性填入验证码")
            return True
    except Exception as exc2:
        logger.warning(f"一次性输入验证码也失败: {exc2}")
    return False


def _wait_login_success(d, timeout=20) -> bool:
    """等登录成功（进入主聊天界面），期间顺手关掉干扰弹窗。

    兼容场景：
      - 正常登录：直接进主页
      - 新手机号短信验证后：弹出「完善账号信息」或「请设置密码」页 → 点击「跳过」
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 检测「请设置密码」/「完善账号信息」页 → 点击「跳过」
        xml = dump_xml(d)
        if "请设置密码" in xml or "设置密码后可使用" in xml or "完善账号信息" in xml or "请设置用户名和密码" in xml:
            logger.info("🔔 检测到「完善账号信息/请设置密码」页，点击「跳过」")
            clicked = False
            for text in ("跳过", "Skip"):
                try:
                    obj = d(text=text)
                    if obj.exists(timeout=1.5):
                        obj.click()
                        clicked = True
                        logger.info(f"✅ 已点击「{text}」")
                        break
                except Exception:
                    pass
            if not clicked:
                # 兜底：按坐标点击（「跳过」按钮，约 80% 屏宽、71% 屏高）
                w, h = _screen_size(d)
                d.click(int(w * 0.80), int(h * 0.71))
                logger.info("✅ 已按坐标点击「跳过」")
            time.sleep(2.0)
            continue  # 继续检测是否已进入主页

        if is_logged_in(d):
            _dismiss_popups(d)
            return True
        time.sleep(1.0)
    return False


def _handle_new_phone_registration(d) -> bool:
    """检测并处理新手机号注册流程（图1「该手机尚未注册百度」→ 图2「完善账号信息」）。

    触发场景：输入手机号 → 点「验证码登录」后，系统检测到该手机号未注册，
    弹出「该手机尚未注册百度，请先完成注册」弹窗。

    处理流程：
      1. 检测「该手机尚未注册百度」弹窗 → 点「同意并注册」
      2. 进入「完善账号信息」页 → 点「跳过」
      3. 跳过后直接进入文心主页（已登录状态）

    返回：True 表示已处理新注册流程（且已进入已登录状态），False 表示非新手机号
    """
    # 先等一小会让弹窗出现（点击验证码登录后弹窗有延迟）
    time.sleep(2.0)

    xml = dump_xml(d)
    if "该手机尚未注册百度" not in xml and "请先完成注册" not in xml:
        return False

    logger.info("🔔 检测到新手机号注册弹窗「该手机尚未注册百度」")

    # ---- 第一步：点击「同意并注册」----
    clicked = False
    for text in ("同意并注册", "同意", "注册"):
        try:
            obj = d(text=text)
            if obj.exists(timeout=2):
                obj.click()
                clicked = True
                logger.info(f"✅ 已点击「{text}」")
                break
        except Exception:
            pass
    if not clicked:
        # 兜底：按坐标点击（弹窗右下角蓝色按钮，约 68% 屏宽、62% 屏高）
        w, h = _screen_size(d)
        d.click(int(w * 0.68), int(h * 0.62))
        logger.info("✅ 已按坐标点击「同意并注册」")

    time.sleep(2.5)  # 等待完善账号信息页加载

    # ---- 第二步：检测并处理「完善账号信息」页 → 点「跳过」----
    xml = dump_xml(d)
    if "完善账号信息" not in xml and "请设置用户名和密码" not in xml:
        # 可能点完「同意并注册」直接进主页了，没有完善信息页
        logger.info("未出现「完善账号信息」页，可能已直接登录")
        if is_logged_in(d):
            logger.info("✅ 新手机号注册后直接登录成功")
            return True
        return True  # 也算处理完成，让后续流程去检测登录状态

    logger.info("🔔 进入「完善账号信息」页，点击「跳过」")

    # 点击「跳过」按钮
    clicked = False
    for text in ("跳过", "Skip"):
        try:
            obj = d(text=text)
            if obj.exists(timeout=2):
                obj.click()
                clicked = True
                logger.info(f"✅ 已点击「{text}」")
                break
        except Exception:
            pass
    if not clicked:
        # 兜底：按坐标点击（底部「跳过」按钮，约 50% 屏宽、71% 屏高）
        w, h = _screen_size(d)
        d.click(int(w * 0.50), int(h * 0.71))
        logger.info("✅ 已按坐标点击「跳过」")

    time.sleep(2.0)

    # ---- 第三步：检测并处理「请设置密码」页 → 点「跳过」----
    xml = dump_xml(d)
    if "请设置密码" in xml or "设置密码后可使用" in xml:
        logger.info("🔔 检测到「请设置密码」页，点击「跳过」")
        clicked = False
        for text in ("跳过", "Skip"):
            try:
                obj = d(text=text)
                if obj.exists(timeout=2):
                    obj.click()
                    clicked = True
                    logger.info(f"✅ 已点击「{text}」")
                    break
            except Exception:
                pass
        if not clicked:
            # 兜底：按坐标点击（「跳过」按钮，约 80% 屏宽、71% 屏高）
            w, h = _screen_size(d)
            d.click(int(w * 0.80), int(h * 0.71))
            logger.info("✅ 已按坐标点击「跳过」")
        time.sleep(2.0)

    # 验证是否已进入主页
    if is_logged_in(d):
        logger.info("✅ 新手机号注册+跳过后已成功进入主页")
        return True

    logger.warning("⚠ 新手机号注册流程处理完成，但未检测到已登录状态")
    return True  # 仍然返回 True，让后续 _wait_login_success 去兜底


def login_with_phone(d, phone: str, vcode: str | None = None, poller=None) -> bool:
    """完整登录流程（图片步骤 ⑤~⑨）。

    :param phone:   手机号
    :param vcode:   直接指定验证码（测试用）；None 时走 poller 轮询
    :param poller:  轮询函数 poller(after_ts) -> vcode，
                    after_ts 为「短信实际发出」的时间戳，只查该时间之后的新码
    """
    logger.info(f"===== 开始登录文心: {phone} =====")
    start_wenxin(d)
    _back_to_main(d)   # 兼容停在个人主页/设置页等子页面的情况

    # 已登录先登出
    if is_logged_in(d):
        logger.info("检测到已登录，先登出")
        logout(d)
        start_wenxin(d)

    # ---- 等登录落地页（手机号输入页）----
    if not _wait_landing(d, timeout=15):
        # 兜底：重启 App 再等一轮（兼容登出后卡在过渡页的情况）
        force_reset(d)
        start_wenxin(d, wait=5)
        if not _wait_landing(d, timeout=10):
            raise RuntimeError("登录落地页（请输入手机号）一直没出现")

    # ---- ⑤ 输入手机号码 ----
    if not _step5_input_phone(d, phone):
        raise RuntimeError("输入手机号失败")
    time.sleep(0.5)

    # ---- （可选前置）勾选底部协议小圆点，命中则后续不再弹协议窗 ----
    _tick_agreement_checkbox(d)

    # ---- ⑦ 点「验证码登录」----
    if not _step7_click_vcode_login(d):
        raise RuntimeError("点击「验证码登录」失败")
    # ⚠️ 关键时间戳：短信在点击（并通过协议弹窗）后下发，
    #    轮询只查该时间之后的新码，防止拿到历史旧码
    vcode_send_ts = time.time()

    # ---- 新手机号检测：点验证码登录后若弹出「该手机尚未注册百度」→ 走注册流程 ----
    if _handle_new_phone_registration(d):
        # 新手机号：已完成注册+跳过完善信息，直接进入主页
        if _wait_login_success(d, timeout=20):
            logger.info(f"✅ 新手机号 {phone} 注册并登录成功")
            return True
        raise RuntimeError(f"新手机号 {phone} 注册流程完成后未进入已登录状态")

    # ---- ⑥ 协议弹窗 → 点「同意」（未弹窗则自动通过）----
    if not _step6_agree_agreement(d):
        raise RuntimeError("协议弹窗处理失败")

    # ---- 等短信验证码页就绪 ----
    if not _wait_sms_page(d, timeout=15):
        raise RuntimeError("短信验证码页一直没出现")

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

    # ---- ⑨ 输入验证码（逐位模拟真人输入，输满 6 位一般自动提交）----
    if not _step9_input_vcode(d, vcode):
        raise RuntimeError("输入验证码失败")

    # ---- 确认登录成功 ----
    if not _wait_login_success(d, timeout=20):
        raise RuntimeError(f"手机号 {phone} 登录后未进入已登录状态")

    logger.info(f"✅ 手机号 {phone} 登录成功")
    return True
