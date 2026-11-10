package com.qianwen.sendsms

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 前台服务：持续长轮询 fastapi_sms，领到任务就发短信并回执。
 *
 * 用户点「开启监收」后一直运行，直到点「停止监收」或通知栏的停止按钮。
 * 网络异常时按 3~15 秒退避重试，且错误日志 30 秒最多一条，避免刷屏。
 */
class SmsSendService : Service() {

    companion object {
        const val ACTION_START = "com.qianwen.sendsms.START"
        const val ACTION_STOP = "com.qianwen.sendsms.STOP"

        private const val CHANNEL_ID = "send_sms_service"
        private const val NOTIFICATION_ID = 1001
        private const val RESTART_REQUEST_CODE = 1002

        /** 服务被系统回收后，通过前台服务 PendingIntent 尽快拉起。 */
        fun scheduleRestart(context: Context) {
            val intent = Intent(context, SmsSendService::class.java).setAction(ACTION_START)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            val pending = if (Build.VERSION.SDK_INT >= 26) {
                PendingIntent.getForegroundService(context, RESTART_REQUEST_CODE, intent, flags)
            } else {
                PendingIntent.getService(context, RESTART_REQUEST_CODE, intent, flags)
            }
            val alarms = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            alarms.setAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + 2_000,
                pending,
            )
        }

        fun cancelRestart(context: Context) {
            val intent = Intent(context, SmsSendService::class.java).setAction(ACTION_START)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            val pending = if (Build.VERSION.SDK_INT >= 26) {
                PendingIntent.getForegroundService(context, RESTART_REQUEST_CODE, intent, flags)
            } else {
                PendingIntent.getService(context, RESTART_REQUEST_CODE, intent, flags)
            }
            (context.getSystemService(Context.ALARM_SERVICE) as AlarmManager).cancel(pending)
            pending.cancel()
        }

        fun start(context: Context) {
            val intent = Intent(context, SmsSendService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }
    }

    @Volatile
    private var running = false

    @Volatile
    private var userStopped = false

    @Volatile
    private var worker: Thread? = null

