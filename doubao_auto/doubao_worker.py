# -*- coding: utf-8 -*-
"""
doubao_mobile.py
============================================================
豆包 Android App Worker —— 按网页版 doubao.py + worker_lib.py 的结构复刻。

核心原则：
1. 不读 Excel，不自己维护任务列表；直接使用 worker_lib 任务池。
2. 直接从 worker_lib.claim_work() 任务池认领 1 个最小单元。
3. 执行：新对话 -> 输入问题 -> 发送 -> 等待回答完成 -> 提取回答/思考/来源
   -> 手机长截图 -> 上传截图 -> callback 回后端。
4. 成功后 worker_lib 继续认领下一个任务；失败则 worker_lib 自动 abort 归还任务池。
5. 不开启深度思考。保持手机豆包当前默认状态，和用户现有网页版脚本保持一致。
6. 长截图不是单张手机屏幕截图，而是滚动手机页面逐屏截图后拼接成完整长图。
7. 长截图严格采用“先顶部展开资料 -> 顶部向下滚动截图 -> 拼接”的流程。
8. 不依赖固定屏幕坐标，优先使用 resource-id / text / content-desc。
"""

import os
import re
import time
import subprocess
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None

# --- 替代 worker_lib.log 的本地 logging ---
import logging as _logging
_logging.basicConfig(
    level=_logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
_logger = _logging.getLogger('doubao_worker')
def log(msg): _logger.info(str(msg))

# 方便外部 ask_one 调用（run.py 里用）
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent
SCREENSHOT_DIR = BASE_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)


def cleanup_temp_screenshots():
    """删除长截图过程产生的临时探针图片。"""
    patterns = (
        "_bottom_probe.png",
        "_probe.png",
        "_probe2.png",
        "_return_before.png",
        "_return_after.png",
        "_top_probe_before.png",
        "_top_probe_after.png",
        "_top_final_before.png",
        "_top_final_after.png",
    )

    for name in patterns:
        try:
            p = SCREENSHOT_DIR / name
            if p.exists():
                p.unlink()
        except Exception:
            pass

    for p in SCREENSHOT_DIR.glob("_frame_*.png"):
        try:
            p.unlink()
        except Exception:
            pass


PLATFORM_NAME = "doubao_app"  # 必须与后端 task_result.ai_platform 一致

DOUBAO_PACKAGE = "com.larus.nova"
INPUT_RESOURCE_ID = "com.larus.nova:id/input_text"
NEW_CHAT_RESOURCE_ID = "com.larus.nova:id/larus_chat_top_left_create_new_cvs"

ANSWER_TIMEOUT = 600
POLL_INTERVAL = 1.0

# 豆包 Android 回答完成后的精确标志。
# 已从最新 UI hierarchy 确认：
# resource-id = com.larus.nova:id/msg_action_copy
# content-desc = 复制
# 这里只检测，不点击。
ANSWER_COPY_RESOURCE_ID = "com.larus.nova:id/msg_action_copy"
ANSWER_COPY_DESC = "复制"
# 豆包当前真实联网资料 UI
REFERENCE_TITLE_RESOURCE_ID = "com.larus.nova:id/ll_reference_title"
REFERENCE_TITLE_TEXT_ID = "com.larus.nova:id/tv_reference_title"
REFERENCE_KEYWORD_RESOURCE_ID = "com.larus.nova:id/sub_keyword_reference"
REFERENCE_ITEM_RESOURCE_ID = "com.larus.nova:id/ll_source_item"
REFERENCE_CONTENT_RESOURCE_ID = "com.larus.nova:id/tv_reference_content"
MESSAGE_LIST_RESOURCE_ID = "com.larus.nova:id/message_list"
MESSAGE_LIST_PARENT_RESOURCE_ID = "com.larus.nova:id/message_list_parent"
FAST_BUTTON_RESOURCE_ID = "com.larus.nova:id/fast_button"

# 长截图参数
MAX_SCROLLS = 80
SCROLL_WAIT = 1.2
SCROLL_STABLE_ROUNDS = 3
BOTTOM_EXTRA_WAIT = 2.0

# Android UI 中表示“正在生成”的文字
GENERATING_WORDS = (
    "停止生成",
    "停止回答",
    "正在生成",
    "正在思考",
    "生成中",
    "思考中",
)

# 页面顶部/底部常见无关元素
IGNORE_TEXT = {
    "豆包",
    "新对话",
    "分享",
    "更多",
}


def run_adb(*args, timeout=30, check=False):
    """执行 adb，优先使用当前唯一连接设备。"""
    no_device_cmds = {"devices", "connect", "disconnect", "start-server", "kill-server"}
    first_arg = str(args[0]) if args else ""
    if first_arg not in no_device_cmds and _device_serial:
        cmd = ["adb", "-s", _device_serial, *map(str, args)]
    else:
        cmd = ["adb", *map(str, args)]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
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
        raise RuntimeError("没有检测到 Android 手机，请先执行 adb devices 并确认状态为 device")

    # 优先匹配配置的端口（"5554" in serial 同时命中 emulator-5554 和 127.0.0.1:5554）
    if port and port > 0:
        target = str(port)
        for serial in devices:
            if target in serial:
                log(f"匹配到端口 {port} 的设备: {serial}")
                _device_serial = serial
                return serial
        log(f"没找到端口 {port} 对应的设备，回退自动取第一台")

    if len(devices) > 1:
        log(f"检测到多个 Android 设备，将使用第一个: {devices[0]}")
    else:
        log(f"检测到 Android 设备: {devices[0]}")
    serial = devices[0]
    _device_serial = serial
    return serial


def connect_phone():
    if u2 is None:
        raise RuntimeError(
            "缺少 uiautomator2，请执行：pip install -U uiautomator2"
        )

    serial = get_device_serial()
    log(f"连接手机 UIAutomator2: {serial}")
    d = u2.connect(serial)

    # 确认连接
    try:
        info = d.info
        log(
            f"手机已连接: "
            f"{info.get('productName') or info.get('brand') or ''} "
            f"{info.get('displayWidth')}x{info.get('displayHeight')}"
        )
    except Exception as e:
        raise RuntimeError(f"uiautomator2 连接成功但读取设备信息失败: {e}")

    return d


def human_wait(seconds=2):
    time.sleep(seconds)


def start_doubao(d):
    log("打开手机豆包")
    d.app_start(DOUBAO_PACKAGE, stop=False)
    time.sleep(3)

    # 如果当前停留在其它页面，重新拉起前台
    try:
        if not d.app_current().get("package") == DOUBAO_PACKAGE:
            d.app_start(DOUBAO_PACKAGE, stop=False)
            time.sleep(3)
    except Exception:
        pass


def dump_xml(d, path=None):
    xml = d.dump_hierarchy(compressed=False)
    if path:
        Path(path).write_text(xml, encoding="utf-8")
    return xml


def node_texts(d):
    """取得当前 UI 中的所有 text/content-desc。"""
    try:
        xml = dump_xml(d)
    except Exception as e:
        log(f"读取 UI XML 失败: {e}")
        return []

    # UIAutomator XML 属性
    values = []
    for m in re.finditer(r'\b(text|content-desc)="([^"]*)"', xml):
        value = m.group(2)
        if value:
            value = (
                value.replace("&quot;", '"')
                .replace("&amp;", "&")
                .replace("&#10;", "\n")
            )
            values.append(value)

    return values


def has_generating_text(d):
    texts = node_texts(d)
    joined = "\n".join(texts)
    return any(word in joined for word in GENERATING_WORDS)


def new_chat(d):
    """
    和网页版 new_chat(page) 对应。
    首选 resource-id，不使用固定坐标。
    """
    log("新建对话")

    try:
        btn = d(resourceId=NEW_CHAT_RESOURCE_ID)
        if btn.exists(timeout=3):
            btn.click()
            time.sleep(2)
            log("已点击新对话按钮")
            return True
    except Exception as e:
        log(f"resource-id 新对话失败: {e}")

    # 兼容 UI 版本变化：按文字/描述找“新对话”
    selectors = [
        {"text": "新对话"},
        {"descriptionContains": "新对话"},
        {"textContains": "新对话"},
    ]

    for selector in selectors:
        try:
            obj = d(**selector)
            if obj.exists(timeout=2):
                obj.click()
                time.sleep(2)
                log("已通过文字选择器新建对话")
                return True
        except Exception:
            pass

    log("未找到新对话按钮，尝试返回到聊天首页")
    try:
        d.press("back")
        time.sleep(1)
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------
#  输入模式检测：豆包会记住上次的输入方式，登录后进来时底部若是
#  「按住说话」语音按钮，直接 set_text 会打偏（甚至找不到输入框）。
#  因此每次提问前先检测一遍，发现是语音模式就点键盘图标切回输入框。
# ---------------------------------------------------------------------

