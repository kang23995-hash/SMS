# -*- coding: utf-8 -*-
"""
yuanbao_auto 全局配置。所有脚本共享这一个文件，改这里就行。

与 deepseek_auto/config.py 的差异：
  1. 包名换成腾讯元宝（YUANBAO_PACKAGE_CANDIDATES 候选列表，自动探测真实包名）
  2. 平台标识换成 tencent_app（必须与后端 task_result.ai_platform 一致）
  3. 人机验证类型不同：元宝是「选择最符合描述的图片」九宫格选择题，
     仍然用「千问视觉大模型」找正确图片 → 点选 → 点「确定」

🔐 关于 API Key：
  千问 API Key 不写在源码里，而是放在项目根目录的 .env 文件（已被 .gitignore 忽略），
  也可以用系统环境变量 QWEN_API_KEY 覆盖。本模块启动时会自动加载 .env。
"""

import os
from pathlib import Path

# ======== 项目根目录 ========
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
DATA_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """极简 .env 加载器：读取项目根目录 .env，注入到环境变量（不覆盖已存在的变量）。

    避免额外依赖 python-dotenv；.env 每行格式 KEY=VALUE，# 开头为注释。
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # .env 读取失败不致命，继续走默认/系统环境变量
        pass


_load_dotenv()

# ======== 服务端（FastAPI 短信系统） ========
# ⚠️ 这是 **跑 FastAPI 的那台电脑** 的 IP。
# 模拟器在另一台电脑上，所以必须用 IP，不能写 127.0.0.1。
# 查本机 IP：在 FastAPI 那台电脑上打开 cmd 执行 ipconfig，找 IPv4 地址。
SERVER_BASE_URL = os.getenv("SMS_SERVER_BASE_URL", "http://172.28.108.248:8000")

# ======== 后台任务系统（rpa-service / 网关） ========
# 与 deepseek_auto 保持一致：可用环境变量覆盖
RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://localhost:8084")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")

# ======== ADB ========
# 雷电模拟器通常自带 adb.exe，路径一般在：
#   D:\leidian\LDPlayer9\adb.exe
# 或者如果你已经把 adb 加到 PATH 了，直接留空字符串也行。
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"

# 雷电模拟器连接端口。雷电 9 默认 5555、5557、5559 起，多开则递增。
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5555"))

# ======== 腾讯元宝 App 包名 / 平台标识 ========
# 元宝版本更新可能改包名，保留多个候选，运行时自动探测真实包名
YUANBAO_PACKAGE_CANDIDATES = (
    "com.tencent.hunyuan.app.chat",
    "com.tencent.hunyuan.app",
    "com.tencent.hunyuan",
    "com.tencent.yuanbao",
    "com.tencent.mobileqq.yuanbao",
)
PLATFORM_NAME = "tencent_app"   # 必须与后端 task_result.ai_platform 存储值一致

# ======== 验证码轮询 ========
# 点「下一步」、短信真正发出以后，最多等多少秒、每隔多少秒查一次
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "腾讯科技")

# ======== 手机号批次 ========
QUESTIONS_PER_PHONE = 10    # 每个手机号登录后问几个问题（批次大小，到量自动登出换号）

# ======== 千问视觉大模型（人机验证破解） ========
# API Key 从 .env 或系统环境变量读取（见文件头部说明）
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
# OpenAI 兼容接口地址。
# ⚠️ 如果 key 不是阿里云官方 DashScope 签发（官方为 sk- 开头、无圆点），
#    而是第三方中转站签发，请把下面的地址改成中转站给你的 BaseURL。
QWEN_BASE_URL = os.getenv(
    "QWEN_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
# 视觉模型名。官方 DashScope 可用 qwen-vl-max-latest / qwen-vl-plus-latest；
# 中转站若名称不同（如 qwen-vl-max），按中转站文档改。
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-vl-max-latest")
# 单次请求超时（秒）与请求失败最大重试次数
QWEN_TIMEOUT = 60
QWEN_MAX_RETRIES = 2

# ======== 人机验证求解策略 ========
CAPTCHA_MAX_ATTEMPTS = 5        # 一次登录最多尝试几轮（点错会自动刷新新题，重新调大模型分析）
CAPTCHA_CLICK_WAIT = 2.5        # 点击目标+确定后等待结果的时间（秒）
CAPTCHA_DEBUG_KEEP = True       # 是否保留验证过程截图（screenshots/captcha_debug/）

# ======== 本地 --once 测试用问题 ========
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
