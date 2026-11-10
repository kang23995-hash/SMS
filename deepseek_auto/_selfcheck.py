# -*- coding: utf-8 -*-
"""
自检脚本：验证千问 VL 连通 + u2 截图不报错（多设备环境）
"""
import sys
sys.path.insert(0, r"c:\Users\ziqin\Desktop\work_files\deepseek_auto")

import uiautomator2 as u2
from captcha_solver import _screen, analyze_screen, _img_to_b64
from config import EMULATOR_ADB_PORT, QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL

print("=" * 60)
print("自检 1: u2 连接 + 截图（多设备环境）")
print("=" * 60)

# 找到正确的 serial
import subprocess
port = EMULATOR_ADB_PORT
r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=15)
print(f"\nadb devices 原始输出:")
print(r.stdout)

devs = [l.split()[0] for l in r.stdout.splitlines()[1:] 
        if "device" in l and "*" not in l and l.strip()]
print(f"在线设备: {devs}")

serial = None
if port and port > 0:
    for s in devs:
        if str(port) in s:
            serial = s
            break
if not serial:
    serial = devs[0] if devs else None
print(f"选中 serial: {serial}")

if not serial:
    print("❌ 没有在线设备，结束")
    sys.exit(1)

d = u2.connect(serial)
print(f"✅ u2 连接成功: {d.info.get('displayWidth')}x{d.info.get('displayHeight')}")

img = _screen(d)
print(f"✅ _screen(d) 成功: {img.size}")

print()
print("=" * 60)
print("自检 2: 千问 VL API 连通性（发一张截图问它有没有弹窗）")
print("=" * 60)
print(f"API Key: {QWEN_API_KEY[:8]}...{QWEN_API_KEY[-4:]}")
print(f"Base URL: {QWEN_BASE_URL}")
print(f"Model: {QWEN_MODEL}")

result = analyze_screen(img)
print(f"\n✅ 千问返回: {result}")

print()
print("=" * 60)
print("全部通过！")
print("=" * 60)
