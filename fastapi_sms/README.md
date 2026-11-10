# 短信监测系统 · 服务端

接收安卓 APP 上报的短信，落库到 MySQL，并提供一个网页随时查看。
配套安卓端为同目录下的 `../SMSUploader` 工程。

技术栈：**FastAPI + uvicorn + aiomysql(PyMySQL 异步版) + python-dotenv + Pydantic + Jinja2**。

---

## 一、功能一览

| 能力 | 说明 |
| --- | --- |
| 短信接收接口 | `POST /api/sms/receive`，Pydantic 强校验 |
| 双数据库支持 | `DB_TYPE` 一键切换 **MySQL**（aiomysql）/ **PostgreSQL**（asyncpg），业务代码零改动 |
| 验证码 / 发送方解析 | 入库时自动从正文提取验证码（`sms_vcode`）与短信签名（`sender_name`），长度自适应 |
| 手机号归一化 | 自动去掉 `+86` 等国家码，表里只存 11 位本地号码 |
| 鉴权（可开关） | `X-Token` 校验，用 `API_TOKEN_ENABLED` 控制开关；默认关闭，见第五节说明 |
| 消息去重 | `message_unique_id` 唯一索引，重复上报不重复入库 |
| 事务保证 | 写操作包在事务里，异常自动回滚 |
| 连接池 | aiomysql 连接池复用连接，不每次新建 |
| 断连自愈 | MySQL 重启后连接池自动重建，服务不用重启 |
| IP 限流 | 滑动窗口，防止异常设备打垮数据库 |
| 非阻塞日志 | 日志走内存队列 + 后台线程落盘，不阻塞事件循环 |
| Web 列表页 | Bootstrap 页面，分页 + 按设备/手机号/发送号码筛选 |
| 批量删除 | 页面勾选单条/多条删除，或一键清空全部 |
| 健康检查 | `GET /health`，真实探测数据库连通性 |
| 多 worker | 支持 `--workers N`，适配 10 台以上手机并发上报 |

---

## 二、目录结构

```
fastapi_sms/
├── main.py                    FastAPI 入口（应用组装、生命周期、中间件）
├── config.py                  配置读取（.env -> Settings，不硬编码密钥）
├── logger.py                  非阻塞日志（QueueHandler + QueueListener）
├── database.py                连接池、事务、健康检查、断连重试（MySQL / PostgreSQL 双后端）
├── repository.py              短信表数据访问（全部参数化 SQL，含删除）
├── models.py                  Pydantic 请求模型 + 数据库行模型
├── sms_parse.py               短信正文解析（验证码 / 发送方名称 / 手机号归一化）
├── security.py                X-Token 鉴权（常量时间比较）
├── middleware.py              访问日志 + 按 IP 限流
├── errors.py                  统一异常处理 {"code","msg"} 信封
├── routes/
│   ├── sms.py                 POST /api/sms/receive、/delete、/clear
│   └── web.py                 GET / 列表页、GET /health
├── templates/index.html       列表页模板
├── static/bootstrap.min.css   本地 Bootstrap（不依赖 CDN）
├── sql/
│   ├── sms.sql                建库建表（MySQL）
│   └── sms_postgresql.sql     建库建表（PostgreSQL）
├── scripts/
│   ├── test_api.sh            curl 接口测试（Git Bash / Linux / macOS）
│   ├── test_api.ps1           接口测试（Windows PowerShell）
│   └── load_test.py           并发压测（仅标准库，无需额外装包）
├── logs/                      运行日志（自动创建，按天/大小轮转）
├── requirements.txt           依赖清单
└── .env / .env.example        环境配置
```

---

## 三、环境要求

- Python **3.10+**（推荐 3.12）
- 数据库二选一：
  - **MySQL 5.7+ / 8.0+**（默认，命令兼容 MariaDB）
  - **PostgreSQL 12+**（通过 `DB_TYPE=postgresql` 切换）
- 电脑与手机在**同一局域网**

---

## 四、安装与启动

### 1. 建数据库

**MySQL（默认）**：用 Navicat / MySQL Workbench / 命令行执行 `sql/sms.sql`：

```bash
mysql -u root -p < sql/sms.sql
```

