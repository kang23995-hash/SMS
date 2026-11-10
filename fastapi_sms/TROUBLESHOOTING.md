# 故障排查手册

按「现象 → 原因 → 解决」组织。遇到问题先按下面的**一分钟自检**走一遍。

---

## 一分钟自检

```cmd
:: 1. 服务活着吗？（在跑服务的电脑上）
curl http://127.0.0.1:8000/health

:: 2. 端口在监听吗？应看到 0.0.0.0:8000
netstat -ano | findstr :8000

:: 3. 防火墙规则在吗？
netsh advfirewall firewall show rule name="SMS Monitor 8000"

:: 4. 电脑的局域网 IP 是多少？
ipconfig
```

四条都正常，问题多半在手机侧（权限 / 电池优化 / APP 里的 IP 填错）。

---

## 一、网络不通：手机连不上电脑

**现象**：手机浏览器打不开 `http://电脑IP:8000/`；APP 日志一直「上传失败」。

### 排查顺序

**① 确认服务监听的是 `0.0.0.0` 而不是 `127.0.0.1`**

```cmd
netstat -ano | findstr :8000
```

- 看到 `0.0.0.0:8000` → 正确
- 看到 `127.0.0.1:8000` → 只有本机能连。改 `.env` 里 `APP_HOST=0.0.0.0` 后重启服务

**② 确认防火墙放行了 8000**

```cmd
netsh advfirewall firewall show rule name="SMS Monitor 8000"
```

没有规则就执行（管理员 CMD）：

```cmd
netsh advfirewall firewall add rule name="SMS Monitor 8000" dir=in action=allow protocol=TCP localport=8000
```

**③ 确认手机和电脑在同一网段**

- 手机 `设置 → WLAN → 当前网络详情`，看 IP 是不是 `192.168.1.x`
- 电脑 `ipconfig` 看到的也是 `192.168.1.x` → 同网段
- 如果手机是 `192.168.43.x`（开了热点）或 `10.x`，说明不在同一网络

**④ 确认 APP 里填的 IP 正确**

别填 `127.0.0.1`（那是手机自己），必须是电脑的局域网 IP，格式 `192.168.1.100:8000`，
不要带 `http://`（带了也能自动纠正，但最好别带）。

**⑤ 临时验证是不是防火墙的锅**

```cmd
netsh advfirewall set allprofiles state off
```

手机再试一次。**验证完记得立刻打开**：

```cmd
netsh advfirewall set allprofiles state on
```

**⑥ 电脑侧反向验证**

在**另一台**电脑上执行 `curl http://192.168.1.100:8000/health`：
- 通 → 问题在手机侧
- 不通 → 问题在电脑侧（服务或防火墙）

### 其它可能

- **公司 / 学校 WiFi 开了「AP 隔离」**：同一 WiFi 下设备之间不能互访。
  解决办法：用手机热点，让电脑连手机热点，或换一个普通路由器。
- **Windows 把当前 WiFi 识别成「公用网络」**：防火墙规则的 `-Profile Private` 不生效。
  改规则为 `-Profile Any`，或把网络位置改成「专用」。

---

## 二、数据库连接失败

**现象**：`/health` 返回 `database: down`；日志出现
`创建 MySQL 连接池失败`；接口返回 503。

### 常见原因与处理

| 日志关键字 | 原因 | 解决 |
| --- | --- | --- |
| `(2003, "Can't connect to MySQL server")` | MySQL 没启动 / 端口不对 | 启动 MySQL 服务；确认 `.env` 的 `DB_PORT`（默认 3306） |
| `(1045, "Access denied for user")` | 账号或密码错 | 核对 `.env` 的 `DB_USER` / `DB_PASSWORD` |
| `(1049, "Unknown database 'sms_monitor'")` | 库没建 | 执行 `sql/sms.sql` |
| `cryptography package is required...` | 缺加密包（MySQL 8 默认认证插件需要） | `pip install cryptography`（requirements.txt 里已含） |
| `Too many connections` | 连接数超了 | 调小 `APP_WORKERS` 或 `DB_POOL_MAX`；或调大 MySQL 的 `max_connections` |
| `Table 'sms_monitor.sms_records' doesn't exist` | 表没建 | 执行 `sql/sms.sql` |

### 逐项验证

```cmd
:: MySQL 活着吗
mysql -u root -p -e "SELECT VERSION();"

:: 库和表在吗
mysql -u root -p -e "USE sms_monitor; SHOW TABLES; SELECT COUNT(*) FROM sms_records;"

:: 连接数上限
mysql -u root -p -e "SHOW VARIABLES LIKE 'max_connections'; SHOW STATUS LIKE 'Threads_connected';"
```

### 关于「数据库重启后要不要重启服务」

**不需要**。本项目对 `MySQL server has gone away` / `Lost connection`
这类错误会自动重建连接池并重试，MySQL 恢复后接口会自动恢复正常。
`/health` 会实时反映状态。

---

## 三、APP 上传失败 / 上传超时

