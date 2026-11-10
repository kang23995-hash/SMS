# SMS上传（SMS Uploader）

一个极简的 Android 应用：**监听本机收到的短信，并把这些短信以 JSON 形式通过局域网 HTTP 上传到电脑上的 FastAPI 服务**。

本工程基于 [capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway)（Apache-2.0）裁剪重构，仅保留短信监听与上传这一条链路，界面与网络层全部重写。

---

## 一、保留与移除的功能

**只保留**
- 短信监听（后台前台服务常驻）
- 局域网 HTTP 上传短信到电脑端 FastAPI

**已移除（原项目的其余能力）**
- 远程发短信 API（SEND_SMS 相关全部删除）
- 设备内置本地 HTTP 服务（Ktor Server）
- 复杂消息过滤面板
- 多 Webhook 配置
- 本地持久化缓存失败消息
- 本地历史短信存储、设备管理页面、加密模块、Firebase 推送等

---

## 二、目录结构

```
SMSUploader/
├── LICENSE                     Apache-2.0 许可证（来自原项目）
├── NOTICE                      版权与来源声明
├── README.md                   本文档
├── build.gradle                根构建脚本
├── settings.gradle
├── gradle.properties
├── gradlew / gradlew.bat       Gradle Wrapper 8.0
├── gradle/wrapper/
└── app/
    ├── build.gradle            app 模块构建脚本（compileSdk 34 / minSdk 24）
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/lan/smsuploader/
        │   ├── App.kt                          Application 入口，初始化配置
        │   ├── MainActivity.kt                 唯一页面
        │   ├── data/Prefs.kt                   SharedPreferences 持久化
        │   ├── model/SmsPayload.kt             上传 JSON 数据模型
        │   ├── net/Uploader.kt                 OkHttp 上传 + 60 秒重试
        │   ├── service/SmsMonitorService.kt    前台服务（常驻 + 上传调度）
        │   ├── service/SmsReceiver.kt          短信广播接收器
        │   ├── service/BootReceiver.kt         开机自恢复
        │   └── util/
        │       ├── SimUtils.kt                 双卡槽 / 手机号读取
        │       ├── AppEvents.kt                服务与界面通信总线
        │       └── Notifier.kt                 主线程 Toast 工具
        └── res/
            ├── layout/activity_main.xml        单页布局
            ├── values/                         strings / colors / themes
            ├── drawable/ic_stat_sms.xml        通知栏图标
            └── xml/network_security_config.xml 允许局域网明文 HTTP
```

---

## 三、接口契约（与电脑端 FastAPI 约定）

- **方法**：`POST`
- **路径**：`/api/sms/receive`（固定）
- **完整地址**：`http://<你填写的IP:端口>/api/sms/receive`
- **Content-Type**：`application/json; charset=utf-8`

**请求体字段**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `deviceId` | string | 设备唯一 ID（优先取 ANDROID_ID，取不到则生成 UUID 并持久化） |
| `simSlot` | int | 卡槽编号，`1` 或 `2` |
| `simPhoneNumber` | string | 本机该卡槽的手机号，读取不到时为空串 `""` |
| `sender` | string | 发送方号码 |
| `messageContent` | string | 短信正文（长短信已自动拼接） |
| `receiveTime` | string | 接收时间，ISO-8601，带时区，如 `2026-10-12T21:30:05+08:00` |
| `messageUniqueId` | string | 每条消息唯一 ID（UUID） |

**请求示例**

```json
{
  "deviceId": "a1b2c3d4e5f6a7b8",
  "simSlot": 1,
  "simPhoneNumber": "13800000000",
  "sender": "1069030000000",
  "messageContent": "您的验证码是 123456，5 分钟内有效。",
  "receiveTime": "2026-10-12T21:30:05+08:00",
  "messageUniqueId": "3f2a9c10-8b7e-4d21-9f0a-6c5d4e3b2a10"
}
```

**电脑端只需返回 2xx** 即视为上传成功（APP 判定 `response.isSuccessful`）。建议返回：

```json
{ "code": 0, "message": "ok" }
```

**参考接收端（FastAPI，仅示意，请按需自行实现）**

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class SmsPayload(BaseModel):
    deviceId: str
    simSlot: int
    simPhoneNumber: str = ""
    sender: str
    messageContent: str
    receiveTime: str
    messageUniqueId: str

@app.post("/api/sms/receive")
async def receive(payload: SmsPayload):
    print(payload.model_dump())
    return {"code": 0, "message": "ok"}
