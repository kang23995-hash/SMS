#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""短信上报接口 —— 并发压测 / 多设备同时上报验证脚本。

特点：**只依赖 Python 标准库**，不需要额外装任何包。
用线程池模拟多台安卓手机同时上传，统计成功率、QPS 与延迟分位，
并额外验证「同一条消息在并发下也只入库一次」。

用法示例
--------
# 默认：10 台设备 × 20 条 = 200 条请求，并发 50
python scripts/load_test.py --url http://127.0.0.1:8000

# 20 台设备 × 50 条 = 1000 条请求，并发 100
python scripts/load_test.py --url http://127.0.0.1:8000 \
       --devices 20 --messages-per-device 50 --concurrency 100

# 只跑上报，不跑重复校验
python scripts/load_test.py --no-dedup-phase

脚本会先读 /health 判断服务端鉴权开关：关闭时（默认）不带 X-Token，
开启时才需要传 --token。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# --------------------------------------------------------------------- 参数


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="短信上报接口并发压测（仅用标准库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument(
        "--token",
        default="",
        help="X-Token；服务端开启鉴权（API_TOKEN_ENABLED=true）时才需要，"
        "不传则尝试从 .env 读取",
    )
    parser.add_argument("--devices", type=int, default=10, help="模拟的设备数量")
    parser.add_argument(
        "--messages-per-device", type=int, default=20, help="每台设备上报的消息条数"
    )
    parser.add_argument("--concurrency", type=int, default=50, help="并发线程数")
    parser.add_argument("--timeout", type=float, default=15.0, help="单请求超时（秒）")
    parser.add_argument(
        "--dedup-phase",
        dest="dedup_phase",
        action="store_true",
        default=True,
        help="开启第二阶段：重发一部分相同消息 ID，验证并发下去重仍然生效",
    )
    parser.add_argument(
        "--no-dedup-phase", dest="dedup_phase", action="store_false", help="关闭第二阶段"
    )
    return parser.parse_args()


def load_token_from_env() -> str:
    """从上一级目录的 .env 里读 API_TOKEN。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("API_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------- 请求

_stats_lock = threading.Lock()
_latencies: list[float] = []
_status_counts: Counter = Counter()
_msg_counter: Counter = Counter()
_error_samples: list[str] = []


def post_sms(endpoint: str, token: str, payload: dict, timeout: float) -> tuple[int, str, float]:
    """发一条上报请求，返回 (HTTP 状态码, 响应体, 耗时秒)。

    ``token`` 为空时不发送 ``X-Token`` 头 —— 服务端若关闭了鉴权
    （``API_TOKEN_ENABLED=false``，默认）即可正常收单。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["X-Token"] = token

    request = urllib.request.Request(endpoint, data=data, method="POST", headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            code = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        code = exc.code
    except Exception as exc:  # noqa: BLE001 —— 网络层异常统一记为 0
        body = f"{type(exc).__name__}: {exc}"
        code = 0
    return code, body, time.perf_counter() - started


def record(code: int, body: str, cost: float) -> None:
    """线程安全地累计统计信息。"""
    try:
        msg = json.loads(body).get("msg", "")
    except Exception:  # noqa: BLE001
        msg = ""
    with _stats_lock:
        _latencies.append(cost)
        _status_counts[code] += 1
        _msg_counter[msg or f"HTTP {code}"] += 1
        if code != 200 and len(_error_samples) < 5:
            _error_samples.append(f"HTTP {code} {body[:160]}")


# --------------------------------------------------------------------- 主流程


def build_payload(device_index: int, seq: int, unique_id: str, base_time: datetime) -> dict:
    """构造一条上报报文，字段与安卓 APP 完全一致。"""
    return {
        "deviceId": f"loadtest-device-{device_index:03d}",
        "simSlot": (device_index % 2) + 1,
        "simPhoneNumber": f"138{device_index:08d}"[:11],
        "sender": f"1069{seq:06d}",
        "messageContent": f"压测短信 device={device_index} seq={seq}",
        "receiveTime": (base_time + timedelta(seconds=seq)).isoformat(timespec="seconds"),
        "messageUniqueId": unique_id,
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1))
    return ordered[k]