**现象**：手机 APP 日志出现「第 N 次上传失败」，60 秒后弹「上传超时，已丢弃该消息」。

### ⚠️ 最常见原因：X-Token 校验被打开了

服务端日志如果长这样（`POST /api/sms/receive -> 401`，来源是手机 IP）：

```
WARNING | security | 鉴权失败：提供的 Token=<缺失>，来源 IP=192.168.1.103
WARNING | errors   | 业务异常 code=401 msg=无效的 X-Token path=/api/sms/receive
INFO    | app.access | POST /api/sms/receive -> 401  client=192.168.1.103
```

说明**服务端要求 Token，但安卓 APP 根本不会发送 `X-Token` 请求头**
（`Uploader.kt` 里从未设置过任何自定义请求头）。
这种组合下每一条短信都会被拒绝，APP 重试 60 秒后丢弃。

**解决**：把服务端 `.env` 改成

```ini
API_TOKEN_ENABLED=false
```

然后重启服务。重启时的日志里应出现：

```
WARNING | __main__ | 鉴权已关闭（API_TOKEN_ENABLED=false）：上报接口 /api/sms/receive 不校验 X-Token
```

也可以访问 `/health` 确认，返回里 `"auth":"disabled"` 即为关闭状态。

> 反向操作：如果你确实想启用鉴权，就必须先给 APP 加上请求头
> （改 `Uploader.kt` 后重新打包 APK），两边同时配置同一个 Token。
> 只改一边一定是坏的。

### 先看返回码

| APP 看到的 | 服务端返回 | 原因与处理 |
| --- | --- | --- |
| 上传失败（HTTP 401） | `{"code":401,...}` | **服务端开了鉴权，但 APP 不会发送 X-Token**。把 `.env` 的 `API_TOKEN_ENABLED` 改成 `false` 重启（详见本节开头的说明） |
| 上传失败（HTTP 422） | `{"code":422,...}` | 报文格式问题。看服务端日志里「报文校验失败」的具体字段；通常是 APP 版本与服务端字段约定不一致 |
| 上传失败（HTTP 429） | `{"code":429,...}` | 触发限流。调大 `.env` 里的 `RATE_LIMIT_TIMES`，或设 `RATE_LIMIT_ENABLED=false` |
| 上传失败（HTTP 503） | `{"code":503,...}` | 数据库不可用 → 见「二、数据库连接失败」；或服务端没配 `API_TOKEN` |
| 连接被拒绝 / 无响应 | 无返回 | 网络不通 → 见「一、网络不通」 |
| 上传超时（60 秒） | — | 以上都不是，看服务端日志与网络质量 |

### 在服务端确认请求到没到

在电脑上实时看日志：

```cmd
powershell -Command "Get-Content logs\app_*.log -Wait -Tail 30"
```

然后让手机发一条短信，观察是否出现：

```
收到短信上报 unique_id=xxx device=xxx slot=1 ...
```

- **日志有** → 请求到了，看后面的结果（入库成功 / 重复 / 报错）
- **日志没有** → 请求根本没到服务端，是网络问题（见第一节）

### 关于「重复消息也返回 200」

这是**正确行为**。同一条短信（`messageUniqueId` 相同）重复上报时，
服务端返回 `{"code":200,"msg":"duplicate ignored"}`，
APP 会显示「上传成功」——因为数据确实已经在库里了。
这避免了 APP 白重试 60 秒。

### 发短信没反应？

- 确认 APP 按钮显示的是「停止监测」（说明服务在跑）
- 确认没有开启「短信拦截 / 骚扰拦截」把发送号码拦下了
- 某些 ROM（如 MIUI）会限制第三方应用接收短信，检查是否有相关开关

---

## 四、手机 APP 被系统杀后台

**现象**：过一段时间不发短信了；APP 日志停留在很早以前。

### 必做配置

1. **电池优化白名单**：`设置 → 电池 → 电池优化 → SMS上传 → 不受限制`
2. **自启动**：`设置 → 应用管理 → SMS上传 → 自启动 → 允许`
3. **后台运行**：`设置 → 应用管理 → SMS上传 → 省电策略 → 无限制`
4. **锁定后台**：打开最近任务，给 APP 卡片加锁（防止「一键清理」清掉）
5. **通知权限**：允许通知（前台服务通知是保活的关键）

### 品牌专用路径

| 品牌 | 路径 |
| --- | --- |
| 小米 / Redmi | 手机管家 → 应用管理 → 权限 → 自启动；省电策略选「无限制」；后台加锁 |
| 华为 / 荣耀 | 手机管家 → 应用启动管理 → SMS上传 → 关闭「自动管理」，手动全开 |
| OPPO / realme | 设置 → 电池 → 应用耗电管理 → 允许完全后台行为 |
| vivo / iQOO | 设置 → 电池 → 后台高耗电 → 允许；自启动管理 → 允许 |
| 三星 | 设置 → 电池 → 后台使用限制 → 把 APP 加入「从不休眠的应用」 |

### 补救手段

即使被杀，本项目也做了两层兜底：

