package com.lan.smsuploader.net

import com.lan.smsuploader.model.SmsPayload
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * 负责把短信 JSON 上传到电脑端 FastAPI 服务。
 *
 * 要点：
 *  - OkHttp 单例（内部连接池全局复用，减少 TCP 握手开销）；
 *  - 协程调度在 IO 线程，不阻塞 UI；
 *  - 单条消息在 60 秒窗口内持续重试，重试间隔 2~5 秒；
 *  - 超过 60 秒仍未成功即视为超时丢弃（服务本身继续监听后续短信）。
 */
object Uploader {

    /** 固定接口路径。 */
    const val RECEIVE_PATH = "/api/sms/receive"

    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** 单条消息最长重试时长：60 秒。 */
    private const val MAX_RETRY_WINDOW_MS = 60_000L

    /** 重试间隔区间：2~5 秒。 */
    private const val RETRY_MIN_MS = 2_000L
    private const val RETRY_MAX_MS = 5_000L

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    sealed class Result {
        /** 收到 2xx 响应，上传成功。 */
        data class Success(val code: Int) : Result()

        /** 60 秒窗口内未成功，消息被丢弃。 */
        data class Timeout(val lastError: String) : Result()
    }

    /**
     * 上传一条短信。
     *
     * @param serverAddress 用户填写的“IP:端口”，例如 192.168.1.100:8000
     * @param onAttempt     每次尝试失败后的回调（用于界面日志），参数为第几次尝试与失败原因
     */
    suspend fun upload(
        serverAddress: String,
        payload: SmsPayload,
        onAttempt: (attempt: Int, reason: String) -> Unit = { _, _ -> },
    ): Result = withContext(Dispatchers.IO) {

        val url = buildUrl(serverAddress)
            ?: return@withContext Result.Timeout("服务器地址无效：$serverAddress")

        val request = Request.Builder()
            .url(url)
            .post(payload.toJsonString().toRequestBody(JSON))
            .build()

        val deadline = System.currentTimeMillis() + MAX_RETRY_WINDOW_MS
        var attempt = 0
        var lastReason: String

        while (true) {
            attempt++
            lastReason = try {
                client.newCall(request).execute().use { response ->
                    if (response.isSuccessful) {
                        return@withContext Result.Success(response.code)
                    }
                    "HTTP ${response.code}"
                }
            } catch (e: Exception) {
                e.message ?: e.javaClass.simpleName
            }

            onAttempt(attempt, lastReason)

            val remain = deadline - System.currentTimeMillis()
            if (remain <= 0L) break

            val wait = Random.nextLong(RETRY_MIN_MS, RETRY_MAX_MS + 1)
            delay(minOf(wait, remain))
        }

        Result.Timeout(lastReason)
    }

    /** 由用户输入的地址拼出完整 URL；非法输入返回 null。 */
    fun buildUrl(serverAddress: String): String? {
        val hostPort = normalize(serverAddress) ?: return null
        return "http://$hostPort$RECEIVE_PATH"
    }

    /**
     * 归一化用户输入：去掉协议头、首尾空白与多余斜杠、以及误粘进来的路径。
     * 返回 "host:port" 形式，无法解析时返回 null。
     */
    fun normalize(serverAddress: String): String? {
        var addr = serverAddress.trim()
        if (addr.isEmpty()) return null

        addr = addr.removePrefix("http://").removePrefix("https://")
        addr = addr.trimEnd('/')

        val slash = addr.indexOf('/')
        if (slash >= 0) addr = addr.substring(0, slash)

        if (addr.isEmpty()) return null
        return addr
    }
}
