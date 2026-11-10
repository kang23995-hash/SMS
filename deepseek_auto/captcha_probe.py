# -*- coding: utf-8 -*-
"""
captcha_probe.py —— 人机验证弹窗探针
=====================================
在 DeepSeek 弹出人机验证时运行本脚本，dump 全屏 XML + 截图 + 检测信号诊断。

用法：
  1. 开 DeepSeek 到验证码登录页，点「发送验证码」
  2. 看到人机验证弹窗出现后，立刻在 deepseek_auto 目录下运行：
       python captcha_probe.py
  3. 脚本会在 screenshots/captcha_probe/ 下产出：
       probe_YYYYMMDD_HHMMSS_screen.png    —— 整屏截图
       probe_YYYYMMDD_HHMMSS_hierarchy.xml —— 完整 UI XML
       probe_YYYYMMDD_HHMMSS_report.txt    —— 诊断报告（哪些检测信号命中/没命中）
  4. 把 screenshots/captcha_probe/ 整个目录发出来就能定位
"""
from __future__ import annotations

import os, sys, time, re, logging, subprocess
from datetime import datetime
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import uiautomator2 as u2

# 自动用 config 的端口匹配模拟器
try:
    from config import EMULATOR_ADB_PORT
except Exception:
    EMULATOR_ADB_PORT = 0

PROBE_DIR = Path(__file__).parent / "screenshots" / "captcha_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("probe")


def run_adb(*args):
    return subprocess.run(
        ["adb", *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
    )


def get_serial():
    port = EMULATOR_ADB_PORT
    if port and port > 0:
        try:
            run_adb("connect", f"127.0.0.1:{port}")
        except Exception:
            pass
    r = run_adb("devices")
    devs = [l.split()[0] for l in r.stdout.decode(errors="ignore").splitlines()[1:]
            if "device" in l and "*" not in l and l.strip()]
    if not devs:
        raise RuntimeError("没设备")
    if port and port > 0:
        for s in devs:
            if str(port) in s:
                return s
    return devs[0]


ts = datetime.now().strftime("%Y%m%d_%H%M%S")
log.info(f"[probe] 连接设备: {get_serial()}")
d = u2.connect(get_serial())
log.info(f"[probe] 屏幕: {d.info.get('displayWidth')}x{d.info.get('displayHeight')}")

xml = d.dump_hierarchy(compressed=False)
shot = d.screenshot()

screen_path = PROBE_DIR / f"probe_{ts}_screen.png"
xml_path = PROBE_DIR / f"probe_{ts}_hierarchy.xml"
shot.save(str(screen_path))
xml_path.write_text(xml, encoding="utf-8")
log.info(f"[probe] 已保存: {screen_path.name} / {xml_path.name}")

# === 诊断 ===
report_lines = []
report_lines.append(f"时间: {datetime.now()}\n")
report_lines.append(f"屏幕分辨率: {d.info.get('displayWidth')}x{d.info.get('displayHeight')}\n")
report_lines.append(f"Activity: {d.app_current().get('activity')}\n")

# 1. XML 长度 & 基本统计
report_lines.append(f"\n=== XML 基本信息 ===")
report_lines.append(f"XML 总长: {len(xml)} 字符")
report_lines.append(f"<node> 数量: {xml.count('<node ')}")
report_lines.append(f"<WebView>: {xml.count('WebView')}")
report_lines.append(f"webkit: {xml.count('webkit')}")
report_lines.append(f"<ImageView>: {xml.count('ImageView')}")
report_lines.append(f"<Button>: {xml.count('Button')}")

# 2. 当前 detect_captcha 里的三种信号
QUESTION_PATTERN = re.compile(r"^请?点击.{0,6}(图中|下图|图片中|上图)?.{1,40}$")
CAPTCHA_MARKERS = ("换一批", "安全验证", "人机验证", "完成验证", "行为验证")

report_lines.append(f"\n=== 信号 a: 问题文字匹配 ===")
import xml.etree.ElementTree as ET
try:
    root = ET.fromstring(xml)
    nodes = [{"text": (n.attrib.get("text") or "").strip(),
              "desc": (n.attrib.get("content-desc") or "").strip(),
              "class": n.attrib.get("class", ""),
              "rid": n.attrib.get("resource-id", "")}
             for n in root.iter("node")]
except Exception as e:
    nodes = []
    report_lines.append(f"XML 解析失败: {e}")

question_hits = []
for n in nodes:
    for v in (n["text"], n["desc"]):
        if v and QUESTION_PATTERN.match(v):
            question_hits.append(v)
report_lines.append(f"命中问题文字: {question_hits if question_hits else '(无)'}")

report_lines.append(f"\n=== 信号 b: 辅助标记词 ===")
for m in CAPTCHA_MARKERS:
    hit = m in xml
    report_lines.append(f"  '{m}': {'✅ 在 XML 中' if hit else '❌ 不存在'}")

# 3. 列出所有非空 text/desc（截断显示，帮助发现弹窗文字）
report_lines.append(f"\n=== 所有非空 text/desc（前 80 条）===")
seen = set()
cnt = 0
for n in nodes:
    for v in (n["text"], n["desc"]):
        if v and v not in seen:
            seen.add(v)
            report_lines.append(f"  [{n['class'][:25]:25s}] rid={n['rid'][:30]:30s} text/desc=\"{v}\"")
            cnt += 1
            if cnt >= 80:
                break
    if cnt >= 80:
        break

# 4. resource-id 聚合
report_lines.append(f"\n=== unique resource-id ===")
rids = sorted(set(n["rid"] for n in nodes if n["rid"]))
for r in rids:
    report_lines.append(f"  {r}")

# 5. 弹窗相关关键词全量搜索
ALL_CAPTCHA_KEYWORDS = [
    "验证", "验证码", "人机", "安全", "滑动", "点图", "图片", "点击",
    "换一批", "关闭", "跳过", "刷新", "answer", "captcha", "verify",
    "挑战", "选择", "匹配",
]
report_lines.append(f"\n=== 弹窗相关关键词在 XML 中的出现位置 ===")
for kw in ALL_CAPTCHA_KEYWORDS:
    idx = xml.find(kw)
    if idx >= 0:
        snippet = xml[max(0, idx - 30):idx + len(kw) + 30].replace("\n", " ")
        report_lines.append(f"  '{kw}': ...{snippet}...")

report_text = "\n".join(report_lines)
report_path = PROBE_DIR / f"probe_{ts}_report.txt"
report_path.write_text(report_text, encoding="utf-8")

print(f"\n[probe] ===== 诊断报告 =====")
print(report_text)
print(f"\n[probe] 全部文件在: {PROBE_DIR}")
