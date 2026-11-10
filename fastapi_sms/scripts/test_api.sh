#!/usr/bin/env bash
# =====================================================================
#  短信上报接口 —— curl 单接口测试脚本
#
#  用途：不启动安卓 APP，直接验证服务端能否正常收单、去重、鉴权、报错。
#
#  用法（Git Bash / Linux / macOS）：
#     ./scripts/test_api.sh
#     ./scripts/test_api.sh http://192.168.1.100:8000
#     ./scripts/test_api.sh http://192.168.1.100:8000 你的Token   # 服务端开启鉴权时才需要
#
#  脚本会先访问 /health 判断服务端鉴权开关：
#     - 已关闭（默认 API_TOKEN_ENABLED=false）→ 不带 Token，并跳过 401 测试；
#     - 已开启 → 必须提供 Token，否则直接提示退出。
#
#  Windows 用户建议直接跑同目录的 test_api.ps1。
# =====================================================================

set -u

BASE_URL="${1:-http://127.0.0.1:8000}"
TOKEN="${2:-}"

# 没在命令行给 Token 时，尝试从 .env 里读（脚本同级目录的上一级）
if [ -z "$TOKEN" ]; then
    ENV_FILE="$(dirname "$0")/../.env"
    if [ -f "$ENV_FILE" ]; then
        TOKEN="$(grep -E '^API_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r')"
    fi
fi

# ---- 问服务端：现在到底要不要 Token ----
HEALTH_BODY="$(curl -s "$BASE_URL/health" 2>/dev/null || true)"
AUTH_STATE="$(printf '%s' "$HEALTH_BODY" | grep -o '"auth"[^,}]*' | grep -o 'enabled\|disabled' | head -1)"
[ -z "$AUTH_STATE" ] && AUTH_STATE="unknown"

if [ "$AUTH_STATE" = "enabled" ] && [ -z "$TOKEN" ]; then
    echo "× 服务端开启了 X-Token 校验，但未提供 Token。"
    echo "  用法: $0 <BASE_URL> <API_TOKEN>"
    echo "  或把服务端 .env 的 API_TOKEN_ENABLED 改为 false 后重启。"
    exit 1
fi

# 按需拼装 X-Token 请求头（Token 为空时完全不发送）
AUTH_HEADER=()
[ -n "$TOKEN" ] && AUTH_HEADER=(-H "X-Token: $TOKEN")

API="$BASE_URL/api/sms/receive"
PASS=0
FAIL=0

# 每次运行用不同的唯一 ID，避免第二次运行被去重规则挡住
RUN_ID="$(date +%s)$$"

# 发一次请求并把状态码/响应体打出来。额外参数原样透传给 curl。
post() {
    local name="$1"; shift
    local body_file; body_file="$(mktemp)"
    local code
    code="$(curl -s -o "$body_file" -w '%{http_code}' "$@" || echo "000")"
    echo "  -> $name : HTTP $code"
    echo "     响应: $(cat "$body_file")"
    rm -f "$body_file"
}

code_of() { curl -s -o /dev/null -w '%{http_code}' "$@" || echo "000"; }

check() {
    local name="$1" expect="$2" actual="$3"
    if [ "$expect" = "$actual" ]; then
        echo "  [OK]   $name   (HTTP $actual)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $name   (期望 $expect，实际 $actual)"
        FAIL=$((FAIL + 1))
    fi
}

echo "======================================================"
echo " 目标服务: $BASE_URL"
if [ "$AUTH_STATE" = "enabled" ]; then
    echo " 鉴权状态: 已开启，携带 X-Token ${TOKEN:0:4}***（长度 ${#TOKEN}）"
else
    echo " 鉴权状态: 已关闭，不发送 X-Token"
fi
echo "======================================================"

# ---------------------------------------------------------------------
echo
echo "[1] 健康检查"
post "GET /health" "$BASE_URL/health"
check "GET /health" "200" "$(code_of "$BASE_URL/health")"

# ---------------------------------------------------------------------
echo
echo "[2] 正常上报一条短信（预期 200 success）"
post "首次上报" -X POST "$API" \
    -H "Content-Type: application/json" "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d "{
      \"deviceId\": \"curl-test-device\",
      \"simSlot\": 1,
      \"simPhoneNumber\": \"13800138000\",
      \"sender\": \"10690000\",
      \"messageContent\": \"curl 测试短信 $RUN_ID\",
      \"receiveTime\": \"2026-10-12T21:30:05+08:00\",
      \"messageUniqueId\": \"curl-$RUN_ID-0001\"
    }"
