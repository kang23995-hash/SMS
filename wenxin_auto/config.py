# -*- coding: utf-8 -*-
"""
wenxin_auto 全局配置。所有脚本共享这一个文件，改这里就行。

与 deepseek_auto/config.py 的差异：
  1. 包名换成文心（com.baidu.newapp，百度「文心」App，原文心一言）
  2. 平台标识换成 wenxin_app（必须与后端 task_result.ai_platform 一致）
  3. 移除「千问视觉大模型 / 人机验证」全部配置 —— 文心登录没有滑块/点选等
     人机验证，短信验证码直接下发，无需 AI 破解环节
  4. 新增文心特有的「对话模式」「网页来源入口」等 UI 文案配置

🔐 关于私密配置：
  本项目不需要任何大模型 API Key。服务端地址等可用系统环境变量覆盖，
  也可以写在项目根目录的 .env 文件（已被 .gitignore 忽略），
  本模块启动时会自动加载 .env。
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
RPA_SERVICE_URL = os.getenv("RPA_SERVICE_URL", "http://172.28.108.138:8084")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://172.28.108.138:8080")

# ======== ADB ========
# 雷电模拟器通常自带 adb.exe，路径一般在：
#   D:\leidian\LDPlayer9\adb.exe
# 或者如果你已经把 adb 加到 PATH 了，直接留空字符串也行。
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"

# 雷电模拟器连接端口。雷电 9 默认 5555、5557、5559 起，多开则递增。
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5560"))

# ======== 文心 App 包名 / 平台标识 ========
# 文心（原文心一言）百度官方 App 包名，已通过 adb pm 实机确认
WENXIN_PACKAGE = "com.baidu.newapp"
PLATFORM_NAME = "wenxin_app"   # 必须与后端 task_result.ai_platform 存储值一致

# ======== 验证码轮询 ========
# 短信发出以后，最多等多少秒、每隔多少秒查一次
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "百度")

# ======== 手机号批次 ========
QUESTIONS_PER_PHONE = 10    # 每个手机号登录后问几个问题（批次大小，到量自动登出换号）

# ======== 文心「切换模式」配置 ========
# 登录后新建对话，顶部有「对话 ▼」下拉与「工作」页签：
#   - 本 Worker 只在「对话」模式下采集（不是「工作」），switch_mode() 会确保这一点；
#   - 若希望通过「新对话 ▼ / 对话 ▼」下拉切换具体模型（如 深度思考/标准），
#     把目标菜单项文案写进 WENXIN_CHAT_MODE；留空字符串 = 不主动切换模型，
#     保持 App 当前默认模型（最稳，推荐先用空值跑通全流程）。
WENXIN_CHAT_MODE = os.getenv("WENXIN_CHAT_MODE", "")

# ======== 文心字体配色（区分思考过程 / 回答正文） ========
# 与 DeepSeek 同源的判定逻辑：思考过程为灰色、回答正文为近黑色。
# 文心实际配色如与默认值有偏差，实机跑一轮后按 debug 日志里的采样色微调即可。
WENXIN_THINKING_RGB = (0x7D, 0x7F, 0x85)   # 灰色 —— 思考过程
WENXIN_ANSWER_RGB = (0x19, 0x1B, 0x23)     # 黑色 —— AI 回答正文

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