# 语音模式下居中显示的「按住说话」按钮文案。注意与输入框占位文案
# 「发消息或按住说话...」不同，这里用精确匹配，不会误判占位提示。
HOLD_TO_SPEAK_TEXT = "按住说话"

# 「切换到键盘/输入框」按钮可能命中的 resource-id（不同豆包版本命名不一）
# 注意：不要用 descriptionContains="键盘"，会误匹配系统输入法切换按钮！
KEYBOARD_TOGGLE_RIDS = (
    "com.larus.nova:id/action_keyboard_group",
    "com.larus.nova:id/action_keyboard",
    # 语音/键盘共用同一个槽位时，槽位 id 可能仍是下面这两个
    "com.larus.nova:id/action_speak_group",
    "com.larus.nova:id/action_speak",
)

# 「更多面板」(⊕) 按钮的 resource-id：语音模式下键盘图标就紧邻它左侧，
# 用作定位兜底
MORE_PANEL_RIDS = (
    "com.larus.nova:id/iv_action_menu_entrance",
    "com.larus.nova:id/action_menu_entrance",
)


def _has_hold_to_speak(d) -> bool:
    """底部是否出现「按住说话」语音按钮（精确文案，排除输入框占位提示）。"""
    try:
        return bool(d(text=HOLD_TO_SPEAK_TEXT).exists(timeout=0.3))
    except Exception:
        return False


def _input_text_ready(d) -> bool:
    """输入区是否已是可用的文字输入框（EditText）。"""
    # 存在「按住说话」按钮 → 语音模式，不算就绪
    if _has_hold_to_speak(d):
        return False
    try:
        return bool(d(resourceId=INPUT_RESOURCE_ID).exists(timeout=1.0))
    except Exception:
        return False


def _adb_tap(x: int, y: int) -> bool:
    """用 adb shell input tap 精准点击坐标。uiautomator2 的 d.click 偶尔失灵时兜底。"""
    try:
        r = run_adb("shell", "input", "tap", str(int(x)), str(int(y)), timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _parse_bounds(attrib):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrib.get("bounds", ""))
    if not m:
        return None
    return tuple(int(v) for v in m.groups())  # (x1,y1,x2,y2)


def _find_node_in_dump(root, *, rid=None, desc=None, desc_contains=None, text=None):
    """在 dump 的 ET 根节点里找第一个匹配的元素，返回 (elem, bounds)。"""
    for elem in root.iter():
        if rid is not None and elem.get("resource-id") != rid:
            continue
        if desc is not None and elem.get("content-desc") != desc:
            continue
        if desc_contains is not None and desc_contains not in (elem.get("content-desc") or ""):
            continue
        if text is not None and elem.get("text") != text:
            continue
        b = _parse_bounds(elem.attrib)
        if b:
            return elem, b
    return None, None


def _click_between_speak_and_more(d) -> bool:
    """坐标定位：在「按住说话」按钮和「更多面板」⊕按钮之间点击键盘切换图标。

    从真实截图看，底部栏布局是：方形图标 | 按住说话 | 九宫格键盘图标 | ⊕更多面板。
    键盘图标没有可靠的文字/desc，descriptionContains="键盘" 会误匹配系统输入法。
    所以这里用「按住说话」右边界 + 「更多面板」左边界做双锚点，点击它们中点。
    """
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception as e:
        log(f"dump 界面失败: {e}")
        return False

    # 找「按住说话」按钮（左锚）
    _, speak = _find_node_in_dump(root, text=HOLD_TO_SPEAK_TEXT)
    # 找「更多面板」⊕（右锚）
    more = None
    for rid in MORE_PANEL_RIDS:
        _, more = _find_node_in_dump(root, rid=rid)
        if more:
            break
    if not more:
        _, more = _find_node_in_dump(root, desc="更多面板")

    if not speak:
        log("定位失败：没找到「按住说话」按钮")
        return False
    if not more:
        log("定位失败：没找到「更多面板」按钮")
        return False

    sx1, sy1, sx2, sy2 = speak
    mx1, my1, mx2, my2 = more

    # 键盘切换图标在两者之间，取水平中点；垂直方向用「按住说话」的中心
    x = (sx2 + mx1) // 2
    y = (sy1 + sy2) // 2

    log(f"定位到键盘切换按钮：按住说话右边界={sx2}, 更多面板左边界={mx1}, 点击点=({x},{y})")

    # 优先 adb 精准点击，失败回退 uiautomator2
    if _adb_tap(x, y):
        log(f"adb 已点击键盘切换按钮 ({x},{y})")
        return True
    try:
        d.click(x, y)
        log(f"已点击键盘切换按钮 ({x},{y})")
        return True
    except Exception as e:
        log(f"坐标点击切换按钮失败: {e}")
        return False


def _click_keyboard_toggle(d) -> bool:
    """点击「切换回输入框」按钮（语音模式下的键盘图标）。

    策略：
      1. 按 resource-id 找按钮，取中心点用 adb tap（因为 clickable=false，obj.click 不灵）
      2. 双锚点坐标兜底：「按住说话」右边界 与 「更多面板」左边界 的中点
    """
    # 1) 按 resource-id 找（点击时若 clickable=false，则取坐标用 adb 点）
    # 注意：action_speak_group 在语音模式下可能是「按住说话」按钮本身，
    #       必须排除掉，否则会误点语音按钮。
    for rid in KEYBOARD_TOGGLE_RIDS:
        try:
            obj = d(resourceId=rid)
            if not obj.exists(timeout=0.5):
                continue
            # 如果该节点内部包含「按住说话」文本，说明它就是语音按钮本身，跳过
            if rid.endswith("action_speak_group") or rid.endswith("action_speak"):
                try:
                    if obj.child(text=HOLD_TO_SPEAK_TEXT).exists:
                        log(f"rid「{rid}」是语音按钮本身，跳过")
                        continue
                except Exception:
                    pass
            b = obj.info.get("bounds")
            if b and "left" in b:
                cx = (b["left"] + b["right"]) // 2
                cy = (b["top"] + b["bottom"]) // 2
                if _adb_tap(cx, cy):
                    log(f"adb 已按 rid「{rid}」中心点击 ({cx},{cy})")
                    return True
        except Exception:
            pass

    # 2) 双锚点坐标兜底
    return _click_between_speak_and_more(d)


def ensure_input_mode(d, retries: int = 3) -> bool:
    """确保底部输入区处于「文字输入框」模式，而不是「按住说话」语音模式。

    豆包会记住上次的输入方式，登录后进来时底部可能是「按住说话」按钮，
    这时直接 set_text 会打偏或抛「找不到豆包输入框」。所以每次提问前检测一遍，
    发现是语音模式就点键盘图标切回文字输入框。

    :param retries: 切换失败时的重试次数
    :return: True 表示输入区已是文字输入框（可用）；
             False 表示未能确认/切换成功（不阻断流程，仅提示）
    """
    for attempt in range(1, retries + 1):
        if _input_text_ready(d):
            log("输入区已是文字输入框模式 ✅")
            return True

        # 只有确认为「按住说话」语音模式才动手，避免在其它页面乱点
        if not _has_hold_to_speak(d):
            log("未检测到输入框，也未检测到「按住说话」按钮，跳过自动切换")
            return False

        log(f"检测到「按住说话」语音模式，尝试切换回输入框（第 {attempt}/{retries} 次）")
        clicked = _click_keyboard_toggle(d)
        if not clicked:
            log("未找到「切换键盘」按钮，本轮切换未执行")
            continue

        # 点击后等一下 UI 切换动画，再验证是否切成功
        time.sleep(2.0)
        if _input_text_ready(d):
            log("已切换到文字输入框模式 ✅")
            return True
        if not _has_hold_to_speak(d):
            # 不再是语音模式但也没检测到输入框——可能是进了别的页面，先不管
            log("语音模式已消失，但未检测到输入框，视为切换中")
            time.sleep(1.0)
            if _input_text_ready(d):
                log("已切换到文字输入框模式 ✅")
                return True

    log("⚠️ 输入框模式检测/切换失败：底部可能仍是「按住说话」，"
        "后续输入可能失败，请检查豆包 UI 是否改版")
    return False


