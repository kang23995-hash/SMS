package com.qianwen.sendsms

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * 配置页：
 *  - 填 fastapi_sms 服务地址（电脑局域网 IP:8000）
 *  - 填本机设备名（可选）与两张 SIM 卡的手机号
 *  - 开启/停止监收，实时看状态与日志
 */
class MainActivity : Activity() {

    companion object {
        private const val PERMISSION_REQUEST = 100
        private const val ACTION_START = "start"
        private const val ACTION_DETECT = "detect"
    }

    private lateinit var etServer: EditText
    private lateinit var etName: EditText
    private lateinit var etSim1: EditText
    private lateinit var etSim2: EditText
    private lateinit var etToken: EditText
    private lateinit var btnToggle: Button
    private lateinit var tvStatus: TextView
    private lateinit var tvSimStatus: TextView
    private lateinit var tvLog: TextView
    private lateinit var logScroll: ScrollView

    private var pendingAction: String? = null
    private val uiHandler = Handler(Looper.getMainLooper())
    private val logLines = ArrayDeque<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        loadConfigIntoUi()
        renderState(ServiceState.running, ServiceState.status)
        refreshLog(RuntimeLog.snapshot())
        maybeAutofillSims()
    }

    override fun onStart() {
        super.onStart()
        RuntimeLog.listener = { line ->
            uiHandler.post { appendLog(line) }
        }
        ServiceState.listener = { running, status ->
            uiHandler.post { renderState(running, status) }
        }
        renderState(ServiceState.running, ServiceState.status)
        refreshLog(RuntimeLog.snapshot())
        // 页面重新打开时也检查一次，避免服务异常退出后一直等待系统调度。
        if (AppConfig.isMonitoringEnabled(this) && !ServiceState.running) {
            runCatching { SmsSendService.start(this) }
                .onFailure { RuntimeLog.log("恢复监收失败：${it.message}") }
        }
    }

    override fun onStop() {
        RuntimeLog.listener = null
        ServiceState.listener = null
        super.onStop()
    }

    // =================================================================
    //  界面
    // =================================================================

    private fun buildUi() {
        val scroll = ScrollView(this)
        scroll.setBackgroundColor(Color.parseColor("#F5F6FA"))

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(18), dp(16), dp(28))
        }
        scroll.addView(
            content,
            ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ),
        )
        setContentView(scroll)

        content.addView(titleView("短信代发"))
        content.addView(
            hintView(
                "把这台手机变成短信发送端：填写服务器地址与两张卡的手机号，" +
                    "点「开启监收」后，fastapi_sms 会把 qianwen_auto 的验证短信任务派发过来。",
            ),
        )

        // ---- 服务器 ----
        val serverBox = section("服务器")
        etServer = field("电脑局域网IP:8000（无需输入 http://）", InputType.TYPE_TEXT_VARIATION_URI)
        etName = field("设备名（默认本机型号）", InputType.TYPE_CLASS_TEXT)
        etToken = field("通信令牌（服务端未启用可留空）", InputType.TYPE_CLASS_TEXT)
        serverBox.addView(fieldLabel("fastapi_sms 地址"))
        serverBox.addView(etServer)
        serverBox.addView(fieldLabel("设备名称"))
        serverBox.addView(etName)
        serverBox.addView(fieldLabel("X-Token（可选）"))
        serverBox.addView(etToken)
        content.addView(serverBox)

        // ---- SIM ----
        val simBox = section("双卡手机号（点击检测，自动识别卡1和卡2）")
        etSim1 = field("卡1 手机号，例如 17367050162", InputType.TYPE_CLASS_PHONE)
        etSim2 = field("卡2 手机号（没有就留空）", InputType.TYPE_CLASS_PHONE)
        simBox.addView(fieldLabel("卡1"))
        simBox.addView(etSim1)
        simBox.addView(fieldLabel("卡2"))
        simBox.addView(etSim2)

        val simRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(10), 0, 0)
        }
        val btnDetect = smallButton("检测SIM卡") { ensurePermissions(ACTION_DETECT) }
        simRow.addView(
            btnDetect,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
        )
        val spacer = View(this)
        simRow.addView(spacer, LinearLayout.LayoutParams(dp(8), 1))
        val btnTest = smallButton("测试连接") { testConnection() }
        simRow.addView(
            btnTest,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
        )
        simBox.addView(simRow)
        tvSimStatus = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(Color.parseColor("#6B7280"))
            setPadding(dp(2), dp(10), dp(2), 0)
            setLineSpacing(0f, 1.2f)
        }
        simBox.addView(tvSimStatus)
        content.addView(simBox)

        // ---- 开关 ----
        btnToggle = Button(this).apply {
            isAllCaps = false
            text = "开启监收"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            setTextColor(Color.WHITE)
            typeface = Typeface.DEFAULT_BOLD
            background = rounded("#3B6EF6", 14)
            setOnClickListener {
                if (ServiceState.running) stopMonitoring() else ensurePermissions(ACTION_START)
            }
        }
        content.addView(
            btnToggle,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(52),
            ).apply { topMargin = dp(18) },
        )

        tvStatus = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setTextColor(Color.parseColor("#3B6EF6"))
            setPadding(dp(4), dp(10), dp(4), dp(4))
        }
        content.addView(tvStatus)

        // ---- 日志 ----
        val logHeader = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        logHeader.addView(
            sectionTitle("运行日志"),
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
        )
        logHeader.addView(smallButton("一键清空") {
            RuntimeLog.clear()
            refreshLog(emptyList())
            toast("运行日志已清空")
        })
        content.addView(logHeader)
        logScroll = ScrollView(this).apply {
            setBackgroundColor(Color.WHITE)
            setPadding(dp(10), dp(10), dp(10), dp(10))
        }
        tvLog = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11.5f)
            setTextColor(Color.parseColor("#2A2F3A"))
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }
        logScroll.addView(tvLog)
        content.addView(
            logScroll,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(240),
            ).apply { topMargin = dp(4) },
        )
    }

    private fun titleView(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 24f)
        setTextColor(Color.parseColor("#1B2233"))
        typeface = Typeface.DEFAULT_BOLD
    }

    private fun hintView(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f)
        setTextColor(Color.parseColor("#6B7280"))
        setPadding(0, dp(6), 0, dp(2))
        setLineSpacing(0f, 1.25f)
    }

    private fun sectionTitle(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        setTextColor(Color.parseColor("#1B2233"))
        typeface = Typeface.DEFAULT_BOLD
        setPadding(dp(4), dp(16), dp(4), dp(6))
    }

    private fun section(title: String): LinearLayout {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(14), dp(14), dp(14))
            background = rounded("#FFFFFF", 14)
        }
        box.addView(TextView(this).apply {
            text = title
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13.5f)
            setTextColor(Color.parseColor("#1B2233"))
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, 0, 0, dp(8))
        })
        return box
    }

    private fun fieldLabel(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
        setTextColor(Color.parseColor("#6B7280"))
        setPadding(dp(2), dp(8), 0, dp(4))
    }

    private fun field(hint: String, inputType: Int): EditText = EditText(this).apply {
        this.hint = hint
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        setSingleLine(true)
        this.inputType = inputType
        setTextColor(Color.parseColor("#111827"))
        setHintTextColor(Color.parseColor("#9CA3AF"))
        setPadding(dp(12), dp(10), dp(12), dp(10))
        background = rounded("#F1F3F8", 10)
    }

    private fun smallButton(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
        setTextColor(Color.parseColor("#3B6EF6"))
        background = rounded("#E8EEFF", 10)
        setOnClickListener { onClick() }
    }

    private fun rounded(color: String, radiusDp: Int): GradientDrawable = GradientDrawable().apply {
        setColor(Color.parseColor(color))
        cornerRadius = dp(radiusDp).toFloat()
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    // =================================================================
    //  配置读写
    // =================================================================

    private fun loadConfigIntoUi() {
        val cfg = AppConfig.load(this)
        etServer.setText(cfg.serverUrl)
        etName.setText(cfg.deviceName.ifBlank { DeviceIdentity.defaultName() })
        etSim1.setText(cfg.sim1)
        etSim2.setText(cfg.sim2)
        etToken.setText(cfg.token)
    }

    private fun readConfig(): AppConfig = AppConfig(
        serverUrl = AppConfig.normalizeServerUrl(etServer.text.toString()),
        deviceName = etName.text.toString().trim(),
        sim1 = normalizePhone(etSim1.text.toString()),
        sim2 = normalizePhone(etSim2.text.toString()),
        token = etToken.text.toString().trim(),
    )

    private fun saveConfig(): Boolean {
        val cfg = readConfig()
        if (!AppConfig.isValidServerUrl(cfg.serverUrl)) {
            toast("请输入正确的地址和端口，例如 192.168.1.10:8000")
            return false
        }
        if (cfg.sim1.isBlank() && cfg.sim2.isBlank()) {
            toast("至少填写一张卡的手机号")
            return false
        }
        if (cfg.sim1.isNotBlank() && cfg.sim1.length != 11) {
            toast("卡1 手机号应为 11 位")
            return false
        }
        if (cfg.sim2.isNotBlank() && cfg.sim2.length != 11) {
            toast("卡2 手机号应为 11 位")
            return false
        }
        etServer.setText(cfg.baseUrl)
        AppConfig.save(this, cfg)
        return true
    }

    // =================================================================
    //  操作
    // =================================================================

    private fun ensurePermissions(action: String) {
        val missing = requiredPermissions(action).filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            perform(action)
            return
        }
        pendingAction = action
        requestPermissions(missing.toTypedArray(), PERMISSION_REQUEST)
    }

    private fun requiredPermissions(action: String): List<String> {
        val list = mutableListOf(Manifest.permission.READ_PHONE_STATE)
        if (action == ACTION_START) list.add(Manifest.permission.SEND_SMS)
        if (Build.VERSION.SDK_INT >= 26) list.add(Manifest.permission.READ_PHONE_NUMBERS)
        if (action == ACTION_START && Build.VERSION.SDK_INT >= 33) {
            list.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        return list
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != PERMISSION_REQUEST) return

        val action = pendingAction
        pendingAction = null
        if (action == null) return

        val missing = requiredPermissions(action).filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            val names = missing.joinToString("、") { it.substringAfterLast('.') }
            RuntimeLog.log("权限未授予：$names")
            toast("缺少权限：$names")
            return
        }
        if (action != null) perform(action)
    }

    private fun perform(action: String) {
        when (action) {
            ACTION_START -> startMonitoring()
            ACTION_DETECT -> detectSims()
        }
    }

    private fun startMonitoring() {
        if (!saveConfig()) return

        val cfg = readConfig()
        val intent = Intent(this, SmsSendService::class.java)
            .setAction(SmsSendService.ACTION_START)
        try {
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(intent) else startService(intent)
        } catch (exc: Exception) {
            RuntimeLog.log("启动监收失败：${exc.message}")
            toast("启动失败：${exc.message}")
            return
        }
        renderState(true, "启动中…")
        RuntimeLog.log(
            "请求开启监收：${cfg.baseUrl} 卡1=${cfg.sim1.ifBlank { "-" }} " +
                "卡2=${cfg.sim2.ifBlank { "-" }}",
        )
    }

    private fun stopMonitoring() {
        AppConfig.setMonitoringEnabled(this, false)
        SmsSendService.cancelRestart(this)
        try {
            val intent = Intent(this, SmsSendService::class.java)
                .setAction(SmsSendService.ACTION_STOP)
            startService(intent)
        } catch (exc: Exception) {
            RuntimeLog.log("停止监收失败：${exc.message}")
        }
        renderState(false, "正在停止…")
    }

    private fun detectSims() {
        val sims = SmsSender.listSims(this)
        if (sims.isEmpty()) {
            tvSimStatus.text = "未检测到可用 SIM 卡，请确认已插卡并授予电话权限。"
            RuntimeLog.log("没有读到 SIM 卡：请确认已插卡且已授予电话权限")
            toast("未检测到 SIM 卡")
            return
        }

        val filled = fillDetectedSims(sims, overwrite = true)
        sims.forEach { sim ->
            RuntimeLog.log(
                "${sim.slotLabel}：subId=${sim.subscriptionId} 运营商=${sim.carrierName.ifBlank { "-" }} " +
                    "号码=${sim.number.ifBlank { "系统未提供，请手动填写" }}",
            )
        }
        val missing = sims.count { it.slotIndex in 0..1 && it.number.isBlank() }
        if (missing == 0) {
            toast("已识别 ${sims.size} 张卡，自动填入 $filled 个手机号")
        } else {
            toast("已填入 $filled 个手机号，$missing 张卡需手动补填")
        }
    }

    private fun maybeAutofillSims() {
        if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        if (!etSim1.text.isNullOrBlank() && !etSim2.text.isNullOrBlank()) return
        val sims = SmsSender.listSims(this)
        if (sims.isNotEmpty()) fillDetectedSims(sims, overwrite = false)
    }

    /** 按系统真实卡槽逐槽填充；手动检测时刷新旧值，启动页只补空值。 */
    private fun fillDetectedSims(
        sims: List<SmsSender.SimInfo>,
        overwrite: Boolean,
    ): Int {
        var filled = 0
        sims.forEach { sim ->
            val target = when (sim.slotIndex) {
                0 -> etSim1
                1 -> etSim2
                else -> null
            }
            if (target != null && sim.number.isNotBlank() &&
                (overwrite || target.text.isNullOrBlank())
            ) {
                target.setText(sim.number)
                filled++
            }
        }
        tvSimStatus.text = sims.joinToString("\n") { sim ->
            "${sim.slotLabel}｜${sim.carrierName.ifBlank { "未知运营商" }}｜" +
                sim.number.ifBlank { "系统未提供号码，请手动填写" }
        }
        return filled
    }

    private fun testConnection() {
        val cfg = readConfig()
        if (!AppConfig.isValidServerUrl(cfg.serverUrl)) {
            toast("请输入正确的地址和端口，例如 192.168.1.10:8000")
            return
        }
        RuntimeLog.log("正在测试 ${cfg.baseUrl}/health …")
        thread {
            try {
                val body = ServerClient.health(cfg)
                RuntimeLog.log("连接正常：$body")
            } catch (exc: Exception) {
                RuntimeLog.log("连接失败：${exc.javaClass.simpleName} ${exc.message ?: ""}")
            }
        }
    }

    // =================================================================
    //  状态与日志
    // =================================================================

    private fun renderState(running: Boolean, status: String) {
        btnToggle.text = if (running) "停止监收" else "开启监收"
        btnToggle.background = rounded(if (running) "#D64545" else "#3B6EF6", 14)
        tvStatus.text = "状态：$status"
    }

    private fun refreshLog(lines: List<String>) {
        logLines.clear()
        lines.takeLast(300).forEach { logLines.addLast(it) }
        tvLog.text = logLines.joinToString("\n")
        logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun appendLog(line: String) {
        logLines.addLast(line)
        while (logLines.size > 300) logLines.removeFirst()
        tvLog.text = logLines.joinToString("\n")
        logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }
}
