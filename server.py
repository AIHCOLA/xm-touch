#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小满遥控 (xm-touch)
把手机变成 Windows 电脑的触摸板 / 遥控器，同一 WiFi 内使用，手机端零安装。

零第三方依赖：只用 Python 标准库 + Win32 API (ctypes)。

用法:
    python server.py                # 默认端口 8765，随机 4 位配对码
    python server.py --port 9000
    python server.py --pin 1234     # 固定配对码
    python server.py --no-pin       # 关闭配对码（不推荐）
"""

import argparse
import base64
import ctypes
import hashlib
import json
import random
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

if sys.platform != "win32":
    sys.exit("本工具只支持 Windows。")

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------- Win32 结构
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

# 鼠标事件标志
MOVE = 0x0001
LEFTDOWN, LEFTUP = 0x0002, 0x0004
RIGHTDOWN, RIGHTUP = 0x0008, 0x0010
MIDDLEDOWN, MIDDLEUP = 0x0020, 0x0040
WHEEL, HWHEEL = 0x0800, 0x1000
BTN = {0: (LEFTDOWN, LEFTUP), 1: (MIDDLEDOWN, MIDDLEUP), 2: (RIGHTDOWN, RIGHTUP)}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "space": 0x20,
    "delete": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "apps": 0x5D,
    "media_play": 0xB3, "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    "vol_mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF,
    "print": 0x2C, "f5": 0x74, "f11": 0x7A,
}

_last_err = 0


def _send(*items):
    arr = (INPUT * len(items))(*items)
    n = user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))
    return n


def move_rel(dx, dy):
    dx = max(-3000, min(3000, int(dx)))
    dy = max(-3000, min(3000, int(dy)))
    if dx == 0 and dy == 0:
        return
    _send(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, MOVE, 0, 0))))


def button(down, btn=0):
    f = BTN.get(btn, BTN[0])[0 if down else 1]
    _send(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, f, 0, 0))))


def click(btn=0, n=1):
    for i in range(max(1, min(3, int(n)))):
        button(True, btn)
        time.sleep(0.012)
        button(False, btn)
        if i + 1 < n:
            time.sleep(0.055)


def wheel(delta):
    # delta 单位为 120 的整数倍，正数=向上滚动
    step = max(-1200, min(1200, int(delta)))
    if step == 0:
        return
    _send(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, step & 0xFFFFFFFF, WHEEL, 0, 0))))


def hwheel(delta):
    step = max(-1200, min(1200, int(delta)))
    if step == 0:
        return
    _send(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, step & 0xFFFFFFFF, HWHEEL, 0, 0))))


def key_press(vk, mods=()):
    mods = [VK[m] for m in mods if m in VK]
    seq = []
    for m in mods:
        seq.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(m, 0, 0, 0, 0))))
    seq.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0))))
    seq.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))))
    for m in reversed(mods):
        seq.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(m, 0, KEYEVENTF_KEYUP, 0, 0))))
    _send(*seq)


def send_text(text):
    if not text:
        return
    seq = []
    for ch in text:
        for i in range(0, len(ch.encode("utf-16-le")), 2):
            unit = int.from_bytes(ch.encode("utf-16-le")[i:i + 2], "little")
            seq.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0))))
            seq.append(INPUT(type=1, u=_INPUTUNION(
                ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    if seq:
        _send(*seq)


# ---------------------------------------------------------------- 全局状态
class State:
    def __init__(self):
        self.clients = 0
        self.lock = threading.Lock()

    def add(self, n):
        with self.lock:
            self.clients += n
            return self.clients


STATE = State()
CONFIG = {"pin": "", "port": 8765}


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------- WebSocket
def read_frame(rfile):
    hdr = rfile.read(2)
    if not hdr or len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 >> 7
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b"\x00\x00\x00\x00"
    data = rfile.read(length) if length else b""
    if masked and data:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def send_frame(wfile, opcode, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    wfile.write(bytes(head) + payload)
    wfile.flush()


# ---------------------------------------------------------------- 指令处理
def handle_message(msg, conn):
    t = msg.get("t")
    if t == "move":
        move_rel(msg.get("dx", 0), msg.get("dy", 0))
    elif t == "click":
        click(int(msg.get("btn", 0)), int(msg.get("n", 1)))
    elif t == "down":
        button(True, int(msg.get("btn", 0)))
    elif t == "up":
        button(False, int(msg.get("btn", 0)))
    elif t == "wheel":
        # 客户端传像素，50px 折合一个滚轮档位，余量累积
        conn.wheel_acc += float(msg.get("dy", 0))
        step = int(conn.wheel_acc / 50.0)
        if step:
            conn.wheel_acc -= step * 50.0
            wheel(step * 120)
    elif t == "hwheel":
        conn.hwheel_acc += float(msg.get("dx", 0))
        step = int(conn.hwheel_acc / 50.0)
        if step:
            conn.hwheel_acc -= step * 50.0
            hwheel(step * 120)
    elif t == "text":
        send_text(msg.get("s", ""))
    elif t == "key":
        k = msg.get("k", "")
        vk = VK.get(k)
        if vk is None and len(k) == 1:
            vk = ord(k.upper())
        if vk is not None:
            key_press(vk, msg.get("mods") or [])
    elif t == "combo":
        seq = msg.get("keys") or []
        vks = [VK.get(k, ord(k.upper()) if len(k) == 1 else None) for k in seq]
        vks = [v for v in vks if v]
        if vks:
            ins = [INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(v, 0, 0, 0, 0))) for v in vks]
            ins += [INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(v, 0, KEYEVENTF_KEYUP, 0, 0)))
                    for v in reversed(vks)]
            _send(*ins)
    elif t == "ping":
        pass


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xm-touch"
    wheel_acc = 0.0
    hwheel_acc = 0.0

    def log_message(self, fmt, *args):
        pass

    # ---- 工具
    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name, ctype="text/html; charset=utf-8"):
        p = STATIC / name
        if not p.exists():
            self.send_error(404)
            return
        self._send_bytes(p.read_bytes(), ctype)

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/ws":
            return self._ws(u)
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path in ("/control", "/control.html"):
            return self._file("control.html")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/qr.png":
            return self._qr(u)
        if path == "/api/state":
            body = json.dumps({
                "clients": STATE.clients,
                "pin": CONFIG["pin"],
                "port": CONFIG["port"],
                "url": f"http://{local_ip()}:{CONFIG['port']}",
            }, ensure_ascii=False).encode()
            return self._send_bytes(body, "application/json; charset=utf-8")
        # 静态资源兜底（图标等）
        try:
            p = (STATIC / path.lstrip("/")).resolve()
            if p.is_file() and STATIC.resolve() in p.parents:
                ctype = "text/html; charset=utf-8"
                if p.suffix == ".js":
                    ctype = "application/javascript; charset=utf-8"
                elif p.suffix == ".css":
                    ctype = "text/css; charset=utf-8"
                return self._send_bytes(p.read_bytes(), ctype)
        except Exception:
            pass
        self.send_error(404)

    def _qr(self, u):
        data = (parse_qs(u.query).get("data") or [""])[0]
        if not data:
            self.send_error(400)
            return
        try:
            import qrcode
            img = qrcode.make(data, border=2, box_size=8)
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return self._send_bytes(buf.getvalue(), "image/png")
        except Exception:
            self.send_error(500)

    # ---- WebSocket
    def _ws(self, u):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(400)
            return
        pin = (parse_qs(u.query).get("pin") or [""])[0]
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        if CONFIG["pin"] and pin != CONFIG["pin"]:
            send_frame(self.wfile, 0x1, json.dumps({"type": "denied"}, ensure_ascii=False))
            send_frame(self.wfile, 0x8, b"")
            self.close_connection = True
            return

        self.wheel_acc = 0.0
        self.hwheel_acc = 0.0
        n = STATE.add(1)
        print(f"  [+] 手机已连接（在线 {n}）", flush=True)
        try:
            send_frame(self.wfile, 0x1, json.dumps({"type": "hello"}, ensure_ascii=False))
            while True:
                fr = read_frame(self.rfile)
                if fr is None:
                    break
                opcode, data = fr
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    send_frame(self.wfile, 0xA, data)
                    continue
                if opcode == 0x1:
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    handle_message(msg, self)
        except (ConnectionResetError, ConnectionAbortedError, OSError, ValueError):
            pass
        finally:
            n = STATE.add(-1)
            print(f"  [-] 手机已断开（在线 {n}）", flush=True)
            self.close_connection = True


# ---------------------------------------------------------------- 启动
def banner(url, pin):
    line = "=" * 46
    print(f"\n{line}")
    print("  小满遥控 · 已启动")
    print(line)
    print(f"  手机浏览器打开:  {url}")
    print(f"  配对码:          {pin}")
    print(f"{line}")
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(f"{url}/?pin={pin}")
        q.make()
        q.print_ascii(invert=True)
    except Exception:
        print("  （未安装 qrcode，跳过二维码，手动输地址即可）")
    print("  手机与电脑需在同一 WiFi。Ctrl+C 停止。\n")


class Server(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会允许多个进程绑同一端口，容易误开两份，这里禁掉
    allow_reuse_address = False
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pin", default="")
    ap.add_argument("--no-pin", action="store_true")
    ap.add_argument("--no-browser", action="store_true", help="启动时不自动打开电脑端控制页")
    args = ap.parse_args()

    pin = "" if args.no_pin else (args.pin or f"{random.randint(0, 9999):04d}")
    CONFIG["pin"] = pin
    CONFIG["port"] = args.port

    url = f"http://{local_ip()}:{args.port}"
    try:
        srv = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        print(f"\n[!] 端口 {args.port} 已被占用，可能已经启动过一个实例了。")
        print("    可以换个端口：python server.py --port 8766\n")
        sys.exit(1)
    banner(url, pin or "（无）")
    if not args.no_browser:
        try:
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/control")).start()
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