def ensure_input(d):
    """找到豆包输入框。"""
    try:
        obj = d(resourceId=INPUT_RESOURCE_ID)
        if obj.exists(timeout=10):
            return obj
    except Exception:
        pass

    # 兼容 resource-id 变化
    for kwargs in (
        {"className": "android.widget.EditText"},
        {"textContains": "发消息"},
        {"descriptionContains": "发消息"},
    ):
        try:
            obj = d(**kwargs)
            if obj.exists(timeout=3):
                return obj
        except Exception:
            pass

    raise RuntimeError("找不到豆包输入框")


def clear_and_input(d, question):
    log("输入问题")

    # 先确保处于文字输入框模式（而不是「按住说话」语音模式），
    # 否则后续 click/set_text 会打偏，导致问题发不出去。
    ensure_input_mode(d)

    edit = ensure_input(d)
    edit.click()

    # uiautomator2 set_text 能直接走 Android 自动化输入链路，
    # 不使用 adb shell input text，避免中文编码问题。
    try:
        edit.set_text(str(question))
    except Exception as e:
        log(f"set_text 失败，尝试 fast input: {e}")
        try:
            d.set_fastinput_ime(True)
            d.send_keys(str(question), clear=True)
        finally:
            try:
                d.set_fastinput_ime(False)
            except Exception:
                pass

    time.sleep(0.8)


def send_message(d):
    """
    通过豆包界面上的“发送按钮”发送问题。

    不再使用 d.send_action("send") 作为主要发送方式，
    因为实际设备上 IME send 没有成功把问题发送出去。

    优先级：
    1. resource-id 发送按钮
    2. content-desc=发送
    3. text=发送
    4. 其它常见发送按钮选择器
    5. 最后才用 Enter 兜底
    """

    # 发送按钮的常见 resource-id / content-desc。
    # 真机和模拟器 resource-id 可能不同，都要覆盖。
    send_selectors = [
        ("resourceId", "com.larus.nova:id/send_button"),
        ("resourceId", "com.larus.nova:id/input_send"),
        ("resourceId", "com.larus.nova:id/send"),
        # 雷电模拟器豆包 App 用的发送按钮 id：
        ("resourceId", "com.larus.nova:id/action_send_container"),
        ("description", "发送"),
        ("text", "发送"),
    ]

    # 1. 优先通过精确选择器找发送按钮
    for selector_type, value in send_selectors:
        try:
            if selector_type == "resourceId":
                obj = d(resourceId=value)
            elif selector_type == "description":
                obj = d(description=value)
            else:
                obj = d(text=value)

            if obj.exists:
                try:
                    obj.click()
                except Exception:
                    obj.click(timeout=3)

                time.sleep(1)
                log(f"已通过发送按钮发送: {selector_type}={value}")
                return True

        except Exception as e:
            log(f"发送按钮选择器失败 {selector_type}={value}: {e}")

    # 2. 通过 UI hierarchy 扫描可点击的“发送”节点
    try:
        xml = dump_xml(d)
        root = ET.fromstring(xml)

        candidates = []
        for node in root.iter("node"):
            text_value = (node.attrib.get("text") or "").strip()
            desc_value = (node.attrib.get("content-desc") or "").strip()
            resource_id = (node.attrib.get("resource-id") or "").strip()
            clickable = node.attrib.get("clickable") == "true"
            enabled = node.attrib.get("enabled") != "false"
            visible = node.attrib.get("visible-to-user") != "false"

            if not (clickable and enabled and visible):
                continue

            if (
                text_value == "发送"
                or desc_value == "发送"
                or resource_id.endswith(":id/send_button")
                or resource_id.endswith(":id/input_send")
                or resource_id.endswith(":id/send")
                # 雷电模拟器的 action_send_container 也要覆盖
                or resource_id.endswith(":id/action_send_container")
                or "send" in resource_id.lower()
            ):
                candidates.append(node)

        if candidates:
            # 从最后一个候选发送按钮开始尝试。
            for node in reversed(candidates):
                bounds = node.attrib.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not m:
                    continue

                x1, y1, x2, y2 = map(int, m.groups())
                x = (x1 + x2) // 2
                y = (y1 + y2) // 2

                try:
                    d.click(x, y)
                    time.sleep(1)
                    log(f"已通过 UI hierarchy 发送按钮发送: ({x}, {y})")
                    return True
                except Exception as e:
                    log(f"UI hierarchy 发送按钮点击失败: {e}")

    except Exception as e:
        log(f"扫描发送按钮失败: {e}")

    # 3. 最后才使用 Enter 作为兜底。
    # 注意：正常情况下不会走到这里。
    try:
        d.press("enter")
        time.sleep(1)
        log("发送按钮未找到，最后使用 Enter 兜底发送")
        return True
    except Exception as e:
        log(f"Enter 兜底发送失败: {e}")

    return False


def answer_copy_button_exists(d):
    """
    精确判断当前回答是否已经完成。

    豆包 Android UI hierarchy 已确认：
        resource-id = com.larus.nova:id/msg_action_copy
        content-desc = 复制

    注意：这里只检测“复制”按钮，不点击它。
    """
    try:
        obj = d(resourceId=ANSWER_COPY_RESOURCE_ID)
        if obj.exists:
            return True
    except Exception:
        pass

    # resource-id 失效时，用 content-desc 兜底。
    try:
        obj = d(description=ANSWER_COPY_DESC)
        if obj.exists:
            return True
    except Exception:
        pass

    return False


def has_real_answer_text(d):
    """辅助确认回答正文已经出现，避免刚发送时误判。"""
    try:
        xml = dump_xml(d)
        blocks = extract_message_blocks(xml)
        return bool(blocks and len(clean_text(blocks[-1])) >= 10)
    except Exception:
        return False



def click_fast_bottom_button(d):
    """
    豆包当前真实 UI 有一个“回到底部”按钮：
        com.larus.nova:id/fast_button
        content-desc = 回到底部

    只点击这个按钮，不用猜坐标。
    """
    try:
        obj = d(resourceId=FAST_BUTTON_RESOURCE_ID)
        if obj.exists:
            obj.click()
            time.sleep(1.2)
            log("已点击“回到底部”按钮")
            return True
    except Exception as e:
        log(f"点击“回到底部”按钮失败: {e}")

    try:
        obj = d(description="回到底部")
        if obj.exists:
            obj.click()
            time.sleep(1.2)
            log("已通过 content-desc 点击“回到底部”")
            return True
    except Exception as e:
        log(f"通过 content-desc 点击“回到底部”失败: {e}")

    return False


def force_bottom_before_answer_check(d):
    """
    发送以后，如果页面没有停在回答最底部，就主动点击豆包自己的
    “回到底部”按钮。

    注意：
    - 不使用 swipe；
    - 不调用 scroll_to_bottom；
    - 不滚到顶部；
    - 不会点击资料区域。
    """
    for _ in range(5):
        if click_fast_bottom_button(d):
            continue

        # 没有“回到底部”按钮，通常意味着已经在底部。
        try:
            rv = d(resourceId=MESSAGE_LIST_RESOURCE_ID)
            if rv.exists:
                # 再用 RecyclerView 的 toEnd 做一次保险。
                try:
                    rv.scroll.toEnd()
                    time.sleep(0.8)
                except Exception:
                    pass
        except Exception:
            pass

        return True

    return True


