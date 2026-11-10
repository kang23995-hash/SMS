package com.lan.smsuploader.util

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 前台服务与界面之间的进程内通信总线。
 *
 * 由于服务与 Activity 处于同一进程，直接用一个单例即可：
 *  - [logs]   状态日志流，界面订阅后实时展示；
 *  - [events] 一次性 UI 事件（弹窗）流。
 */
object AppEvents {

    data class LogLine(val time: String, val text: String)

    /** 需要弹窗提示的事件类型。 */
    sealed class UiEvent {
        abstract val title: String

        data class NewSms(override val title: String, val detail: String) : UiEvent()
        data class Success(override val title: String, val detail: String) : UiEvent()
        data class Timeout(override val title: String, val detail: String) : UiEvent()
        data class Error(override val title: String, val detail: String) : UiEvent()
    }

    private const val MAX_LINES = 300

    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    private val _logs = MutableStateFlow<List<LogLine>>(emptyList())
    val logs = _logs.asStateFlow()

    private val _events = MutableSharedFlow<UiEvent>(extraBufferCapacity = 32)
    val events = _events.asSharedFlow()

    /** 追加一条日志（线程安全）。 */
    fun log(text: String) = synchronized(this) {
        val line = LogLine(timeFormat.format(Date()), text)
        _logs.value = (_logs.value + line).takeLast(MAX_LINES)
    }

    fun emit(event: UiEvent) {
        _events.tryEmit(event)
    }

    fun clearLogs() {
        _logs.value = emptyList()
    }
}
