# -*- coding: utf-8 -*-
"""探针补全：在当前验证码输入页（已发码但未填），模拟一个"提交后卡住"的时序
——输入假码（或干脆不填直接点确定看会不会报错/卡死），然后连续观测页面变化。

关键是摸清：各阶段的「XML 长度范围」「WebView 节点数」「白像素率」——
这些数字组合起来就是 stuck 检测器的阈值依据。
"""
import sys, time, os, re, hashlib
sys.path.insert(0, r"C:\Users\ziqin\Desktop\work_files\yuanbao_auto")
import uiautomator2 as u2
import numpy as np
from PIL import Image

from yuanbao_login import dump_xml, _input_vcode, _back

# 从 config 读取模拟器端口，兼容环境变量覆盖；未配置则自动取第一台
import subprocess
_port = None
try:
    _env = os.getenv("EMULATOR_ADB_PORT")
    if _env:
        _port = int(_env)
    else:
        from config import EMULATOR_ADB_PORT as _cfg_port
        _port = _cfg_port
except Exception:
    pass

# 先尝试 TCP 连接（雷电/MuMu 等模拟器需要 adb connect）
if _port and _port > 0:
    try:
        subprocess.run(["adb", "connect", f"127.0.0.1:{_port}"], timeout=15,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        pass

# 查所有在线设备，优先匹配配置端口
_result = subprocess.run(["adb", "devices"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
_all = [l.split()[0] for l in _result.stdout.decode("utf-8", errors="ignore").splitlines()[1:]
        if l.strip() and not l.startswith("*") and "device" in l]
if not _all:
    raise RuntimeError("没连到任何 Android 设备 / 模拟器")

_serial = None
if _port and _port > 0:
    for s in _all:
        if str(_port) in s:
            _serial = s
            break

if not _serial:
    _serial = _all[0]

print(f"[stuck_probe2] 连接设备: {_serial}")
d = u2.connect(_serial)
out = r"C:\Users\ziqin\Desktop\work_files\yuanbao_auto\screenshots\stuck_probe"
os.makedirs(out, exist_ok=True)

def snap(tag):
    x = dump_xml(d)
    shot = d.screenshot()
    arr = np.array(shot.convert("RGB"))
    wr = float(np.mean(np.all(arr > 240, axis=2)))
    hv = hashlib.md5(x.encode()).hexdigest()[:6]
    print(f"[{tag:16s}] xml={len(x):6d} webv={x.count('WebView')} webkit={x.count('webkit')} "
          f"white={wr:.3f} hash={hv} size={shot.size}")
    shot.save(os.path.join(out, f"{tag}.png"))
    open(os.path.join(out, f"{tag}.xml"), "w", encoding="utf-8").write(x)
    return x

print("当前 activity:", d.app_current().get("activity"))
snap("00_before")

# 场景 A：不填码直接按返回键 → 看会不会回到手机号输入页
_back(d, wait=1.0)
snap("10_after_back")

# 场景 B：再回来，填一个假码（7 位或全 0 等），看会不会提交后卡住
# 先点下一步或回到验证码输入页
from yuanbao_login import _click_next, _input_phone
# 先拉回到验证码输入状态
for _ in range(4):
    x = dump_xml(d)
    if "输入 6 位验证码" in x:
        break
    _back(d, wait=1.0)
else:
    print("没能回到验证码输入页；重新启动登录")
    try:
        from yuanbao_login import start_yuanbao, force_reset
        force_reset(d); start_yuanbao(d, wait=6)
    except Exception as e:
        print(f"  reset 异常: {e}")
    sys.exit(1)

snap("20_ready_fill")

# 填一个全 0 的假码，观察 15 秒
_input_vcode(d, "000000")
time.sleep(0.3)
snap("30_filled_fake")

for i in range(15):
    x = dump_xml(d)
    shot = d.screenshot()
    arr = np.array(shot.convert("RGB"))
    wr = float(np.mean(np.all(arr > 240, axis=2)))
    has_captcha = any(k in x for k in ("选择最符合描述的图片", "换一批", "确定"))
    has_login = any(k in x for k in ("Hi，用户", "发消息", "按住"))
    has_err = any(k in x for k in ("验证码错误", "验证码不正确", "请重新输入"))
    print(f"  fake t={i:2d} xml={len(x):6d} white={wr:.3f} captcha={has_captcha} logged={has_login} err={has_err}")
    shot.save(os.path.join(out, f"40_fake_t{i:02d}.png"))
    open(os.path.join(out, f"40_fake_t{i:02d}.xml"), "w", encoding="utf-8").write(x)
    time.sleep(1.0)

print("\n探针结束，现场:", out)
