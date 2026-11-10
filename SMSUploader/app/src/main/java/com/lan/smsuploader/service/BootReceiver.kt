package com.lan.smsuploader.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.lan.smsuploader.R
import com.lan.smsuploader.data.Prefs
import com.lan.smsuploader.util.AppEvents

/**
 * 开机自恢复：若用户此前开启过监测，则开机后自动重新拉起前台服务，
 * 减少“重启后忘记开启”导致漏收短信的情况。
 *
 * 若监测处于关闭状态，则不做任何事。
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED &&
            action != ACTION_QUICKBOOT
        ) {
            return
        }

        if (!Prefs.monitoringEnabled) return

        AppEvents.log(context.getString(R.string.log_boot_restore))
        SmsMonitorService.start(context)
    }

    companion object {
        /** 部分厂商（HTC/OPPO 等）的快速开机广播。 */
        private const val ACTION_QUICKBOOT = "android.intent.action.QUICKBOOT_POWERON"
    }
}