def wait_answer_finish(d):
    """
    等待回答完成。

    核心规则：
      1. 发送后始终尽量保持在回答底部；
      2. 如果豆包出现“回到底部”按钮，说明当前不在底部，
         立即点击它；
      3. 最终只以 msg_action_copy（复制）作为回答完成标志；
      4. 不滚到顶部；
      5. 不点击资料；
      6. 不点击复制。

    这样可以避免：
      回答已经完成
        -> 页面停在回答上方
        -> UI hierarchy 看不到当前回答的复制按钮
        -> 程序误以为没有完成
    """
    log("等待回答完成：强制保持在回答底部，检测“复制”按钮")

    start_time = time.time()
    seen_copy = 0
    last_bottom_click = 0.0

    # 刚发送后先给 App 一点时间开始生成。
    time.sleep(0.8)

    while time.time() - start_time < ANSWER_TIMEOUT:
        try:
            # 第一优先级：检查复制按钮。
            if answer_copy_button_exists(d):
                seen_copy += 1
                log(f"检测到“复制”按钮，第 {seen_copy}/2 次")

                if seen_copy >= 2:
                    log("确认回答完成：已检测到“复制”按钮")

                    # 关键：确认完成以后再强制一次到底部。
                    # 这一步不会滚顶部，也不会触碰资料。
                    click_fast_bottom_button(d)

                    time.sleep(0.8)
                    return True

            else:
                seen_copy = 0

                # 如果不在底部，豆包会出现“回到底部”按钮。
                # 每 1.5 秒最多点击一次，避免疯狂点击。
                now = time.time()
                if now - last_bottom_click >= 1.5:
                    if click_fast_bottom_button(d):
                        last_bottom_click = now

        except Exception as e:
            log(f"检测回答状态失败: {e}")

        time.sleep(POLL_INTERVAL)

    log("回答超时：未检测到“复制”按钮")
    return False


def clean_text(text):
    if not text:
        return ""

    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_ui_nodes(xml):
    """
    使用 XML 解析器读取 Android UI hierarchy。
    不再使用容易被嵌套 <node> 截断的正则表达式。
    返回 [(text, content_desc, resource_id, bounds, class), ...]
    """
    import xml.etree.ElementTree as ET

    nodes = []

    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log(f"UI XML 解析失败，退回正则解析: {e}")
        for m in re.finditer(
            r'<node\b[^>]*?(?:text|content-desc)="([^"]+)"[^>]*/?>',
            xml,
            re.S,
        ):
            nodes.append({
                "text": clean_text(m.group(1)),
                "desc": "",
                "rid": "",
                "bounds": "",
                "class": "",
            })
        return nodes

    for elem in root.iter("node"):
        text = clean_text(elem.attrib.get("text", ""))
        desc = clean_text(elem.attrib.get("content-desc", ""))
        rid = elem.attrib.get("resource-id", "")
        bounds = elem.attrib.get("bounds", "")
        cls = elem.attrib.get("class", "")

        if text or desc:
            nodes.append({
                "text": text,
                "desc": desc,
                "rid": rid,
                "bounds": bounds,
                "class": cls,
            })

    return nodes

def extract_message_blocks(xml):
    """
    手机豆包没有网页版 DOM class，因此采用 UI hierarchy 做消息块启发式提取。

    重点：
    - 排除输入框、按钮、导航文字；
    - 保留较长 TextView / content-desc；
    - 去掉父节点包含子节点造成的重复文本；
    - 最后一个较长文本块通常是最终回答。
    """
    nodes = parse_ui_nodes(xml)

    candidates = []
    for n in nodes:
        text = n["text"] or n["desc"]
        text = clean_text(text)

        if len(text) < 20:
            continue

        if n["rid"] == INPUT_RESOURCE_ID:
            continue

        if any(x in text for x in (
            "发消息或按住空格说话",
            "深度思考",
            "新对话",
            "复制",
            "重新生成",
            "点赞",
            "点踩",
            "分享",
        )) and len(text) < 100:
            continue

        candidates.append(text)

    # 去掉明显的父子重复：
    # 如果一个候选文本完全包含另一个候选文本，且较长者只是父容器，
    # 先保留更细粒度文本。
    unique = []
    for text in candidates:
        if text in unique:
            continue
        unique.append(text)

    filtered = []
    for text in unique:
        is_parent_duplicate = False
        for other in unique:
            if text == other:
                continue
            if len(other) > len(text) + 30 and text in other:
                is_parent_duplicate = True
                break
        if not is_parent_duplicate:
            filtered.append(text)

    # 再去掉明显 UI 标签
    result = []
    for text in filtered:
        if text in result:
            continue
        result.append(text)

    return result




def extract_thinking_and_answer(xml):
    """
    从当前 Android UI hierarchy 提取：
      thinkingContent = 联网搜索资料（如果有）
      answerText = 最终回答

    联网资料不是每个问题都有，所以：
      - 找不到 reference 节点 -> thinkingContent = ""
      - 找不到 source item -> thinkingContent = ""
      - 都不应该导致任务失败。

    answer 则使用多级兜底：
      1. content_view / 长 TextView 候选；
      2. extract_message_blocks()；
      3. 最长正文候选。
    """
    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log(f"解析 XML 失败: {e}")
        # XML 异常时仍尝试已有的启发式解析，不因为资料解析失败而丢掉回答。
        blocks = extract_message_blocks(xml)
        return "", (max(blocks, key=len) if blocks else "")

    # ---------- 联网搜索资料：可选 ----------
    thinking_parts = []

    title_node = root.find(
        ".//*[@resource-id='com.larus.nova:id/tv_reference_title']"
    )
    if title_node is not None:
        title = clean_text(title_node.attrib.get("text", ""))
        if title:
            thinking_parts.append(title)

    keyword_node = root.find(
        ".//*[@resource-id='com.larus.nova:id/sub_keyword_reference']"
    )
    if keyword_node is not None:
        keyword = clean_text(keyword_node.attrib.get("text", ""))
        if keyword:
            thinking_parts.append("搜索关键词：\n" + keyword)

    source_nodes = root.findall(
        ".//*[@resource-id='com.larus.nova:id/ll_source_item']"
    )

    for item in source_nodes:
        idx_node = item.find(
            ".//*[@resource-id='com.larus.nova:id/tv_reference_index']"
        )
        content_node = item.find(
            ".//*[@resource-id='com.larus.nova:id/tv_reference_content']"
        )

        idx = (
            clean_text(idx_node.attrib.get("text", ""))
            if idx_node is not None else ""
        )
        content = (
            clean_text(content_node.attrib.get("text", ""))
            if content_node is not None else ""
        )

        if content:
            thinking_parts.append(
                f"{idx} {content}".strip() if idx else content
            )

    thinking = "\n\n".join(
        dict.fromkeys(x for x in thinking_parts if x)
    )

    # ---------- 最终回答 ----------
    answer_candidates = []

    excluded_ids = {
        "com.larus.nova:id/tv_reference_title",
        "com.larus.nova:id/tv_reference_content",
        "com.larus.nova:id/tv_reference_index",
        "com.larus.nova:id/sub_keyword_reference",
    }

    excluded_exact = {
        "发消息或按住说话...",
        "复制",
        "播放",
        "点赞",
        "点踩",
        "分享",
        "重新生成",
    }

    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        cls = node.attrib.get("class", "")
        txt = clean_text(node.attrib.get("text", ""))

        if cls != "android.widget.TextView":
            continue
        if not txt or rid in excluded_ids:
            continue
        if txt in excluded_exact:
            continue
        if len(txt) < 10:
            continue

        # 排除明显的固定 UI。
        if txt in {
            "对话", "打电话", "拍题答疑", "视频通话",
            "新对话", "搜索", "来源",
        }:
            continue

        answer_candidates.append(txt)

    # 兜底：使用旧的消息块提取器。
    if not answer_candidates:
        blocks = extract_message_blocks(xml)
        answer_candidates.extend(blocks)

    # 去重并排除联网资料标题本身。
    answer_candidates = list(dict.fromkeys(
        x for x in answer_candidates
        if x and x not in thinking_parts
    ))

    answer = max(answer_candidates, key=len) if answer_candidates else ""

    return clean_text(thinking), clean_text(answer)




