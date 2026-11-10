# -*- coding: utf-8 -*-
"""千问登录「人机验证 → 发送短信码」自动处理。

现象
----
输入手机号、点「下一步」，或输入验证码后，千问偶尔会弹出：::

    身份验证
    发送短信码
    请使用手机号 173******62 编辑如下
    短信内容：XZ960572
    发送至 1069076020201123 完成验证
    [立即去发送短信]
    [已发送短信，下一步]

这一步要求用**当前登录的手机号**给阿里的服务号发一条带指定内容的短信。
手机号是自己号池里的，短信没法在本机自动完成，所以把任务交给局域网里
持有该手机号的 send_sms 手机设备去发（见 fastapi_sms /api/sms-send/*）。

本模块职责
----------
1. 识别这个页面（不能误判成普通的「输入验证码」页）
2. 从页面提取「短信内容」和「发送至」的号码（XML 文本优先，OCR 兜底）
3. 带上当前正在登录的手机号，提交给 fastapi_sms 等待设备回执
4. 设备发送成功后，点「已发送短信，下一步」
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

from config import (
    SERVER_BASE_URL,
    SMS_SEND_ENABLED,
    SMS_SEND_TIMEOUT,
    SMS_SEND_TOKEN,
)

logger = logging.getLogger(__name__)

# 页面出现这些字样才认为是「发送短信码」人机验证
PAGE_REQUIRED = "发送短信码"
PAGE_ALT_HINTS = ("身份验证", "短信内容", "发送至")
SEND_DONE_BUTTON = "已发送短信"

TASK_URL = f"{SERVER_BASE_URL.rstrip('/')}/api/sms-send/task"


# =====================================================================
#  页面文本采集
# =====================================================================

def _xml_texts(d) -> str:
    """把当前界面的所有 text / content-desc 拼成带换行的文本。"""
    try:
        from qianwen_login import dump_xml
        xml = dump_xml(d)
    except Exception:
        xml = ""

    if not xml:
        return ""

    parts: list[str] = []
    for attr in ("text", "content-desc"):
        for value in re.findall(rf'{attr}="([^"]*)"', xml):
            value = value.strip()
            if value:
                parts.append(value)
    # content-desc 可能和 text 重复，去掉重复项但保留顺序
    seen: set[str] = set()
    unique: list[str] = []
    for item in parts:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return "\n".join(unique)


def _ocr_texts(d) -> str:
    """截图 OCR 兜底（XML 里文字被拆散/被 WebView 吞掉时用）。"""
    try:
        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""

    try:
        img = d.screenshot()
        if not isinstance(img, Image.Image):
            img = Image.open(img)
        arr = np.asarray(img.convert("RGB"))

        global _OCR_ENGINE
        if _OCR_ENGINE is None:
            logger.info("[人机验证] 初始化 OCR 引擎…")
            _OCR_ENGINE = RapidOCR()
        result, _ = _OCR_ENGINE(arr)
        if not result:
            return ""

        items = []
        for box, text, score in result:
            if not text or float(score) < 0.4:
                continue
            ys = [pt[1] for pt in box]
            xs = [pt[0] for pt in box]
            items.append((int(min(ys)), int(min(xs)), str(text).strip()))
        items.sort(key=lambda x: (x[0], x[1]))
        return "\n".join(item[2] for item in items if item[2])
    except Exception as exc:
        logger.debug("[人机验证] OCR 失败: %s", exc)
        return ""


_OCR_ENGINE = None


def get_page_text(d, allow_ocr: bool = True) -> str:
    """页面文本。allow_ocr=False 时只读 XML（快，用于轮询等页面出现）。"""
    text = _xml_texts(d)
    if PAGE_REQUIRED in text or ("短信内容" in text and "发送至" in text):
        return text
    if not allow_ocr:
        return text
    ocr = _ocr_texts(d)
    if ocr:
        return text + "\n" + ocr
    return text


# =====================================================================
#  判定与解析
# =====================================================================

def is_identity_page_text(text: str) -> bool:
    if not text:
        return False
    if PAGE_REQUIRED in text:
        return True
    return all(hint in text for hint in PAGE_ALT_HINTS)


def parse_identity_text(text: str) -> dict:
    """从页面文本里提取短信内容与收件号码。

    页面文字可能是整段，也可能被拆成多个 TextView/OCR 行，所以所有
    正则都允许中间出现空白和换行。
    """
    if not text:
        return {}

    flat = text.replace("\u3000", " ")
    content = ""
    recipient = ""

    # 例：短信内容：XZ960572 / 短信内容：\nXZ960572
    match = re.search(r"短信内容\s*[:：]?\s*([A-Za-z0-9]{4,40})", flat)
    if match:
        content = match.group(1)

    # 例：发送至1069076020201123完成验证 / 发送至 1069076020201123
    match = re.search(r"发送至\s*[:：]?\s*(\+?[\d][\d\s\-]{4,30})", flat)
    if match:
        recipient = re.sub(r"\D", "", match.group(1))

    # OCR 可能把「短信内容」和值分到不相邻的行，再兜一次：找页面里
    # 最像验证码的短串（大写字母+数字，6~12 位），但要有「短信内容」前提
    if not content and "短信内容" in flat:
        match = re.search(r"\b([A-Z0-9]{6,12})\b", flat)
        if match:
            content = match.group(1)

    return {
        "content": content,
        "recipient": recipient,
    }


def extract_full_login_phone(text: str) -> str:
    """从页面文本里提取未被星号打码的完整手机号（取不到返回空串）。"""
    if not text:
        return ""
    # 例：验证码已发送到 +86 173 6705 0162
    match = re.search(r"已发送到\s*\+?86?[\s\-]*((?:\d[\s\-]?){11})", text)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 11:
            return digits
    return ""


def detect_identity_page(d, allow_ocr: bool = True) -> Optional[dict]:
    """当前界面是「发送短信码」页则返回解析结果，否则 None。"""
    text = get_page_text(d, allow_ocr=allow_ocr)
    if not is_identity_page_text(text):
        return None

    info = parse_identity_text(text)
    if (not info.get("content") or not info.get("recipient")) and allow_ocr:
        # XML 文本可能被拆开，OCR 再补一次
        ocr = _ocr_texts(d)
        if ocr:
            merged = text + "\n" + ocr
            retry = parse_identity_text(merged)
            info["content"] = info.get("content") or retry.get("content", "")
            info["recipient"] = info.get("recipient") or retry.get("recipient", "")
            text = merged
    info["page_text"] = text
    info["page_phone"] = extract_full_login_phone(text)
    return info


def wait_identity_page(d, wait_s: float = 4.0) -> bool:
    """在 wait_s 秒内等「发送短信码」页出现。

    先用 XML 轻量轮询（不起 OCR），最后再整体检测一次（含 OCR 兜底），
    避免普通登录也每次都跑 OCR 拖慢流程。
    """
    deadline = time.time() + max(0.0, wait_s)
    while time.time() < deadline:
        if detect_identity_page(d, allow_ocr=False) is not None:
            return True
        time.sleep(0.8)
    return detect_identity_page(d, allow_ocr=True) is not None


# =====================================================================
#  请求手机设备代发
# =====================================================================

def request_sms_send(
    *,
    send_phone: str,
    recipient: str,
    content: str,
    timeout: float = 0.0,
) -> tuple[bool, str]:
    """把代发任务提交给 fastapi_sms，阻塞等待手机设备回执。"""
    timeout = timeout or SMS_SEND_TIMEOUT
    headers = {}
    if SMS_SEND_TOKEN:
        headers["X-Token"] = SMS_SEND_TOKEN

    payload = {
        "send_phone": send_phone,
        "recipient": recipient,
        "content": content,
        "requester": "qianwen_auto",
        "wait": True,
        "timeout_s": timeout,
    }

    logger.warning(
        "[人机验证] 提交短信代发：发送号=%s 收件号=%s 内容=%s（等待设备回执，最多 %gs）",
        send_phone, recipient, content, timeout,
    )
    try:
        resp = requests.post(
            TASK_URL, json=payload, headers=headers,
            timeout=timeout + 25,
        )
    except requests.RequestException as exc:
        return False, f"请求 fastapi_sms 失败：{exc}"

    try:
        data = resp.json()
    except ValueError:
        data = {}

    msg = str(data.get("msg") or "").strip()
    if resp.status_code == 200 and int(data.get("code") or 0) == 200:
        logger.info(
            "[人机验证] 手机设备发送成功：device=%s task=%s msg=%s",
            data.get("device_id") or "-", data.get("task_id") or "-", msg,
        )
        return True, msg or "短信已发送"

    logger.error(
        "[人机验证] 代发失败 HTTP %s code=%s msg=%s",
        resp.status_code, data.get("code"), msg or resp.text[:200],
    )
    return False, msg or f"HTTP {resp.status_code}"


# =====================================================================
#  页面操作
# =====================================================================

def _click_sent_button(d) -> bool:
    """点「已发送短信，下一步」。"""
    try:
        if d(textContains=SEND_DONE_BUTTON).exists(timeout=1.5):
            d(textContains=SEND_DONE_BUTTON).click()
            logger.info("[人机验证] 已点击「已发送短信，下一步」")
            return True
    except Exception:
        pass

    # 坐标兜底：按钮在页面下部约 70% 高度处
    try:
        w, h = d.window_size()
        d.click(w // 2, int(h * 0.70))
        logger.info("[人机验证] 用坐标兜底点击「已发送短信，下一步」")
        return True
    except Exception as exc:
        logger.warning("[人机验证] 点击「已发送短信」失败：%s", exc)
        return False


def handle_identity_verification(
    d,
    phone: str,
    *,
    stage: str = "",
    max_rounds: int = 3,
) -> bool:
    """检测并在需要时完成「发送短信码」验证。

    :return: True = 页面出现过且已处理完；False = 当前页面不是这个验证页。
    :raises RuntimeError: 页面出现了但解析/代发/点击任一环节失败。
    """
    if not SMS_SEND_ENABLED:
        return False

    info = detect_identity_page(d)
    if info is None:
        return False

    logger.warning(
        "[人机验证] 检测到「发送短信码」页面（%s） 内容=%s 收件号=%s 页面手机号=%s",
        stage or "-", info.get("content") or "?", info.get("recipient") or "?",
        info.get("page_phone") or "-",
    )

    if not info.get("content") or not info.get("recipient"):
        raise RuntimeError(
            "人机验证页解析失败："
            f"短信内容={info.get('content')!r} 收件号={info.get('recipient')!r}"
        )

    # 页面里若能读到完整手机号，以它为准（比传参更贴近当前实际登录号）
    page_phone = info.get("page_phone") or ""
    send_phone = page_phone or phone
    if page_phone and phone and not page_phone.endswith(phone[-4:]):
        logger.warning(
            "[人机验证] 页面手机号 %s 与登录手机号 %s 不一致，以页面为准",
            page_phone, phone,
        )

    clicked_for_content = ""

    for round_no in range(1, max_rounds + 1):
        ok, msg = request_sms_send(
            send_phone=send_phone,
            recipient=info["recipient"],
            content=info["content"],
        )
        if not ok:
            raise RuntimeError(f"短信代发失败：{msg}")

        if not _click_sent_button(d):
            raise RuntimeError("点击「已发送短信，下一步」失败")

        clicked_for_content = info["content"]

        # 等页面变化：正常会跳到验证码页/登录页，也可能换一条新内容再验证一次
        changed = False
        for _ in range(10):
            time.sleep(1.0)
            again = detect_identity_page(d)
            if again is None:
                changed = True
                logger.info("[人机验证] 验证页已消失，继续登录流程")
                break
            if again.get("content") and again["content"] != clicked_for_content:
                logger.warning(
                    "[人机验证] 出现新的验证内容 %s（第 %d 轮）",
                    again["content"], round_no,
                )
                info = again
                break

        if changed:
            return True

        if round_no == max_rounds:
            break

        # 内容没变且页面没走：再点一次，避免第一次点击没被响应
        _click_sent_button(d)
        time.sleep(1.5)

    raise RuntimeError(
        f"人机验证未通过：已提交 {clicked_for_content or '?'} 但页面仍未离开"
    )
