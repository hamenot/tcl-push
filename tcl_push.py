# -*- coding: utf-8 -*-
"""
TCL 多屏互动 · APK 推送工具

把 TCL 电视自带的「多屏互动」(com.tcl.MultiScreenInteraction_TV) 推送协议做成本地小工具：
拖入 APK 自动识别图标/名称/包名/版本，填好电视 IP 点一下，电视右上角按「右」+ 确认即可安装。

TCL 固件会拒绝一切 adb install（INSTALL_FAILED_VERIFICATION_FAILURE），
所以这里走多屏互动自己的「手机推送安装」协议（TCP 6553）。
"""

import hashlib
import json
import math
import os
import re
import base64
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tkinter as tk
from tkinter import filedialog, ttk

APP_NAME = "TCL 多屏互动推送"
TV_PORT = 6553
ADB_PORT = 18765
NO_PROGRESS_TIMEOUT = 30          # 电视多少秒没有任何进展就放弃
HARD_TIMEOUT = 300                # 兜底总时长（大文件下载）
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TCLPushTool")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

WIN_BG = "#f3f3f3"        # 标题栏 / 标签栏
CONTENT = "#ffffff"       # 内容区
SIDEBAR = "#fafafa"       # 左侧导航
SIDE_ACTIVE = "#ededed"   # 导航选中底
SIDE_HOVER = "#f2f2f2"
CARD = "#fbfbfb"          # APK 卡片
LINE = "#e5e5e5"          # 分隔线
BORDER = "#d8d8d8"        # 控件边框
TRACK = "#efefef"         # 分段控件底槽
TEXT = "#1a1a1a"
MUTED = "#6e6e6e"
ACCENT = "#0067c0"
ACCENT_DK = "#005a9e"
OKC = "#0f7b3f"
ERRC = "#c42b1c"
FONT = "Segoe UI Variable Text"
FONT_FB = "Segoe UI"


# ================================================================ 资源解析

def _pool(data, off):
    """解析 ResStringPool，返回 (字符串列表, chunk 结束偏移)。"""
    _ct, _hs, csize = struct.unpack_from("<HHI", data, off)
    count, _styles, flags, sstart, _x = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    offs = struct.unpack_from("<%dI" % count, data, off + 28)
    base = off + sstart
    out = []
    for o in offs:
        q = base + o
        try:
            if utf8:
                q += 1 if not (data[q] & 0x80) else 2
                n = data[q]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[q + 1]
                    q += 2
                else:
                    q += 1
                out.append(data[q:q + n].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", data, q)[0]
                q += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, q)[0]
                    q += 2
                out.append(data[q:q + n * 2].decode("utf-16-le", "replace"))
        except Exception:
            out.append("")
    return out, off + csize


def _axml(data):
    """解析 AndroidManifest.xml，产出 (元素名, {属性: (类型, 数据, 原字符串)})。"""
    if data[:2] != b"\x03\x00":
        raise ValueError("清单不是 Android 二进制格式")
    pool, off = _pool(data, 8)
    end = len(data)
    while off + 8 <= end:
        try:
            ctype, _hs, csize = struct.unpack_from("<HHI", data, off)
        except struct.error:
            return
        if csize <= 0:
            return
        if ctype == 0x0102:
            try:
                nidx = struct.unpack_from("<I", data, off + 20)[0]
                astart, asize, acount = struct.unpack_from("<HHH", data, off + 24)
                attrs = {}
                aoff = off + 16 + astart
                for i in range(acount):
                    p = aoff + i * asize
                    _ns, aname, raw = struct.unpack_from("<III", data, p)
                    _vs, _r, dtype, dval = struct.unpack_from("<HBBI", data, p + 12)
                    key = pool[aname] if aname < len(pool) else ""
                    if key:
                        attrs[key] = (dtype, dval,
                                      pool[raw] if (raw != 0xFFFFFFFF and raw < len(pool)) else None)
                yield (pool[nidx] if nidx < len(pool) else ""), attrs
            except Exception:
                pass
        off += csize


def _arsc_index(data):
    """解析 resources.arsc，返回 (全局字符串池, {资源ID: [(密度, 类型, 数据)]})。"""
    gpool, off = _pool(data, 12)
    idx = {}
    while off + 8 <= len(data):
        try:
            ctype, hsize, csize = struct.unpack_from("<HHI", data, off)
        except struct.error:
            break
        if csize <= 0:
            break
        if ctype == 0x0200:  # RES_TABLE_PACKAGE
            pkg_id = struct.unpack_from("<I", data, off + 8)[0]
            j = off + hsize
            while j + 8 <= off + csize:
                t2, h2, s2 = struct.unpack_from("<HHI", data, j)
                if s2 <= 0:
                    break
                if t2 == 0x0201:  # RES_TABLE_TYPE
                    tid, flags = data[j + 8], data[j + 9]
                    ecount, estart = struct.unpack_from("<II", data, j + 12)
                    csize_cfg = struct.unpack_from("<I", data, j + 20)[0]
                    dens = struct.unpack_from("<H", data, j + 20 + 14)[0] if csize_cfg >= 16 else 0
                    lang = data[j + 20 + 8:j + 20 + 12] if csize_cfg >= 12 else b"\x00" * 4
                    if flags & 0x01:
                        arr = [(struct.unpack_from("<H", data, j + h2 + i * 4)[0],
                                struct.unpack_from("<H", data, j + h2 + i * 4 + 2)[0] * 4)
                               for i in range(ecount)]
                    elif flags & 0x02:
                        arr = [(i, struct.unpack_from("<H", data, j + h2 + i * 2)[0] * 4)
                               for i in range(ecount)]
                    else:
                        arr = [(i, struct.unpack_from("<I", data, j + h2 + i * 4)[0])
                               for i in range(ecount)]
                    for i, eo in arr:
                        if eo == 0xFFFFFFFF:
                            continue
                        p = j + estart + eo
                        esize, eflags = struct.unpack_from("<HH", data, p)
                        if eflags & 0x0001:      # 复杂条目（bag），跳过
                            continue
                        _vs, _r, dtype, dval = struct.unpack_from("<HBBI", data, p + esize)
                        rid = (pkg_id << 24) | (tid << 16) | i
                        idx.setdefault(rid, []).append((dens, lang, dtype, dval))
                j += s2
        off += csize
    return gpool, idx