**PostgreSQL**：执行 `sql/sms_postgresql.sql`（脚本幂等，可重复运行）：

```bash
psql -U postgres -f sql/sms_postgresql.sql
```

执行完可以自检一下：

```sql
-- MySQL
USE sms_monitor;
SHOW CREATE TABLE sms_records;   -- 确认表和索引都在
```

```sql
-- PostgreSQL
\c sms_monitor
\d sms_records
```

### 2. 配置 .env

```bash
copy .env.example .env        # Windows
cp .env.example .env          # Linux / macOS
```

必改一项：

```ini
DB_PASSWORD=你的MySQL密码
```

选择数据库后端（默认 MySQL，可无缝切到 PostgreSQL）：

```ini
# MySQL（默认）
DB_TYPE=mysql
DB_HOST=127.0.0.1
DB_PORT=3306          # 不写则按 DB_TYPE 自动取默认端口（MySQL 3306 / PG 5432）
DB_USER=root
DB_PASSWORD=你的MySQL密码
DB_NAME=sms_monitor

# PostgreSQL —— 只需改成下面这组（先装 asyncpg 并执行 sql/sms_postgresql.sql）
# DB_TYPE=postgresql
# DB_HOST=127.0.0.1
# DB_PORT=5432
# DB_USER=postgres
# DB_PASSWORD=你的PG密码
# DB_NAME=sms_monitor
```

> 切换后端只改 `.env` 即可，业务代码无需改动：`database.py` 会按 `DB_TYPE`
> 选择驱动（aiomysql / asyncpg），并把 SQL 里的 `%s` 占位符自动转成 PostgreSQL 的 `$1, $2 ...`。

关于鉴权（默认已关闭，开箱即用）：

```ini
API_TOKEN_ENABLED=false    # false = 上报接口不校验 Token
API_TOKEN=                 # 仅当上面为 true 时才生效
```

> **为什么默认关闭**：配套的安卓 APP（`../SMSUploader`）目前**不会发送
> `X-Token` 请求头**。如果服务端强制校验，APP 上报会被一律拒绝，
> 表现为 APP 日志里反复「上传失败」、60 秒后弹「上传超时，已丢弃该消息」。
> 所以用 APP 直接上报时必须保持 `false`。
>
> 想开启鉴权，需要先给 APP 加上请求头（改 `Uploader.kt` 后重新打包），
> 两边同时配置同一个 Token。详见 `DEPLOY.md` 的「访问控制与安全提醒」。

生成随机 Token：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

其它常用项：

```ini
APP_HOST=0.0.0.0      # 必须 0.0.0.0，否则手机连不上
APP_PORT=8000
APP_WORKERS=4         # CPU 核心数
DB_POOL_MAX=10        # 每 worker 的连接数上限
```

> ⚠️ `APP_WORKERS × DB_POOL_MAX` 不能超过 MySQL 的 `max_connections`（默认 151）。
> 例如 4 × 10 = 40，安全。服务启动时会自动检查并提醒。

### 3. 装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

> 如果用的是 uv：`uv pip install -r requirements.txt`

### 4. 启动

**开发调试（单进程，改代码自动重启）：**

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**生产运行（多 worker，推荐）：**

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log --timeout-keep-alive 30 --backlog 2048
```

也可以直接用内置入口（读取 .env 里的配置）：

```bash
python main.py
```

参数说明：

| 参数 | 作用 |
| --- | --- |
| `--host 0.0.0.0` | 监听所有网卡，手机才能访问（写 127.0.0.1 只有本机能连） |
| `--workers 4` | 4 个进程并行处理请求，约等于 CPU 核心数 |
| `--no-access-log` | 关掉 uvicorn 自带访问日志（本项目已有自己的，避免重复） |
| `--timeout-keep-alive 30` | 长连接保持 30 秒，减少握手开销 |
| `--backlog 2048` | 并发连接排队上限，防止瞬间洪峰被拒 |

启动成功后日志会打印配置概览；浏览器打开 `http://127.0.0.1:8000/` 即可看到列表页。

---

## 五、接口说明

### POST /api/sms/receive

