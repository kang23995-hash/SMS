# -*- coding: utf-8 -*-
"""
qianwen_auto 全局配置。所有脚本共享这一个文件，改这里就行。

相对 deepseek_auto/config.py 的差异：
  1. 包名换成千问（com.aliyun.tongyi 候选）
  2. 平台标识换成 qianwen_app（必须与后端 task_result.ai_platform 一致）
  3. 登录偶尔会出现「身份验证 / 发送短信码」人机验证（见 sms_verify.py），
     由局域网 send_sms 手机代发，其余情况仍是「下一步直接发短信」
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
    """极简 .env 加载器：读取项目根目录 .env，注入到环境变量（不覆盖已存在的变量）。"""
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
        pass


_load_dotenv()

# ======== 服务端（FastAPI 短信系统） ========
# ⚠️ 这是 **跑 FastAPI 的那台电脑** 的 IP。
# 模拟器在另一台电脑上，所以必须用 IP，不能写 127.0.0.1。
SERVER_BASE_URL = os.getenv("SMS_SERVER_BASE_URL", "http://172.28.108.248:8000")

# ======== 后台任务系统（rpa-service / 网关） ========
RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://localhost:8084")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8080")

# ======== ADB ========
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5562"))

# ======== 千问 App 包名（已验证）========
# 真机/雷电模拟器验证过：只有 com.aliyun.tongyi 有效
QIANWEN_PACKAGE = "com.aliyun.tongyi"
# Launcher Activity（必须完整路径，不能只写 .MainActivity）
# 用 `adb shell cmd package resolve-activity --brief com.aliyun.tongyi` 确认
QIANWEN_LAUNCHER_ACTIVITY = "com.ucpro.MainActivity"
QIANWEN_COMPONENT = f"{QIANWEN_PACKAGE}/{QIANWEN_LAUNCHER_ACTIVITY}"
# 兼容旧代码引用
QIANWEN_PACKAGE_CANDIDATES = (QIANWEN_PACKAGE,)

# 平台标识（必须与后端 task_result.ai_platform 存储值一致）
PLATFORM_NAME = "qianwen_app"

# ======== 验证码轮询 ========
# 常规流程点"下一步"直接发短信；偶发「发送短信码」人机验证见下方配置
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "千问")

# ======== 人机验证「发送短信码」代发 ========
# 登录时偶尔会弹出「身份验证 / 发送短信码」页面，要求用登录手机号给
# 阿里云服务号发一条指定内容的短信。开启后由局域网里的 send_sms 手机
# 自动代发，成功后 qianwen_auto 再点「已发送短信，下一步」。
SMS_SEND_ENABLED = os.getenv("SMS_SEND_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "y", "on",
)
# 等待手机设备发送成功的最长秒数（超时则本轮登录失败，由主循环重试）
SMS_SEND_TIMEOUT = float(os.getenv("SMS_SEND_TIMEOUT", "180"))
# fastapi_sms 代发接口的可选 X-Token（与服务端 SMS_SEND_TOKEN 一致；留空即可）
SMS_SEND_TOKEN = os.getenv("SMS_SEND_TOKEN", "").strip()

# ======== 手机号批次 ========
QUESTIONS_PER_PHONE = 10    # 每个手机号登录后问几个问题（批次大小，到量自动登出换号）

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