def open_sources_mobile(d):
    """
    联网资料是可选的：
      - 有资料：展开一次；
      - 没资料：直接跳过；
      - 已经展开：绝不再次点击。

    不依赖单一 resource-id，因为不同回答/不同 App 状态下，
    参考资料节点可能暂时不出现在当前 hierarchy。
    """
    log("检查联网资料区域（可选，只允许展开一次）")

    try:
        xml = d.dump_hierarchy(compressed=False)

        # 已展开的强证据：资料条目或关键词节点已经存在。
        if (
            "com.larus.nova:id/ll_source_item" in xml
            or "com.larus.nova:id/sub_keyword_reference" in xml
        ):
            log("检测到联网资料已经展开，不再点击")
            return True

        # 参考资料标题存在，但列表还没展开。
        if "com.larus.nova:id/ll_reference_title" in xml:
            try:
                title = d(resourceId=REFERENCE_TITLE_RESOURCE_ID)
                if title.exists:
                    log("检测到资料标题，尚未展开，点击一次")
                    title.click()
                    time.sleep(1.5)

                    xml2 = d.dump_hierarchy(compressed=False)
                    if (
                        "com.larus.nova:id/ll_source_item" in xml2
                        or "com.larus.nova:id/sub_keyword_reference" in xml2
                    ):
                        log("资料展开成功")
                        return True

                    log("点击资料标题后没有出现资料条目，继续任务")
                    return False
            except Exception as e:
                log(f"通过 resource-id 展开资料失败，继续任务: {e}")

        # resource-id 没有暴露时，用页面上真实显示的标题匹配。
        try:
            title_regex = r"搜索\s*\d+\s*个关键词.*参考\s*\d+\s*篇资料"
            obj = d(textMatches=title_regex)
            if obj.exists:
                log("通过文字匹配找到联网资料标题，点击一次")
                obj.click()
                time.sleep(1.5)
                xml2 = d.dump_hierarchy(compressed=False)
                if (
                    "com.larus.nova:id/ll_source_item" in xml2
                    or "com.larus.nova:id/sub_keyword_reference" in xml2
                ):
                    log("资料展开成功")
                    return True
        except Exception as e:
            log(f"通过文字匹配展开资料失败，继续任务: {e}")

    except Exception as e:
        log(f"检查联网资料失败，但资料是可选项，继续任务: {e}")

    log("当前回答没有可展开的联网资料，正常继续")
    return False











def content_signature(img):
    """
    只比较“可滚动聊天内容区”，避开：
    - 顶部固定状态栏/标题栏
    - 底部固定输入框/快捷按钮

    之前用屏幕顶部/底部区域判断位置，会因为固定 Header/Input
    一直不变而误判已经到顶部/底部。
    """
    w, h = img.size
    y1 = int(h * 0.16)
    y2 = int(h * 0.82)
    crop = img.crop((0, y1, w, y2))
    crop.thumbnail((240, 420))
    return crop.tobytes()













def scroll_to_top(d, max_rounds=60, stable_rounds=3):
    """
    真正滚到聊天顶部。

    核心规则：
      不是调用一次 toBeginning 就认为到了顶部。
      而是：一直向上滚 -> 比较消息区域前后画面 ->
      只有连续多次向上滚动后画面完全不再变化，才确认顶部。

    Android 手势方向：手指从下往上是向下看；
    手指从上往下拖，则聊天内容向下移动，回到更早的消息。
    因此这里始终在 message_list 区域执行“向下拖”来找顶部。
    """
    log("开始寻找真正聊天顶部：一直向上滚，直到彻底滚不动")

    stable = 0
    last_sig = None
    no_change_count = 0  # 连续无变化计数（用于提前退出）

    for i in range(max_rounds):
        try:
            before = save_message_area(
                d, SCREENSHOT_DIR / "_top_probe_before.png"
            )
            before_sig = message_area_signature(before)

            # 优先操作真实 RecyclerView；如果 API 不稳定则用实际手势兜底。
            moved_by_api = False
            try:
                rv = d(resourceId=MESSAGE_LIST_RESOURCE_ID)
                if rv.exists:
                    rv.scroll.backward(steps=12)
                    moved_by_api = True
            except Exception as e:
                log(f"顶部第 {i + 1} 次 RecyclerView 向上滚动失败，改用手势: {e}")

            if not moved_by_api:
                swipe_down(d, duration=0.35)

            time.sleep(0.75)

            after = save_message_area(
                d, SCREENSHOT_DIR / "_top_probe_after.png"
            )
            after_sig = message_area_signature(after)

            changed = before_sig != after_sig

            if changed:
                stable = 0
                no_change_count = 0
                last_sig = after_sig
                log(
                    f"[TOP] 第 {i + 1} 次：仍可向上移动，继续滚动"
                )
            else:
                stable += 1
                no_change_count += 1
                log(
                    f"[TOP] 第 {i + 1} 次：已经滚不动，"
                    f"连续不动 {stable}/{stable_rounds}"
                )

                # 必须连续多次完全不变，防止 RecyclerView 惯性/动画造成误判。
                if stable >= stable_rounds:
                    cleanup_temp_screenshots()
                    log("========== 已确认真正到达聊天顶部 ==========")
                    return True

            # 新增：如果连续 5 次滚动后画面完全无变化，提前确认到达顶部
            # 防止某些极端情况下 stable 计数被重置导致一直滚
            if no_change_count >= 5:
                cleanup_temp_screenshots()
                log(f"========== 连续 {no_change_count} 次无变化，确认到达顶部 ==========")
                return True

            # 每轮再做一次极短等待，让 RecyclerView 惯性彻底停止。
            time.sleep(0.25)

        except Exception as e:
            log(f"[TOP] 第 {i + 1} 次顶部探测异常: {e}")
            # 异常不直接判定顶部，继续尝试。
            stable = 0
            time.sleep(0.5)

    cleanup_temp_screenshots()
    log("========== 顶部定位失败：滚动达到最大次数仍无法确认 ==========")
    return False

def scroll_to_bottom(d):
    """
    回到聊天真正底部。

    优先点击豆包自己的“回到底部”按钮，
    再使用 RecyclerView.toEnd 兜底。
    """
    log("截图完成：恢复到聊天真正底部")

    # 第一优先级：豆包自己提供的“回到底部”按钮。
    for _ in range(3):
        if click_fast_bottom_button(d):
            time.sleep(0.5)
            # 按钮消失通常意味着已经到底。
            try:
                if not d(resourceId=FAST_BUTTON_RESOURCE_ID).exists:
                    log("已通过豆包“回到底部”按钮确认到底")
                    return True
            except Exception:
                pass

    # 第二优先级：真实 RecyclerView。
    try:
        obj = d(resourceId=MESSAGE_LIST_RESOURCE_ID)
        if obj.exists:
            try:
                obj.scroll.toEnd()
                time.sleep(1.5)
                log("已通过 message_list RecyclerView 回到底部")
                return True
            except Exception as e:
                log(f"RecyclerView.toEnd 失败: {e}")
    except Exception as e:
        log(f"获取 message_list 失败: {e}")

    # 最后兜底，只在消息区域向上滑。
    for _ in range(40):
        try:
            before = save_message_area(d, SCREENSHOT_DIR / "_bottom_probe.png")
            swipe_up(d)
            time.sleep(0.6)
            after = save_message_area(d, SCREENSHOT_DIR / "_bottom_probe_after.png")

            if message_area_signature(before) == message_area_signature(after):
                log("通过消息区域画面确认已到聊天底部")
                cleanup_temp_screenshots()
                return True
        except Exception:
            pass

    cleanup_temp_screenshots()
    log("底部定位达到最大次数")
    return False




def adb_screenshot_bytes():
    # 确保设备序列号已获取（run.py 可能没调用 connect_device）
    if not _device_serial:
        get_device_serial()
    result = run_adb("exec-out", "screencap", "-p", timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            "adb screencap 失败: "
            + result.stderr.decode("utf-8", errors="ignore")
        )
    return result.stdout


def save_screen(path):
    data = adb_screenshot_bytes()
    Path(path).write_bytes(data)
    return Image.open(BytesIO(data)).convert("RGB")


def image_signature(img):
    """用底部区域缩略图判断页面是否变化。"""
    w, h = img.size
    top = max(0, h - int(h * 0.22))
    crop = img.crop((0, top, w, h))
    crop.thumbnail((180, 300))
    return crop.tobytes()


