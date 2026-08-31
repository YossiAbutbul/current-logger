#!/usr/bin/env python3
"""
fsc-bridge.py - local bridge so a browser page can reach an R&S FSC3.

Serves fsc3-panel.html over http://localhost:8765/ and exposes a WebSocket at
/ws that relays SCPI lines to the instrument over either:

    tcp://<host>:5555             LAN (raw SCPI socket; R&S handhelds use 5555,
                                  many other R&S instruments use 5025)
    serial://COM5?baud=115200     Windows COM port
    serial://COM5?raw=1           COM port, skipping CDC line configuration

NOTE on raw=1: it exists for vendor CDC devices (bInterfaceProtocol 0xFF) whose
control requests fail. It was tested against this FSC3 and did NOT work - writes
reach the bulk OUT pipe but the instrument never answers, because usbser.sys
cannot issue SET_CONTROL_LINE_STATE (EscapeCommFunction SETDTR -> WinError 31).
That device needs the R&S USB driver bound in place of Microsoft's usbser.sys.

Standard library only - no pip install. Python 3.8+.

    python fsc-bridge.py
    python fsc-bridge.py --open tcp://172.16.10.1:5555

Then open http://localhost:8765/ and pick the transport in the page.
"""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "fsc3-panel.html")

# ─────────────────────────────── transports ────────────────────────────────


class TcpLink:
    """Raw SCPI socket - R&S handhelds (FSC/FSH) answer on 5555, others on 5025."""

    def __init__(self, host, port=5555, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(0.2)
        self.info = f"tcp://{host}:{port}"

    def write(self, data: bytes):
        self.sock.sendall(data)

    def read(self) -> bytes:
        try:
            return self.sock.recv(65536)
        except socket.timeout:
            return b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class SerialLink:
    """Windows COM port via kernel32 - avoids a pyserial dependency."""

    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING = 3

    class DCB(ctypes.Structure):
        _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
                    ("fFlags", wintypes.DWORD), ("wReserved", wintypes.WORD),
                    ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                    ("ByteSize", ctypes.c_ubyte), ("Parity", ctypes.c_ubyte),
                    ("StopBits", ctypes.c_ubyte), ("XonChar", ctypes.c_char),
                    ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                    ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char),
                    ("wReserved1", wintypes.WORD)]

    class TIMEOUTS(ctypes.Structure):
        _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                    ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                    ("ReadTotalTimeoutConstant", wintypes.DWORD),
                    ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                    ("WriteTotalTimeoutConstant", wintypes.DWORD)]

    def __init__(self, port, baud=115200, flow="none", raw=False):
        if os.name != "nt":
            raise RuntimeError("serial:// transport is Windows-only in this bridge")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p]
        for f in (k.SetCommState, k.GetCommState, k.SetCommTimeouts,
                  k.ReadFile, k.WriteFile, k.CloseHandle, k.PurgeComm):
            f.argtypes = None
        self.k = k
        self.h = k.CreateFileW(rf"\\.\{port}", self.GENERIC_READ | self.GENERIC_WRITE,
                               0, None, self.OPEN_EXISTING, 0, None)
        if self.h == ctypes.c_void_p(-1).value or self.h is None:
            err = ctypes.get_last_error()
            hint = ""
            if err == 31:
                hint = ("  <- ERROR_GEN_FAILURE: the port exists but the bound driver "
                        "cannot open it. Install the vendor (R&S) USB driver; the "
                        "Microsoft usbser.sys driver does not work with this device.")
            elif err == 5:
                hint = "  <- access denied: another program already holds the port."
            raise OSError(f"CreateFile {port} failed, WinError {err}{hint}")

        def bail(what):
            err = ctypes.get_last_error()
            k.CloseHandle(ctypes.c_void_p(self.h))   # never leak the port handle
            self.h = None
            hint = ("  <- the port opens but the bound driver rejects CDC line "
                    "configuration. Install the vendor (R&S) USB driver, or use "
                    "raw=1 to skip line configuration.") if err == 31 else ""
            raise OSError(f"{what} failed, WinError {err}{hint}")

        if raw:
            # Some vendor CDC devices (protocol 0xFF) have no SET_LINE_CODING and
            # fail SetCommState; the bulk pipes still work, so skip configuration.
            self.info = f"serial://{port}?raw=1"
            k.SetCommTimeouts(ctypes.c_void_p(self.h), ctypes.byref(
                self.TIMEOUTS(50, 0, 300, 0, 2000)))
            return

        dcb = self.DCB()
        dcb.DCBlength = ctypes.sizeof(dcb)
        if not k.GetCommState(ctypes.c_void_p(self.h), ctypes.byref(dcb)):
            bail("GetCommState")
        dcb.BaudRate, dcb.ByteSize, dcb.Parity, dcb.StopBits = baud, 8, 0, 0
        # fBinary=1, fParity=0, fDtrControl=DTR_ENABLE(1)<<4, fRtsControl<<12
        flags = 0x0001 | (1 << 4)
        if flow == "hardware":
            flags |= (1 << 2)          # fOutxCtsFlow
            flags |= (2 << 12)         # fRtsControl = RTS_CONTROL_HANDSHAKE
        else:
            flags |= (1 << 12)         # fRtsControl = RTS_CONTROL_ENABLE
        dcb.fFlags = flags
        if not k.SetCommState(ctypes.c_void_p(self.h), ctypes.byref(dcb)):
            bail("SetCommState")

        t = self.TIMEOUTS(ReadIntervalTimeout=50, ReadTotalTimeoutMultiplier=0,
                          ReadTotalTimeoutConstant=200,
                          WriteTotalTimeoutMultiplier=0, WriteTotalTimeoutConstant=2000)
        k.SetCommTimeouts(ctypes.c_void_p(self.h), ctypes.byref(t))
        k.PurgeComm(ctypes.c_void_p(self.h), 0x000F)
        self.info = f"serial://{port}?baud={baud}"

    def write(self, data: bytes):
        n = wintypes.DWORD()
        if not self.k.WriteFile(ctypes.c_void_p(self.h), data, len(data),
                                ctypes.byref(n), None):
            raise OSError(f"WriteFile failed, WinError {ctypes.get_last_error()}")

    def read(self) -> bytes:
        buf = ctypes.create_string_buffer(4096)
        n = wintypes.DWORD()
        if not self.k.ReadFile(ctypes.c_void_p(self.h), buf, 4096,
                               ctypes.byref(n), None):
            raise OSError(f"ReadFile failed, WinError {ctypes.get_last_error()}")
        return buf.raw[:n.value]

    def close(self):
        if self.h is None:
            return
        try:
            self.k.CloseHandle(ctypes.c_void_p(self.h))
        except Exception:
            pass
        self.h = None


