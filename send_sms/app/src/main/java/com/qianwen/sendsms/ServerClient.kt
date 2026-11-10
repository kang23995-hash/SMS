package com.qianwen.sendsms

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * 与 fastapi_sms 的 /api/sms-send 接口通信。
 *
 * 全部用 HttpURLConnection + org.json，不引第三方库：
 * 离线环境也能构建，APK 体积也小。
 */
object ServerClient {

    data class Task(
        val id: Int,
        val sendPhone: String,
        val recipient: String,
        val content: String,
    )

    data class PollResult(
        val task: Task?,
        val httpCode: Int,
    )

    /** 长轮询领任务。204 表示这一轮没有任务。 */
    fun poll(
        cfg: AppConfig,
        deviceId: String,
        deviceName: String,
        sim1: String,
        sim2: String,
        waitSeconds: Int = 25,
    ): PollResult {
        val query = buildString {
            append("?device_id=").append(enc(deviceId))
            append("&device_name=").append(enc(deviceName))
            append("&sim1=").append(enc(normalizePhone(sim1)))
            append("&sim2=").append(enc(normalizePhone(sim2)))
            append("&app_version=1.0.1")
            append("&wait=").append(waitSeconds.coerceIn(0, 30))
        }
        val conn = open("${cfg.baseUrl}/api/sms-send/poll$query", "GET", cfg.token)
        conn.readTimeout = (waitSeconds + 15) * 1000
        try {
            val code = conn.responseCode
            if (code == HttpURLConnection.HTTP_NO_CONTENT) {
                return PollResult(null, code)
            }
            val body = readBody(conn, code)
            if (code != 200) {
                throw IOException("轮询失败 HTTP $code ${body.take(200)}")
            }
            val data = JSONObject(body).optJSONObject("data")
                ?: throw IOException("响应缺少 data：${body.take(200)}")
            return PollResult(
                Task(
                    id = data.getInt("task_id"),
                    sendPhone = data.optString("send_phone"),
                    recipient = data.optString("recipient"),
                    content = data.optString("content"),
                ),
                code,
            )
        } finally {
            conn.disconnect()
        }
    }

    /** 回执发送结果。 */
    fun report(
        cfg: AppConfig,
        taskId: Int,
        deviceId: String,
        ok: Boolean,
        detail: String,
    ): Boolean {
        val payload = JSONObject()
            .put("task_id", taskId)
            .put("device_id", deviceId)
            .put("ok", ok)
            .put("detail", detail.take(400))
            .toString()

        val conn = open("${cfg.baseUrl}/api/sms-send/result", "POST", cfg.token)
        try {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val body = readBody(conn, code)
            if (code != 200) {
                RuntimeLog.log("回执失败 HTTP $code ${body.take(200)}")
                return false
            }
            return true
        } finally {
            conn.disconnect()
        }
    }

    /** 测试连接，返回服务端 /health 摘要。 */
    fun health(cfg: AppConfig): String {
        val conn = open("${cfg.baseUrl}/health", "GET", cfg.token)
        conn.connectTimeout = 6000
        conn.readTimeout = 6000
        try {
            val code = conn.responseCode
            val body = readBody(conn, code)
            if (code != 200) throw IOException("HTTP $code ${body.take(200)}")
            return body.take(200)
        } finally {
            conn.disconnect()
        }
    }

    private fun open(url: String, method: String, token: String): HttpURLConnection {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 10_000
            readTimeout = 30_000
            setRequestProperty("Accept", "application/json")
            if (token.isNotBlank()) setRequestProperty("X-Token", token)
        }
        return conn
    }

    private fun readBody(conn: HttpURLConnection, code: Int): String {
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        return stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
    }

    private fun enc(value: String): String = URLEncoder.encode(value, "UTF-8")
}
