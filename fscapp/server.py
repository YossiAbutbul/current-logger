"""HTTP + WebSocket server: serves web/ and relays test events.

Standard library only. The WebSocket implementation covers exactly what this
app needs - text frames, ping/pong, close - and nothing more.
"""

import base64
import hashlib
import json
import mimetypes
import os
import re
import socket
import struct
import threading
import urllib.parse

from . import storage
from .config import DEFAULTS, WEB_DIR, coerce
from .runner import TestRunner

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ────────────────────────────── websocket ──────────────────────────────────


def ws_send(sock, payload):
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
        if opcode == 0x9:
            sock.sendall(struct.pack("!BB", 0x8A, len(data)) + bytes(data))
            continue
        if opcode in (0x1, 0x2):
            yield data.decode("utf-8", "replace")


class Hub:
    """Fans test events out to every connected browser."""

    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.runner = None
        self.history = []

    def add(self, sock):
        with self.lock:
            self.clients.append(sock)

    def remove(self, sock):
        with self.lock:
            if sock in self.clients:
                self.clients.remove(sock)

    def emit(self, msg):
        if msg.get("ev") != "waiting":
            self.history.append(msg)
            del self.history[:-400]
        payload = json.dumps(msg)
        with self.lock:
            dead = []
            for c in self.clients:
                try:
                    ws_send(c, payload)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)


# ──────────────────────────────── static ───────────────────────────────────


def serve_static(sock, url_path):
    """Serve a file from web/. Rejects anything that escapes that directory."""
    rel = url_path.lstrip("/") or "index.html"
    full = os.path.normpath(os.path.join(WEB_DIR, rel))
    if not full.startswith(os.path.abspath(WEB_DIR) + os.sep) \
            and full != os.path.abspath(os.path.join(WEB_DIR, "index.html")):
        return respond(sock, b"", "403 Forbidden", "text/plain")
    if not os.path.isfile(full):
        return respond(sock, b"not found", "404 Not Found", "text/plain")
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",
                                              "application/json"):
        ctype += "; charset=utf-8"
    with open(full, "rb") as f:
        respond(sock, f.read(), "200 OK", ctype)


def respond(sock, body, status="200 OK", ctype="application/json"):
    sock.sendall(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                 f"Cache-Control: no-store\r\nContent-Length: {len(body)}\r\n\r\n"
                 .encode() + body)


def json_response(sock, obj, status="200 OK"):
    respond(sock, json.dumps(obj).encode(), status, "application/json; charset=utf-8")


# ──────────────────────────────── routing ──────────────────────────────────


def handle_ws(sock, head, hub, host, scpi_port, results_root):
    key = re.search(r"Sec-WebSocket-Key:\s*(\S+)", head, re.I)
    if not key:
        return
    accept = base64.b64encode(
        hashlib.sha1(key.group(1).encode() + WS_GUID).digest()).decode()
    sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                 b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                 + accept.encode() + b"\r\n\r\n")
    sock.settimeout(None)
    hub.add(sock)
    try:
        ws_send(sock, json.dumps({"ev": "hello", "defaults": DEFAULTS,
                                  "instrument": f"{host}:{scpi_port}"}))
        # Replay a live run so a reopened tab catches up. For a run that already
        # ended send only its result, tagged, so it cannot masquerade as current.
        if hub.runner and hub.runner.is_alive():
            for m in hub.history[-120:]:
                ws_send(sock, json.dumps(m))
            ws_send(sock, json.dumps({"ev": "state", "state": hub.runner.state}))
        else:
            for m in reversed(hub.history):
                if m.get("ev") == "finished":
                    ws_send(sock, json.dumps({**m, "replay": True}))
                    break

        for raw in ws_frames(sock):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            cmd = msg.get("cmd")
            if cmd == "start":
                if hub.runner and hub.runner.is_alive():
                    ws_send(sock, json.dumps(
                        {"ev": "error", "msg": "a test is already running"}))
                    continue
                try:
                    cfg = coerce(msg.get("config"))
                except (TypeError, ValueError) as e:
                    ws_send(sock, json.dumps(
                        {"ev": "error", "msg": f"bad configuration: {e}"}))
                    continue
                hub.history.clear()
                hub.runner = TestRunner(cfg, host, scpi_port, hub.emit, results_root)
                hub.emit({"ev": "started", "config": cfg})
                hub.runner.start()
            elif cmd == "stop":
                if hub.runner and hub.runner.is_alive():
                    hub.runner.stop()
                else:
                    ws_send(sock, json.dumps(
                        {"ev": "error", "msg": "no test is running"}))
    except (ConnectionError, OSError):
        pass
    finally:
        hub.remove(sock)


def handle_client(sock, hub, host, scpi_port, results_root):
    try:
        sock.settimeout(15)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = sock.recv(4096)
            if not chunk:
                return
            req += chunk
        head = req.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        line = head.split("\r\n")[0]
        path = line.split(" ")[1] if " " in line else "/"
        parsed = urllib.parse.urlparse(path)
        qs = urllib.parse.parse_qs(parsed.query)

        if "upgrade: websocket" in head.lower():
            return handle_ws(sock, head, hub, host, scpi_port, results_root)

        if parsed.path == "/api/tests":
            return json_response(sock, {"tests": storage.list_tests(results_root)})
        if parsed.path == "/api/test":
            t = storage.read_test(qs.get("name", [""])[0], results_root)
            return json_response(sock, t or {"error": "not found"},
                                 "200 OK" if t else "404 Not Found")
        if parsed.path == "/api/capture":
            c = storage.read_capture(qs.get("name", [""])[0],
                                     qs.get("file", [""])[0], results_root)
            return json_response(sock, c or {"error": "not found"},
                                 "200 OK" if c else "404 Not Found")

        serve_static(sock, "/index.html" if parsed.path == "/" else parsed.path)
    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def serve(bind, port, host, scpi_port, results_root=None):
    hub = Hub()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(8)
    print(f"FSC3 burst-decay test  ->  http://{bind}:{port}/")
    print(f"  instrument : {host}:{scpi_port}")
    print(f"  results    : {storage.results_root(results_root)}")
    print("  Ctrl+C to stop\n")
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle_client,
                             args=(conn, hub, host, scpi_port, results_root),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.close()