def list_com_ports():
    """Enumerate COM ports from the registry (no pyserial needed)."""
    out = []
    if os.name != "nt":
        return out
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(key, i)
                out.append({"port": val, "device": name})
                i += 1
            except OSError:
                break
    except OSError:
        pass
    return out


def open_target(url, baud=115200, flow="none"):
    m = re.match(r"^tcp://([^:/]+)(?::(\d+))?/?$", url, re.I)
    if m:
        return TcpLink(m.group(1), int(m.group(2) or 5555))
    m = re.match(r"^serial://([^/?]+)(?:\?(.*))?$", url, re.I)
    if m:
        q = dict(p.split("=", 1) for p in (m.group(2) or "").split("&") if "=" in p)
        return SerialLink(m.group(1), int(q.get("baud", baud)), q.get("flow", flow),
                          q.get("raw", "0") not in ("0", "", "false"))
    raise ValueError(f"unrecognised target {url!r} - use tcp://host:5555 or serial://COM5")


# ────────────────────────────── websocket ──────────────────────────────────


def ws_send(sock, payload: str):
    data = payload.encode()
    n = len(data)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    sock.sendall(head + data)


def ws_frames(sock):
    """Yield decoded text payloads from a client socket."""
    buf = b""

    def need(n):
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    while True:
        b0, b1 = need(2)
        opcode, masked, ln = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", need(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", need(8))[0]
        mask = need(4) if masked else b"\0\0\0\0"
        data = bytearray(need(ln))
        if masked:
            for i in range(ln):
                data[i] ^= mask[i % 4]
        if opcode == 0x8:
            raise ConnectionError
        if opcode == 0x9:                      # ping -> pong
            sock.sendall(struct.pack("!BB", 0x8A, len(data)) + bytes(data))
            continue
        if opcode in (0x1, 0x2):
            yield data.decode("utf-8", "replace")


