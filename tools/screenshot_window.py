"""开发期截图工具：只截取目标窗口本身（PrintWindow），不截整个桌面，避开另一块屏幕的干扰。

用法：
    python tools/screenshot_window.py 输出.png [演示用APK] [main|settings]

启动主程序（可用环境变量预填演示数据），等窗口出现后截图，再关掉进程。依赖 Pillow。
"""
import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import time

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def find_window(title_part):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def cb(hwnd, lp):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def capture(hwnd, out_path):
    rect = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mfc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(mfc, bmp)
    ok = user32.PrintWindow(hwnd, mfc, 2)
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                    ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                    ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                    ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = width, -height
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mfc, bmp, 0, height, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(out_path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc)
    user32.ReleaseDC(hwnd, hdc)
    return ok, (width, height)


if __name__ == "__main__":
    out = sys.argv[1]
    apk = sys.argv[2] if len(sys.argv) > 2 else ""
    env = dict(os.environ)
    if apk:
        env["TCLPUSH_DEMO_APK"] = apk
    if len(sys.argv) > 3:
        env["TCLPUSH_DEMO_PAGE"] = sys.argv[3]
    py = sys.executable
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tcl_push.py")
    p = subprocess.Popen([py, src], env=env)
    hwnd = None
    for _ in range(30):
        time.sleep(0.5)
        hwnd = find_window("TCL 多屏互动推送")
        if hwnd:
            break
    if not hwnd:
        print("没找到窗口")
        p.kill()
        sys.exit(1)
    time.sleep(1.5)
    ok, size = capture(hwnd, out)
    print("PrintWindow ok=%s size=%s -> %s" % (ok, size, out))
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
