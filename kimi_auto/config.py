# -*- coding: utf-8 -*-
"""
kimi_auto 全局配置。所有脚本共享这一个文件，改这里就行。

与 deepseek_auto /config.py 的差异：
  1. 包名换成 Kimi（com.moonshot.kimichat）
  2. 平台标识换成 kimi_app（必须与后端 task_result.ai_platform 存储值一致）
  3. Kimi 登录默认无图形验证码，但多次登出/登录触发风控时会弹出
     **滑块拼图人机验证**（"请完成安全验证"弹窗）。此时需要千问视觉大模型破解，
     所以本项目 **需要** QWEN_API_KEY，captcha_solver.py（滑块拼图版）已移植。
  4. 新增 Kimi 界面常量（登录 ①~⑩ 步骤文案、问答界面 content-desc 锚点），
     供 kimi_login.py / kimi_worker.py 共享，避免硬编码散落各处

🔐 关于私密配置：
  SMS_SERVER_BASE_URL / RPA_SERVICE_URL / BACKEND_BASE_URL 等可以放在项目根目录
  的 .env（已被 .gitignore 忽略），也可以用系统环境变量覆盖。本模块启动时自动加载 .env。
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
        # .env 读取失败不致命，继续走默认 / 系统环境变量
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
# 雷电模拟器自带 adb.exe，路径一般在 D:\leidian\LDPlayer9\adb.exe；
# 若已把 adb 加入 PATH，留空即可。
ADB_PATH = ""  # 例: r"D:\leidian\LDPlayer9\adb.exe"

# 雷电模拟器连接端口。雷电 9 默认 5555、5557、5559 起，多开则递增。
# 本项目实机调试端口为 5556（emulator-5556）。
EMULATOR_ADB_PORT = int(os.getenv("EMULATOR_ADB_PORT", "5558"))

# ======== Kimi App 包名 / 平台标识 ========
# Kimi（Moonshot）当前包名为 com.moonshot.kimichat（实测 3.1.4 / versionCode 379）。
# 版本更新可能改包名，保留候选列表，运行时自动探测真实包名。
KIMI_PACKAGE_CANDIDATES = (
    "com.moonshot.kimichat",
    "com.moonshot.kimi",
    "com.moonshot.kimi.app",
    "ai.moonshot.kimi",
)
KIMI_PACKAGE = KIMI_PACKAGE_CANDIDATES[0]
PLATFORM_NAME = "kimi_app"   # 必须与后端 task_result.ai_platform 存储值一致

# ======== 验证码轮询 ========
# 点「验证并登录」、短信真正发出以后，最多等多少秒、每隔多少秒查一次
VCODE_POLL_INTERVAL = 2.0   # 每 2 秒查一次
VCODE_POLL_TIMEOUT = 90.0   # 总共最多等 90 秒，超时判本轮失败

# 本平台的短信签名（短信【xxx】里的名字），轮询验证码时带上做精确匹配。
# 多平台自动化同时跑时各带各的签名，防止同时收到多条验证码时拿串平台。
# 各平台签名对照：深度求索→deepseek_auto / 豆包→doubao_auto /
#   百度→wenxin_auto / 腾讯科技→yuanbao_auto / 月之暗面→kimi_auto / 千问→qianwen_auto
VCODE_SENDER_NAME = os.getenv("VCODE_SENDER_NAME", "月之暗面")

# ======== 手机号批次 ========
# 业务规则（用户明确要求）：
#   「每提问 10 个问题，手机号的次数 +1」
# 也就是说 **每跑满一批（10 题）才把该手机号的使用次数 +1**，而不是每题都 +1。
# 计数逻辑在 run.py 的 PhoneSessionManager.mark_used() 中实现：
#   批次内每完成一题只累加批次计数，累计到 QUESTIONS_PER_PHONE 时才对
#   PhoneDB.increment(phone) 加一次，然后登出换号。
QUESTIONS_PER_PHONE = 10    # 每个手机号登录后问几个问题（批次大小，到量自动登出换号）

# ======== 千问视觉大模型（滑块拼图人机验证破解） ========
# Kimi 登录触发风控时会弹出「请完成安全验证」滑块拼图：
#   完整背景图上有缺口 + 拼图块贴在左下方滑块条左端 → 要算缺口 x 坐标 → 滑动
# 解法：把滑块条截图发给千问-VL，让它返回缺口 x 像素距离 → 模拟真人滑动
#
# ⚠️ 必须在 .env 里填 QWEN_API_KEY 才能用（deepseek_auto/yuanbao_auto 已有该配置）
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
QWEN_BASE_URL = os.getenv(
    "QWEN_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-vl-max-latest")   # 或 qwen2.5-vl-7b-instruct
QWEN_TIMEOUT = 60          # 单次请求超时秒数
QWEN_MAX_RETRIES = 2       # 请求失败重试次数

# 滑块拼图验证码求解参数
SLIDE_CAPTCHA_MAX_ATTEMPTS = 5   # 一次登录最多尝试几轮（答错会自动换题）
SLIDE_CAPTCHA_SWIPE_DURATION = 1.2   # 模拟真人滑动时长（秒）
SLIDE_CAPTCHA_AI_FIRST = True     # True=先用 AI 求解，失败换 OpenCV 兜底
CAPTCHA_DEBUG_KEEP = True         # 是否保留验证过程截图（screenshots/captcha_debug/）

# ======== 时间参数（秒） ========
# 以下均为问答环节的节奏控制，写在这里方便统一调整
ANSWER_HARD_TIMEOUT = 600        # 单题等待回答的最长时间
ANSWER_MIN_WAIT = 6.0            # 发送后强制静默等待（避免过早判定）
ANSWER_MIN_COMPLETE_LENGTH = 40  # 认为“已经有回答”的最小字符数
POLL_INTERVAL = 1.0              # 快轮询间隔
POLL_INTERVAL_SLOW = 2.0         # 异常时的慢轮询间隔

MAX_TOP_SWIPES = 25              # 滚到顶最多滑几次
MAX_BOTTOM_SWIPES = 25           # 滚到底最多滑几次
SCROLL_STABLE_ROUNDS = 3         # 连续几轮画面不变判定“到边界”
MAX_CAPTURE_FRAMES = 200         # 长截图最多采多少屏（安全上限）
CAPTURE_SWIPE_DURATION = 1.2     # 长截图时单次滑动时长
CAPTURE_SETTLE_WAIT = 2.0        # 长截图时滑动后稳定等待
CAPTURE_FRACTION = 0.42          # 长截图时单次滑动占视窗高度的比例

# ======== Kimi 界面常量（登录 ①~⑩ 步骤 + 问答界面锚点） ========
# 依据用户标注截图（①~⑩）与实机 XML 实测（1080x1920 竖屏）整理。

# ---- 登录前落地页（未登录） ----
KIMI_LOGIN_ENTRY_WORDS = ("去登录",)                     # ③ 主页的「去登录」按钮
KIMI_LANDING_WORDS = ("微信登录", "手机号登录")           # 登录落地页两个入口
KIMI_PHONE_LOGIN_WORD = "手机号登录"                      # ⑥ 点它进手机号登录
KIMI_AGREE_WORDS = ("同意并继续", "同意")                 # ⑦ 协议弹窗（取消 / 同意并继续）
KIMI_AGREE_DIALOG_TITLE = "用户协议及隐私协议"            # ⑦ 弹窗标题（用于确认弹窗已出现）
KIMI_PHONE_INPUT_HINT = "请输入手机号"                    # ⑧ 手机号输入框 hint
KIMI_VERIFY_BTN_WORD = "验证并登录"                       # ⑨ 点它发短信（无图形验证码）
KIMI_VCODE_TITLE = "输入 6 位验证码"                      # ⑩ 验证码页标题
KIMI_VCODE_SUBTITLE_PREFIX = "短信验证码已发至"           # ⑩ 验证码页副标题前缀
KIMI_VCODE_RESEND_PATTERN = r"\d+\s*s?\s*后重新发送"      # ⑩ 重新发送倒计时（判断验证码页已就绪）

# ---- 已登录主页 ----
KIMI_LOGGED_IN_HINTS = ("开启新会话", "导航按钮", "尽管问，带图也行")  # 已登录主界面特征
KIMI_HOME_INPUT_HINTS = ("尽管问，带图也行", "随时发消息")            # 输入框 hint（两种状态）
KIMI_NAV_DESC = "导航按钮"        # ① 左上角打开抽屉（content-desc，bounds 中心约 (114,138)）
KIMI_NEW_CHAT_DESC = "开启新会话"  # 新建对话按钮（content-desc，bounds 约 [768,66][876,210]）
KIMI_SEND_DESC = "发送讯息"        # 发送按钮（content-desc）
KIMI_MORE_INPUT_DESC = "更多输入方式"   # 输入框左侧「+」
KIMI_VOICE_INPUT_DESC = "切换至语音输入"  # 输入框右侧语音
KIMI_JUMP_BOTTOM_DESC = "JumpToBottom"   # 回到底部悬浮按钮（英文 desc）

# ---- 侧边抽屉 / 设置（登出 ①~⑤） ----
KIMI_DRAWER_AVATAR_DESC = "用户头像"    # ① 抽屉里的头像（点它进设置页）
KIMI_DRAWER_SEARCH_WORD = "搜索"        # 抽屉底部搜索框（判断抽屉已打开）
KIMI_SETTINGS_TITLE = "设置"            # 设置页标题
KIMI_LOGOUT_WORD = "退出登录"           # ④ 设置页底部按钮 / ⑤ 弹窗确认按钮
KIMI_LOGOUT_DIALOG_TEXT = "历史会话记录和账号信息不会被删除"  # ⑤ 弹窗正文（确认弹窗已出现）

# ---- 问答消息区（采集 / 判定用） ----
# 思考区（灰色，缩进）与回答正文（左对齐）在 XML 里表现为 **左边界不同**：
#   实测（1080x1920）：思考正文 x1≈132，回答正文 x1≈48，用户提问右对齐 x2≈1032。
# 因此用缩进阈值把两块内容天然分开，比字体颜色更稳（Kimi 两者都接近黑色）。
KIMI_THINK_BODY_X1_MIN = 126     # 左边界 >= 该值 → 视为缩进内容（思考正文 / 工具提示）
KIMI_ANSWER_X1_MAX = 120         # 左边界 <= 该值 → 视为左对齐的回答正文
KIMI_THINKING_TITLE_WORD = "思考已完成"   # 思考已完成标题（点击可展开 / 收起思考面板）
KIMI_THINKING_RUN_WORD = "思考中"         # 思考进行中标题
KIMI_THINKING_TITLE_PATTERN = r"思考中|思考已完成|思考了\s*\d|已深度思考|深度思考用时"
# 回答操作条 content-desc（出现即说明该条回答已生成完毕；
# 当前仅以「复制」作为完成判定锚点，见 kimi_worker.answer_copy_button_exists）
KIMI_ACTION_BAR_DESCS = ("read_aloud", "复制", "分享", "评价回答", "更多")
KIMI_TOOL_HINT_PATTERN = r"使用\s*\d+\s*个工具|正在使用|调用工具|已使用\s*\d+\s*个工具"
KIMI_FOOTER_HINT = "内容由 AI 生成"
KIMI_HEADER_WORDS = ("导航按钮", "开启新会话", "快速", "进阶", "更多")  # 顶部悬浮栏文本（截图需剔除）

# ======== 本地 --once / --debug 测试用问题 ========
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