**请求头**

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `Content-Type` | 是 | `application/json` |
| `X-Token` | **视开关而定** | `API_TOKEN_ENABLED=false`（默认）时可不带；为 `true` 时必须与 `.env` 的 `API_TOKEN` 完全一致 |

**请求体**

```json
{
  "deviceId": "a1b2c3d4e5f6a7b8",
  "simSlot": 1,
  "simPhoneNumber": "13800138000",
  "sender": "10690000",
  "messageContent": "您的验证码是 123456",
  "receiveTime": "2026-10-12T21:30:05+08:00",
  "messageUniqueId": "3f2a9c10-8b7e-4d21-9f0a-6c5d4e3b2a10"
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `deviceId` | string | 必填，1~128 字符 |
| `simSlot` | int | 必填，0~8（1/2 为卡槽，0 表示未知） |
| `simPhoneNumber` | string | 可选，≤32 字符，读不到时传空串；入库前会自动去掉 `+86` / `0086` 国家码 |
| `sender` | string | 必填，1~64 字符 |
| `messageContent` | string | 必填，≤10000 字符 |
| `receiveTime` | string | 必填，ISO-8601，**建议带时区** |
| `messageUniqueId` | string | 必填，≤64 字符，全局唯一，用于去重 |

> `receiveTime` 带时区时会自动换算到 `APP_TIMEZONE`（默认 `Asia/Shanghai`）再入库；
> 不带时区则按本地时间原样存储。

**响应**

| 场景 | HTTP | 响应体 |
| --- | --- | --- |
| 新消息入库成功 | 200 | `{"code":200,"msg":"success"}` |
| 重复消息（已存在） | 200 | `{"code":200,"msg":"duplicate ignored"}` |
| X-Token 缺失或错误 | 401 | `{"code":401,"msg":"无效的 X-Token"}`（**仅 `API_TOKEN_ENABLED=true` 时**） |
| 报文格式/字段错误 | 422 | `{"code":422,"msg":"请求数据校验失败：..."}` |
| 触发 IP 限流 | 429 | `{"code":429,"msg":"请求过于频繁，请在 N 秒后重试"}` |
| 数据库不可用 | 503 | `{"code":503,"msg":"数据库暂时不可用，请稍后重试"}` |
| 开启了鉴权但没配 Token | 503 | `{"code":503,"msg":"服务端未配置 API_TOKEN..."}` |
| 其它内部错误 | 500 | `{"code":500,"msg":"服务内部错误，请查看服务端日志"}` |

> **重复消息也返回 200**，这是有意为之：让 APP 认为上传成功并停止重试，
> 否则 APP 会白白重试 60 秒后才丢弃。

**入库时自动解析的两个字段**（无需 APP 上报，服务端从正文里提取）：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `sender_name` | 正文里的 `【xxx】` 签名 | 取第一处书名号内容，如 `【百度】…` → `百度`；没有则空串 |
| `sms_vcode` | 「验证码 / 校验码 / 动态码…」附近的数字 | **长度自适应**（4~8 位），不受固定位数限制；没有则空串 |

解析逻辑集中在 `sms_parse.py`，是纯函数、可单独测试。策略是**先锚定关键词再就近取数字**，
所以短信里的「有效期 30 分钟」、金额、号码等其它数字不会被误当验证码。

### POST /api/sms/delete

按主键批量删除（页面「删除选中」调用；单选即列表里只勾一条）。

```json
{ "ids": [12, 13, 14] }
```

返回 `{"code":200,"msg":"已删除 3 条","deleted":3}`。

### POST /api/sms/clear

清空整张表（页面「清空全部」调用，**不可逆**）。必须显式确认：

```json
{ "confirm": true }
```

不带 `confirm=true` 会返回 400 `{"code":400,"msg":"清空操作需要显式确认（confirm=true）"}`。

> ⚠️ **安全提示**：列表页和上面两个删除接口都**没有登录认证**——凡是能访问
> 到本服务的人（同一局域网内）都能查看甚至删除数据。若部署在不可信网络，
> 请把服务只绑到 `127.0.0.1`，或在前面加一层带认证的反向代理
> （详见 `DEPLOY.md`「访问控制」一节）。

### GET /health

真实探测数据库连通性，返回 `200`（正常）或 `503`（数据库不可用），
并附带当前鉴权开关状态：

```json
{"code":200,"msg":"ok","database":"up","auth":"disabled","detail":"ok"}
```

`auth` 为 `enabled` / `disabled`，测试脚本与排障时可据此判断要不要带 `X-Token`。

### GET /

短信列表页：分页、按接收时间倒序、支持设备 ID / SIM 卡号 / 发送号码筛选。
页面展示发送方名称与验证码两列，并支持勾选后「删除选中」或「清空全部」。

### GET /docs

FastAPI 自动生成的交互式接口文档，可直接在页面上试调接口。

---

## 六、测试与压测

### 接口测试

```bash
# Windows PowerShell
.\scripts\test_api.ps1
.\scripts\test_api.ps1 -BaseUrl http://192.168.1.100:8000 -Token 你的Token