def top_image_signature(img):
    """用顶部区域缩略图判断是否已经到聊天顶部。"""
    w, h = img.size
    bottom = min(h, int(h * 0.30))
    crop = img.crop((0, 0, w, bottom))
    crop.thumbnail((180, 300))
    return crop.tobytes()


def swipe_down(d, duration=0.35):
    """向下滑动，让聊天内容向下移动，从而回到更早/更顶部的消息。"""
    w, h = get_screen_size(d)
    x = w // 2
    y1 = int(h * 0.25)
    y2 = int(h * 0.80)
    d.swipe(x, y1, x, y2, duration=duration)


def get_screen_size(d):
    try:
        info = d.info
        return int(info["displayWidth"]), int(info["displayHeight"])
    except Exception:
        result = run_adb("shell", "wm", "size", timeout=15)
        text = result.stdout.decode("utf-8", errors="ignore")
        m = re.search(r"(\d+)x(\d+)", text)
        if not m:
            return 1080, 1920
        return int(m.group(1)), int(m.group(2))


def swipe_up(d, duration=0.35):
    w, h = get_screen_size(d)
    x = w // 2

    # 从屏幕约 78% 高度滑到 25% 高度
    y1 = int(h * 0.78)
    y2 = int(h * 0.25)

    d.swipe(x, y1, x, y2, duration=duration)


def at_bottom(d):
    """
    UIAutomator scrollable 节点判断。
    不是所有豆包版本都提供 scrollable，因此失败时返回 False。
    """
    try:
        xml = dump_xml(d)
        # Android UI hierarchy 的 scrollable 属性
        scrollables = re.findall(
            r'<node\b[^>]*scrollable="true"[^>]*>', xml
        )
        if not scrollables:
            return False

        # 有些版本会提供 scrollable + bounds，但没有当前位置属性，
        # 所以这里只作为辅助，不单独决定结束。
        return False
    except Exception:
        return False


def get_message_list_bounds(d):
    """
    获取豆包真正的消息滚动区域。

    当前 App 已确认存在：
        com.larus.nova:id/message_list_parent

    直接裁剪这个区域，而不是截整张手机屏幕。
    这样可以彻底排除每一屏都会重复出现的：
        - 顶部标题栏
        - "对话 / 打电话 / 拍题答疑 / 视频通话"
        - 底部输入框
        - 右下角"回到底部"悬浮箭头按钮

    如果当前版本没有返回 bounds，则使用安全的屏幕比例兜底。
    """
    # 悬浮箭头按钮大约占屏幕高度 6%，裁掉它
    _, h = get_screen_size(d)
    float_btn_height = int(h * 0.06)

    try:
        obj = d(resourceId="com.larus.nova:id/message_list_parent")
        if obj.exists:
            info = obj.info
            bounds = info.get("bounds") or info.get("visibleBounds")

            if bounds:
                left = int(bounds.get("left", 0))
                top = int(bounds.get("top", 0))
                right = int(bounds.get("right", 0))
                bottom = int(bounds.get("bottom", 0)) - float_btn_height

                if right > left and bottom > top:
                    return left, top, right, bottom
    except Exception as e:
        log(f"读取 message_list_parent bounds 失败: {e}")

    # 兜底：只保留中间聊天区域，同样裁掉底部悬浮按钮
    w, h = get_screen_size(d)
    return (
        0,
        int(h * 0.09),
        w,
        int(h * 0.84) - float_btn_height,
    )


def save_message_area(d, path):
    """
    截取“消息滚动区域”，而不是整张手机屏幕。
    """
    data = adb_screenshot_bytes()
    full = Image.open(BytesIO(data)).convert("RGB")

    left, top, right, bottom = get_message_list_bounds(d)

    left = max(0, min(left, full.width - 1))
    top = max(0, min(top, full.height - 1))
    right = max(left + 1, min(right, full.width))
    bottom = max(top + 1, min(bottom, full.height))

    crop = full.crop((left, top, right, bottom))
    crop.save(path, format="PNG")
    return crop


def message_area_signature(img):
    """
    消息区域签名。
    只用于判断"滚动后是否真的产生了新的聊天内容"。

    优化：用感知哈希 + 差异阈值，忽略微小动画/光标闪烁造成的误判。
    """
    import hashlib
    # 缩到很小，减少噪声影响
    crop = img.copy()
    crop.thumbnail((64, 64))
    # 转灰度再哈希，忽略颜色微小变化
    gray = crop.convert("L")
    # 用 MD5 哈希作为签名，比直接 tobytes 更稳定
    return hashlib.md5(gray.tobytes()).digest()




def full_screenshot_mobile(d, path):
    """
    严格的消息区长截图流程：

    1. 先滚到顶部；
    2. 必须确认已经到顶部；
    3. 顶部确认成功后，才开始第一张截图；
    4. 从上往下滚动消息 RecyclerView；
    5. 每次滚动后截图；
    6. 到底部后拼接；
    7. 最后恢复到底部。

    绝不允许“顶部没确认就开始截图”。
    """
    log("开始长截图：第一步，必须一直向上滚到彻底不动")
    cleanup_temp_screenshots()

    if not scroll_to_top(d):
        cleanup_temp_screenshots()
        raise RuntimeError("长截图停止：没有确认真正到达聊天顶部")

    # 再等待一次，确保最后一次无位移后的 RecyclerView 已经停止惯性。
    time.sleep(1.0)

    # 最终硬性确认：再向上滚一轮；如果仍无位移，才允许第一张截图。
    final_before = save_message_area(
        d, SCREENSHOT_DIR / "_top_final_before.png"
    )
    swipe_down(d, duration=0.35)
    time.sleep(0.8)
    final_after = save_message_area(
        d, SCREENSHOT_DIR / "_top_final_after.png"
    )

    if message_area_signature(final_before) != message_area_signature(final_after):
        cleanup_temp_screenshots()
        log("最终顶部复核发现仍可向上移动，重新寻找真正顶部")
        if not scroll_to_top(d):
            raise RuntimeError("最终顶部复核失败：无法确认顶部")
        time.sleep(1.0)

    log("========== 顶部已完全确认，现在才开始第一张截图 ==========")

    frames = []
    max_frames = 100
    
    for i in range(max_frames):
        # ===== 1. 先截当前屏，和上一帧对比 =====
        current = save_message_area(
            d,
            SCREENSHOT_DIR / f"_content_{i:03d}.png"
        )

        if not frames:
            frames.append(current.copy())
            log("已采集第 1 屏（顶部第一屏）")
        else:
            # 和上一帧不同才加入 frames
            if message_area_signature(current) != message_area_signature(frames[-1]):
                frames.append(current.copy())
                log(f"已采集第 {len(frames)} 屏（消息区域）")
            else:
                log("当前屏与上一屏相同，跳过")

        # ===== 2. 向下滚：短距 + 慢速 + 长等待 =====
        # 之前的问题：swipe_up 从 78%→25% 滑 53% 屏幕高度，
        #              一次飞半个屏幕，字还没停稳就截图了，内容截不全。
        #
        # 现在策略：
        #   - 只在消息区域内滑，不碰顶部标题栏和底部输入框
        #   - 只滑消息区域高度的 30%（≈ 1~2 条消息的高度）
        #   - duration=1.0 秒慢速滑，惯性极小
        #   - 滑完等 2.0 秒，确保 RecyclerView 完全停稳
        #   这样每一屏都有大量重叠，拼接算法才能准确找到重叠位置
        try:
            left, top, right, bottom = get_message_list_bounds(d)
            cx = (left + right) // 2
            area_h = bottom - top
            # 从区域 75% 高度滑到 25% 高度 = 向上移 50% 区域高度
            y1 = int(top + area_h * 0.75)
            y2 = int(top + area_h * 0.25)
            d.swipe(cx, y1, cx, y2, duration=1.0)
        except Exception:
            # 如果拿不到消息区域 bounds，兜底全屏短距慢滑
            w, h = get_screen_size(d)
            x = w // 2
            d.swipe(x, int(h * 0.70), x, int(h * 0.25), duration=1.0)

        # 必须等够久——让 RecyclerView 惯性 + 可能的回弹完全结束
        time.sleep(2.0)

        # ===== 3. 滚后探测：曾见过复制按钮 且 现在没了 → 停 =====
        # 滚到顶部后 AI 回答在屏幕外（没复制按钮）
        # 向下滚 → AI 回答进入屏幕（有复制按钮，seen_copy=True）
        # 继续滚 → AI 回答滚出屏幕（没复制按钮） → 这时候才停！
        # frames 就包含了从顶部到 AI 回答完整滚过的所有屏
        try:
            has_copy = d(resourceId=ANSWER_COPY_RESOURCE_ID).exists
            if has_copy:
                log("检测到复制按钮 → 停止滚动")
                break
            log("还没看到复制按钮，继续滚...")
        except Exception as e:
            log(f"复制按钮检测异常: {e}")

    if not frames:
        cleanup_temp_screenshots()
        raise RuntimeError("没有采集到消息区域截图")

    # 丢掉最后一屏——那是滚到底后多截的一帧空白/残缺画面
    if frames:
        frames.pop()

    log(f"消息区域共采集 {len(frames)} 屏，开始拼接")

    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("长截图需要 numpy，请执行：pip install numpy")

    def find_overlap(upper, lower):
        a = upper.convert("RGB")
        b = lower.convert("RGB")

        w = min(a.width, b.width)
        target_w = min(500, w)

        scale = target_w / w
        ah = max(1, int(a.height * scale))
        bh = max(1, int(b.height * scale))

        aa = a.resize((target_w, ah))
        bb = b.resize((target_w, bh))

        min_ov = max(20, int(min(ah, bh) * 0.05))
        max_ov = int(min(ah, bh) * 0.75)

        best_overlap = min_ov
        best_score = float("inf")

        for ov in range(min_ov, max_ov + 1, 3):
            if ov >= ah or ov >= bh:
                continue

            up = aa.crop((0, ah - ov, target_w, ah))
            lo = bb.crop((0, 0, target_w, ov))

            sample_h = min(160, ov)
            up = up.resize((160, sample_h))
            lo = lo.resize((160, sample_h))

            arr1 = np.asarray(up, dtype=np.int16)
            arr2 = np.asarray(lo, dtype=np.int16)

            score = float(np.mean(np.abs(arr1 - arr2)))

            if score < best_score:
                best_score = score
                best_overlap = ov

        return int(best_overlap / scale)

    canvas = frames[0].copy()

    for idx in range(1, len(frames)):
        upper = frames[idx - 1]
        lower = frames[idx]

        overlap = find_overlap(upper, lower)
        overlap = max(0, min(overlap, lower.height - 1))

        new_part = lower.crop(
            (0, overlap, lower.width, lower.height)
        )

        if new_part.height <= 0:
            continue

        merged = Image.new(
            "RGB",
            (canvas.width, canvas.height + new_part.height)
        )
        merged.paste(canvas, (0, 0))
        merged.paste(new_part, (0, canvas.height))
        canvas = merged

    canvas.save(path, format="PNG", optimize=False)
    cleanup_temp_screenshots()

    log(
        f"消息区域长截图完成: {path}, "
        f"尺寸={canvas.width}x{canvas.height}"
    )

    # 最后才恢复到底部。
    scroll_to_bottom(d)
    return True



