# -*- coding: utf-8 -*-
"""
stuck_page_probe.py —— 诊断白屏卡死现场

不走真的验证码流程（需要真实手机号/真码），而是：
  1. 拉起元宝、强制登出、回到登录页
  2. 点「手机登录」→「同意」→ 输入手机号 → 点「下一步」
     （停在"验证码输入页"——真提交验证码后才会触发白屏，
      我们观察填码前的状态，以及可以测试假码的行为）
  3. 等 15 秒，每秒：截屏 + dump XML + 计算白像素率 + 打印状态特征
  4. 最后存一张现场图，看看"卡在登录流程中"时的真实样子

运行结束后，这些诊断会变成登录正式流程的 stuck 检测器。
"""
import sys, time, hashlib, io, os, re
from collections import deque
sys.path.insert(0, r"C:\Users\ziqin\Desktop\work_files\yuanbao_auto")

import uiautomator2 as u2
from PIL import Image
import numpy as np
import cv2

from yuanbao_login import (
    connect_phone, start_yuanbao, logout, is_logged_in,
    _wait_landing, _click_phone_login, _agree_protocol,
    _input_phone, _click_next, dump_xml,
)
from captcha_solver import wait_captcha_and_solve, is_captcha_present

d = connect_phone()
start_yuanbao(d, wait=6)
print("start fg:", d.app_current().get("package"))

# 确保登出
if is_logged_in(d):
    print("先登出...")
    try:
        logout(d)
    except Exception as e:
        print(f"  logout 异常: {e}")
    start_yuanbao(d, wait=6)

# 走登录流程，不填码
if not _wait_landing(d, timeout=20):
    print("❌ 落地页没出来，停")
    sys.exit(1)
_click_phone_login(d); time.sleep(1.0)
_agree_protocol(d); time.sleep(1.0)
_input_phone(d, "18798853401"); time.sleep(0.8)
# 提交一下假验证码试试会不会触发白屏
if _click_next(d):
    print("点了下一步，等了 1s 后看状态")
    time.sleep(1.0)

# 观察 15 秒：每秒一张截图 + XML 哈希 + 白像素率
outdir = r"C:\Users\ziqin\Desktop\work_files\yuanbao_auto\screenshots\stuck_probe"
os.makedirs(outdir, exist_ok=True)
prev_hash = None
hash_changes = 0
white_hist = deque(maxlen=15)

for i in range(15):
    t0 = time.time()
    xml = dump_xml(d)
    xml_hash = hashlib.md5(xml.encode("utf-8")).hexdigest()
    if xml_hash != prev_hash:
        hash_changes += 1
        prev_hash = xml_hash

    shot = d.screenshot()
    arr = np.array(shot.convert("RGB"))
    # 白色/接近白色像素 (R,G,B 都 > 240) 的比例
    white_ratio = float(np.mean(np.all(arr > 240, axis=2)))
    white_hist.append(white_ratio)

    webview_nodes = len(re.findall(r'WebView|webview|android\.webkit', xml, re.I))
    web_nodes = len(re.findall(r'android\.webkit\.WebView', xml))
    edit_nodes = xml.count('class="android.widget.EditText"')
    captcha_markers = [m for m in ("选择最符合描述的图片","换一批","确定") if m in xml]
    login_hints = [m for m in ("登录","手机登录","同意","服务协议","输入验证码","验证码","6 位","短信") if m in xml]
    logged = is_logged_in(d)
    fg = d.app_current().get("package")
    sz = shot.size

    print(f"t={i:2d} fg={fg!r:40s} size={sz} xml={len(xml):6d} hash={xml_hash[:6]} "
          f"white={white_ratio:6.1%} webView={webview_nodes} edit={edit_nodes} "
          f"captcha={captcha_markers or '-'} login={logged} hints={login_hints[:4]}")

    shot.save(os.path.join(outdir, f"t{i:02d}.png"))
    open(os.path.join(outdir, f"t{i:02d}.xml"), "w", encoding="utf-8").write(xml)

    dt = time.time() - t0
    time.sleep(max(0.0, 1.0 - dt))

print("\n=== 15 秒观察总结 ===")
print(f"XML hash 变化次数: {hash_changes}/15")
print(f"白像素率历史: {[round(w,3) for w in white_hist]}")
print(f"最终状态: is_logged_in={is_logged_in(d)}, "
      f"is_captcha_present={is_captcha_present(d)}, fg={d.app_current().get('package')}")
print(f"观察截图与 XML: {outdir}")