CODE="$(code_of -X POST "$API" -H "Content-Type: application/json" \
    "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d "{\"deviceId\":\"curl-test-device\",\"simSlot\":1,\"simPhoneNumber\":\"13800138000\",\"sender\":\"10690000\",\"messageContent\":\"curl 测试短信B $RUN_ID\",\"receiveTime\":\"2026-10-12T21:30:06+08:00\",\"messageUniqueId\":\"curl-$RUN_ID-0002\"}")"
check "第二条上报" "200" "$CODE"

# ---------------------------------------------------------------------
echo
echo "[3] 重复上报同一条（预期 200 duplicate ignored，且不会重复入库）"
BODY="$(curl -s -X POST "$API" \
    -H "Content-Type: application/json" "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d "{\"deviceId\":\"curl-test-device\",\"simSlot\":1,\"simPhoneNumber\":\"13800138000\",\"sender\":\"10690000\",\"messageContent\":\"curl 测试短信 $RUN_ID\",\"receiveTime\":\"2026-10-12T21:30:05+08:00\",\"messageUniqueId\":\"curl-$RUN_ID-0001\"}")"
echo "  -> 响应: $BODY"
if echo "$BODY" | grep -q "duplicate"; then
    echo "  [OK]   重复消息被识别并忽略"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] 重复消息未被识别"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------
echo
if [ "$AUTH_STATE" = "enabled" ]; then
    echo "[4] 鉴权失败（预期 401）"
    post "不带 X-Token" -X POST "$API" -H "Content-Type: application/json" \
        -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"2026-10-12T21:30:05","messageUniqueId":"no-token-1"}'
    check "缺 Token" "401" "$(code_of -X POST "$API" -H 'Content-Type: application/json' -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"2026-10-12T21:30:05","messageUniqueId":"no-token-2"}')"

    post "错误 X-Token" -X POST "$API" -H "Content-Type: application/json" -H "X-Token: wrong-token" \
        -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"2026-10-12T21:30:05","messageUniqueId":"bad-token-1"}'
    check "错误 Token" "401" "$(code_of -X POST "$API" -H 'Content-Type: application/json' -H 'X-Token: wrong-token' -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"2026-10-12T21:30:05","messageUniqueId":"bad-token-2"}')"
else
    echo "[4] 鉴权测试 —— 已跳过"
    echo "    服务端 API_TOKEN_ENABLED=false，上报接口不校验 X-Token。"
    echo "    想测这一项：把服务端 .env 的 API_TOKEN_ENABLED 改为 true 重启后再跑。"
fi

# ---------------------------------------------------------------------
echo
echo "[5] 报文校验（预期 422）"
post "字段不全" -X POST "$API" \
    -H "Content-Type: application/json" "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d '{"deviceId":"x","simSlot":1}'
check "字段缺失" "422" "$(code_of -X POST "$API" -H 'Content-Type: application/json' "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" -d '{"deviceId":"x"}')"

post "非法 JSON" -X POST "$API" \
    -H "Content-Type: application/json" "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d '{这不是合法 JSON'
check "非法 JSON" "422" "$(code_of -X POST "$API" -H 'Content-Type: application/json' "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" -d '{这不是合法 JSON')"

post "时间格式不合法" -X POST "$API" \
    -H "Content-Type: application/json" "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" \
    -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"昨天","messageUniqueId":"bad-time-1"}'
check "时间非法" "422" "$(code_of -X POST "$API" -H 'Content-Type: application/json' "${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"}" -d '{"deviceId":"x","simSlot":1,"simPhoneNumber":"","sender":"1","messageContent":"x","receiveTime":"昨天","messageUniqueId":"bad-time-2"}')"

# ---------------------------------------------------------------------
echo
echo "======================================================"
echo " 结果：通过 $PASS 项，失败 $FAIL 项"
echo " 提示：成功入库的短信可以在浏览器打开 $BASE_URL/ 查看"
echo "======================================================"
[ "$FAIL" -eq 0 ] || exit 1