import json as _json
import html as _html
import random as _random


def _text_to_html(text):
    """
    把手机 App 提取到的纯文本转换成和网页版 doubao.py 类似的 HTML 结构。

    网页版的 answer 是 markdown 渲染后的 HTML（通过 stripAttrs(body.outerHTML) 取得），
    包含 <p>, <ul>, <ol>, <h1>, <strong> 等标签。
    手机 App 没有 markdown DOM，但我们可以用启发式规则尽量还原：

      - 空行分段 -> <p>...</p>
      - 以 "- " / "* " 开头 -> <ul><li>...</li></ul>
      - 以 "1. " / "2. " 开头 -> <ol><li>...</li></ol>
      - 段落内换行 -> <br>
      - 行内 markdown 标记（**加粗**、`代码`）-> 转成 <strong> / <code>

    这样前端拿到的 HTML 结构和网页版一致，不会因为格式不同而显示异常。
    """
    if not text:
        return ""

    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()

    paragraphs = re.split(r"\n\s*\n", text)

    html_parts = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        lines = para.split("\n")

        is_ul = all(
            re.match(r"^[\-\*]\s+", line.strip()) or not line.strip()
            for line in lines
        )
        is_ol = all(
            re.match(r"^\d+[\.、]\s+", line.strip()) or not line.strip()
            for line in lines
        )

        if is_ul:
            li_items = [
                re.sub(r"^[\-\*]\s+", "", line.strip())
                for line in lines
                if line.strip()
            ]
            if li_items:
                html_parts.append("<ul>")
                for item in li_items:
                    html_parts.append(
                        f"<li>{_html_escape_inline(item)}</li>"
                    )
                html_parts.append("</ul>")
                continue

        if is_ol:
            li_items = [
                re.sub(r"^\d+[\.、]\s+", "", line.strip())
                for line in lines
                if line.strip()
            ]
            if li_items:
                html_parts.append("<ol>")
                for item in li_items:
                    html_parts.append(
                        f"<li>{_html_escape_inline(item)}</li>"
                    )
                html_parts.append("</ol>")
                continue

        html_parts.append(
            f"<p>{_html_escape_inline(para).replace(chr(10), '<br>')}</p>"
        )

    return "\n".join(html_parts)


def _html_escape_inline(text):
    """
    HTML 转义 + 行内 markdown 标记转换：
      **text** -> <strong>text</strong>
      `code` -> <code>code</code>
    """
    if not text:
        return ""

    escaped = _html.escape(text, quote=False)

    escaped = re.sub(
        r"\*\*(.+?)\*\*",
        r"<strong>\1</strong>",
        escaped,
        flags=re.DOTALL,
    )

    escaped = re.sub(
        r"`([^`]+?)`",
        r"<code>\1</code>",
        escaped,
    )

    return escaped


def extract_thinking_and_answer(xml):
    """
    Android 版内容提取：
      thinking = 联网搜索/参考资料区域（HTML 格式，和网页版 doubao.py 对齐）
      answer   = 最终回答（HTML 格式，和网页版 doubao.py 的 markdown 渲染结果对齐）

    网页版 doubao.py 的 answer 是通过 stripAttrs(body.outerHTML) 取得的 markdown
    渲染后 HTML，包含 <p>, <ul>, <ol>, <h1>, <strong> 等标签。手机 App 没有
    markdown DOM，所以用 _text_to_html 启发式还原结构，保证前端拿到的格式一致。

    sourceInfo 不在这里返回，和网页版一样单独序列化。
    """
    root = ET.fromstring(xml)

    # ---------- thinking ----------
    title_node = root.find(
        ".//*[@resource-id='com.larus.nova:id/tv_reference_title']"
    )

    title = clean_text(
        title_node.attrib.get("text", "")
    ) if title_node is not None else ""

    keyword_node = root.find(
        ".//*[@resource-id='com.larus.nova:id/sub_keyword_reference']"
    )

    keyword = clean_text(
        keyword_node.attrib.get("text", "")
    ) if keyword_node is not None else ""

    cards = []

    for item in root.findall(
        f".//*[@resource-id='{REFERENCE_ITEM_RESOURCE_ID}']"
    ):
        content_node = item.find(
            f".//*[@resource-id='{REFERENCE_CONTENT_RESOURCE_ID}']"
        )

        text = clean_text(
            content_node.attrib.get("text", "")
        ) if content_node is not None else ""

        if text:
            cards.append((text, ""))

    # ---------- answer ----------
    excluded_ids = {
        "com.larus.nova:id/tv_reference_title",
        "com.larus.nova:id/tv_reference_content",
        "com.larus.nova:id/tv_reference_index",
        "com.larus.nova:id/sub_keyword_reference",
    }

    excluded_exact = {
        "复制",
        "播放",
        "点赞",
        "点踩",
        "分享",
        "重新生成",
        "搜索",
        "来源",
        "对话",
        "打电话",
        "拍题答疑",
        "视频通话",
    }

    candidates = []

    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        cls = node.attrib.get("class", "")
        text = clean_text(node.attrib.get("text", ""))

        if cls != "android.widget.TextView":
            continue

        if not text or rid in excluded_ids:
            continue

        if text in excluded_exact:
            continue

        if len(text) < 10:
            continue

        if "深度思考" in text and len(text) < 100:
            continue

        candidates.append(text)

    answer_plain = max(candidates, key=len) if candidates else ""

    if not answer_plain:
        blocks = extract_message_blocks(xml)
        answer_plain = max(blocks, key=len) if blocks else ""

    answer_html = _text_to_html(answer_plain) if answer_plain else ""

    thinking_html_parts = []
    if title:
        thinking_html_parts.append(
            f"<div>{_html.escape(title)}</div>"
        )
    if keyword:
        thinking_html_parts.append(
            "<div><strong>搜索关键词：</strong><br>"
            f"{_html.escape(keyword).replace(chr(10), '<br>')}"
            "</div>"
        )
    for card_title, card_url in cards:
        card_title = clean_text(card_title)
        if not card_title:
            continue
        if card_url:
            thinking_html_parts.append(
                "<div>"
                f'<a href="{_html.escape(card_url, quote=True)}">'
                f"{_html.escape(card_title)}"
                "</a>"
                "</div>"
            )
        else:
            thinking_html_parts.append(
                f"<div>{_html.escape(card_title)}</div>"
            )

    thinking_html = "\n\n".join(thinking_html_parts)

    return thinking_html, answer_html, answer_plain


