package com.lan.smsuploader.model

import org.json.JSONObject

/**
 * 上传到电脑端 FastAPI 服务的短信数据包。
 *
 * 固定接口路径：POST /api/sms/receive
 *
 * JSON 结构（字段名与电脑端约定保持一致，不要随意改动）：
 * {
 *   "deviceId":         "设备唯一ID",
 *   "simSlot":          1,                    // 卡槽编号 1 / 2
 *   "simPhoneNumber":   "13800000000",        // 本机 SIM 号码，读取不到时为空串
 *   "sender":           "106xxxx",            // 发送方号码
 *   "messageContent":   "短信正文",
 *   "receiveTime":      "2026-10-12T21:30:05+08:00",  // ISO-8601 接收时间
 *   "messageUniqueId":  "UUID"                // 每条消息唯一 ID
 * }
 */
data class SmsPayload(
    val deviceId: String,
    val simSlot: Int,
    val simPhoneNumber: String,
    val sender: String,
    val messageContent: String,
    val receiveTime: String,
    val messageUniqueId: String,
) {

    fun toJson(): JSONObject = JSONObject().apply {
        put("deviceId", deviceId)
        put("simSlot", simSlot)
        put("simPhoneNumber", simPhoneNumber)
        put("sender", sender)
        put("messageContent", messageContent)
        put("receiveTime", receiveTime)
        put("messageUniqueId", messageUniqueId)
    }

    fun toJsonString(): String = toJson().toString()

    companion object {
        // 前台服务 Intent 透传时使用的 extra key
        const val EXTRA_DEVICE_ID = "extra_device_id"
        const val EXTRA_SIM_SLOT = "extra_sim_slot"
        const val EXTRA_SIM_PHONE = "extra_sim_phone"
        const val EXTRA_SENDER = "extra_sender"
        const val EXTRA_CONTENT = "extra_content"
        const val EXTRA_RECEIVE_TIME = "extra_receive_time"
        const val EXTRA_UNIQUE_ID = "extra_unique_id"
    }
}
