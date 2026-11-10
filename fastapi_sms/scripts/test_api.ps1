<#
=====================================================================
 短信上报接口 —— Windows PowerShell 测试脚本

 用途：不启动安卓 APP，直接在 Windows 上验证服务端收单、去重、鉴权、报错。

 用法（Windows 10/11 自带的 Windows PowerShell 5.1 即可）：
     cd D:\UserData\Desktop\SMS_Server\fastapi_sms
     .\scripts\test_api.ps1
     .\scripts\test_api.ps1 -BaseUrl http://192.168.1.100:8000
     .\scripts\test_api.ps1 -Token 你的Token      # 服务端开启鉴权时才需要

 脚本会先访问 /health 判断服务端鉴权开关：
     - 已关闭（默认 API_TOKEN_ENABLED=false）→ 不带 Token，并跳过 401 测试；
     - 已开启 → 必须提供 Token，否则直接提示退出。

 若提示「禁止运行脚本」，临时放开即可：
     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
=====================================================================
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Token = ""
)

$ErrorActionPreference = "Continue"

# 没传 Token 就从上级目录的 .env 里读
if ([string]::IsNullOrWhiteSpace($Token)) {
    $envFile = Join-Path $PSScriptRoot "..\.env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*API_TOKEN\s*=' } | Select-Object -First 1
        if ($line) { $Token = ($line -split '=', 2)[1].Trim() }
    }
}

function Invoke-Raw {
    param([string]$Url, [string]$Method = "GET", [hashtable]$Headers = @{}, [string]$Body = $null)
    try {
        $params = @{
            Uri = $Url; Method = $Method; Headers = $Headers
            UseBasicParsing = $true; TimeoutSec = 15
        }
        if ($Body) { $params["Body"] = $Body; $params["ContentType"] = "application/json" }
        $resp = Invoke-WebRequest @params
        return @{ Code = [int]$resp.StatusCode; Body = [string]$resp.Content }
    }
    catch {
        $r = $_.Exception.Response
        if ($r -ne $null) {
            try {
                $stream = $r.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                return @{ Code = [int]$r.StatusCode; Body = $reader.ReadToEnd() }
            } catch { }
        }
        return @{ Code = 0; Body = $_.Exception.Message }
    }
}

$script:Pass = 0
$script:Fail = 0

function Test-Case {
    param([string]$Name, [int]$Expect, [int]$Actual)
    if ($Expect -eq $Actual) {
        Write-Host ("  [OK]   {0}   (HTTP {1})" -f $Name, $Actual) -ForegroundColor Green
        $script:Pass++
    } else {
        Write-Host ("  [FAIL] {0}   (期望 {1}，实际 {2})" -f $Name, $Expect, $Actual) -ForegroundColor Red
        $script:Fail++
    }
}

function New-Payload {
    param([string]$UniqueId, [string]$DeviceId = "ps-test-device", [string]$Content = "PowerShell 测试短信", [string]$ReceiveTime = "2026-10-12T21:30:05+08:00")
    return (@{
        deviceId         = $DeviceId
        simSlot          = 1
        simPhoneNumber   = "13800138000"
        sender           = "10690000"
        messageContent   = $Content
        receiveTime      = $ReceiveTime
        messageUniqueId  = $UniqueId
    } | ConvertTo-Json -Compress)
}

# ---- 先问服务端：现在到底要不要 Token ----
$health = Invoke-Raw -Url "$BaseUrl/health"
$authState = "unknown"
if ($health.Body -match '"auth"\s*:\s*"(\w+)"') { $authState = $Matches[1] }

if ($authState -eq "enabled" -and [string]::IsNullOrWhiteSpace($Token)) {
    Write-Host "× 服务端开启了 X-Token 校验，但未提供 Token。" -ForegroundColor Red
    Write-Host "  用法: .\test_api.ps1 -BaseUrl <地址> -Token <Token>" -ForegroundColor Red
    Write-Host "  或把服务端 .env 的 API_TOKEN_ENABLED 改为 false 后重启。" -ForegroundColor Red
    exit 1
}

# 按需拼装请求头：Token 为空时完全不发送 X-Token
$authHeaders = @{}
if (-not [string]::IsNullOrWhiteSpace($Token)) { $authHeaders["X-Token"] = $Token }

