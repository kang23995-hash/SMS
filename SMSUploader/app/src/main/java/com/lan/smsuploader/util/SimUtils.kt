package com.lan.smsuploader.util

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.SubscriptionInfo
import android.telephony.SubscriptionManager
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat

/** 单个卡槽的信息。slotNumber 为面向用户的卡槽编号：1 或 2。 */
data class SimSlotInfo(
    val slotNumber: Int,
    val phoneNumber: String?,
)

/**
 * SIM 卡槽读取工具（双卡/多卡支持）。
 *
 * 多重回退策略确保在各种机型上都能尽量读到手机号：
 *  1. SubscriptionManager.getActiveSubscriptionInfoList()
 *  2. TelephonyManager.getLine1Number()（兜底）
 *
 * 若所有方式均无法读取号码，返回 null，由界面引导用户手动填写。
 */
object SimUtils {

    private fun subscriptionManager(context: Context): SubscriptionManager? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP_MR1) {
            context.getSystemService(SubscriptionManager::class.java)
        } else {
            null
        }

    private fun telephonyManager(context: Context): TelephonyManager? =
        context.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager

    private fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    /** 是否有读取卡槽/号码所需的基础权限。 */
    fun hasPhonePermission(context: Context): Boolean =
        granted(context, Manifest.permission.READ_PHONE_STATE)

    fun canReadPhoneNumber(context: Context): Boolean =
        granted(context, Manifest.permission.READ_PHONE_STATE) &&
            (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                granted(context, Manifest.permission.READ_PHONE_NUMBERS))

    /**
     * 读取所有活跃卡槽。按卡槽编号升序返回。
     * 无权限 / 无 SIM / 系统不支持时返回空列表。
     */
    @SuppressLint("MissingPermission")
    fun readActiveSlots(context: Context): List<SimSlotInfo> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP_MR1) return emptyList()
        if (!hasPhonePermission(context)) return emptyList()

        val manager = subscriptionManager(context) ?: return emptyList()
        val canReadNumber = canReadPhoneNumber(context)

        // ---- 方式 1: getActiveSubscriptionInfoList ----
        val fromActive: List<SubscriptionInfo>? = try {
            manager.activeSubscriptionInfoList
        } catch (_: SecurityException) {
            null
        } catch (_: Exception) {
            null
        }
        if (!fromActive.isNullOrEmpty()) {
            return fromActive
                .map { info: SubscriptionInfo ->
                    SimSlotInfo(
                        slotNumber = info.simSlotIndex + 1,
                        phoneNumber = if (canReadNumber) resolveNumber(manager, info) else null,
                    )
                }
                .sortedBy { it.slotNumber }
        }

        // ---- 方式 2: TelephonyManager 兜底 ----
        return readSlotsFromTelephony(context)
    }

    /**
     * 使用 TelephonyManager 获取 SIM 卡数量和号码（最终兜底方案）。
     * 仅在有 READ_PHONE_STATE 权限且至少有一张 SIM 卡时返回结果。
     */
    @SuppressLint("MissingPermission")
    private fun readSlotsFromTelephony(context: Context): List<SimSlotInfo> {
        if (!hasPhonePermission(context)) return emptyList()
        val tm = telephonyManager(context) ?: return emptyList()

        val simCount = readSimCount(tm)
        if (simCount <= 0) return emptyList()

        return (1..simCount).map { slot ->
            SimSlotInfo(
                slotNumber = slot,
                phoneNumber = getLine1NumberForSlot(tm, slot),
            )
        }
    }

    /** 读取 SIM 卡数量，兼容多卡设备。 */
    @SuppressLint("MissingPermission")
    private fun readSimCount(tm: TelephonyManager): Int {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                tm.activeModemCount.coerceAtLeast(1)
            } else {
                tm.phoneCount.coerceAtLeast(1)
            }
        } catch (_: Exception) {
            val state = try { tm.simState } catch (_: Exception) { null }
            if (state == TelephonyManager.SIM_STATE_READY) 1 else 0
        }
    }

    /** 通过 TelephonyManager.getLine1Number() 尝试读取指定卡槽的手机号。 */
    @SuppressLint("MissingPermission")
    private fun getLine1NumberForSlot(tm: TelephonyManager, slotNumber: Int): String? {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                tm.createForSubscriptionId(slotNumber).line1Number
            } else {
                tm.line1Number
            }
        } catch (_: Exception) {
            try { tm.line1Number } catch (_: Exception) { null }
        }?.takeIf { it.isNotBlank() }
    }

    /** 根据 subscriptionId 换算出卡槽编号（1/2）。 */
    @SuppressLint("MissingPermission")
    fun getSlotNumberBySubscriptionId(context: Context, subscriptionId: Int): Int? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP_MR1) return null
        if (!hasPhonePermission(context)) return null

        val manager = subscriptionManager(context) ?: return null
        return try {
            manager.activeSubscriptionInfoList
                ?.find { it.subscriptionId == subscriptionId }
                ?.let { it.simSlotIndex + 1 }
        } catch (_: SecurityException) {
            null
        }
    }

    /** 按卡槽编号实时读取本机号码。 */
    @SuppressLint("MissingPermission")
    fun getPhoneNumberBySlot(context: Context, slotNumber: Int): String? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP_MR1) return null
        if (!canReadPhoneNumber(context)) return null

        val manager = subscriptionManager(context) ?: return null
        val fromSub = try {
            manager.activeSubscriptionInfoList
                ?.find { it.simSlotIndex + 1 == slotNumber }
                ?.let { resolveNumber(manager, it) }
        } catch (_: SecurityException) {
            null
        }
        if (!fromSub.isNullOrBlank()) return fromSub

        return getLine1NumberForSlot(
            telephonyManager(context) ?: return null,
            slotNumber,
        )
    }

    /**
     * Android 13+ 优先用 SubscriptionManager.getPhoneNumber()（可从运营商/IMS 获取），
     * 低版本回退到已废弃的 SubscriptionInfo.number。都可能为 null。
     */
    @SuppressLint("MissingPermission")
    private fun resolveNumber(manager: SubscriptionManager, info: SubscriptionInfo): String? {
        val fromSystem = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            runCatching { manager.getPhoneNumber(info.subscriptionId) }.getOrNull()
        } else {
            null
        }
        return fromSystem?.takeIf { it.isNotBlank() }
            ?: @Suppress("DEPRECATION") info.number?.takeIf { it.isNotBlank() }
    }
}
