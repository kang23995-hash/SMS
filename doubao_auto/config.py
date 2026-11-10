# -*- coding: utf-8 -*-
"""
全局配置。所有脚本共享这一个文件，改这里就行。

把「你的真实值」替换好以后，模拟器那边所有脚本直接 import config 就能用。
"""

from pathlib import Path
import os

# ======== 项目根目录 ========
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
DATA_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ======== 服务端（FastAPI 短信系统） ========
# ⚠️ 这是 **跑 FastAPI 的那台电脑** 的 IP。
# 模拟器在另一台电脑上，所以必须用 IP，不能写 127.0.0.1。
# 查本机 IP：在 FastAPI 那台电脑上打开 cmd 执行 ipconfig，找 IPv4 地址。
SERVER_BASE_URL = "http://172.28.108.248:8000"

# ======== ADB ========
# 雷电模拟器通常自带 adb.exe，路径一般在：
#   D:\leidian\LDPlayer9\adb.exe  （你电脑上可能是 D:\leidian\dnplayer.exe 同目录）
# 或者如果你已经把 adb 加到 PATH 了，直接留空字符串也行。
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"

# 雷电模拟器连接端口。雷电 9 默认 5555、5557、5559 起，多开则递增。
# 也可以用环境变量 EMULATOR_ADB_PORT 覆盖（优先级更高），方便多模拟器场景。
# 设为 0 或负数时回退到自动取第一台 adb 设备。
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5554"))

# ======== 豆包 App 包名 ========
DOUBAO_PACKAGE = "com.larus.nova"

# ======== 验证码轮询 ========
# 点「发送验证码」以后，最多等多少秒、每隔多少秒查一次
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "豆包")

# ======== 提问配置 ========
QUESTIONS_PER_PHONE = 10    # 每个手机号登录后问几个问题
ANSWER_TIMEOUT = 600        # 等豆包回答最长多少秒（秒）
POLL_INTERVAL = 1.0        # 检测回答状态的间隔

# ======== 问题列表 ========
# 把你想问的问题填在下面。脚本会按顺序挨个问。
# 如果不够 10 个，就循环用；超过 10 个，只取前 10 个。
QUESTIONS = [
    "你好，请做一下自我介绍",
    "帮我写一首关于秋天的短诗",
    "解释一下什么是大语言模型",
    "Python 和 JavaScript 哪个更适合初学者",
    "给我推荐 5 本值得一读的书",
    "如何在 Windows 上搭建 Python 开发环境",
    "解释一下 HTTP 状态码 404 和 403 的区别",
    "帮我写一个快速排序的 Python 实现",
    "深度学习和机器学习是什么关系",
    "给我一些提高编程能力的建议",
]
