#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假电视：按 TCL 多屏互动的报文格式回话，用来在 PC 上回归 Java 协议层。

用法：
    python fake_tv.py --mode ok|denied|fail|stall|wrongpkg [--port 6553]

它会把自己的应答逐步打到 stdout，最后打印 SUMMARY 行供测试脚本断言。
"""
from __future__ import unicode_literals

import argparse
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

PKG = "com.example.demo"
LABEL = "演示应用"
VC = 121303001


def send(sock, text):
    body = text.encode("utf-8")
    sock.sendall(struct.pack(">i", len(body)) + body)


def recv_msgs(buf):
    out = []
    while len(buf) >= 4:
        n = struct.unpack(">i", buf[:4])[0]
        if n < 0 or len(buf) < 4 + n:
            break
        out.append(buf[4:4 + n].decode("utf-8", "replace"))
        buf = buf[4 + n:]
    return out, buf


def fetch(url):
    """模拟电视去下载 APK，返回 (状态码, 字节数)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "FakeTVCast"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return r.status, len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--port", type=int, default=6553)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    report = {"mode": args.mode, "frames": [], "http": None, "beat": 0, "ok": False}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(1)
    srv.settimeout(25)
    print("WAITING")

    try:
        conn, addr = srv.accept()
    except socket.timeout:
        print("TIMEOUT accept")
        return 1
    conn.settimeout(1.0)
    print("ACCEPTED %s" % (addr,))

    buf = b""
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            try:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            msgs, buf = recv_msgs(buf)
            for m in msgs:
                report["frames"].append(m)
                print("RECV %s" % m)
                if m == "159>>1>>0":
                    continue
                if m == "150>>":
                    report["beat"] += 1
                    continue
                parts = m.split(">>")
                if parts[0] != "223" or len(parts) < 5 or parts[1] != "INSTALL":
                    continue
                report["install_fields"] = len(parts)
                pkg, label, url = parts[2], parts[3], parts[4]
                size_mb, vcode, md5v = parts[7], parts[6], parts[-1]
                report["install"] = {"pkg": pkg, "label": label, "url": url,
                                     "size_mb": size_mb, "version_code": vcode, "md5": md5v}
                if pkg != PKG:
                    print("BADPKG %s" % pkg)
                    continue

                # 电视真正去把包拉下来
                try:
                    code, n = fetch(url)
                    report["http"] = {"status": code, "bytes": n}
                    print("FETCH %d %d" % (code, n))
                except Exception as e:
                    report["http"] = {"error": str(e)}
                    print("FETCHERR %s" % e)

                mode = args.mode
                send(conn, "223>>INSTALL>>%s>>%s>>confirm_pending" % (pkg, label))
                time.sleep(0.4)
                if mode == "denied":
                    # 真实电视上就是先弹确认框，用户可以点拒绝
                    send(conn, "223>>INSTALL>>%s>>%s>>denied" % (pkg, label))
                    time.sleep(0.3)
                    report["ok"] = True
                    break
                send(conn, "223>>PROGRESS>>%s>>%s>>1>>10>>x" % (pkg, label))
                time.sleep(0.4)
                if mode == "fail":
                    send(conn, "223>>PROGRESS>>%s>>%s>>2>>0>>x" % (pkg, label))
                    time.sleep(0.3)
                    report["ok"] = True
                    break
                if mode == "wrongpkg":
                    # 冒一个别的包名的完成消息，客户端必须忽略它
                    send(conn, "223>>PROGRESS>>com.other.app>>Other>>5>>100>>x")
                    time.sleep(0.9)
                send(conn, "223>>PROGRESS>>%s>>%s>>3>>50>>x" % (pkg, label))
                if mode == "slow":
                    print("SLOW")
                    time.sleep(30)
                    report["ok"] = True
                    break
                if mode == "stall":
                    # 进安装阶段后彻底沉默，客户端应因「无进展」超时
                    print("STALL")
                    time.sleep(20)
                    report["ok"] = True
                    break
                # 停留 6.5 秒，顺带验证客户端会不会发心跳
                time.sleep(6.5)
                send(conn, "223>>PROGRESS>>%s>>%s>>5>>100>>x" % (pkg, label))
                time.sleep(0.3)
                report["ok"] = True
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass
        srv.close()

    print("SUMMARY " + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
