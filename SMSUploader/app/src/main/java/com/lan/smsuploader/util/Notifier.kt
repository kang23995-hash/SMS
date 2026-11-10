package com.lan.smsuploader.util

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.widget.Toast

/**
 * 轻量提示工具：把 Toast 抛回主线程执行。
 * 前台服务在后台运行时也能给出即时的文字反馈（弹窗由界面负责）。
 */
object Notifier {

    private val mainHandler = Handler(Looper.getMainLooper())

    fun toast(context: Context, text: String) {
        val appContext = context.applicationContext
        mainHandler.post {
            Toast.makeText(appContext, text, Toast.LENGTH_LONG).show()
        }
    }
}