- **开机自恢复**：手机重启后，如果之前开着监测，APP 会自动重新拉起服务；
- **静态广播接收器**：即使进程被杀，系统收到短信时仍会唤醒 APP 处理。

但如果被 ROM 彻底冻结（如「后台冻结」），仍可能漏收。所以上面的配置还是要做完。

---

## 五、网页显示问题

### `/` 打不开

- 服务没启动 → 检查进程、看 `logs/error_*.log`
- 端口被占用 → `netstat -ano | findstr :8000`，找到 PID 后在任务管理器结束它，
  或改用其它端口（同时要改手机 APP 里的配置）

### 页面能打开但样式全无（像纯文本）

Bootstrap 是本地文件 `static/bootstrap.min.css`，检查它是否存在：

```cmd
dir static\bootstrap.min.css
```

缺失就从工程里重新拷贝一份。若是通过反向代理访问，
确认代理也转发了 `/static/` 路径。

### 页面显示「数据库暂时不可用」

服务本身和页面都正常，只是数据库连不上 → 见「二、数据库连接失败」。
（这是刻意的设计：数据库挂了页面仍能打开并给出明确提示，而不是白屏。）

### 页面没有数据

- 数据库里确实没数据 → 用 `scripts/load_test.py` 造几条测试数据验证链路
- 有数据但列表为空 → 检查是否设了筛选条件，点「重置」
- 筛选条件写错了 → 设备 ID / 号码是**包含匹配**，输一小段即可

---

## 六、性能问题

### 出现 429（请求过于频繁）

默认每个 IP 每 60 秒 300 次。10 台设备正常上报远达不到这个量。
如果确实触发了，说明有异常：

- 某台设备陷入重试循环 → 检查该设备的 Token 是否配错（401 会导致重试）
- 真的业务量很大 → 调大 `.env` 的 `RATE_LIMIT_TIMES` 和 `RATE_LIMIT_WINDOW`

> 注意：多 worker 时每个进程各自计数，实际阈值 ≈ 单 worker 阈值 × worker 数。

### 接口变慢

```cmd
:: 看慢查询
mysql -u root -p -e "SHOW VARIABLES LIKE 'slow_query_log';"
```

- 先确认索引都在：`SHOW INDEX FROM sms_monitor.sms_records;`
  应包含 `uk_message_unique_id`、`idx_receive_time`、`idx_device_receive`
- 数据量大了以后筛选变慢 → 见 README「性能与容量」里的前缀匹配优化说明
- 连接池不够用 → 调大 `DB_POOL_MAX`（同时确认 MySQL 的 `max_connections` 够）

### 压测但压不上去

```cmd
.venv\Scripts\python.exe scripts\load_test.py --token 你的Token --devices 20 --messages-per-device 50 --concurrency 100
```

观察 QPS 与延迟。若失败率上升，先看 `logs/error_*.log`，
常见是数据库连接数打满或磁盘 IO 瓶颈。

---

## 七、日志怎么用

| 文件 | 内容 |
| --- | --- |
| `logs/app_<pid>.log` | 全部日志 |
| `logs/error_<pid>.log` | 只含 WARNING / ERROR，**排障优先看这个** |

**实时跟踪**（PowerShell）：

```powershell
Get-Content logs\error_*.log -Wait -Tail 50
```

**多 worker 会有多个文件**（文件名带进程 PID），这是刻意设计，
避免多进程共写同一文件导致轮转冲突。要合并看：

```cmd
type logs\app_*.log | findstr "ERROR"
```

**日志级别**：改 `.env` 的 `LOG_LEVEL=DEBUG` 可以看到更详细的 SQL 与流程信息，
排查完记得改回 `INFO`（DEBUG 会产生大量日志）。

---

## 八、数据相关

### 数据没入库但接口返回了 200

不可能同时发生。200 只有两种：
`msg=success`（已入库）或 `msg=duplicate ignored`（这条消息之前已入库）。
如果页面看不到，检查：

```sql
SELECT * FROM sms_records WHERE message_unique_id = '你日志里的那个ID';
```

`receive_time` 是短信接收时间，页面按它倒序。如果手机时间不准，
这条可能排到了别的位置——用发送号码搜一下。

### 想删掉压测数据

```sql
DELETE FROM sms_records WHERE device_id LIKE 'loadtest-%' OR device_id LIKE 'curl-%' OR device_id LIKE 'ps-%';
```

### 表越来越大

```sql
-- 按月归档/清理
DELETE FROM sms_records WHERE receive_time < DATE_SUB(NOW(), INTERVAL 6 MONTH);
OPTIMIZE TABLE sms_records;   -- 回收空间
```

---

## 九、还是没解决？

收集以下信息，基本能定位所有问题：

1. `curl http://127.0.0.1:8000/health` 的返回
2. `logs/error_*.log` 里的报错堆栈
3. 手机 APP 的日志截图（含完整的失败原因）
4. `netstat -ano | findstr :8000` 的输出
5. `sql/sms.sql` 执行后 `SHOW CREATE TABLE sms_records\G` 的结果
