package com.qianwen.sendsms

/** 服务运行状态，供界面实时展示（同一进程，直接回调即可）。 */
object ServiceState {
    @Volatile
    var running: Boolean = false

    @Volatile
    var status: String = "未开启"

    @Volatile
    var listener: ((Boolean, String) -> Unit)? = null

    fun update(newRunning: Boolean, newStatus: String) {
        running = newRunning
        status = newStatus
        listener?.invoke(newRunning, newStatus)
    }
}
