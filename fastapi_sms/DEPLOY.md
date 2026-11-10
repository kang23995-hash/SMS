# 短信监测系统 · 完整部署文档

从零开始，把「手机收短信 → 局域网上传 → 电脑入库 → 网页查看」整条链路跑通。

---

## 〇、整体架构

```
┌──────────────┐   局域网 HTTP POST     ┌────────────────────────────┐
│  安卓手机     │  ───────────────────▶  │  电脑 (Windows)             │
│  SMSUploader │   /api/sms/receive     │  uvicorn + FastAPI :8000   │
│  (前台服务)   │   X-Token 鉴权         │        │                   │
└──────────────┘                        │        ▼                   │
        ▲                               │   aiomysql 连接池          │
        │                               │        │                   │
        │  浏览器访问 电脑IP:8000         │        ▼                   │
        └───────────────────────────────│   MySQL  sms_monitor       │
                                        └────────────────────────────┘
```

关键点：**电脑必须监听 `0.0.0.0`，并且防火墙放行 8000 端口**，
否则手机连不上——这是最常见的失败原因。

---

## 一、部署前准备

| 项目 | 要求 |
| --- | --- |
| 电脑 | Windows 10/11（Linux 同理，命令见每节括注） |
| Python | 3.10+ |
| MySQL | 5.7+ / 8.0+ |
| 网络 | 手机与电脑连**同一个 WiFi / 同一台路由器** |
| 手机 | Android 7.0 及以上 |

**先查电脑的局域网 IP**（后面手机里要填这个）：

```cmd
ipconfig
```

找到「无线局域网适配器 WLAN」下的 `IPv4 地址`，例如 `192.168.1.100`。
记下来，假设就是 `192.168.1.100`。

> 建议在路由器里给电脑绑定**静态 IP / DHCP 保留**，否则路由器重启后 IP 变了，
> 手机里的配置也要跟着改。

---

## 二、电脑端：部署服务

### 步骤 1：建数据库

执行 `sql/sms.sql`：

```cmd
mysql -u root -p < sql\sms.sql
```

验证：

```cmd
mysql -u root -p -e "USE sms_monitor; SHOW CREATE TABLE sms_records\G"
```

应能看到 `sms_records` 表以及 `uk_message_unique_id` 等索引。

> **改用 PostgreSQL**：执行 `psql -U postgres -f sql/sms_postgresql.sql`，
> 并在 `.env` 里设置 `DB_TYPE=postgresql`、`DB_PORT=5432`（详见 `README.md` 第四节）。
> 两种数据库业务代码一致，切换只改 `.env`。

### 步骤 2：配置 .env

```cmd
copy .env.example .env
```

编辑 `.env`，至少改这一项：

```ini
DB_PASSWORD=你的MySQL密码
```

鉴权开关保持默认即可（**已默认关闭**，开箱可用）：

```ini
API_TOKEN_ENABLED=false
```

> **重要**：配套的安卓 APP 目前**不会发送 `X-Token` 请求头**。
> 如果把 `API_TOKEN_ENABLED` 改成 `true`，APP 上报会被服务端一律拒绝，
> 现象是 APP 日志里反复「上传失败」、60 秒后弹「上传超时，已丢弃该消息」
> （服务端日志会显示 `POST /api/sms/receive -> 401`）。
> 想启用鉴权，必须先给 APP 加上请求头，两边用同一个 Token。
> 详见第七节「访问控制与安全提醒」。

如需生成随机 Token（开启鉴权时才用得上）：

```cmd
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

### 步骤 3：安装依赖

```cmd
cd D:\UserData\Desktop\SMS_Server\fastapi_sms
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

（Linux/macOS：`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`）

### 步骤 4：启动服务

```cmd
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log
```

启动日志里应看到：

```
  listen                 : 0.0.0.0:8000
  workers                : 4
  mysql                  : 127.0.0.1:3306/sms_monitor
  token_configured       : True
```

浏览器打开 `http://127.0.0.1:8000/`，能看到列表页（此时无数据）就算成功。

---

## 三、局域网配置与防火墙放行

### 1. 确认监听地址正确

`.env` 里必须是：

```ini
APP_HOST=0.0.0.0
```

写成 `127.0.0.1` 时只有电脑自己能访问，手机永远连不上。

### 2. 放行 8000 端口

**Windows —— 管理员身份打开 PowerShell 或 CMD，执行其中一条：**