class Session:
    """One browser connection and its instrument link."""

    def __init__(self, sock, default_open=None):
        self.sock, self.link, self.rx_thread = sock, None, None
        self.term = "\n"
        self.lock = threading.Lock()
        self.alive = True
        if default_open:
            self.do_open(default_open)

    def emit(self, **kw):
        with self.lock:
            try:
                ws_send(self.sock, json.dumps(kw))
            except OSError:
                self.alive = False

    def do_open(self, url, baud=115200, flow="none", term="\n"):
        self.do_close()
        self.term = term
        try:
            self.link = open_target(url, baud, flow)
        except Exception as e:
            self.emit(ev="error", msg=str(e))
            return
        self.emit(ev="open", ok=True, info=self.link.info)
        print(f"  [open]  {self.link.info}")
        self.rx_thread = threading.Thread(target=self.pump, daemon=True)
        self.rx_thread.start()

    def pump(self):
        link, acc = self.link, b""
        while self.alive and self.link is link:
            try:
                chunk = link.read()
            except Exception as e:
                self.emit(ev="error", msg=f"read: {e}")
                break
            if not chunk:
                continue
            acc += chunk
            while b"\n" in acc or b"\r" in acc:
                idx = min(i for i in (acc.find(b"\n"), acc.find(b"\r")) if i >= 0)
                line, acc = acc[:idx], acc[idx + 1:]
                line = line.strip()
                if line:
                    self.emit(ev="rx", data=line.decode("latin-1"))

    def do_tx(self, text):
        if not self.link:
            self.emit(ev="error", msg="not connected to an instrument")
            return
        try:
            self.link.write((text + self.term).encode("latin-1"))
        except Exception as e:
            self.emit(ev="error", msg=f"write: {e}")

    def do_close(self):
        if self.link:
            link, self.link = self.link, None
            link.close()
            self.emit(ev="closed")
            print("  [close]")

    def serve(self):
        try:
            for raw in ws_frames(self.sock):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd = msg.get("cmd")
                if cmd == "open":
                    self.do_open(msg.get("url", ""), int(msg.get("baud", 115200)),
                                 msg.get("flow", "none"), msg.get("term", "\n"))
                elif cmd == "tx":
                    self.do_tx(msg.get("data", ""))
                elif cmd == "ports":
                    self.emit(ev="ports", list=list_com_ports())
                elif cmd == "close":
                    self.do_close()
        except (ConnectionError, OSError):
            pass
        finally:
            self.alive = False
            self.do_close()
            try:
                self.sock.close()
            except OSError:
                pass


# ──────────────────────────────── http ─────────────────────────────────────

def handle_client(sock, addr, default_open):
    try:
        sock.settimeout(10)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = sock.recv(4096)
            if not chunk:
                return
            req += chunk
        head = req.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        line = head.split("\r\n")[0]
        path = line.split(" ")[1] if " " in line else "/"

        if "upgrade: websocket" in head.lower():
            key = re.search(r"Sec-WebSocket-Key:\s*(\S+)", head, re.I)
            if not key:
                return
            accept = base64.b64encode(
                hashlib.sha1(key.group(1).encode() + WS_GUID).digest()).decode()
            sock.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
            sock.settimeout(None)
            print(f"  browser connected from {addr[0]}")
            Session(sock, default_open).serve()
            print("  browser disconnected")
            return

        if path in ("/", "/index.html", "/fsc3-panel.html"):
            if not os.path.exists(PAGE):
                body = b"fsc3-panel.html not found next to fsc-bridge.py"
                sock.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                return
            with open(PAGE, "rb") as f:
                body = f.read()
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
        else:
            sock.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Local bridge for the FSC3 web panel")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", dest="target", default=None,
                    help="auto-connect, e.g. tcp://172.16.10.1:5555 or serial://COM5")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(8)
    print(f"FSC3 bridge listening on http://{args.host}:{args.port}/")
    print(f"  panel     : {PAGE}")
    ports = list_com_ports()
    print(f"  COM ports : {', '.join(p['port'] for p in ports) or '(none)'}")
    if args.target:
        print(f"  auto-open : {args.target}")
    print("  Ctrl+C to stop\n")
    try:
        while True:
            sock, addr = srv.accept()
            threading.Thread(target=handle_client,
                             args=(sock, addr, args.target), daemon=True).start()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