    private var wakeLock: PowerManager.WakeLock? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
    private val watchdog = object : Runnable {
        override fun run() {
            if (running && worker?.isAlive != true) {
                RuntimeLog.log("检测到监收线程中断，正在自动恢复")
                ServiceState.update(true, "监收线程恢复中…")
                startWorker()
            }
            if (running) mainHandler.postDelayed(this, 15_000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            userStopped = true
            AppConfig.setMonitoringEnabled(this, false)
            cancelRestart(this)
            stopMonitoring()
            return START_NOT_STICKY
        }
        // START_STICKY 以空 Intent 重启时，只恢复用户明确开启过的监收。
        if (intent == null && !AppConfig.isMonitoringEnabled(this)) {
            stopSelf()
            return START_NOT_STICKY
        }

        userStopped = false
        AppConfig.setMonitoringEnabled(this, true)
        startForegroundCompat()
        acquireWakeLock()
        running = true
        if (worker?.isAlive != true) {
            ServiceState.update(true, "启动中…")
            startWorker()
        }
        mainHandler.removeCallbacks(watchdog)
        mainHandler.postDelayed(watchdog, 15_000)
        return START_STICKY
    }

    private fun startWorker() {
        worker = Thread({ loop() }, "sms-send-worker").also { it.start() }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        val shouldRestart = running && !userStopped && AppConfig.isMonitoringEnabled(this)
        running = false
        mainHandler.removeCallbacks(watchdog)
        worker?.interrupt()
        worker = null
        releaseWakeLock()
        if (shouldRestart) {
            ServiceState.update(true, "服务被回收，正在自动恢复…")
            RuntimeLog.log("监收服务被系统回收，2 秒后自动重启")
            runCatching { scheduleRestart(this) }
                .onFailure { RuntimeLog.log("自动重启调度失败：${it.message}") }
        } else {
            ServiceState.update(false, "未开启")
            RuntimeLog.log("监收已停止")
        }
        super.onDestroy()
    }

    private fun loop() {
        val cfg0 = AppConfig.load(this)
        RuntimeLog.log(
            "监收已开启：${cfg0.baseUrl.ifBlank { "(未填服务器)" }} " +
                "设备=${cfg0.deviceName.ifBlank { DeviceIdentity.defaultName() }} " +
                "卡1=${normalizePhone(cfg0.sim1).ifBlank { "-" }} " +
                "卡2=${normalizePhone(cfg0.sim2).ifBlank { "-" }}",
        )

        var errorStreak = 0
        var lastErrorLogAt = 0L

        while (running) {
            val cfg = AppConfig.load(this)
            if (!cfg.isValid()) {
                ServiceState.update(true, "配置不完整：请填写服务器地址和手机号")
                sleepQuietly(5_000)
                continue
            }

            try {
                val deviceId = DeviceIdentity.id(this)
                val deviceName = cfg.deviceName.ifBlank { DeviceIdentity.defaultName() }

                val poll = ServerClient.poll(
                    cfg = cfg,
                    deviceId = deviceId,
                    deviceName = deviceName,
                    sim1 = cfg.sim1,
                    sim2 = cfg.sim2,
                    waitSeconds = 25,
                )
                errorStreak = 0

                val task = poll.task
                if (task == null) {
                    // 心跳正常：只更新状态，不写日志
                    ServiceState.update(
                        true,
                        "监收中（${timeFormat.format(Date())} 心跳正常）",
                    )
                    continue
                }

                RuntimeLog.log(
                    "收到任务 #${task.id}：用 ${task.sendPhone} 发给 ${task.recipient}，" +
                        "内容 ${task.content}",
                )
                ServiceState.update(true, "正在发送任务 #${task.id}…")

                val result = SmsSender.send(this, cfg, task)
                val reported = ServerClient.report(
                    cfg = cfg,
                    taskId = task.id,
                    deviceId = deviceId,
                    ok = result.ok,
                    detail = result.detail,
                )

                RuntimeLog.log(
                    "任务 #${task.id} ${if (result.ok) "成功" else "失败"}：${result.detail}" +
                        if (reported) "" else "（回执上报失败）",
                )
                ServiceState.update(
                    true,
                    "监收中（最近任务 #${task.id} ${if (result.ok) "成功" else "失败"}）",
                )
            } catch (exc: InterruptedException) {
                break
            } catch (exc: Exception) {
                errorStreak++
                val now = System.currentTimeMillis()
                if (now - lastErrorLogAt > 30_000) {
                    RuntimeLog.log(
                        "连接/发送异常：${exc.javaClass.simpleName} ${exc.message ?: ""}",
                    )
                    lastErrorLogAt = now
                }
                ServiceState.update(true, "连接异常，自动重试中…")
                sleepQuietly(minOf(3_000L * errorStreak, 15_000L))
            }
        }
        RuntimeLog.log("监收线程退出")
    }

    private fun stopMonitoring() {
        running = false
        mainHandler.removeCallbacks(watchdog)
        worker?.interrupt()
        worker = null
        releaseWakeLock()
        ServiceState.update(false, "未开启")
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        runCatching {
            val power = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = power.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "send_sms:monitoring",
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        }.onFailure {
            RuntimeLog.log("唤醒锁获取失败，将继续以前台服务运行：${it.message}")
        }
    }

    private fun releaseWakeLock() {
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        wakeLock = null
    }

    private fun sleepQuietly(millis: Long) {
        try {
            Thread.sleep(millis)
        } catch (_: InterruptedException) {
            // 退出时被唤醒，下一轮 while 会结束
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            "短信代发监听",
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "保持与 fastapi_sms 的长连接，接收短信代发任务"
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, SmsSendService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle("短信代发运行中")
            .setContentText("正在等待 fastapi_sms 的任务")
            .setContentIntent(openIntent)
            .setOngoing(true)
            .addAction(0, "停止监收", stopIntent)
            .build()
    }

    private fun startForegroundCompat() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }
}

/** 开机或覆盖安装后，仅恢复用户此前明确开启的监收。 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        if (!AppConfig.isMonitoringEnabled(context)) return
        try {
            SmsSendService.start(context)
            RuntimeLog.log("系统启动完成，正在恢复短信监收")
        } catch (exc: Exception) {
            RuntimeLog.log("开机恢复监收失败：${exc.message}")
            runCatching { SmsSendService.scheduleRestart(context) }
        }
    }
}