```powershell
# PowerShell（推荐，带中文说明）
New-NetFirewallRule -DisplayName "SMS Monitor 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

```cmd
:: CMD / netsh
netsh advfirewall firewall add rule name="SMS Monitor 8000" dir=in action=allow protocol=TCP localport=8000
```

> `-Profile Private` 表示只对「专用网络」放行。
> 如果你的 WiFi 被 Windows 识别为「公用网络」，需要改成 `-Profile Any`，
> 或把网络位置改为「专用」。

**Linux：**

```bash
sudo ufw allow 8000/tcp          # Ubuntu / Debian
sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload   # CentOS / RHEL
```

### 3. 放行 MySQL（一般不需要）

手机不直连数据库，**不要**把 3306 暴露到局域网。MySQL 只需本机可访问即可。

### 4. 自检：从另一台设备访问

用手机浏览器打开：

```
http://192.168.1.100:8000/
```

能出列表页 → 局域网通了，可以进入下一步。
打不开 → 见 `TROUBLESHOOTING.md` 的「一、网络不通」。

---

## 四、注册为开机自启服务（可选但强烈建议）

手动开着的黑窗口一关服务就没了。生产使用建议注册成 Windows 服务。

### 方案 A：NSSM（最简单）

1. 下载 NSSM：https://nssm.cc/download ，解压后把 `nssm.exe` 放到 `C:\nssm\`。
2. 管理员 CMD：

```cmd
C:\nssm\nssm.exe install SmsMonitor "D:\UserData\Desktop\SMS_Server\fastapi_sms\.venv\Scripts\python.exe" "-m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log"

C:\nssm\nssm.exe set SmsMonitor AppDirectory "D:\UserData\Desktop\SMS_Server\fastapi_sms"

C:\nssm\nssm.exe set SmsMonitor AppStdout "D:\UserData\Desktop\SMS_Server\fastapi_sms\logs\service_out.log"
C:\nssm\nssm.exe set SmsMonitor AppStderr "D:\UserData\Desktop\SMS_Server\fastapi_sms\logs\service_err.log"

:: 崩溃自动重启
C:\nssm\nssm.exe set SmsMonitor AppExit Default Restart
C:\nssm\nssm.exe set SmsMonitor AppRestartDelay 5000

C:\nssm\nssm.exe start SmsMonitor
```

管理命令：`nssm restart SmsMonitor` / `nssm stop SmsMonitor` / `nssm remove SmsMonitor confirm`

### 方案 B：任务计划程序

1. 打开「任务计划程序」→「创建任务」
2. 常规：勾选「不管用户是否登录都要运行」+「使用最高权限运行」
3. 触发器：新建 →「启动时」
4. 操作：新建 → 程序填 `.venv\Scripts\python.exe`，
   参数填 `-m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log`，
   起始于填 `D:\UserData\Desktop\SMS_Server\fastapi_sms`

### 方案 C（Linux）：systemd

`/etc/systemd/system/sms-monitor.service`：

```ini
[Unit]
Description=SMS Monitor API
After=network.target mysql.service

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/opt/fastapi_sms
ExecStart=/opt/fastapi_sms/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sms-monitor
sudo systemctl status sms-monitor
```

---

## 五、手机端：安装 APP 与权限配置

APP 工程在 `../SMSUploader`，已编译好的安装包为 `SMSUploader-debug.apk`。

### 1. 安装

- 把 APK 传到手机（微信文件传输 / QQ / 数据线 / 网盘均可）
- 点击安装，若提示「禁止安装未知应用」，去
  `设置 → 安全 → 安装未知应用`，允许该来源
- 或用数据线：`adb install -r SMSUploader-debug.apk`

### 2. 权限配置（必做）

**运行时权限**：首次点「开启监测」时 APP 会自动弹出授权，
短信、电话、通知三项都要**允许**。若误点拒绝：
`设置 → 应用 → SMS上传 → 权限` 里手动打开。

| 权限 | 不给的后果 |
| --- | --- |
| 短信 | 完全无法工作 |
| 电话 | 拿不到卡槽号和本机号码（会默认卡槽 1） |
| 通知 | 前台服务通知不显示，后台存活率下降 |

**电池优化白名单（关键）**：APP 会弹窗引导，选择「前往设置」并允许后台运行。
若错过，手动去 `设置 → 电池 → 电池优化` 把本应用设为「不受限制 / 不优化」。

**国产系统额外设置**（小米/华为/OPPO/vivo 等，不做会被杀后台）：

| 品牌 | 路径 |
| --- | --- |
| 小米 MIUI | 手机管家 → 应用管理 → SMS上传 → 自启动 / 后台运行 / 省电策略「无限制」 |
| 华为 EMUI | 手机管家 → 应用启动管理 → 改为「手动管理」，三项全开 |
| OPPO/vivo | 设置 → 电池 → 应用耗电管理 → 允许后台运行 + 自启动 |
| 通用 | 在最近任务里对本应用「加锁」，防止一键清理时被杀 |

### 3. 填写配置

打开 APP：

1. **电脑 FastAPI 服务 IP:端口**：填 `192.168.1.100:8000`（换成你第二步查到的 IP）
2. 点 **检测手机号**，确认卡槽号码；读不出来就手动填
3. 点 **开启监测**，按钮变成「停止监测」即启动成功

---

## 六、系统联调测试步骤

按顺序做，每一步都能独立定位问题。

### 第 1 步：服务活着吗？

电脑浏览器打开 `http://127.0.0.1:8000/health`：

