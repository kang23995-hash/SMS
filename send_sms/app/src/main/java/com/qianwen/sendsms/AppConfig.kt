package com.qianwen.sendsms

import android.content.Context
import android.provider.Settings
import java.net.URI
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * 手机端配置：服务器地址、设备名、两张卡的手机号。
 * 服务端靠卡1/卡2号码把任务精准派发到对应设备和卡槽。
 */
data class AppConfig(
    val serverUrl: String,
    val deviceName: String,
    val sim1: String,
    val sim2: String,
    val token: String,
) {
    val baseUrl: String
        get() = normalizeServerUrl(serverUrl)

    fun isValid(): Boolean = isValidServerUrl(serverUrl) &&
        (sim1.isNotBlank() || sim2.isNotBlank())

    companion object {
        private const val PREFS = "send_sms_config"
        private const val KEY_SERVER = "server_url"
        private const val KEY_NAME = "device_name"
        private const val KEY_SIM1 = "sim1"
        private const val KEY_SIM2 = "sim2"
        private const val KEY_TOKEN = "token"
        private const val KEY_MONITORING = "monitoring_enabled"

        /** 允许只输入 IP/域名和端口，统一补齐协议并去掉末尾斜杠。 */
        fun normalizeServerUrl(raw: String): String {
            val value = raw.trim()
            if (value.isEmpty()) return ""
            val withScheme = if (
                value.startsWith("http://", ignoreCase = true) ||
                value.startsWith("https://", ignoreCase = true)
            ) value else "http://$value"
            return withScheme.trimEnd('/')
        }

        fun isValidServerUrl(raw: String): Boolean {
            val normalized = normalizeServerUrl(raw)
            if (normalized.isEmpty()) return false
            return try {
                val uri = URI(normalized)
                (uri.scheme.equals("http", true) || uri.scheme.equals("https", true)) &&
                    !uri.host.isNullOrBlank() && uri.port <= 65535 && uri.userInfo == null
            } catch (_: Exception) {
                false
            }
        }

        fun setMonitoringEnabled(ctx: Context, enabled: Boolean) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putBoolean(KEY_MONITORING, enabled)
                .apply()
        }

        fun isMonitoringEnabled(ctx: Context): Boolean =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getBoolean(KEY_MONITORING, false)

        fun load(ctx: Context): AppConfig {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            return AppConfig(
                serverUrl = p.getString(KEY_SERVER, "") ?: "",
                deviceName = p.getString(KEY_NAME, "") ?: "",
                sim1 = p.getString(KEY_SIM1, "") ?: "",
                sim2 = p.getString(KEY_SIM2, "") ?: "",
                token = p.getString(KEY_TOKEN, "") ?: "",
            )
        }

        fun save(ctx: Context, cfg: AppConfig) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY_SERVER, cfg.baseUrl)
                .putString(KEY_NAME, cfg.deviceName.trim())
                .putString(KEY_SIM1, normalizePhone(cfg.sim1))
                .putString(KEY_SIM2, normalizePhone(cfg.sim2))
                .putString(KEY_TOKEN, cfg.token.trim())
                .apply()
        }
    }
}

/** 号码归一：只留数字，去掉 +86 / 0086 前缀，不截断服务号。 */
fun normalizePhone(raw: String): String {
    var digits = raw.filter { it.isDigit() }
    if (digits.length > 11 && digits.startsWith("0086")) digits = digits.substring(4)
    else if (digits.length == 13 && digits.startsWith("86")) digits = digits.substring(2)
    return digits
}

/** 设备唯一标识：优先用持久化的随机 UUID，避免恢复出厂/多设备冲突。 */
object DeviceIdentity {
    private const val PREFS = "send_sms_config"
    private const val KEY_ID = "device_id"

    @Suppress("DEPRECATION")
    fun id(ctx: Context): String {
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val saved = p.getString(KEY_ID, "")
        if (!saved.isNullOrBlank()) return saved

        val androidId = try {
            Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID)
        } catch (_: Exception) {
            null
        }
        val value = if (!androidId.isNullOrBlank() && androidId != "9774d56d682e549c") {
            "android_$androidId"
        } else {
            "uuid_${UUID.randomUUID()}"
        }
        p.edit().putString(KEY_ID, value).apply()
        return value
    }

    fun defaultName(): String = "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}".trim()
}

/** 进程内日志总线：服务写、界面读，不需要跨进程/广播。 */
object RuntimeLog {
    private const val MAX_LINES = 400
    private val lines = ArrayDeque<String>()
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    @Volatile
    var listener: ((String) -> Unit)? = null

    fun log(message: String) {
        val line = "${timeFormat.format(Date())}  $message"
        synchronized(lines) {
            lines.addLast(line)
            while (lines.size > MAX_LINES) lines.removeFirst()
        }
        listener?.invoke(line)
    }

    fun snapshot(): List<String> = synchronized(lines) { lines.toList() }

    fun clear() = synchronized(lines) { lines.clear() }
}
