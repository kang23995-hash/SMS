"""短信正文解析：从短信内容里提取「验证码」与「发送方名称」。

这里只放**纯函数**（不碰数据库、不读配置），方便单独测试：

* :func:`extract_vcode`        —— 取验证码（数字长度自适应，不写死 6 位）
* :func:`extract_sender_name`  —— 取短信签名（``【xxx】`` 里的名称）
* :func:`normalize_phone`      —— 手机号归一化（去掉 ``+86`` 等国家区号）

提取策略（为什么这样设计）
--------------------------
验证码长度并不固定（常见 4~6 位，个别 8 位），所以**不能写死位数**。
做法是先锚定「验证码 / 校验码 / 动态码 …」这类关键词，再在关键词后面
很近的距离内取第一段数字。这样既适应任意位数，又几乎不会误伤短信里
其它数字（金额、有效期 30 分钟、号码等）——因为它们离关键词往往
很远，或位数不满足 4~8 位的要求。

示例（全部来自真实短信）：

    【百度】验证码：467526（有效期30分钟）……          -> 467526
    【深度求索】验证码：265652，有效期 5 分钟……        -> 265652
    【豆包】验证码264098，用于手机验证码登录……         -> 264098
    【千问】……当前验证码为 959278 ，该验证码5分钟内有效 -> 959278
    【月之暗面】验证码：702237，该验证码将用于Kimi登录  -> 702237
"""

from __future__ import annotations

import re

__all__ = ["extract_vcode", "extract_sender_name", "normalize_phone"]


# ---------------------------------------------------------------------
#  验证码提取
# ---------------------------------------------------------------------

# 触发提取的关键词。顺序无关紧要，正则里用「择一」匹配。
# 中文关键词彼此差异很大，不会互相干扰；英文加 \b 避免匹配到单词内部
# （例如 "decode" 里不会命中 "code"）。
_VCODE_KEYWORDS = (
    r"验证码|校验码|动态码|确认码|安全码|口令码|一次性密码"
    r"|verification\s+code|auth(?:entication)?\s+code|\bcode\b|\bOTP\b"
)

# 关键词后面的第一段数字就是验证码：
#   * [^\d\n]{0,8}? —— 关键词与数字之间允许少量非数字字符（为、是、：、空格等），
#                     用懒惰匹配取“最近”的一段；上限 8 是为了防止跨太远误抓。
#   * (\d(?:[ \-]?\d){3,7}) —— 4~8 位数字，容忍数字之间夹一个空格或连字符
#                     （个别网关会把验证码排版成 "123 456"）。
# 捕获到的数字会把空格/连字符清掉再返回。
_VCODE_RE = re.compile(
    rf"(?:{_VCODE_KEYWORDS})[^\d\n]{{0,8}}?(\d(?:[ \-]?\d){{3,7}})",
    re.IGNORECASE,
)

# 上限保护：正文异常长时不必再扫，避免无谓开销
_VCODE_MAX_LEN = 4000


def extract_vcode(message_content: str) -> str:
    """从短信正文里提取验证码。

    :return: 提取到的验证码字符串（已去掉内部空格/连字符）；
             没命中关键词或找不到合适的数字时返回空串 ``""``。
    """
    if not message_content:
        return ""

    text = message_content.strip()
    if not text:
        return ""
    if len(text) > _VCODE_MAX_LEN:
        text = text[:_VCODE_MAX_LEN]

    match = _VCODE_RE.search(text)
    if not match:
        return ""

    # 去掉可能夹在数字之间的空格 / 连字符
    return re.sub(r"[ \-]", "", match.group(1))


# ---------------------------------------------------------------------
#  发送方名称（短信签名）提取
# ---------------------------------------------------------------------

# 短信签名一般是正文最前面的【xxx】。
#   * 内容 1~30 字、不含换行与右书名号
#   * 只取第一处，避免把「验证码提供他人可能导致百度账号被盗」里
#     没有书名号的部分误判——没有【】就返回空串。同时兼容全角 ［］。
_SENDER_NAME_RE = re.compile(r"【([^】\n]{1,30})】|［([^］\n]{1,30})］")


def extract_sender_name(message_content: str) -> str:
    """提取短信签名（``【xxx】`` 中的 ``xxx``）。

    :return: 名称字符串；没有书名号签名时返回空串 ``""``。
    """
    if not message_content:
        return ""
    match = _SENDER_NAME_RE.search(message_content)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


# ---------------------------------------------------------------------
#  手机号归一化
# ---------------------------------------------------------------------

# 号码里常见的分隔符：空格、半/全角连字符、括号
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-()（）]")

# 中国手机号：11 位、以 1 开头
_CN_MOBILE_RE = re.compile(r"^1\d{10}$")

# 需要剥离的国家/地区码前缀（按长度从长到短尝试，避免 0086 被先吃掉 86）
_COUNTRY_PREFIXES = ("+86", "0086", "86")


def normalize_phone(raw: str) -> str:
    """把手机号归一到「纯 11 位本地号码」。

    规则（只做保守的区号剥离，绝不猜测号码本身）：

    * 先去掉空格 / 连字符 / 括号等分隔符；
    * 再去掉 ``+86`` / ``0086`` / ``86`` 国家码前缀——**仅当**去掉后
      剩下的正好是 11 位、以 1 开头的中国手机号，才会真正剥离，
      以免把本身就是本地形式的号码误删开头数字；
    * 其它情况原样返回（例如读不到号码时的空串）。

    >>> normalize_phone("+8618798853401")
    '18798853401'
    >>> normalize_phone("0086 187-9885-3401")
    '18798853401'
    >>> normalize_phone("18798853401")
    '18798853401'
    >>> normalize_phone("")
    ''
    """
    if not raw:
        return ""

    phone = _PHONE_SEPARATORS_RE.sub("", raw.strip())
    if not phone:
        return ""

    for prefix in _COUNTRY_PREFIXES:
        if not phone.startswith(prefix):
            continue
        remainder = phone[len(prefix):]
        # 只有「去掉区号后恰好是中国手机号」才剥离，避免误删
        if _CN_MOBILE_RE.match(remainder):
            return remainder

    return phone
