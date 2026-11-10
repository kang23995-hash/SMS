# -*- coding: utf-8 -*-
"""
deepseek_auto 全局配置。所有脚本共享这一个文件，改这里就行。

与 doubao_auto/config.py 的差异：
  1. 包名换成 DeepSeek（com.deepseek.chat）
  2. 平台标识换成 deepseek_app（必须与后端 task_result.ai_platform 一致）
  3. 新增「千问视觉大模型」配置 —— 用于破解点击「发送验证码」后弹出的人机验证

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
# 与 doubao_auto 保持一致：可用环境变量覆盖
RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://localhost:8084")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")

# ======== ADB ========
# 雷电模拟器通常自带 adb.exe，路径一般在：
#   D:\leidian\LDPlayer9\adb.exe
# 或者如果你已经把 adb 加到 PATH 了，直接留空字符串也行。
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"

# 雷电模拟器连接端口。雷电 9 默认 5555、5557、5559 起，多开则递增。
# 也可以用环境变量 EMULATOR_ADB_PORT 覆盖（优先级更高），方便多模拟器场景。
# 设为 0 或负数时回退到自动取第一台 adb 设备。
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5556"))

# ======== DeepSeek App 包名 / 平台标识 ========
DEEPSEEK_PACKAGE = "com.deepseek.chat"
PLATFORM_NAME = "deepseek_app"   # 必须与后端 task_result.ai_platform 存储值一致

# ======== 验证码轮询 ========
# 通过人机验证、短信真正发出以后，最多等多少秒、每隔多少秒查一次
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "深度求索")

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
QWEN_TIMEOUT = 120  # 千问 VL 大模型响应慢（60-90s），必须够长
QWEN_MAX_RETRIES = 2

# ======== 人机验证求解策略 ========
CAPTCHA_MAX_ATTEMPTS = 3        # 一次登录最多尝试点几次（点错会自动刷新新题）
CAPTCHA_CLICK_WAIT = 2.0        # 点击目标后等待结果的时间（秒）
CAPTCHA_DEBUG_KEEP = True       # 是否保留验证过程截图（screenshots/captcha_debug/）

# ======== OpenCV 视觉预处理（人机验证 v4.0） ========
# 是否启用 OpenCV 颜色分割提取候选物体（False 则回退到纯大模型方案）
CAPTCHA_USE_OPENCV = os.getenv("CAPTCHA_USE_OPENCV", "true").lower() == "true"
# OpenCV 颜色分割最小面积阈值（像素），小于此值的色块视为噪点忽略
CAPTCHA_MIN_AREA = int(os.getenv("CAPTCHA_MIN_AREA", "300"))
# 发给千问的图片最大边长（压缩以提速）
CAPTCHA_QWEN_MAX_SIDE = int(os.getenv("CAPTCHA_QWEN_MAX_SIDE", "512"))

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
