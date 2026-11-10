package com.qianwen.sendsms

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.SmsManager
import android.telephony.SubscriptionInfo
import android.telephony.SubscriptionManager
import android.telephony.TelephonyManager
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 真正执行短信发送的部分。
 *
 * 为什么用 SmsManager 而不是打开短信 App 点按：
 *  - SmsManager 可以直接指定 subscriptionId，双卡时精确定位到卡1/卡2；
 *  - 不需要把手机界面切来切去，速度快、不会被人碰屏幕打断；
 *  - 系统会通过 PendingIntent 返回「已交给运营商」的结果，能可靠回执。
 */
object SmsSender {

    data class Result(val ok: Boolean, val detail: String)

    data class SimInfo(
        val slotIndex: Int,
        val subscriptionId: Int,
        val carrierName: String,
        val number: String,
    ) {
        val slotLabel: String get() = "卡${slotIndex + 1}"
    }

    /** 读取当前激活的 SIM 卡（需要 READ_PHONE_STATE 权限）。 */
    @SuppressLint("MissingPermission")
    fun listSims(ctx: Context): List<SimInfo> {
        if (ctx.checkSelfPermission(Manifest.permission.READ_PHONE_STATE)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return emptyList()
        }
        return try {
            val manager = ctx.getSystemService(Context.TELEPHONY_SUBSCRIPTION_SERVICE)
                    as SubscriptionManager
            val list = manager.activeSubscriptionInfoList ?: return emptyList()
            list.sortedBy { it.simSlotIndex }.map { info ->
                SimInfo(
                    slotIndex = info.simSlotIndex,
                    subscriptionId = info.subscriptionId,
                    carrierName = info.carrierName?.toString().orEmpty(),
                    number = readNumber(ctx, manager, info),
                )
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    /**
     * 逐张 SIM 尝试所有公开号码接口。不同厂商只开放其中一部分接口，
     * 必须按 subscriptionId 读取，避免双卡时都拿到默认卡的号码。
     */
    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    private fun readNumber(
        ctx: Context,
        subscriptions: SubscriptionManager,
        info: SubscriptionInfo,
    ): String {
        val candidates = mutableListOf<String>()
        candidates.add(info.number.orEmpty())

        if (Build.VERSION.SDK_INT >= 31) {
            runCatching { subscriptions.getPhoneNumber(info.subscriptionId) }
                .getOrNull()?.let(candidates::add)
        }

        val telephony = ctx.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
        runCatching {
            telephony.createForSubscriptionId(info.subscriptionId).line1Number
        }.getOrNull()?.let(candidates::add)

        // 部分旧机型的分卡 TelephonyManager 仍返回空，默认接口可作为卡1的补充。
        if (info.simSlotIndex == 0) {
            runCatching { telephony.line1Number }.getOrNull()?.let(candidates::add)
        }

        return candidates.asSequence()
            .map(::normalizePhone)
            .firstOrNull { it.isNotBlank() }
            .orEmpty()
    }

    /**
     * 按任务里的 send_phone 选择对应的 SIM 卡并发送。
     *
     * 选卡顺序：
     *  1. 任务号码 == 配置的卡1/卡2 号码 → 对应卡槽；
     *  2. 若系统能读到卡号，优先按卡号匹配（防止卡槽顺序与用户认知不一致）；
     *  3. 只有一张卡时直接用它（并在日志里说明）。
     */
    fun send(ctx: Context, cfg: AppConfig, task: ServerClient.Task): Result {
        if (ctx.checkSelfPermission(Manifest.permission.SEND_SMS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return Result(false, "未授予「发送短信」权限，请在系统设置中允许")
        }
        if (task.recipient.isBlank() || task.content.isBlank()) {
            return Result(false, "任务内容为空")
        }

        val taskPhone = normalizePhone(task.sendPhone)
        val sim1 = normalizePhone(cfg.sim1)
        val sim2 = normalizePhone(cfg.sim2)

        val configuredSlot = when {
            sim1.isNotEmpty() && sim1 == taskPhone -> 0
            sim2.isNotEmpty() && sim2 == taskPhone -> 1
            else -> -1
        }
        if (configuredSlot < 0) {
            return Result(
                false,
                "任务号码 ${task.sendPhone} 与本机配置（卡1=$sim1 卡2=$sim2）不匹配",
            )
        }

        val sims = listSims(ctx)
        if (sims.isEmpty()) {
            return Result(false, "读取不到 SIM 卡，请授予「电话/读取设备状态」权限后重试")
        }

        val sim = sims.firstOrNull { normalizePhone(it.number) == taskPhone }
            ?: sims.firstOrNull { it.slotIndex == configuredSlot }
            ?: sims.singleOrNull()

        if (sim == null) {
            val found = sims.joinToString("、") { "${it.slotLabel}(subId=${it.subscriptionId})" }
            return Result(false, "未找到卡${configuredSlot + 1}；当前识别到：$found")
        }

        RuntimeLog.log(
            "开始发送：${sim.slotLabel} subId=${sim.subscriptionId} " +
                "收件人=${task.recipient} 内容=${task.content}",
        )
        return doSend(ctx, sim, task)
    }

    @SuppressLint("MissingPermission")
    private fun doSend(ctx: Context, sim: SimInfo, task: ServerClient.Task): Result {
        val action = "com.qianwen.sendsms.SMS_SENT.${System.currentTimeMillis()}.${task.id}"
        val manager: SmsManager = try {
            if (Build.VERSION.SDK_INT >= 31) {
                ctx.getSystemService(SmsManager::class.java)
                    .createForSubscriptionId(sim.subscriptionId)
            } else {
                SmsManager.getSmsManagerForSubscriptionId(sim.subscriptionId)
            }
        } catch (exc: Exception) {
            return Result(false, "初始化 SmsManager 失败：${exc.message}")
        }
        val parts: ArrayList<String> = try {
            manager.divideMessage(task.content)
        } catch (exc: Exception) {
            return Result(false, "拆分短信内容失败：${exc.message}")
        }
        if (parts.isEmpty()) return Result(false, "短信内容为空")

        val latch = CountDownLatch(parts.size)
        val errors = mutableListOf<Int>()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                // 发送结果在广播的 resultCode 里（不是 Intent 的字段）
                val code = resultCode
                synchronized(errors) {
                    if (code != Activity.RESULT_OK) errors.add(code)
                }
                latch.countDown()
            }
        }

        val filter = IntentFilter(action)
        val registered = try {
            if (Build.VERSION.SDK_INT >= 33) {
                ctx.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                ctx.registerReceiver(receiver, filter)
            }
            true
        } catch (_: Exception) {
            false
        }
        if (!registered) return Result(false, "注册短信结果广播失败")

        return try {
            val sentIntents = ArrayList<PendingIntent>(parts.size)
            for (i in parts.indices) {
                val intent = Intent(action).setPackage(ctx.packageName)
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                sentIntents.add(
                    PendingIntent.getBroadcast(ctx, 1000 + i, intent, flags),
                )
            }

            if (parts.size == 1) {
                manager.sendTextMessage(task.recipient, null, task.content, sentIntents[0], null)
            } else {
                manager.sendMultipartTextMessage(task.recipient, null, parts, sentIntents, null)
            }

            val finished = latch.await(90, TimeUnit.SECONDS)
            when {
                !finished -> Result(false, "等待运营商回执超时（90s）")
                errors.isNotEmpty() -> Result(
                    false,
                    "发送失败：${describe(errors.first())}（${sim.slotLabel}）",
                )
                else -> Result(true, "短信已发送（${sim.slotLabel}，subId=${sim.subscriptionId}）")
            }
        } catch (exc: SecurityException) {
            Result(false, "系统拒绝发送短信：${exc.message}")
        } catch (exc: Exception) {
            Result(false, "发送异常：${exc.javaClass.simpleName} ${exc.message ?: ""}")
        } finally {
            try {
                ctx.unregisterReceiver(receiver)
            } catch (_: Exception) {
                // 已经注销/注册失败，忽略
            }
        }
    }

    private fun describe(code: Int): String = when (code) {
        SmsManager.RESULT_ERROR_GENERIC_FAILURE -> "运营商拒绝或网络失败"
        SmsManager.RESULT_ERROR_NO_SERVICE -> "当前无信号/无服务"
        SmsManager.RESULT_ERROR_NULL_PDU -> "短信 PDU 为空"
        SmsManager.RESULT_ERROR_RADIO_OFF -> "飞行模式或射频关闭"
        else -> "错误码 $code"
    }
}
