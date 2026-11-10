# SMS — 安卓短信自动化与采集工具集

本仓库收录「短信监测 / 短信自动发送」链路，以及若干大模型 App 的安卓端自动化采集脚本。所有子项目各自独立，可单独运行。

## 项目一览

| 目录 | 类型 | 说明 |
| --- | --- | --- |
| `fastapi_sms` | 服务端（Python） | 短信监测系统服务端：接收安卓 App 上报的短信、落库（MySQL / PostgreSQL）、提供网页查看与短信发送记录接口。FastAPI + uvicorn + SQLAlchemy/异步驱动 + Jinja2。 |
| `SMSUploader` | 安卓 App（Kotlin） | 监听本机短信，通过局域网 HTTP 以 JSON 上报到 `fastapi_sms`。基于 capcom6/android-sms-gateway 裁剪重构，界面与网络层重写。 |
| `send_sms` | 安卓 App（Kotlin） | 从服务端拉取待发送任务，在本机自动发送短信，并回传发送结果。 |
| `deepseek_auto` | 自动化脚本（Python） | DeepSeek App 端自动化：登录、验证码、问答采集。依赖 uiautomator2 + ADB。 |
| `doubao_auto` | 自动化脚本（Python） | 豆包 App 端自动化问答采集，含项目开发文档（架构、数据流、难点）。 |
| `kimi_auto` | 自动化脚本（Python） | Kimi App 端自动化，含滑块验证码识别。 |
| `qianwen_auto` | 自动化脚本（Python） | 通义千问 App 端自动化，含模板匹配与短信验证码校验。 |
| `wenxin_auto` | 自动化脚本（Python） | 文心一言 App 端自动化。 |
| `yuanbao_auto` | 自动化脚本（Python） | 腾讯元宝 App 端自动化，含图标定位与卡死页面探测。 |

## 整体链路

```
安卓机群 ──(短信上报 HTTP)──> fastapi_sms ──(任务下发)──> send_sms ──(发短信/回传)──> 安卓机群
    │                                                              │
    └── *_auto 自动化脚本（ADB + uiautomator2 驱动 App 登录/问答）──┘
```

## 快速开始

**服务端**

```bash
cd fastapi_sms
cp .env.example .env      # 按需填写数据库连接与令牌
uv sync                   # 或 pip install -r requirements.txt
python main.py
```

**安卓端**

两个 Android 工程（`SMSUploader`、`send_sms`）用 Android Studio 打开，或在装有 Gradle 的环境执行：

```bash
cd SMSUploader
./gradlew assembleDebug
```

**自动化脚本**（每个 `*_auto` 目录结构一致）

```bash
cd qianwen_auto
pip install -r requirements.txt
cp .env.example .env      # 填入 API Key、中转站地址、短信服务地址
python run.py
```

各项目的启动细节见目录内的 `README.md`、`启动教学.md`、`项目开发文档.md`。

## 关于配置与数据

出于安全考虑，仓库**不包含**以下内容（已由 `.gitignore` 排除）：

- 各项目的 `.env`（真实 API Key、数据库口令、局域网地址）；仓库中只保留 `.env.example` 模板。
- 运行时数据库文件（`data/*.db`，含手机号等数据）。
- Python 虚拟环境、`__pycache__`、日志、截图与 Android 构建产物（`build/`、`.gradle/`、`local.properties`）。

克隆后按 `.env.example` 自行生成本地配置即可运行；`*.apk` 为已构建的安装包，可直接安装。

## 许可

`SMSUploader` 含 Apache-2.0 许可与 NOTICE，沿用上游条款；其余项目仅作个人学习与自用。
