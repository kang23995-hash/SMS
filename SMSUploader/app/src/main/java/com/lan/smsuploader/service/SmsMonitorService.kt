package com.lan.smsuploader.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.lan.smsuploader.MainActivity
import com.lan.smsuploader.R
import com.lan.smsuploader.data.Prefs
import com.lan.smsuploader.model.SmsPayload
import com.lan.smsuploader.net.Uploader
import com.lan.smsuploader.util.AppEvents
import com.lan.smsuploader.util.Notifier
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * 短信监测前台服务。
 *
 * 职责：
 *  1. 常驻后台并展示常驻通知，降低被系统回收的概率；
 *  2. 接收 [SmsReceiver] 转交的短信，调用 [Uploader] 上传（含 60 秒重试窗口）；
 *  3. 通过 [AppEvents] 把状态推给界面。
 *
 * 注意：本服务不负责拦截短信，短信由清单中注册的静态广播接收器捕获，即使进程被杀、
 * 被系统重新拉起后也能继续工作。
 */
class SmsMonitorService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var foregroundStarted = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_UPLOAD -> {
                ensureForeground(getString(R.string.notif_listening))
                intent.toPayload()?.let { handleIncomingSms(it) }
            }

            else -> {
                // 系统在 START_STICKY 后重启服务时 intent 可能为 null
                if (intent == null && !Prefs.monitoringEnabled) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                Prefs.monitoringEnabled = true
                ensureForeground(getString(R.string.notif_listening))
                AppEvents.log(getString(R.string.log_monitoring_started))
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        stopForegroundCompat()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- 上传流程

    private fun handleIncomingSms(payload: SmsPayload) {
        AppEvents.log(
            getString(R.string.log_new_sms, payload.sender, payload.simSlot)
        )
        AppEvents.emit(
            AppEvents.UiEvent.NewSms(
                getString(R.string.dialog_new_sms_title),
                getString(R.string.dialog_new_sms_message),
            )
        )
        Notifier.toast(this, getString(R.string.toast_new_sms))
        updateNotification(getString(R.string.notif_uploading))

        scope.launch {
            val result = Uploader.upload(
                serverAddress = Prefs.serverAddress,
                payload = payload,
                onAttempt = { attempt, reason ->
                    AppEvents.log(getString(R.string.log_upload_retry, attempt, reason))
                },
            )

            when (result) {
                is Uploader.Result.Success -> {
                    AppEvents.log(getString(R.string.log_upload_success, result.code))
                    AppEvents.emit(
                        AppEvents.UiEvent.Success(
                            getString(R.string.dialog_upload_success_title),
                            getString(R.string.dialog_upload_success_message),
                        )
                    )
                    Notifier.toast(this@SmsMonitorService, getString(R.string.toast_upload_success))
                }

                is Uploader.Result.Timeout -> {
                    AppEvents.log(getString(R.string.log_upload_timeout, result.lastError))
                    AppEvents.emit(
                        AppEvents.UiEvent.Timeout(
                            getString(R.string.dialog_upload_timeout_title),
                            getString(R.string.dialog_upload_timeout_message),
                        )
                    )
                    Notifier.toast(this@SmsMonitorService, getString(R.string.toast_upload_timeout))
                }
            }

            // 恢复常驻通知文案；服务本身不因单条失败而停止
            if (Prefs.monitoringEnabled) {
                updateNotification(getString(R.string.notif_listening))
            }
        }
    }

    // ---------------------------------------------------------------- 通知

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notif_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.notif_channel_desc)
            setShowBadge(false)
        }
        notificationManager().createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val contentIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_sms)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(contentIntent)
            .build()
    }

    private fun ensureForeground(text: String) {
        val notification = buildNotification(text)
        if (!foregroundStarted) {
            startForeground(NOTIFICATION_ID, notification)
            foregroundStarted = true
        } else {
            notificationManager().notify(NOTIFICATION_ID, notification)
        }
    }

    private fun updateNotification(text: String) {
        if (!foregroundStarted) return
        notificationManager().notify(NOTIFICATION_ID, buildNotification(text))
    }

    @Suppress("DEPRECATION")
    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            stopForeground(true)
        }
        foregroundStarted = false
    }

    private fun notificationManager(): NotificationManager =
        getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        private const val CHANNEL_ID = "sms_monitor"
        private const val NOTIFICATION_ID = 1001

        const val ACTION_START = "com.lan.smsuploader.action.START"
        const val ACTION_UPLOAD = "com.lan.smsuploader.action.UPLOAD"

        /** 启动监测（同时作为常驻通知的入口）。 */
        fun start(context: Context) {
            startSafely(context, Intent(context, SmsMonitorService::class.java).setAction(ACTION_START))
        }

        /** 停止监测。 */
        fun stop(context: Context) {
            context.stopService(Intent(context, SmsMonitorService::class.java))
        }

        /** 把捕获到的短信交给服务上传。 */
        fun startUpload(context: Context, payload: SmsPayload) {
            val intent = Intent(context, SmsMonitorService::class.java)
                .setAction(ACTION_UPLOAD)
                .putExtra(SmsPayload.EXTRA_DEVICE_ID, payload.deviceId)
                .putExtra(SmsPayload.EXTRA_SIM_SLOT, payload.simSlot)
                .putExtra(SmsPayload.EXTRA_SIM_PHONE, payload.simPhoneNumber)
                .putExtra(SmsPayload.EXTRA_SENDER, payload.sender)
                .putExtra(SmsPayload.EXTRA_CONTENT, payload.messageContent)
                .putExtra(SmsPayload.EXTRA_RECEIVE_TIME, payload.receiveTime)
                .putExtra(SmsPayload.EXTRA_UNIQUE_ID, payload.messageUniqueId)
            startSafely(context, intent)
        }

        /**
         * Android 12+ 对「后台启动前台服务」有额外限制。这里优先用
         * startForegroundService，失败时退化为普通 startService（服务已在运行时可用），
         * 避免因系统限制直接崩溃。
         */
        private fun startSafely(context: Context, intent: Intent) {
            try {
                ContextCompat.startForegroundService(context, intent)
            } catch (e: Exception) {
                runCatching { context.startService(intent) }
            }
        }
    }
}

private fun Intent.toPayload(): SmsPayload? {
    val uniqueId = getStringExtra(SmsPayload.EXTRA_UNIQUE_ID) ?: return null
    return SmsPayload(
        deviceId = getStringExtra(SmsPayload.EXTRA_DEVICE_ID).orEmpty(),
        simSlot = getIntExtra(SmsPayload.EXTRA_SIM_SLOT, 1),
        simPhoneNumber = getStringExtra(SmsPayload.EXTRA_SIM_PHONE).orEmpty(),
        sender = getStringExtra(SmsPayload.EXTRA_SENDER).orEmpty(),
        messageContent = getStringExtra(SmsPayload.EXTRA_CONTENT).orEmpty(),
        receiveTime = getStringExtra(SmsPayload.EXTRA_RECEIVE_TIME).orEmpty(),
        messageUniqueId = uniqueId,
    )
}
