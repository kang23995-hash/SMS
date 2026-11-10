package com.lan.smsuploader

import android.app.Application
import com.lan.smsuploader.data.Prefs

/**
 * 应用入口，仅负责初始化配置存储。
 *
 * 本工程基于 capcom6/android-sms-gateway (Apache-2.0) 裁剪重构，
 * 仅保留“短信监听 + 局域网 HTTP 上传”能力，详见 NOTICE / LICENSE。
 */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Prefs.init(this)
    }
}