$Api = "$BaseUrl/api/sms/receive"
$RunId = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

Write-Host "======================================================"
Write-Host " 目标服务: $BaseUrl"
if ($authState -eq "enabled") {
    $mask = $Token.Substring(0, [Math]::Min(4, $Token.Length))
    Write-Host (" 鉴权状态: 已开启，携带 X-Token {0}***（长度 {1}）" -f $mask, $Token.Length)
} else {
    Write-Host " 鉴权状态: 已关闭，不发送 X-Token"
}
Write-Host "======================================================"

Write-Host "`n[1] 健康检查"
Write-Host ("  -> 响应: {0}" -f $health.Body)
Test-Case -Name "GET /health" -Expect 200 -Actual $health.Code

Write-Host "`n[2] 正常上报"
$uid1 = "ps-$RunId-0001"
$r1 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body (New-Payload -UniqueId $uid1 -Content "PS 测试短信 $RunId")
Write-Host ("  -> 响应: {0}" -f $r1.Body)
Test-Case -Name "首次上报" -Expect 200 -Actual $r1.Code

$r2 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body (New-Payload -UniqueId "ps-$RunId-0002" -Content "PS 测试短信B $RunId")
Write-Host ("  -> 响应: {0}" -f $r2.Body)
Test-Case -Name "第二条上报" -Expect 200 -Actual $r2.Code

Write-Host "`n[3] 重复上报（预期 200 duplicate ignored）"
$r3 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body (New-Payload -UniqueId $uid1 -Content "内容改了也不该入库")
Write-Host ("  -> 响应: {0}" -f $r3.Body)
if ($r3.Body -match "duplicate") {
    Write-Host "  [OK]   重复消息被识别并忽略" -ForegroundColor Green; $script:Pass++
} else {
    Write-Host "  [FAIL] 重复消息未被识别" -ForegroundColor Red; $script:Fail++
}

if ($authState -eq "enabled") {
    Write-Host "`n[4] 鉴权失败（预期 401）"
    $r4 = Invoke-Raw -Url $Api -Method POST -Body (New-Payload -UniqueId "ps-$RunId-notoken")
    Write-Host ("  -> 响应: {0}" -f $r4.Body)
    Test-Case -Name "缺少 X-Token" -Expect 401 -Actual $r4.Code

    $r5 = Invoke-Raw -Url $Api -Method POST -Headers @{ "X-Token" = "wrong-token" } -Body (New-Payload -UniqueId "ps-$RunId-badtoken")
    Write-Host ("  -> 响应: {0}" -f $r5.Body)
    Test-Case -Name "错误 X-Token" -Expect 401 -Actual $r5.Code
} else {
    Write-Host "`n[4] 鉴权测试 —— 已跳过"
    Write-Host "    服务端 API_TOKEN_ENABLED=false，上报接口不校验 X-Token。"
    Write-Host "    想测这一项：把服务端 .env 的 API_TOKEN_ENABLED 改为 true 重启后再跑。"
}

Write-Host "`n[5] 报文校验（预期 422）"
$r6 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body '{"deviceId":"x","simSlot":1}'
Write-Host ("  -> 响应: {0}" -f $r6.Body)
Test-Case -Name "字段缺失" -Expect 422 -Actual $r6.Code

$r7 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body '{这不是合法 JSON'
Write-Host ("  -> 响应: {0}" -f $r7.Body)
Test-Case -Name "非法 JSON" -Expect 422 -Actual $r7.Code

$r8 = Invoke-Raw -Url $Api -Method POST -Headers $authHeaders -Body (New-Payload -UniqueId "ps-$RunId-badtime" -ReceiveTime "昨天")
Write-Host ("  -> 响应: {0}" -f $r8.Body)
Test-Case -Name "时间格式非法" -Expect 422 -Actual $r8.Code

Write-Host "`n======================================================"
Write-Host (" 结果：通过 {0} 项，失败 {1} 项" -f $script:Pass, $script:Fail)
Write-Host (" 提示：成功入库的短信可在浏览器打开 {0}/ 查看" -f $BaseUrl)
Write-Host "======================================================"
if ($script:Fail -gt 0) { exit 1 }