def fetch_auth_state(base_url: str) -> str:
    """从 /health 读取服务端的鉴权开关状态。

    返回 ``enabled`` / ``disabled`` / ``unknown`` / ``unreachable``。
    """
    health_url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("auth", "unknown")
    except urllib.error.HTTPError as exc:
        # 503（数据库挂了）也是「服务活着」，body 里同样带 auth 字段
        try:
            return json.loads(exc.read().decode("utf-8")).get("auth", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"
    except Exception:  # noqa: BLE001
        return "unreachable"


def main() -> int:
    args = parse_args()
    base = args.url.rstrip("/")
    token = args.token or load_token_from_env()

    # 先探活，顺便问清楚服务端到底要不要 Token
    auth_state = fetch_auth_state(base)
    if auth_state == "unreachable":
        print(f"× 无法连接服务 {args.url}")
        print("  请确认服务已启动、地址正确、.env 里 APP_HOST=0.0.0.0、防火墙已放行。")
        return 2

    if auth_state == "enabled" and not token:
        print("× 服务端开启了 X-Token 校验（/health 显示 auth=enabled），必须提供 Token：")
        print("    python scripts/load_test.py --token 你的Token")
        print("  或把服务端 .env 的 API_TOKEN_ENABLED 改为 false 后重启。")
        return 1

    endpoint = base + "/api/sms/receive"
    total = args.devices * args.messages_per_device

    print("=" * 66)
    print(" 短信上报接口 并发压测")
    print("=" * 66)
    print(f" 目标接口   : {endpoint}")
    print(f" 模拟设备数 : {args.devices} 台")
    print(f" 每台条数   : {args.messages_per_device} 条")
    print(f" 总请求数   : {total} 条")
    print(f" 并发线程   : {args.concurrency}")
    if auth_state == "enabled":
        print(f" 鉴权状态   : 已开启，携带 X-Token {token[:4]}***（长度 {len(token)}）")
    else:
        print(f" 鉴权状态   : 已关闭，不发送 X-Token")
    print("-" * 66)

    base_time = datetime.now().replace(microsecond=0)

    # ---------------- 第一阶段：各设备各上报不同短信 ----------------
    unique_ids: list[str] = []
    jobs: list[tuple[int, int, str]] = []
    for device_index in range(args.devices):
        for seq in range(args.messages_per_device):
            uid = str(uuid.uuid4())
            unique_ids.append(uid)
            jobs.append((device_index, seq, uid))

    print(f"\n[阶段一] {len(jobs)} 条唯一消息并发上报…")
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                post_sms,
                endpoint,
                token,
                build_payload(d, s, u, base_time),
                args.timeout,
            )
            for d, s, u in jobs
        ]
        for future in as_completed(futures):
            code, body, cost = future.result()
            record(code, body, cost)
    wall_cost = time.perf_counter() - wall_start

    phase1_codes = dict(_status_counts)
    phase1_latency = list(_latencies)
    phase1_msgs = dict(_msg_counter)
    success = phase1_codes.get(200, 0)
    failed = sum(c for code, c in phase1_codes.items() if code != 200)

    print(f" 耗时       : {wall_cost:.2f} 秒")
    print(f" 吞吐(QPS)  : {len(jobs) / wall_cost:.1f} 条/秒")
    print(f" 成功(200)  : {success}")
    print(f" 失败       : {failed}")
    if phase1_latency:
        print(
            " 延迟       : "
            f"平均 {statistics.mean(phase1_latency)*1000:.1f}ms  "
            f"P50 {percentile(phase1_latency,50)*1000:.1f}ms  "
            f"P90 {percentile(phase1_latency,90)*1000:.1f}ms  "
            f"P99 {percentile(phase1_latency,99)*1000:.1f}ms  "
            f"最大 {max(phase1_latency)*1000:.1f}ms"
        )
    print(f" 响应分布   : {json.dumps(phase1_msgs, ensure_ascii=False)}")
    if _error_samples:
        print(" 失败样例   :")
        for sample in _error_samples:
            print("   -", sample)

    verdict_ok = failed == 0

    # ---------------- 第二阶段：并发重发相同 ID，验证去重 ----------------
    dedup_ok = True
    if args.dedup_phase:
        replay_count = min(len(unique_ids), max(10, total // 5))
        replay_ids = unique_ids[:replay_count]
        replay_jobs = [
            (i % args.devices, i, uid) for i, uid in enumerate(replay_ids)
        ]

        print(f"\n[阶段二] 并发重发 {len(replay_jobs)} 条「已存在的消息 ID」，验证去重…")
        with _stats_lock:
            _status_counts.clear()
            _msg_counter.clear()
            _latencies.clear()

        replay_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(
                    post_sms,
                    endpoint,
                    token,
                    build_payload(d, s, u, base_time),
                    args.timeout,
                )
                for d, s, u in replay_jobs
            ]
            for future in as_completed(futures):
                code, body, cost = future.result()
                record(code, body, cost)
        replay_cost = time.perf_counter() - replay_start

        codes = dict(_status_counts)
        msgs = dict(_msg_counter)
        all_200 = set(codes.keys()) == {200}
        all_dup = all("duplicate" in m for m in msgs.keys()) if msgs else False
        dedup_ok = all_200 and all_dup

        print(f" 耗时       : {replay_cost:.2f} 秒")
        print(f" 响应分布   : {json.dumps(msgs, ensure_ascii=False)}")
        print(
            " 结论       : "
            + ("全部被识别为重复消息，未产生重复入库 ✔" if dedup_ok else "存在异常，请检查 ×")
        )

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 66)
    if verdict_ok and dedup_ok:
        print(f" ✔ 压测通过：{args.devices} 台设备 × {args.messages_per_device} 条"
              f" = {success} 条全部成功入库")
        print(f"   峰值吞吐 {len(jobs) / wall_cost:.0f} 条/秒"
              f"（真实手机上报频率远低于此，余量充足）")
        print("   建议到 Web 页面核对：总条数应等于上面的成功数 + 之前已有的数据")
        rc = 0
    else:
        print(" × 压测未通过，请查看上面的失败信息与 logs/error_*.log")
        rc = 1
    print("=" * 66)
    return rc


if __name__ == "__main__":
    sys.exit(main())