# Git Bash / Linux / macOS
bash scripts/test_api.sh
bash scripts/test_api.sh http://192.168.1.100:8000 你的Token
```

覆盖：健康检查、正常上报、重复上报去重、缺/错 Token、字段缺失、非法 JSON、非法时间。

### 并发压测

```bash
# 默认 10 台设备 × 20 条 = 200 条，并发 50
python scripts/load_test.py --url http://127.0.0.1:8000 --token 你的Token

# 20 台设备 × 50 条 = 1000 条，并发 100
python scripts/load_test.py --token 你的Token --devices 20 --messages-per-device 50 --concurrency 100
```

脚本分两阶段：

1. **上报阶段** —— 统计成功率、QPS、延迟 P50/P90/P99；
2. **去重阶段** —— 并发重发已存在的消息 ID，验证不会重复入库。

实测参考（本机 MySQL，2 worker，连接池 10）：10 台设备 200 条并发，全部成功，
吞吐约 1400 条/秒，P99 延迟约 55ms。真实手机的上报频率远低于此，余量充足。

---

## 七、日志

| 文件 | 内容 |
| --- | --- |
| `logs/app_<pid>.log` | 全部日志（级别由 `LOG_LEVEL` 控制） |
| `logs/error_<pid>.log` | 仅 WARNING 及以上，排障优先看这个 |

- 日志通过**内存队列 + 后台线程**写入，不阻塞请求处理；
- 单文件超过 10MB 自动轮转，保留 5 份；
- **多 worker 时每个进程一个文件**（文件名带 PID）。这是刻意设计：
  多个进程共写同一文件会在轮转时冲突（Windows 上尤为明显）。
  想合并查看可以 `type logs\app_*.log`。

---

## 八、性能与容量

- **连接池**：每个 worker 维持 `DB_POOL_MIN ~ DB_POOL_MAX` 条连接并复用，
  避免每请求一次 TCP 握手 + MySQL 认证；
- **事务**：写操作显式 `BEGIN/COMMIT`，异常 `ROLLBACK`；
- **索引**：`uk_message_unique_id` 唯一索引负责去重，
  `idx_receive_time` / `idx_device_receive` 支撑分页与筛选；
- **限流**：默认每 IP 每 60 秒 300 次（`RATE_LIMIT_TIMES`/`RATE_LIMIT_WINDOW`）；
  注意多 worker 时每个进程各自计数，实际阈值 ≈ 单 worker 阈值 × worker 数；
- **异步**：数据库操作全部 `async`，不阻塞事件循环。

**容量规划建议**：10~50 台设备同时上报，`APP_WORKERS=4` + `DB_POOL_MAX=10` 完全够用。
若要再往上加，优先调大 `DB_POOL_MAX` 和 MySQL 的 `max_connections`，而不是无限加 worker。

**已知取舍**：列表页的筛选是「包含匹配」（`LIKE '%关键字%'`），
对排障最友好，但前置通配符会让索引失效。
数据量到百万级后若筛选变慢，可按 `repository.py` 中 `_build_where` 的注释
改成前缀匹配（`关键字%`），即可命中索引。分页本身走 `idx_receive_time`，不受影响。

---

## 九、更多文档

- 完整部署流程（局域网、防火墙、自启、APP 安装、联调）：见 `DEPLOY.md`
- 出问题先看：`TROUBLESHOOTING.md`