```

> 注意：电脑端 FastAPI 启动时要监听 `0.0.0.0`（而非 `127.0.0.1`）才能被手机访问：
> `uvicorn main:app --host 0.0.0.0 --port 8000`

---

## 四、界面说明（单页）

1. **电脑FastAPI服务IP:端口**：输入框，示例 `192.168.1.100:8000`。输入即保存，下次打开自动加载。
2. **检测手机号**：点击读取卡槽 1 / 卡槽 2 的号码，结果在下方文本展示；读取失败时可在「卡槽1/2手机号」输入框手动填写。
3. **开启监测 / 停止监测**：未开启显示「开启监测」，点击启动前台服务；开启后显示「停止监测」，点击停止。
4. **状态日志区**：实时展示监听状态、短信捕获、上传成功/失败、重试等日志（可「清空」）。

---

## 五、使用流程

1. 手机与电脑连到**同一个局域网**（同一 WiFi / 同一路由器）。
2. 把电脑的局域网 IP 和端口填进输入框（例如 `192.168.1.100:8000`）。
3. 点「检测手机号」，确认卡槽号码；读不到就手动填。
4. 点「开启监测」，按提示授予短信 / 电话 / 通知权限，并在弹窗里把应用加入**电池优化白名单**。
5. 给手机发一条测试短信，观察日志与电脑端接收情况。

---

## 六、编译步骤（Android Studio）

**环境要求**
- Android Studio Hedgehog（2023.1）或更新
- JDK 17（Android Studio 自带）
- Android SDK Platform 34、Build-Tools 34.x

**步骤**

1. Android Studio → `File ▸ Open`，选择本 `SMSUploader` 目录。
2. 等待 Gradle Sync 完成（首次会下载 AGP 8.1.2、Kotlin 1.8.22、OkHttp 等依赖）。
   若提示缺少 SDK，按提示安装 Android SDK Platform 34。
3. 菜单 `Build ▸ Build Bundle(s) / APK(s) ▸ Build APK(s)` 生成 Debug APK。
4. 产物位置：`app/build/outputs/apk/debug/app-debug.apk`。

**命令行编译（可选）**

```bash
# Windows
gradlew.bat assembleDebug

# macOS / Linux
./gradlew assembleDebug
```

> 首次运行若报 `SDK location not found`，在工程根目录新建 `local.properties`，写入你的 SDK 路径：
> ```
> sdk.dir=C\:\\Users\\<用户名>\\AppData\\Local\\Android\\Sdk
> ```

**生成正式（Release）APK / 签名**

菜单 `Build ▸ Generate Signed App Bundle / APK`，按向导创建或选择 keystore 即可。本工程 `release` 未开启代码混淆（`minifyEnabled false`），如需开启请在 `app/build.gradle` 中改为 `true`。

---

## 七、APK 安装与权限配置

**安装**

- 方式一：把 `app-debug.apk` 传到手机，用文件管理器点击安装（需在系统设置里允许该来源「安装未知应用」）。
- 方式二：USB 连接后执行：

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

**权限清单及用途**

| 权限 | 用途 |
| --- | --- |
| `RECEIVE_SMS` | 接收系统短信广播（核心） |
| `READ_SMS` | 读取短信内容 / 拼接长短信 |
| `READ_PHONE_STATE` | 识别卡槽、读取号码 |
| `READ_PHONE_NUMBERS` | Android 13+ 读取本机号码 |
| `INTERNET` / `ACCESS_NETWORK_STATE` | 局域网 HTTP 上传 |
| `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC` | 前台服务常驻 |
| `POST_NOTIFICATIONS` | Android 13+ 展示常驻通知 |
| `RECEIVE_BOOT_COMPLETED` | 开机自恢复监测 |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | 引导加入电池白名单 |

短信、电话、通知权限为**运行时权限**，首次点「开启监测」时 APP 会自动弹出授权引导；若误点拒绝，请到「设置 ▸ 应用 ▸ SMS上传 ▸ 权限」中手动开启。

**电池白名单 / 自启动（强烈建议，否则容易被系统杀后台）**

- 关闭电池优化：弹窗「建议关闭电池优化」→ 前往设置 → 允许后台运行；或手动到「设置 ▸ 电池 ▸ 电池优化」把本应用设为「不优化 / 不受限制」。
- 国产系统（小米 / 华为 / OPPO / vivo 等）还需在「手机管家 / 应用管理」里单独开启**自启动**、**后台运行**、**锁定后台任务**，并把省电策略设为「无限制」。

---

## 八、上传重试策略

- 每条短信在 **60 秒**内持续重试上传；
- 每次重试间隔 **2~5 秒**（随机，避免网络抖动时的同步风暴）；
- 60 秒内成功（收到 2xx）→ 弹窗「上传成功」；
- 超过 60 秒仍失败 → 弹窗「上传超时」，**丢弃该条消息**（不做本地缓存）；
- **短信监测服务不会因单条上传失败而停止，会继续监听后续短信。**

网络层使用 OkHttp 单例，内部连接池全局复用，减少 TCP 握手开销；所有请求在协程的 IO 线程执行，不阻塞主线程。

---

## 九、常见问题

- **点开启监测后没有收到短信日志？**
  确认已授予短信权限，且未开启「短信拦截 / 骚扰拦截」把该号码拦截；确认监测按钮已切到「停止监测」状态。
- **一直上传失败 / 超时？**
  ① 手机与电脑是否在同一局域网；② 电脑防火墙是否放行该端口；③ FastAPI 是否监听 `0.0.0.0`；④ 地址是否写成 `IP:端口`（不要带 `http://`，带了也能自动纠正）。
- **手机号读不出来？**
  多数运营商不会把号码写入 SIM 卡，属正常现象，直接在「卡槽1/2手机号」输入框手动填写即可。
- **APP 被系统杀后台？**
  按第七节完成电池白名单 + 自启动设置；本应用也支持开机自恢复。

---

## 十、版权

本工程为 [capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway) 的裁剪重构版，遵循 **Apache License 2.0** 发布，保留原项目版权声明。详见 `LICENSE` 与 `NOTICE`。
