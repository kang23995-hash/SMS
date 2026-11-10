package com.lan.smsuploader

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.lan.smsuploader.data.Prefs
import com.lan.smsuploader.databinding.ActivityMainBinding
import com.lan.smsuploader.net.Uploader
import com.lan.smsuploader.service.SmsMonitorService
import com.lan.smsuploader.util.AppEvents
import com.lan.smsuploader.util.Notifier
import com.lan.smsuploader.util.SimUtils
import kotlinx.coroutines.launch

/**
 * 应用唯一页面。包含四块：
 *  1. 电脑 FastAPI 服务地址输入框（自动保存到 SharedPreferences）；
 *  2. "检测手机号"按钮，读取卡槽 1/2 号码，读不到可手动填写；
 *  3. "开启监测 / 停止监测"切换按钮；
 *  4. 实时状态日志区。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    // 短信监测相关权限申请完成后，继续启动监测
    private val smsPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { proceedAfterPermissions() }

    // 电话权限申请完成后，重新检测手机号
    private val phonePermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { detectPhoneNumbers() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        loadPrefs()
        bindActions()
        observeEvents()
        renderMonitoringState()
    }

    override fun onResume() {
        super.onResume()
        renderMonitoringState()
    }

    // ------------------------------------------------------------ 初始化

    private fun loadPrefs() {
        binding.etServer.setText(Prefs.serverAddress)
        binding.etSim1.setText(Prefs.simNumberForSlot(1).orEmpty())
        binding.etSim2.setText(Prefs.simNumberForSlot(2).orEmpty())
    }

    private fun bindActions() {
        // 服务器地址实时持久化
        binding.etServer.doAfterTextChanged { Prefs.serverAddress = it?.toString().orEmpty() }

        // 卡槽手机号实时持久化（手动填写场景）
        binding.etSim1.doAfterTextChanged { Prefs.setSimNumber(1, it?.toString()) }
        binding.etSim2.doAfterTextChanged { Prefs.setSimNumber(2, it?.toString()) }

        binding.btnDetectPhone.setOnClickListener { onDetectClicked() }

        binding.btnToggleMonitor.setOnClickListener {
            saveInputs()
            if (Prefs.monitoringEnabled) stopMonitoring() else startMonitoring()
        }

        binding.btnClearLog.setOnClickListener {
            AppEvents.clearLogs()
            renderLogs(emptyList())
        }
    }

    // ------------------------------------------------------------ 手机号检测

    private fun onDetectClicked() {
        if (!hasAll(phonePermissions())) {
            phonePermissionLauncher.launch(phonePermissions())
            return
        }
        detectPhoneNumbers()
    }

    private fun detectPhoneNumbers() {
        if (!SimUtils.hasPhonePermission(this)) {
            binding.tvSimResult.setText(R.string.sim_result_none)
            AppEvents.log(getString(R.string.log_permission_denied))
            return
        }

        val slots = SimUtils.readActiveSlots(this)
        if (slots.isEmpty()) {
            binding.tvSimResult.setText(R.string.sim_result_none)
            AppEvents.log(getString(R.string.log_detect_failed))
            return
        }

        val sb = StringBuilder()
        slots.forEach { slot ->
            val number = slot.phoneNumber
            if (number != null) {
                Prefs.setSimNumber(slot.slotNumber, number)
                when (slot.slotNumber) {
                    1 -> binding.etSim1.setText(number)
                    2 -> binding.etSim2.setText(number)
                }
                sb.append(getString(R.string.sim_result_line, slot.slotNumber, number))
            } else {
                sb.append(
                    getString(
                        R.string.sim_result_line,
                        slot.slotNumber,
                        getString(R.string.sim_number_read_failed),
                    )
                )
            }
            sb.append('\n')
        }

        binding.tvSimResult.text = sb.toString().trim()
        AppEvents.log(getString(R.string.log_detect_ok, slots.size))
    }

    // ------------------------------------------------------------ 监测开关

    private fun startMonitoring() {
        val address = binding.etServer.text.toString()
        if (Uploader.normalize(address) == null) {
            AppEvents.log(getString(R.string.log_server_invalid))
            Notifier.toast(this, getString(R.string.log_server_invalid))
            return
        }
        Prefs.serverAddress = address

        // 立即更新状态，避免按钮与服务状态不同步
        Prefs.monitoringEnabled = true
        renderMonitoringState()

        if (!hasAll(monitoringPermissions())) {
            smsPermissionLauncher.launch(monitoringPermissions())
            return
        }
        proceedAfterPermissions()
    }

    private fun proceedAfterPermissions() {
        // 短信权限是硬性要求；电话/通知权限缺失只降级，不阻断监测
        if (!hasAll(requiredSmsPermissions())) {
            Prefs.monitoringEnabled = false
            renderMonitoringState()
            AppEvents.log(getString(R.string.log_permission_denied))
            Notifier.toast(this, getString(R.string.log_permission_denied))
            return
        }

        try {
            SmsMonitorService.start(this)
        } catch (e: Exception) {
            Prefs.monitoringEnabled = false
            renderMonitoringState()
            AppEvents.log("启动服务失败：${e.message}")
            return
        }
        AppEvents.log(getString(R.string.log_server_saved, Prefs.serverAddress))
        renderMonitoringState()
        maybePromptBatteryOptimization()
    }

    private fun stopMonitoring() {
        // 立即更新状态，避免按钮与服务状态不同步
        Prefs.monitoringEnabled = false
        SmsMonitorService.stop(this)
        AppEvents.log(getString(R.string.log_monitoring_stopped))
        renderMonitoringState()
    }

    private fun renderMonitoringState() {
        val isMonitoring = Prefs.monitoringEnabled
        binding.btnToggleMonitor.setText(
            if (isMonitoring) R.string.btn_stop_monitor else R.string.btn_start_monitor
        )
        // 监测中时使用醒目的红色实心，停止时使用红色描边
        if (isMonitoring) {
            binding.btnToggleMonitor.setBackgroundColor(
                ContextCompat.getColor(this, R.color.primary)
            )
        } else {
            binding.btnToggleMonitor.setBackgroundColor(
                ContextCompat.getColor(this, R.color.primary)
            )
        }
    }

    private fun saveInputs() {
        Prefs.serverAddress = binding.etServer.text.toString()
        Prefs.setSimNumber(1, binding.etSim1.text.toString())
        Prefs.setSimNumber(2, binding.etSim2.text.toString())
    }

    // ------------------------------------------------------------ 电池优化引导

    private fun maybePromptBatteryOptimization() {
        if (Prefs.batteryPrompted) return
        Prefs.batteryPrompted = true

        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (powerManager.isIgnoringBatteryOptimizations(packageName)) return

        AlertDialog.Builder(this)
            .setTitle(R.string.battery_dialog_title)
            .setMessage(R.string.battery_dialog_message)
            .setPositiveButton(R.string.battery_dialog_ok) { _, _ -> openBatterySettings() }
            .setNegativeButton(R.string.battery_dialog_cancel, null)
            .show()
    }

    @SuppressLint("BatteryLife")
    private fun openBatterySettings() {
        val direct = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:$packageName"))
        val fallback = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)

        runCatching { startActivity(direct) }
            .onFailure { runCatching { startActivity(fallback) } }
    }

    // ------------------------------------------------------------ 事件订阅

    private fun observeEvents() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch { AppEvents.logs.collect { renderLogs(it) } }
                launch { AppEvents.events.collect { showEvent(it) } }
            }
        }
    }

    private fun renderLogs(lines: List<AppEvents.LogLine>) {
        binding.tvLog.text = lines.joinToString("\n") { "${it.time}  ${it.text}" }
        binding.svLog.post { binding.svLog.fullScroll(View.FOCUS_DOWN) }
    }

    private fun showEvent(event: AppEvents.UiEvent) {
        if (isFinishing || isDestroyed) return
        AlertDialog.Builder(this)
            .setTitle(event.title)
            .setMessage(
                when (event) {
                    is AppEvents.UiEvent.NewSms -> event.detail
                    is AppEvents.UiEvent.Success -> event.detail
                    is AppEvents.UiEvent.Timeout -> event.detail
                    is AppEvents.UiEvent.Error -> event.detail
                }
            )
            .setPositiveButton(android.R.string.ok, null)
            .show()
    }

    // ------------------------------------------------------------ 权限工具

    /** 监测所需申请的全部权限（含可降级的）。 */
    private fun monitoringPermissions(): Array<String> =
        (requiredSmsPermissions().toList() + optionalPermissions()).toTypedArray()

    /** 缺少即无法监测的核心权限。 */
    private fun requiredSmsPermissions(): Array<String> = arrayOf(
        Manifest.permission.RECEIVE_SMS,
        Manifest.permission.READ_SMS,
    )

    /** 缺失只会降级（拿不到卡槽/号码、看不到通知），不阻断监测。 */
    private fun optionalPermissions(): List<String> = buildList {
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.READ_PHONE_NUMBERS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            add(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun phonePermissions(): Array<String> = arrayOf(
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_PHONE_NUMBERS,
    )

    private fun hasAll(permissions: Array<String>): Boolean = permissions.all { granted(it) }

    private fun granted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED
}