```json
{"code":200,"msg":"ok","database":"up","auth":"disabled","detail":"ok"}
```

- 打不开 → 服务没启动，看 `logs/error_*.log`
- `database: down` → MySQL 有问题，见 TROUBLESHOOTING「二、数据库连接失败」

### 第 2 步：接口能收单吗？（不依赖手机）

```cmd
cd D:\UserData\Desktop\SMS_Server\fastapi_sms
.venv\Scripts\python.exe scripts\load_test.py
```

（`load_test.py` 会自己读 `/health` 判断要不要带 Token，默认关闭时无需任何参数。）

看到 `✔ 压测通过` 说明服务端完全正常。同时刷新 `http://127.0.0.1:8000/`，
应能看到 200 条测试数据。

### 第 3 步：手机能连上吗？

手机浏览器打开 `http://192.168.1.100:8000/`：

- 能看到列表页 → 局域网 OK
- 打不开 → 见 TROUBLESHOOTING「一、网络不通」

### 第 4 步：端到端跑通

1. 手机上按第五节的说明配好 APP 并「开启监测」
2. 用**另一部手机**给这部手机发一条短信（内容写「联调测试」）
3. 观察手机 APP 日志区：

```
22:15:30  监测已开启，服务开始常驻监听
22:16:02  新短信：来自 138xxxxxxxx（卡槽1）
22:16:02  上传成功（HTTP 200）
```

并会弹出「上传成功」提示框。

4. 电脑浏览器刷新列表页，应能搜到「联调测试」这条短信，
   设备 ID、卡槽、SIM 卡号、发送号码都正确。

**至此整条链路打通。**

### 第 5 步：并发验证（可选）

把 10 台以上手机都配好并开启监测，同时向它们发短信，然后：

```cmd
.venv\Scripts\python.exe scripts\load_test.py --devices 10 --messages-per-device 20
```

再核对页面数据与 `/health` 是否仍然正常。

---

## 七、访问控制与安全提醒

1. **上报接口的鉴权目前是关闭的**（`API_TOKEN_ENABLED=false`）。

   这是为了配合现状：配套的安卓 APP 不会发送 `X-Token` 请求头，
   若强制校验，APP 上报会被一律拒绝。代价是——**同一局域网内任何设备
   都能向本服务 POST 伪造的短信数据**。

   想开启鉴权，需要两边同时改：

   - **服务端**：`.env` 里设 `API_TOKEN_ENABLED=true` 并配置一个随机 `API_TOKEN`，重启服务；
   - **安卓端**：在 `SMSUploader/app/src/main/java/com/lan/smsuploader/net/Uploader.kt`
     构造请求的地方加一行
     `.addHeader("X-Token", "同一个Token")`，然后重新打包安装 APK。

   开启后，错误的 Token 会返回 401，可以用 `scripts/test_api.sh` 验证。

2. **列表页 `/` 没有登录认证**——局域网内任何人只要知道
   `http://电脑IP:8000/` 就能看到所有短信内容。短信里常有验证码，
   属于敏感信息。若在意，两个办法：
   - 把 `APP_HOST` 改成 `127.0.0.1`，只在电脑本机浏览器查看（手机 APP 就传不进来了，
     所以需要另想办法，例如反向代理）；
   - 在服务前面架一层带 Basic Auth 的 Nginx，只暴露代理端口。

3. **不要把 8000 端口映射到公网**。这个服务是给局域网用的，
   暴露到公网等于把短信库公开。

4. 若开启了鉴权，建议定期更换 `API_TOKEN`（改完记得同步改手机 APP 里的配置）。

5. 服务端日志里**不会打印完整 Token**（只显示前 4 位），可以放心排查。

---

## 八、日常运维

| 操作 | 命令 |
| --- | --- |
| 查服务状态 | 浏览器开 `/health`，或 `nssm status SmsMonitor` |
| 重启服务 | `nssm restart SmsMonitor` |
| 看错误日志 | `type logs\error_*.log` |
| 查最新短信 | 浏览器开 `http://电脑IP:8000/` |
| 数据备份 | `mysqldump -u root -p sms_monitor > backup.sql` |
| 清理历史数据 | `DELETE FROM sms_records WHERE receive_time < '2026-01-01';` |
| 排查问题 | 见 `TROUBLESHOOTING.md` |