def collect_result(d, task_dir):
    """
    结果收集：
      - thinkingContent：HTML（资料标题 + 关键词 + 资料卡片纯文字）
      - answerText：HTML（最终回答）
      - sourceInfo：空字符串（URL 已禁用提取）
    """
    xml_path = task_dir / "window.xml"
    xml = dump_xml(d, xml_path)

    # 保存原始 XML。
    (task_dir / "window_raw.xml").write_text(
        xml or "",
        encoding="utf-8"
    )

    try:
        thinking_html, answer_html, answer_plain = extract_thinking_and_answer(xml)
    except Exception as e:
        log(f"回答/思考提取失败，启用消息块兜底: {e}")
        blocks = extract_message_blocks(xml)
        answer_plain = max(blocks, key=len) if blocks else ""
        answer_html = _text_to_html(answer_plain) if answer_plain else ""
        thinking_html = ""

    # 构建 thinking HTML：从 XML 里读资料标题、关键词、卡片标题（纯文字，无 URL）
    try:
        root = ET.fromstring(xml)

        title_node = root.find(
            ".//*[@resource-id='com.larus.nova:id/tv_reference_title']"
        )
        title = (
            clean_text(title_node.attrib.get("text", ""))
            if title_node is not None else ""
        )

        keyword_node = root.find(
            ".//*[@resource-id='com.larus.nova:id/sub_keyword_reference']"
        )
        keyword = (
            clean_text(keyword_node.attrib.get("text", ""))
            if keyword_node is not None else ""
        )

        cards = []
        for item in root.findall(
            f".//*[@resource-id='{REFERENCE_ITEM_RESOURCE_ID}']"
        ):
            content_node = item.find(
                f".//*[@resource-id='{REFERENCE_CONTENT_RESOURCE_ID}']"
            )
            text = (
                clean_text(content_node.attrib.get("text", ""))
                if content_node is not None else ""
            )
            if text:
                cards.append(text)

        thinking_parts = []
        if title:
            thinking_parts.append(f"<div>{_html.escape(title)}</div>")
        if keyword:
            thinking_parts.append(
                "<div><strong>搜索关键词：</strong><br>"
                f"{_html.escape(keyword).replace(chr(10), '<br>')}"
                "</div>"
            )
        for text in cards:
            thinking_parts.append(f"<div>{_html.escape(text)}</div>")

        thinking = "\n\n".join(thinking_parts) if thinking_parts else thinking_html

    except Exception:
        thinking = thinking_html

    answer = answer_html if answer_html else _text_to_html(answer_plain)
    source_info = ""

    (task_dir / "thinking.txt").write_text(
        thinking or "",
        encoding="utf-8"
    )

    (task_dir / "answer.txt").write_text(
        answer or "",
        encoding="utf-8"
    )

    (task_dir / "sources.txt").write_text(
        "",
        encoding="utf-8"
    )

    return thinking, answer, source_info


# 重新定义 handle_unit：
# 资料只处理一次；截图前再次滚到顶部，但不再做三次确认。


# =====================================================================
#  ask_one —— 高层入口（给 run.py 的 handle_unit 调用）
#  执行一个完整的提问→回答→截图→提取流程，返回结构化 dict
# =====================================================================

def ask_one(d, question: str, tag: str = "") -> dict:
    """
    执行完整的「新对话 → 输入问题 → 发送 → 等回答 → 截图 → 提取」流程。
    和原 doubaoapp.py handle_unit 的逻辑一致，只是不涉及任务池认领/回调。

    :param d: uiautomator2 设备对象
    :param question: 要问的问题
    :param tag: 任务标记（用于截图目录名）
    :return: {"answer": str, "thinking": str, "screenshot": Path|None}
    """
    tag_suffix = f"_{tag}" if tag else ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_dir = SCREENSHOT_DIR / f"task_{ts}{tag_suffix}"
    task_dir.mkdir(parents=True, exist_ok=True)

    log(f"===== ask_one 开始: question={question[:80]} =====")

    # 1. 新对话
    if not new_chat(d):
        raise RuntimeError("未能点击新对话按钮")
    time.sleep(2)

    # 2. 输入问题
    clear_and_input(d, question)
    time.sleep(0.5)

    # 3. 发送
    if not send_message(d):
        raise RuntimeError("发送按钮全部找不到")

    # 4. 等回答完成
    log("等待豆包回答生成...")
    force_bottom_before_answer_check(d)
    if not wait_answer_finish(d):
        raise RuntimeError("回答超时")
    time.sleep(0.8)
    log("回答完成 ✅")

    # 5. 向上滚到顶部（原脚本逻辑）
    log("开始长截图...")
    log("第一步：必须一直向上滚到彻底不动")
    if not scroll_to_top(d):
        log("⚠️ 滚顶失败，用当前位置直接截图")
    time.sleep(1.0)

    # 6. 截图 + 提取
    shot_path = task_dir / "answer.png"
    thinking = ""
    answer = ""

    try:
        full_screenshot_mobile(d, shot_path)
        screenshot_ok = True
    except Exception as e:
        log(f"长截图失败: {e}")
        screenshot_ok = False
        # 单张兜底
        try:
            single_path = SCREENSHOT_DIR / f"doubao_{ts}{tag_suffix}_single.png"
            save_message_area(d, single_path)
            shot_path = single_path
            screenshot_ok = True
        except Exception as e2:
            log(f"单张截图也失败: {e2}")
            shot_path = None

    # 7. 提取回答 / 思考
    try:
        thinking_html, answer_html, answer_plain = extract_thinking_and_answer(
            dump_xml(d, task_dir / "window.xml")
        )
        thinking = thinking_html or ""
        answer = answer_html if answer_html else answer_plain
        if not answer or len(answer.strip()) < 10:
            blocks = extract_message_blocks(dump_xml(d))
            if blocks:
                answer = max(blocks, key=len)
    except Exception as e:
        log(f"回答提取失败: {e}")

    log(f"[ask_one] answer 长度={len(answer)} thinking 长度={len(thinking)} screenshot={shot_path}")
    return {
        "answer": answer or "",
        "thinking": thinking or "",
        "screenshot": str(shot_path) if shot_path and screenshot_ok else None,
        "task_dir": str(task_dir),
    }


def current_is_doubao(d) -> bool:
    """检查当前前台是否是豆包。"""
    try:
        cur = d.app_current()
        return cur.get("package") == DOUBAO_PACKAGE
    except Exception:
        return False


# 供外部 import 的常量
__all__ = [
    "log", "ask_one", "current_is_doubao",
    "send_message", "new_chat", "clear_and_input", "ensure_input_mode",
    "wait_answer_finish", "scroll_to_top", "full_screenshot_mobile",
    "collect_result", "extract_thinking_and_answer",
    "DOUBAO_PACKAGE", "SCREENSHOT_DIR",
]