def parse_apk(path):
    """读出包名、版本、名称、图标。图标/名称会顺着 resources.arsc 反查（资源名常被混淆）。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        man = z.read("AndroidManifest.xml")
        arsc = z.read("resources.arsc") if "resources.arsc" in names else None

        info = {"package": "", "version_name": "", "version_code": "", "label": "",
                "icon_png": None}
        app_attrs = {}
        for elem, attrs in _axml(man):
            if elem == "manifest":
                pkg = attrs.get("package")
                info["package"] = (pkg[2] or "") if pkg else ""
                vn, vc = attrs.get("versionName"), attrs.get("versionCode")
                info["version_name"] = (vn[2] or "") if vn else ""
                info["version_code"] = str(vc[1]) if vc else ""
            elif elem == "application":
                app_attrs = attrs
                break
        if not info["package"]:
            raise ValueError("没找到包名，可能不是标准 APK")

        if arsc:
            gpool, idx = _arsc_index(arsc)

            def resolve(rid, want_png=False):
                vals = idx.get(rid) or []
                if want_png:
                    cands = []
                    for dens, _lang, dtype, dval in vals:
                        if dtype != 0x03 or dval >= len(gpool):
                            continue
                        v = gpool[dval]
                        if v.lower().endswith(".png") and not v.lower().endswith(".9.png"):
                            cands.append((dens, v))
                    return sorted(cands, key=lambda x: -x[0])[0][1] if cands else None
                vals = sorted(vals, key=lambda x: (x[0], x[1]))
                for _d, lang, dtype, dval in vals:
                    if lang == b"\x00\x00\x00\x00" and dtype == 0x03 and dval < len(gpool):
                        return gpool[dval]
                for _d, _l, dtype, dval in vals:
                    if dtype == 0x03 and dval < len(gpool):
                        return gpool[dval]
                return None

            # 名称
            lb = app_attrs.get("label")
            if lb:
                if lb[2]:
                    info["label"] = lb[2]
                elif lb[0] == 0x01:
                    info["label"] = resolve(lb[1]) or ""
            # 图标
            ic = app_attrs.get("icon") or app_attrs.get("roundIcon")
            entry = None
            if ic and ic[0] == 0x01:
                entry = resolve(ic[1], want_png=True)
            elif ic and ic[2]:
                entry = ic[2]
            if entry and entry in names:
                raw = z.read(entry)
                if entry.lower().endswith(".png"):
                    info["icon_png"] = raw
        if not info["label"]:
            stem = os.path.splitext(os.path.basename(path))[0]
            info["label"] = stem
    return info


# ================================================================ 推送协议

def _send(sock, text):
    payload = text.encode("utf-8")
    sock.sendall(struct.pack(">i", len(payload)) + payload)


def _split_messages(buf):
    out = []
    while len(buf) >= 4:
        n = struct.unpack(">i", buf[:4])[0]
        if n < 0 or len(buf) < 4 + n:
            break
        out.append(buf[4:4 + n].decode("utf-8", "replace"))
        buf = buf[4 + n:]
    return out, buf


def tv_reachable(ip, timeout=5.0):
    try:
        socket.create_connection((ip, TV_PORT), timeout=timeout).close()
        return True, ""
    except socket.timeout:
        return False, "连接 %s:%d 超时" % (ip, TV_PORT)
    except OSError as e:
        return False, str(e)


def push_to_tv(ip, url, package, label, version_code, size_mb, on_stage,
               stop_event=None, verifier=None, no_progress_timeout=None, hard_timeout=None):
    """执行一次推送。

    返回 (状态, 说明)，状态 ∈ ok / denied / failed / stopped / timeout / error。
    TCL 有时不回 stage 5，所以 tv 侧没给结论时用 verifier() 在机器上核验是否真的装上了。
    """
    npt = no_progress_timeout or NO_PROGRESS_TIMEOUT
    hard = hard_timeout or HARD_TIMEOUT
    try:
        sock = socket.create_connection((ip, TV_PORT), timeout=6)
    except OSError as e:
        return "error", "连不上电视的多屏互动端口：%s" % e
    sock.settimeout(1.0)
    try:
        _send(sock, "159>>1>>0")
        _send(sock, ">>".join([
            "223", "INSTALL", package, label, url, "2",
            str(version_code or 0), str(int(math.ceil(size_mb))),
            "", "", "", hashlib.md5(url.encode("utf-8")).hexdigest(),
        ]))
        buf = b""
        last_beat = time.time()
        last_progress = time.time()
        started = time.time()
        install_seen = False
        next_check = 0.0
        while True:
            if stop_event is not None and stop_event.is_set():
                return "stopped", "已手动停止"
            if time.time() - last_progress > npt:
                break
            if time.time() - started > hard:
                break
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                pass
            found, buf = _split_messages(buf)
            for msg in found:
                parts = msg.split(">>")
                if len(parts) < 5 or parts[0] != "223" or parts[2] != package:
                    continue
                last_progress = time.time()
                status = parts[4]
                if parts[1] == "INSTALL":
                    if status == "confirm_pending":
                        on_stage("confirm")
                    elif status == "installing":
                        install_seen = True
                        on_stage("install")
                    elif status == "installed":
                        return "ok", "电视报告安装完成"
                    elif status == "denied":
                        return "denied", "电视上选择了拒绝"
                elif parts[1] == "PROGRESS":
                    if status == "1":
                        on_stage("download")
                    elif status == "3":
                        install_seen = True
                        on_stage("install")
                    elif status == "5":
                        return "ok", "电视报告安装完成"
                    elif status == "2":
                        return "failed", "电视下载或安装失败"
            # 电视进到安装阶段后，直接在机器上轮询结果，不等它回 stage 5
            if verifier and install_seen and time.time() >= next_check:
                next_check = time.time() + 2.0
                if verifier():
                    return "ok", "已在电视上核验到安装完成"
            if time.time() - last_beat > 5:
                try:
                    _send(sock, "150>>")
                except OSError:
                    break
                last_beat = time.time()
        if verifier and verifier():
            return "ok", "已在电视上核验到安装完成"
        return "timeout", "电视没有回传最终状态"
    except OSError as e:
        return "error", str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ================================================================ 本地文件服务

class _ApkServer:
    def __init__(self, apk_path, on_log, host="0.0.0.0", port=0):
        self.apk_path = apk_path
        self.on_log = on_log
        self.served = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _serve(self, with_body):
                try:
                    size = os.path.getsize(outer.apk_path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    if with_body:
                        with open(outer.apk_path, "rb") as f:
                            shutil.copyfileobj(f, self.wfile, 256 * 1024)
                        if not outer.served.is_set():
                            outer.on_log("电视开始下载 APK（%.1f MB）" % (size / 1048576.0))
                        outer.served.set()
                except Exception:
                    pass

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def lan_ip():
    """取本机局域网地址：优先 192.168/10./172.16-31，跳过 198.18 这类代理虚拟网卡。"""
    cands = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.append(info[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        cands.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    def score(ip):
        if ip.startswith(("127.", "169.254.")):
            return -1
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            return 0
        if ip.startswith("192.168."):
            return 4
        if ip.startswith("10."):
            return 3
        if re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            return 3
        return 1

    return max(cands, key=score) if cands else "127.0.0.1"


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run_hidden(cmd, timeout=25):
    """调 adb 时不弹出黑色控制台窗口。"""
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)


def find_adb():
    cands = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cands += [os.path.join(base, "adb", "adb.exe"), os.path.join(base, "adb.exe")]
    cands += [
        os.path.join(os.path.expanduser("~"), "platform-tools", "adb.exe"),
        r"C:\platform-tools\adb.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return shutil.which("adb")


def adb_connected(adb, ip):
    try:
        run_hidden([adb, "connect", "%s:5555" % ip])
        devs = run_hidden([adb, "devices"], timeout=20).stdout.decode("utf-8", "ignore")
    except Exception:
        return False
    for line in devs.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(ip) and parts[1] == "device":
            return True
    return False


def port_open(ip, port, timeout=1.5):
    """先快速探一下 5555，避免 adb connect 在防火墙后面卡很久。"""
    try:
        socket.create_connection((ip, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def pkg_fingerprint(adb, ip, package):
    """返回 (versionName, lastUpdateTime)；没装返回 None；查不到返回 "UNKNOWN"。"""
    try:
        out = run_hidden([adb, "-s", "%s:5555" % ip, "shell",
                          "dumpsys package %s | grep -E 'versionName|lastUpdateTime'" % package],
                         timeout=20).stdout.decode("utf-8", "ignore")
    except Exception:
        return "UNKNOWN"
    if not out.strip():
        return "UNKNOWN"
    if "versionName" not in out:
        return None
    vn = re.search(r"versionName=(\S+)", out)
    lt = re.search(r"lastUpdateTime=([0-9\-: ]+)", out)
    return (vn.group(1) if vn else "", lt.group(1).strip() if lt else "")


# ================================================================ 界面

import tkinter.font as tkfont


# 圆角半径：控件与卡片统一 10px，复选框单独 6px
R_CTL = 10     # 按钮 / 输入框 / 分段控件 / 日志框
R_CARD = 10    # 卡片容器、应用图标底板
R_BOX = 6      # 复选框（18px 方框若用 10px 会被钳成圆形，失去方块语义）


def _rr(c, x1, y1, x2, y2, r, corners=(1, 1, 1, 1), **kw):
    """圆角矩形（按 1/4 圆弧采样）。

    corners 依次控制 左上 / 右上 / 右下 / 左下 是否倒角，0 表示保留直角，
    分段控件选中格靠它做「外侧圆角、分隔处直角」。

    注意：Tk 画布不做抗锯齿，小尺寸控件的弧边会有台阶。需要平滑时用 _aa_rrect()。
    """
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = []
    steps = 16
    arcs = ((x1 + r, y1 + r, 180, (x1, y1)),     # 左上
            (x2 - r, y1 + r, 270, (x2, y1)),     # 右上
            (x2 - r, y2 - r, 0, (x2, y2)),       # 右下
            (x1 + r, y2 - r, 90, (x1, y2)))      # 左下
    for i, (cx, cy, a0, corner) in enumerate(arcs):
        if corners[i]:
            for k in range(steps + 1):
                a = math.radians(a0 + 90.0 * k / steps)
                pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
        else:
            pts.extend(corner)
    return c.create_polygon(pts, smooth=False, **kw)


# ---------------------------------------------------------------- 抗锯齿圆角
# Tk 画布只能画硬边多边形，小控件（18px 复选框）的弧边台阶很明显。
# 这里用超采样的方式自己算覆盖率，输出带 alpha 的 PNG 交给 tk.PhotoImage，
# Tk 8.6 在画布上显示图片时会做 alpha 混色，边缘就平滑了。

_AA_CACHE = {}


def _png_bytes(w, h, rows):
    """把逐行 RGBA 字节打包成 PNG（只用 zlib，不依赖 Pillow）。"""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body +
                struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) +
            chunk(b"IEND", b""))


def _hex_rgb(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _in_rrect(px, py, x0, y0, x1, y1, radii):
    """点是否落在圆角矩形内；四个角半径可以不同（左上/右上/右下/左下）。"""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    tl, tr, br, bl = radii
    if px < x0 + tl and py < y0 + tl:
        return (px - x0 - tl) ** 2 + (py - y0 - tl) ** 2 <= tl * tl
    if px > x1 - tr and py < y0 + tr:
        return (px - x1 + tr) ** 2 + (py - y0 - tr) ** 2 <= tr * tr
    if px > x1 - br and py > y1 - br:
        return (px - x1 + br) ** 2 + (py - y1 + br) ** 2 <= br * br
    if px < x0 + bl and py > y1 - bl:
        return (px - x0 - bl) ** 2 + (py - y1 + bl) ** 2 <= bl * bl
    return True


def _aa_rrect(w, h, radii, fill, outline="", width=1.0, samples=5):
    """渲染抗锯齿圆角矩形，返回 PNG 字节。

    描边画在边界内侧（和 Windows 控件的观感一致，也不会被画布裁掉）。
    内部区域整行直接铺色，只有边界一带做超采样，几百像素的框也很快。
    """
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    rmax = min(w, h) / 2.0
    radii = [max(0.0, min(float(v), rmax)) for v in radii]      # 左上 右上 右下 左下
    sw = float(width) if outline else 0.0
    ir = [max(0.0, v - sw) for v in radii]
    ix0, iy0, ix1, iy1 = sw, sw, w - sw, h - sw

    frgb = _hex_rgb(fill) if fill else None
    orgb = _hex_rgb(outline) if (outline and sw > 0) else None
    fillb = bytes(frgb) + b"\xff" if frgb else b"\x00\x00\x00\x00"
    n = samples
    tot = float(n * n)
    band = int(sw + 2)
    rows = []

    for y in range(h):
        yc = y + 0.5
        xa = 0
        while xa < w and not _in_rrect(xa + 0.5, yc, ix0, iy0, ix1, iy1, ir):
            xa += 1
        xb = -1
        if xa < w:
            xb = w - 1
            while xb > xa and not _in_rrect(xb + 0.5, yc, ix0, iy0, ix1, iy1, ir):
                xb -= 1

        row = bytearray(4 * w)
        bands = []
        if frgb and xb - xa >= 2:
            # 中间整段直接在字节数组里铺色，两侧各留 band 像素做采样
            row[4 * (xa + 1):4 * xb] = fillb * (xb - xa - 1)
            bands = [(max(0, xa - band), xa + 1), (xb, min(w, xb + 1 + band))]
        elif xa < w:
            # 只有描边（interior 透明）：两侧各采样一条窄带，中间不用管
            bands = [(max(0, xa - band), min(w, xa + 1)),
                     (max(0, xb), min(w, xb + 1 + band))]
        else:
            bands = [(0, w)] if orgb else []

        for x0, x1 in bands:
            for x in range(x0, x1):
                ns = nf = 0
                for i in range(n):
                    px = x + (i + 0.5) / n
                    for j in range(n):
                        py = y + (j + 0.5) / n
                        if not _in_rrect(px, py, 0.0, 0.0, float(w), float(h), radii):
                            continue
                        if sw and not _in_rrect(px, py, ix0, iy0, ix1, iy1, ir):
                            ns += 1
                        elif frgb:
                            nf += 1
                hit = ns + nf
                if not hit:
                    continue
                if ns and nf:
                    cr = (orgb[0] * ns + frgb[0] * nf) / hit
                    cg = (orgb[1] * ns + frgb[1] * nf) / hit
                    cb = (orgb[2] * ns + frgb[2] * nf) / hit
                elif ns:
                    cr, cg, cb = orgb
                else:
                    cr, cg, cb = frgb
                row[4 * x:4 * x + 4] = bytes((int(cr + 0.5), int(cg + 0.5),
                                              int(cb + 0.5),
                                              int(255.0 * hit / tot + 0.5)))
        rows.append(bytes(row))
    return _png_bytes(w, h, rows)


def _aa_rect(canvas, w, h, radii, fill, outline="", width=1.0):
    """带缓存的抗锯齿圆角矩形图片；radii 可传数字（四角相同）或四元组。"""
    if not isinstance(radii, (tuple, list)):
        radii = (radii,) * 4
    radii = tuple(float(v) for v in radii)
    key = (int(w), int(h), tuple(round(v, 1) for v in radii), fill, outline,
           round(float(width), 1))
    img = _AA_CACHE.get(key)
    if img is None:
        png = _aa_rrect(w, h, radii, fill, outline, width)
        img = tk.PhotoImage(data=base64.b64encode(png), master=canvas)
        _AA_CACHE[key] = img
    return img


def _rrect(c, x1, y1, x2, y2, r, corners=(1, 1, 1, 1), fill="", outline="",
           width=1, **kw):
    """画一个抗锯齿圆角矩形（内部走 _aa_rect 出图）。

    corners 依次控制 左上/右上/右下/左下 是否倒角，分段控件选中格用得上。
    """
    w = max(1, int(round(x2 - x1)))
    h = max(1, int(round(y2 - y1)))
    radii = tuple(r if corners[i] else 0 for i in range(4))
    img = _aa_rect(c, w, h, radii, fill, outline, width)
    return c.create_image(x1, y1, anchor="nw", image=img, **kw)


class _Group(tk.Canvas):
    """分组标题：一条分隔线 + 居中标题，右侧可挂操作文字。"""

    def __init__(self, parent, fam, text, actions=None):
        super().__init__(parent, height=22, bg=CONTENT, highlightthickness=0)
        self.text = text
        self.actions = actions or []
        self.font = tkfont.Font(family=fam, size=9)
        self.bind("<Configure>", lambda e: self._draw())

    def _draw(self):
        w = self.winfo_width() or 400
        y = 11
        self.delete("all")
        self.create_line(0, y, w, y, fill=LINE)
        tw = self.font.measure(self.text) + 20
        self.create_rectangle(w / 2 - tw / 2, y - 9, w / 2 + tw / 2, y + 9,
                              fill=CONTENT, outline="")
        self.create_text(w / 2, y, text=self.text, fill=MUTED, font=self.font)
        if not self.actions:
            return
        widths = [self.font.measure(lbl) for lbl, _cb in self.actions]
        total = sum(widths) + 16 * (len(widths) - 1) + 4
        self.create_rectangle(w - total, y - 9, w, y + 9, fill=CONTENT, outline="")
        x = w
        for (lbl, cb), lw in zip(reversed(self.actions), reversed(widths)):
            tag = "act_" + lbl
            self.create_text(x, y, anchor="e", text=lbl, fill=ACCENT,
                             font=self.font, tags=(tag,))
            self.tag_bind(tag, "<Button-1>", lambda e, f=cb: f())
            self.tag_bind(tag, "<Enter>", lambda e: self.configure(cursor="hand2"))
            self.tag_bind(tag, "<Leave>", lambda e: self.configure(cursor="arrow"))
            x -= lw + 16


class _Check(tk.Frame):
    """复选框：未勾选为空心方框，勾选后整块填成实心蓝方块。

    方框是超采样渲染的图片（_aa_rect），不是多边形，弧边才不会有台阶。
    """

    BOX = 18

    def __init__(self, parent, fam, text, var, command=None, bg=CONTENT):
        super().__init__(parent, bg=bg)
        self.var = var
        self.command = command
        self.c = tk.Canvas(self, width=self.BOX, height=self.BOX, bg=bg,
                           highlightthickness=0, cursor="hand2")
        self.c.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=TEXT, font=(fam, 10), cursor="hand2")
        self.lbl.pack(side="left", padx=(8, 0))
        self.c.bind("<Button-1>", self._click)
        self.lbl.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        self.var.set(not self.var.get())
        self._draw()
        if self.command:
            self.command()

    def _draw(self):
        self.c.delete("all")
        b = self.BOX
        if self.var.get():
            img = _aa_rect(self.c, b, b, R_BOX, ACCENT)
        else:
            img = _aa_rect(self.c, b, b, R_BOX, "#ffffff", "#9d9d9d", 1.0)
        self.c.create_image(0, 0, anchor="nw", image=img)


class _Seg(tk.Canvas):
    """分段切换：整条只共用一组外圆角，中间为直线分隔，选中格蓝框蓝字。"""

    def __init__(self, parent, fam, options, value, on_change):
        self.fam = fam
        self.options = list(options)
        self.value = value
        self.on_change = on_change
        f = tkfont.Font(family=fam, size=10)
        self.ws = [max(82, f.measure(t) + 30) for _v, t in self.options]
        super().__init__(parent, width=sum(self.ws), height=32, bg=CONTENT,
                         highlightthickness=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set_value(self, value):
        if self.value != value:
            self.value = value
            self._draw()

    def _click(self, e):
        x = 0
        for (val, _t), w in zip(self.options, self.ws):
            if e.x <= x + w:
                if self.value != val:
                    self.value = val
                    self._draw()
                    self.on_change(val)
                return
            x += w

    def _draw(self):
        self.delete("all")
        h = 32
        w = sum(self.ws)
        n = len(self.options)
        # 外框：整条控件的唯一一组圆角
        _rrect(self, 1, 1, w - 1, h - 1, R_CTL, fill="#ffffff", outline=BORDER)
        # 分隔：竖直直线，两端贴住外框的直边，不再各自画圆角
        x = 0
        for i, ww in enumerate(self.ws):
            if i:
                self.create_line(x, 1.5, x, h - 1.5, fill=BORDER)
            x += ww
        # 选中格：外侧沿用外框圆角，靠分隔一侧收成直角
        x = 0
        for i, (val, t) in enumerate(self.options):
            ww = self.ws[i]
            active = val == self.value
            if active:
                if i == 0:
                    corners, x1, x2 = (1, 0, 0, 1), 1, x + ww
                elif i == n - 1:
                    corners, x1, x2 = (0, 1, 1, 0), x, w - 1
                else:
                    corners, x1, x2 = (0, 0, 0, 0), x, x + ww
                _rrect(self, x1, 1, x2, h - 1, R_CTL, corners=corners,
                       fill="#ffffff", outline=ACCENT, width=2)
            self.create_text(x + ww / 2, h / 2, text=t, fill=ACCENT if active else TEXT,
                             font=(self.fam, 10, "bold" if active else "normal"))
            x += ww


class _Btn(tk.Canvas):
    """次要按钮：白底圆角 + 1px 边框。"""

    def __init__(self, parent, fam, text, command, height=30, bg=CONTENT):
        f = tkfont.Font(family=fam, size=9)
        self.w = f.measure(text) + 32
        self.h = height
        super().__init__(parent, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.text = text
        self.command = command
        self.fam = fam
        self.hover = False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self.command())
        self._draw()

    def _hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        _rrect(self, 1, 1, self.w - 1, self.h - 1, R_CTL,
               fill="#f6f6f6" if self.hover else "#ffffff", outline="#dcdcdc")
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=TEXT,
                         font=(self.fam, 9))


class _PrimaryBtn(tk.Canvas):
    """主按钮：圆角蓝底，忙碌时变红字「停止推送」，未就绪时灰蓝禁用。"""

    def __init__(self, parent, fam, text, command, width=120, height=34):
        super().__init__(parent, width=width, height=height, bg=CONTENT,
                         highlightthickness=0, cursor="arrow")
        self.w, self.h = width, height
        self.fam = fam
        self.text = text
        self.mode = "disabled"     # normal / busy / disabled
        self.command = command
        self.hover = False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        if self.mode != "disabled":
            self.command()

    def _hover(self, v):
        self.hover = v
        self._draw()

    def set_label(self, text, mode):
        if (text, mode) != (self.text, self.mode):
            self.text, self.mode = text, mode
            self.configure(cursor="arrow" if mode == "disabled" else "hand2")
            self._draw()

    def _draw(self):
        self.delete("all")
        if self.mode == "disabled":
            fill = "#c9d8e6"
        elif self.mode == "busy":
            fill = "#b3352a" if self.hover else ERRC
        else:
            fill = ACCENT_DK if self.hover else ACCENT
        _rrect(self, 1, 1, self.w - 1, self.h - 1, R_CTL, fill=fill, outline="")
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill="#ffffff",
                         font=(self.fam, 10, "bold"))


class _EntryBox(tk.Canvas):
    """圆角输入框：Canvas 画边框，里面嵌一个无边框 Entry。"""

    def __init__(self, parent, fam, var, width=150, height=32):
        super().__init__(parent, width=width, height=height, bg=CONTENT,
                         highlightthickness=0)
        self.w, self.h = width, height
        self.entry = tk.Entry(self, textvariable=var, font=(fam, 10), relief="flat", bd=0,
                              bg="#ffffff", fg=TEXT, insertbackground=TEXT,
                              highlightthickness=0)
        self.create_window(11, height / 2, anchor="w", window=self.entry,
                           width=width - 22, height=height - 12)
        self.entry.bind("<FocusIn>", lambda e: self._draw(True))
        self.entry.bind("<FocusOut>", lambda e: self._draw(False))
        self._draw(False)

    def _draw(self, focused):
        self.delete("bg")
        _rrect(self, 1, 1, self.w - 1, self.h - 1, R_CTL, fill="#ffffff",
               outline=ACCENT if focused else BORDER, tags="bg")
        self.tag_lower("bg")


class _Select(tk.Canvas):
    """圆角下拉：外观和输入框一致，点击弹出一个原生菜单选值。

    ttk.Combobox 是直角原生控件，放在这套 10px 圆角界面里很跳，所以自绘。
    """

    def __init__(self, parent, fam, var, options, on_change=None,
                 width=104, height=30):
        super().__init__(parent, width=width, height=height, bg=CONTENT,
                         highlightthickness=0, cursor="hand2")
        self.fam, self.var = fam, var
        self.options = list(options)
        self.on_change = on_change
        self.w, self.h = width, height
        self.hover = False
        self.bind("<Button-1>", self._popup)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._draw()

    def _hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        _rrect(self, 1, 1, self.w - 1, self.h - 1, R_CTL,
               fill="#f6f6f6" if self.hover else "#ffffff",
               outline=ACCENT if self.hover else BORDER)
        self.create_text(12, self.h / 2, anchor="w", text=self.var.get(),
                         fill=TEXT, font=(self.fam, 10))
        x, y = self.w - 16, self.h / 2 - 2
        self.create_line(x - 4, y, x, y + 4, x + 4, y, fill=MUTED, width=1.4,
                         capstyle="round", joinstyle="round")

    def _pick(self, val):
        self.var.set(val)
        self._draw()
        if self.on_change:
            self.on_change()

    def _popup(self, _e=None):
        m = tk.Menu(self, tearoff=0, font=(self.fam, 10),
                    bg="#ffffff", fg=TEXT, activebackground="#f0f0f0",
                    activeforeground=TEXT, bd=0)
        for opt in self.options:
            m.add_command(label=opt, command=lambda o=opt: self._pick(o))
        try:
            m.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.h + 2)
        finally:
            m.grab_release()



class _BusyBar(tk.Canvas):
    """自绘进度条：一段蓝色来回扫，避免 ttk 主题配色不统一。"""

    def __init__(self, parent):
        super().__init__(parent, height=3, bg=CONTENT, highlightthickness=0)
        self.pos = 0.0
        self.dir = 1
        self._job = None

    def start(self):
        if self._job is None:
            self._tick()

    def stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        self.delete("all")

    def _tick(self):
        w = self.winfo_width() or 400
        self.delete("all")
        self.create_rectangle(0, 0, w, 3, fill="#e9e9e9", outline="")
        seg = max(60, w * 0.3)
        x = (w - seg) * self.pos
        self.create_rectangle(x, 0, x + seg, 3, fill=ACCENT, outline="")
        self.pos += 0.018 * self.dir
        if self.pos >= 1:
            self.pos, self.dir = 1, -1
        elif self.pos <= 0:
            self.pos, self.dir = 0, 1
        self._job = self.after(18, self._tick)


class App:
    def __init__(self, root):
        self.root = root
        self.dlg = None
        self.apk_path = None
        self.meta = None
        self.icon_img = None
        self.busy = False
        self.stop_event = threading.Event()
        self.cfg = self._load_cfg()

        fam = FONT
        try:
            if FONT not in tkfont.families():
                fam = FONT_FB
        except Exception:
            fam = FONT_FB
        self.fam = fam

        self.channel = self.cfg.get("channel", "adb")
        self.to_sec = int(self.cfg.get("timeout", NO_PROGRESS_TIMEOUT) or NO_PROGRESS_TIMEOUT)
        remember = bool(self.cfg.get("remember", True))
        self.ip_var = tk.StringVar(value=(self.cfg.get("ip", "") if remember else ""))
        self.save_var = tk.BooleanVar(value=remember)
        self.verify_var = tk.BooleanVar(value=bool(self.cfg.get("verify", True)))
        self.fallback_var = tk.BooleanVar(value=bool(self.cfg.get("fallback", True)))
        self.timeout_var = tk.StringVar(value="%d 秒" % self.to_sec)

        root.title(APP_NAME)
        root.configure(bg=CONTENT)
        root.geometry("720x520")
        root.minsize(660, 470)
        self._set_icon()
        root.option_add("*Font", (fam, 10))
        self._apply_win11_chrome(root)

        style = ttk.Style()
        for name in ("vista", "winnative", "clam"):
            try:
                style.theme_use(name)
                break
            except tk.TclError:
                continue

        body = tk.Frame(root, bg=CONTENT)
        body.pack(fill="both", expand=True)
        self._build_page(body)
        self._update_btn()
        self._log("把 APK 拖进上方卡片，或点击卡片选择文件。", "muted")

    # ---------- 窗口外观
    def _set_icon(self):
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "app.ico")
            if os.path.isfile(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _apply_win11_chrome(self, win):
        if os.name != "nt":
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            dwm = ctypes.windll.dwmapi

            def setattrx(attr, value):
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            setattrx(20, 0)           # 浅色
            setattrx(33, 2)           # 圆角
            setattrx(35, 0x00FFFFFF)  # 标题栏白色（BGR）
            setattrx(36, 0x001A1A1A)  # 标题文字颜色
        except Exception:
            pass

    # ---------- 小控件工厂
    def _label(self, parent, text, size=10, color=TEXT, bg=CONTENT, bold=False):
        f = (self.fam, size, "bold") if bold else (self.fam, size)
        return tk.Label(parent, text=text, bg=bg, fg=color, font=f, anchor="w")

    def _hint(self, parent, text, px=532):
        """按像素宽度手工折行：Tk 的 wraplength 会把整段中文当成一个「词」，
        导致行首只剩几个字母那种难看断行。"""
        f = tkfont.Font(family=self.fam, size=9)
        lines, cur = [], ""
        for ch in text:
            if cur and f.measure(cur + ch) > px:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
        return tk.Label(parent, text="\n".join(lines), bg=CONTENT, fg=MUTED, font=f,
                        anchor="w", justify="left")

    def _entry(self, parent, var, width=150):
        return _EntryBox(parent, self.fam, var, width=width)

    def _ghost(self, parent, text, cmd):
        return _Btn(parent, self.fam, text, cmd)

    def _make_log(self, parent, height):
        # 圆角描边画在 Canvas 上，内嵌 Text 整体内缩 6px，
        # 这样四角弧线不会被「永远置顶」的 window 项盖掉。
        c = tk.Canvas(parent, height=height, bg=CONTENT, highlightthickness=0)
        inner = tk.Frame(c, bg="#ffffff")
        txt = tk.Text(inner, relief="flat", bd=0, bg="#ffffff", fg="#3a3a3a",
                      font=("Consolas", 9), padx=4, pady=3, wrap="word",
                      state="disabled", highlightthickness=0, insertbackground=TEXT,
                      height=1)
        for tag, color in (("ok", OKC), ("err", ERRC), ("muted", MUTED)):
            txt.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(inner, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        win = c.create_window(6, 6, anchor="nw", window=inner)

        def _layout(e):
            c.delete("bd")
            _rrect(c, 1, 1, e.width - 1, e.height - 1, R_CTL,
                   fill="", outline="#dcdcdc", tags="bd")
            c.itemconfigure(win, width=max(12, e.width - 12),
                            height=max(12, e.height - 12))

        c.bind("<Configure>", _layout)
        return c, txt

    # ---------- 主页面
    def _build_page(self, parent):
        # 底部操作栏
        bot = tk.Frame(parent, bg=CONTENT)
        bot.pack(side="bottom", fill="x", padx=18, pady=(6, 13))
        tk.Frame(bot, height=1, bg=LINE).pack(fill="x")
        self.busy_bar = _BusyBar(bot)
        self.status_row = tk.Frame(bot, bg=CONTENT)
        self.status_row.pack(fill="x", pady=(9, 0))
        self.status_dot = tk.Canvas(self.status_row, width=8, height=8, bg=CONTENT,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", pady=(5, 0))
        self.status_dot.create_oval(0, 0, 8, 8, fill="#c0c0c0", outline="")
        self.status_lbl = tk.Label(self.status_row, text="就绪", bg=CONTENT, fg=MUTED,
                                   font=(self.fam, 9), anchor="w")
        self.status_lbl.pack(side="left", padx=(7, 0))
        self.btn = _PrimaryBtn(self.status_row, self.fam, "推送到电视", self.start_push)
        self.btn.pack(side="right")

        area = tk.Frame(parent, bg=CONTENT, padx=18)
        area.pack(fill="both", expand=True)

        # 应用包（大框：拖拽 / 展示）
        _Group(area, self.fam, "应用包").pack(fill="x", pady=(10, 3))
        self.card = tk.Canvas(area, height=168, bg=CONTENT, highlightthickness=0, cursor="hand2")
        self.card.pack(fill="both", expand=True)
        self.card.bind("<Configure>", self._draw_card)
        self.card.bind("<Button-1>", lambda e: self.browse())
        self._draw_card()

        # 目标设备
        _Group(area, self.fam, "目标设备",
               actions=[("测试连接", self._test_conn)]).pack(fill="x", pady=(10, 3))
        dev = tk.Frame(area, bg=CONTENT)
        dev.pack(fill="x")
        c1 = tk.Frame(dev, bg=CONTENT)
        c1.pack(side="left")
        self._label(c1, "电视 IP 地址", 9, MUTED).pack(anchor="w")
        e1 = self._entry(c1, self.ip_var, 150)
        e1.pack(anchor="w", pady=(3, 0))
        e1.entry.bind("<Return>", lambda ev: self.start_push())
        c2 = tk.Frame(dev, bg=CONTENT)
        c2.pack(side="left", padx=(22, 0), pady=(20, 0))
        _Check(c2, self.fam, "保存为默认地址", self.save_var, self._on_toggle_save).pack(anchor="w")
        c3 = tk.Frame(dev, bg=CONTENT)
        c3.pack(side="left", padx=(26, 0))
        self._label(c3, "传输通道", 9, MUTED).pack(anchor="w")
        self.seg = _Seg(c3, self.fam, [("adb", "ADB 通道"), ("lan", "局域网直连")],
                        self.channel, self._on_channel)
        self.seg.pack(anchor="w", pady=(3, 0))
        self._ghost(dev, "设置", self._open_settings).pack(side="right", pady=(19, 0))

        # 运行日志（小框：固定高度）
        _Group(area, self.fam, "运行日志",
               actions=[("复制", self._copy_log), ("清空", self._clear_log)]
               ).pack(fill="x", pady=(10, 3))
        wrap, self.log_box = self._make_log(area, 118)
        wrap.pack(fill="x")

    # ---------- 设置弹窗
    def _open_settings(self):
        if self.dlg is not None:
            try:
                if self.dlg.winfo_exists():
                    self.dlg.deiconify()
                    self.dlg.lift()
                    self.dlg.focus_set()
                    return
            except Exception:
                pass
        dlg = tk.Toplevel(self.root)
        self.dlg = dlg
        dlg.title("设置")
        dlg.configure(bg=CONTENT)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 520, 452
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry("%dx%d+%d+%d" % (w, h, max(x, 0), max(y, 0)))
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "app.ico")
            if os.path.isfile(ico):
                dlg.iconbitmap(ico)
        except Exception:
            pass
        self._apply_win11_chrome(dlg)

        bottom = tk.Frame(dlg, bg=CONTENT)
        bottom.pack(side="bottom", fill="x", padx=20, pady=(4, 14))
        self._ghost(bottom, "关闭", dlg.destroy).pack(side="right")

        area = tk.Frame(dlg, bg=CONTENT, padx=20)
        area.pack(fill="both", expand=True)

        _Group(area, self.fam, "超时").pack(fill="x", pady=(10, 5))
        row = tk.Frame(area, bg=CONTENT)
        row.pack(fill="x")
        self._label(row, "无进展超时", 9, MUTED).pack(side="left", padx=(0, 10))
        cb = _Select(row, self.fam, self.timeout_var,
                     ["15 秒", "30 秒", "45 秒", "60 秒", "90 秒"],
                     self._save_cfg)
        cb.pack(side="left")
        self._hint(area, "电视超过该时长没有任何进展就放弃；大文件下载另有 300 秒兜底。",
                   452).pack(anchor="w", pady=(6, 0), fill="x")

        _Group(area, self.fam, "安装结果核验").pack(fill="x", pady=(12, 5))
        _Check(area, self.fam, "用 adb 在电视上核验安装结果（推荐）", self.verify_var,
               self._save_cfg).pack(anchor="w")
        self._hint(area, "TCL 有时不回报「安装完成」；勾选后工具会直接查电视上的包版本，"
                         "确认真的装上了，不会一直卡在「正在安装」。",
                   452).pack(anchor="w", pady=(6, 0), fill="x")

        _Group(area, self.fam, "通道回退").pack(fill="x", pady=(12, 5))
        _Check(area, self.fam, "ADB 通道不可用时自动改用局域网直连", self.fallback_var,
               self._save_cfg).pack(anchor="w")
        self._hint(area, "关闭后，ADB 通道失败会直接报错，方便排查是哪一段出了问题。",
                   452).pack(anchor="w", pady=(6, 0), fill="x")

        _Group(area, self.fam, "关于").pack(fill="x", pady=(12, 5))
        for k, v in (("版本", "TCL 多屏互动推送 · 协议 TCP %d" % TV_PORT),
                     ("本机", "局域网地址 " + lan_ip()),
                     ("配置", CONFIG_PATH)):
            r = tk.Frame(area, bg=CONTENT)
            r.pack(fill="x", pady=1)
            self._label(r, k, 9, MUTED).pack(side="left", anchor="n", pady=(1, 0))
            t = self._hint(r, v, px=400)
            t.configure(fg=TEXT)
            t.pack(side="left", padx=(12, 0))

        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ---------- 通道
    def _on_channel(self, key):
        self.channel = key
        self.seg.set_value(key)
        self._save_cfg()

    # ---------- APK 卡片
    def _draw_card(self, event=None):
        c = self.card
        c.delete("all")
        w = c.winfo_width() or 520
        h = c.winfo_height() or 168
        if not self.apk_path:
            _rr(c, 2, 2, w - 2, h - 2, R_CARD, fill="#fafafa", outline="#cfcfcf", dash=(4, 4))
            c.create_text(w / 2, h / 2 - 12, text="把 APK 拖到这里，或点击选择",
                          fill=TEXT, font=(self.fam, 12, "bold"))
            c.create_text(w / 2, h / 2 + 14, text="自动读取图标 · 名称 · 包名 · 版本",
                          fill=MUTED, font=(self.fam, 9))
            return
        _rrect(c, 1, 1, w - 1, h - 1, R_CARD, fill=CARD, outline=LINE)
        size = 56
        cy = h / 2
        ix = 24
        iy = cy - size / 2
        if self.icon_img is not None:
            c.create_image(ix, iy, image=self.icon_img, anchor="nw")
        else:
            _rrect(c, ix, iy, ix + size, iy + size, R_CARD, fill="#e6eef7", outline="")
            letter = (self.meta.get("label") or "?")[:1].upper()
            c.create_text(ix + size / 2, iy + size / 2, text=letter, fill=ACCENT,
                          font=(self.fam, 20, "bold"))
        tx = ix + size + 18
        c.create_text(tx, cy - 27, anchor="w", text=self.meta["label"] or "(未命名)",
                      fill=TEXT, font=(self.fam, 12, "bold"))
        c.create_text(tx, cy - 3, anchor="w", text=self.meta["package"],
                      fill=MUTED, font=("Consolas", 9))
        vc = (" · %s" % self.meta["version_code"]) if self.meta["version_code"] else ""
        c.create_text(tx, cy + 21, anchor="w",
                      text="版本 %s%s" % (self.meta["version_name"] or "-", vc),
                      fill=MUTED, font=(self.fam, 9))
        c.create_text(w - 24, cy, anchor="e", text="更换 APK", fill=ACCENT,
                      font=(self.fam, 9), tags="swap")
        c.tag_bind("swap", "<Button-1>", lambda e: self.browse())

    # ---------- 配置
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        if not hasattr(self, "ip_var"):
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            remember = bool(self.save_var.get())
            data = {"ip": self.ip_var.get().strip() if remember else "",
                    "remember": remember, "channel": self.channel,
                    "timeout": self._timeout_value(),
                    "verify": bool(self.verify_var.get()),
                    "fallback": bool(self.fallback_var.get())}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self._log("保存配置失败：%s" % e, "err")
            return False

    def _on_toggle_save(self):
        if self.save_var.get():
            ip = self._clean_ip()
            if ip:
                self._save_cfg()
                self._log("已设为默认地址：%s" % ip, "muted")
            else:
                self._log("先填一个有效的电视 IP，再勾选保存。", "muted")
                self.save_var.set(False)
        else:
            self._save_cfg()
            self._log("已取消默认地址", "muted")

    # ---------- 日志
    def _log(self, msg, tag=None):
        box = getattr(self, "log_box", None)
        if box is not None:
            try:
                box.configure(state="normal")
                box.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg), tag or ())
                box.see("end")
                box.configure(state="disabled")
            except Exception:
                pass
        self._set_status(msg, tag)

    def _set_status(self, msg, tag=None):
        if not hasattr(self, "status_lbl"):
            return
        color = {"ok": OKC, "err": ERRC}.get(tag, MUTED)
        text = msg if len(msg) <= 56 else msg[:54] + "…"
        self.status_lbl.configure(text=text, fg=color)
        self.status_dot.delete("all")
        self.status_dot.create_oval(
            0, 0, 8, 8, fill={"ok": OKC, "err": ERRC}.get(tag, "#c0c0c0"), outline="")

    def _clear_log(self):
        box = getattr(self, "log_box", None)
        if box is not None:
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.configure(state="disabled")

    def _copy_log(self):
        box = getattr(self, "log_box", None)
        if box is None:
            return
        text = box.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._set_status("日志已复制到剪贴板", "ok")

    # ---------- 文件
    def _clean_ip(self):
        raw = re.sub(r"^https?://", "", self.ip_var.get().strip())
        return raw.split("/")[0].split(":")[0].strip()

    def browse(self):
        if self.busy:
            return
        p = filedialog.askopenfilename(title="选择 APK", filetypes=[("Android 安装包", "*.apk")])
        if p:
            self.load_apk(p)

    def load_apk(self, path):
        if self.busy:
            return
        if not path.lower().endswith(".apk"):
            self._log("只支持 .apk 文件", "err")
            return
        try:
            meta = parse_apk(path)
        except Exception as e:
            self._log("读取 APK 失败：%s" % e, "err")
            return
        self.apk_path, self.meta = path, meta
        self.icon_img = self._load_icon(meta.get("icon_png"))
        self._log("已识别 %s（%s）" % (meta["label"] or meta["package"],
                                   meta["version_name"] or "-"))
        self._draw_card()
        self._update_btn()

    def _load_icon(self, png_bytes):
        """把 APK 里的 PNG 图标缩到 56px 显示（Tk 只支持整数倍降采样）。"""
        if not png_bytes:
            return None
        try:
            img = tk.PhotoImage(data=png_bytes)
            k = max(1, int(round(img.width() / 56.0)))
            if k > 1:
                img = img.subsample(k, k)
            return img
        except Exception:
            return None

    # ---------- 状态
    def _timeout_value(self):
        try:
            return max(5, int(self.timeout_var.get().split()[0]))
        except Exception:
            return NO_PROGRESS_TIMEOUT

    def _update_btn(self):
        if self.busy:
            self.btn.set_label("停止推送", "busy")
        elif self.apk_path:
            self.btn.set_label("推送到电视", "normal")
        else:
            self.btn.set_label("推送到电视", "disabled")

    def _set_busy(self, busy):
        self.busy = busy
        if busy:
            self.busy_bar.pack(fill="x", pady=(8, 0), before=self.status_row)
            self.busy_bar.start()
        else:
            self.busy_bar.stop()
            self.busy_bar.pack_forget()
        self._update_btn()

    def _stage(self, stage):
        txt = {"confirm": "电视已弹窗，请按遥控器「右」选中「同意」，再按确认",
               "download": "电视正在下载 APK…",
               "install": "电视正在安装…（会在电视上自动核验结果）"}.get(stage)
        if txt:
            self._log(txt)

    # ---------- 测试连接
    def _test_conn(self):
        ip = self._clean_ip()
        if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
            self._log("IP 格式不对，例如 192.168.31.135", "err")
            return
        self._log("正在测试 %s …" % ip, "muted")

        def work():
            reach = port_open(ip, TV_PORT, 2.0)
            adb_ok = False
            adb = find_adb()
            if adb and port_open(ip, 5555, 1.2):
                adb_ok = adb_connected(adb, ip)
            if reach and adb_ok:
                self._ui(self._log, "连接正常：多屏互动端口可达，adb 调试已授权（两种通道都可用）", "ok")
            elif reach:
                self._ui(self._log, "多屏互动端口可达；adb 未授权，建议用局域网直连")
            else:
                self._ui(self._log, "连不上 %s:%d，检查电视是否开机、IP 是否正确" % (ip, TV_PORT), "err")

        threading.Thread(target=work, daemon=True).start()

    # ---------- 推送
    def start_push(self):
        if self.busy:
            self.stop_event.set()
            self._log("正在停止…", "muted")
            return
        if not self.apk_path:
            return
        ip = self._clean_ip()
        if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
            self._log("IP 格式不对，例如 192.168.31.135", "err")
            return
        self._save_cfg()
        self.stop_event.clear()
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(ip,), daemon=True).start()

    def _worker(self, ip):
        meta, path = self.meta, self.apk_path
        size = os.path.getsize(path) / 1048576.0
        label = re.sub(r"[^\w\-. ]", "", meta["label"] or "") or meta["package"].split(".")[-1]
        to = self._timeout_value()
        try:
            reach, why = tv_reachable(ip)
            if not reach:
                self._ui(self._log, "连不上电视：%s" % why, "err")
                self._ui(self._log, "检查电视是否开机、IP 是否正确、是否同一 Wi-Fi。", "muted")
                return
            self._ui(self._log, "开始推送到 %s（%s，超时 %ds）" % (
                ip, "ADB 通道" if self.channel == "adb" else "局域网直连", to))

            adb = find_adb()
            verifier = None
            if adb and self.verify_var.get() and port_open(ip, 5555) and adb_connected(adb, ip):
                before = pkg_fingerprint(adb, ip, meta["package"])

                def verifier():
                    now = pkg_fingerprint(adb, ip, meta["package"])
                    if not isinstance(now, tuple):
                        return False          # 没装，或者 adb 问不出来
                    if isinstance(before, tuple):
                        return now != before  # 先装着旧版，就看是否被更新过
                    return True               # 之前没有，现在有了

            if self.channel == "adb" and adb:
                status, why = self._push_adb(ip, adb, meta, label, size, verifier, to)
                if status == "unavailable":
                    if self.fallback_var.get():
                        self._ui(self._log, "ADB 通道不可用：%s" % why, "muted")
                        self._ui(self._log, "改用局域网直连…")
                        status, why = self._push_lan(ip, meta, label, size, verifier, to)
                    else:
                        status, why = "failed", "ADB 通道不可用：%s" % why
            else:
                if self.channel == "adb":
                    self._ui(self._log, "没找到可用的 adb，改用局域网直连。", "muted")
                status, why = self._push_lan(ip, meta, label, size, verifier, to)

            if status == "ok":
                self._ui(self._log, "安装完成 ✔  %s %s" % (
                    meta["package"], meta["version_name"] or ""), "ok")
            elif status == "denied":
                self._ui(self._log, "电视上选择了拒绝，已取消。", "err")
            elif status == "stopped":
                self._ui(self._log, "已停止。")
            elif status == "timeout":
                self._ui(self._log, "超时 %ds：%s" % (to, why), "err")
                self._ui(self._log, "电视上装好了吗？装好后可重新推送，或到「设置」里加大超时。", "muted")
            else:
                self._ui(self._log, "推送失败：%s" % why, "err")
        except Exception as e:
            self._ui(self._log, "出错：%s" % e, "err")
        finally:
            self._ui(self._set_busy, False)

    def _push_adb(self, ip, adb, meta, label, size, verifier, to):
        try:
            if not adb_connected(adb, ip):
                return "unavailable", "电视没有授权 adb 调试"
            r = run_hidden([adb, "reverse", "tcp:%d" % ADB_PORT, "tcp:%d" % ADB_PORT])
            if r.returncode != 0:
                msg = (r.stderr or r.stdout).decode("utf-8", "ignore").strip()
                return "unavailable", "adb reverse 失败：%s" % (msg or "未知原因")
        except Exception as e:
            return "unavailable", "调用 adb 出错：%s" % e
        try:
            srv = _ApkServer(self.apk_path, lambda m: self._ui(self._log, m),
                             host="127.0.0.1", port=ADB_PORT)
        except OSError as e:
            self._adb_cleanup(adb)
            return "unavailable", "端口 %d 被占用（%s）" % (ADB_PORT, e)
        srv.start()
        try:
            url = "http://127.0.0.1:%d/app.apk" % ADB_PORT
            return push_to_tv(ip, url, meta["package"], label, meta["version_code"] or 0,
                              size, self._stage, self.stop_event, verifier,
                              to, max(HARD_TIMEOUT, to * 4))
        finally:
            srv.stop()
            self._adb_cleanup(adb)

    def _push_lan(self, ip, meta, label, size, verifier, to):
        srv = _ApkServer(self.apk_path, lambda m: self._ui(self._log, m))
        srv.start()
        try:
            url = "http://%s:%d/app.apk" % (lan_ip(), srv.port)
            self._ui(self._log, "APK 服务：%s" % url)
            status, why = push_to_tv(ip, url, meta["package"], label, meta["version_code"] or 0,
                                     size, self._stage, self.stop_event, verifier,
                                     to, max(HARD_TIMEOUT, to * 4))
            if status in ("failed", "timeout") and not srv.served.is_set():
                why = "%s（电视没能从电脑取到文件，检查防火墙）" % why
            return status, why
        finally:
            srv.stop()

    def _adb_cleanup(self, adb):
        try:
            run_hidden([adb, "reverse", "--remove", "tcp:%d" % ADB_PORT], timeout=15)
        except Exception:
            pass

    def _ui(self, fn, *args):
        self.root.after(0, lambda: fn(*args))


def make_root():
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk(), True
    except Exception:
        return tk.Tk(), False


def _clean_drop(data):
    s = (data or "").strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    return s


def main():
    root, dnd = make_root()
    app = App(root)
    if dnd:
        from tkinterdnd2 import DND_FILES
        for w in (root, app.card):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: app.load_apk(_clean_drop(e.data)))
            except Exception:
                pass
    else:
        app._log("拖拽组件未加载，可点击卡片选择文件。", "muted")
    demo = os.environ.get("TCLPUSH_DEMO_APK")
    if demo and os.path.isfile(demo):
        app.load_apk(demo)
    if os.environ.get("TCLPUSH_DEMO_PAGE") == "settings":
        root.after(600, app._open_settings)
    root.mainloop()


if __name__ == "__main__":
    main()
