package com.lan.smsuploader.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import com.lan.smsuploader.data.Prefs
import com.lan.smsuploader.model.SmsPayload
import com.lan.smsuploader.util.SimUtils
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * 静态注册的短信广播接收器（见 AndroidManifest）。
 *
 * 采用静态注册而非服务内动态注册，好处是进程被杀后系统仍能把短信广播送进来，
 * 从而重新拉起服务，可靠性更高。
 *
 * 仅在“监测已开启”时处理短信，其余情况直接忽略。
 */
class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return

        // 未开启监测时不做任何事
        if (!Prefs.monitoringEnabled) return

        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (messages.isEmpty()) return

        // 长短信会被拆成多条，这里按顺序拼接为完整正文
        val sender = messages.first().displayOriginatingAddress
            ?: messages.first().originatingAddress
            ?: UNKNOWN_SENDER
        val content = messages.joinToString(separator = "") { it.displayMessageBody ?: "" }
        val receiveMillis = messages.first().timestampMillis

        val simSlot = resolveSimSlot(context, intent)
        val simPhone = Prefs.simNumberForSlot(simSlot)
            ?: SimUtils.getPhoneNumberBySlot(context, simSlot)
            ?: ""

        val payload = SmsPayload(
            deviceId = Prefs.deviceId(context),
            simSlot = simSlot,
            simPhoneNumber = simPhone,
            sender = sender,
            messageContent = content,
            receiveTime = formatTime(receiveMillis),
            messageUniqueId = UUID.randomUUID().toString(),
        )

        // 交给前台服务处理（服务内可长时间重试，不受广播时限约束）
        SmsMonitorService.startUpload(context, payload)
    }

    /**
     * 从广播中解析卡槽编号（1/2）。
     *
     * 短信广播里带的 extra 在不同机型/版本上不完全一致：
     *  - 优先取 subscriptionId（key = "subscription"），再换算为卡槽；
     *  - 退化取 slot 下标（key = "slot"，0 基），+1 得到面向用户的卡槽号；
     *  - 都取不到时默认卡槽 1。
     */
    private fun resolveSimSlot(context: Context, intent: Intent): Int {
        val extras = intent.extras ?: return DEFAULT_SLOT

        val subscriptionId = extras.getInt(EXTRA_SUBSCRIPTION, -1)
        if (subscriptionId >= 0) {
            SimUtils.getSlotNumberBySubscriptionId(context, subscriptionId)?.let { return it }
        }

        val slotIndex = extras.getInt(EXTRA_SLOT, -1)
        if (slotIndex >= 0) return slotIndex + 1

        return DEFAULT_SLOT
    }

    private fun formatTime(millis: Long): String =
        SimpleDateFormat(ISO_8601_PATTERN, Locale.US).format(Date(millis))

    companion object {
        private const val EXTRA_SUBSCRIPTION = "subscription"
        private const val EXTRA_SLOT = "slot"
        private const val DEFAULT_SLOT = 1
        private const val UNKNOWN_SENDER = "未知号码"
        private const val ISO_8601_PATTERN = "yyyy-MM-dd'T'HH:mm:ssXXX"
    }
}
