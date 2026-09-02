"""HTTP + WebSocket server for the TX current test. Standard library only."""

import base64
import hashlib
import json
import mimetypes
import os
import re
import signal
import socket
import struct
import threading
import urllib.parse

from . import storage
from .config import CHANNEL, DEFAULTS, RESOURCE, WEB_DIR, coerce
from .runner import CurrentTestRunner
from .session import ReferenceRecorder, Session

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.runner = None
        self.recorder = None
        self.session = None
        self.ref_peak_ma = None
        self.history = []

    def add(self, s):
        with self.lock:
            self.clients.append(s)

    def remove(self, s):
        with self.lock:
            if s in self.clients:
                self.clients.remove(s)

    def send_to(self, sock, msg):
        """Send to one client under the same lock emit() uses.

        The runner thread broadcasts through emit() while the socket's own
        handler thread replies directly; without a shared lock those two writes
        interleave and produce a corrupt WebSocket frame, which wedges the
        client's parser.
        """
        payload = json.dumps(msg)
        with self.lock:
            try:
                ws_send(sock, payload)
            except OSError:
                if sock in self.clients:
                    self.clients.remove(sock)

    def emit(self, msg):
        # A 48 h run produces thousands of events; keep only a live tail.
        if msg.get("ev") not in ("waiting",):
            slim = msg
            if msg.get("ev") == "capture":
                slim = {k: v for k, v in msg.items()
                        if k not in ("trace_ms", "trace_ma")}
            self.history.append(slim)
            del self.history[:-300]
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


def respond(sock, body, status="200 OK", ctype="application/json"):
    sock.sendall(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                 f"Cache-Control: no-store\r\nContent-Length: {len(body)}\r\n\r\n"
                 .encode() + body)


def json_response(sock, obj, status="200 OK"):
    respond(sock, json.dumps(obj).encode(), status,
            "application/json; charset=utf-8")


def serve_static(sock, url_path):
    rel = url_path.lstrip("/") or "index.html"
    full = os.path.normpath(os.path.join(WEB_DIR, rel))
    root = os.path.abspath(WEB_DIR)
    if not (full == os.path.join(root, "index.html")
            or full.startswith(root + os.sep)):
        return respond(sock, b"", "403 Forbidden", "text/plain")
    if not os.path.isfile(full):
        return respond(sock, b"not found", "404 Not Found", "text/plain")
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",
                                              "application/json"):
        ctype += "; charset=utf-8"
    with open(full, "rb") as f:
        respond(sock, f.read(), "200 OK", ctype)


def handle_ws(sock, head, hub, resource, channel, results_root):
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
        if hub.session is None:
            hub.session = Session(resource, channel)
        hub.send_to(sock, {"ev": "hello", "defaults": DEFAULTS,
                           "instrument": f"{resource} ch{channel}"})
        hub.send_to(sock, {"ev": "connection", **hub.session.info()})
        if hub.ref_peak_ma is not None:
            hub.send_to(sock, {"ev": "ref_done", "ok": True,
                               "ref_peak_ma": hub.ref_peak_ma,
                               "restored": True})
        if hub.runner and hub.runner.is_alive():
            for m in hub.history[-160:]:
                hub.send_to(sock, m)
            hub.send_to(sock, {"ev": "state", "state": hub.runner.state})
        else:
            for m in reversed(hub.history):
                if m.get("ev") == "finished":
                    hub.send_to(sock, {**m, "replay": True})
                    break

        def busy():
            # "finished" is emitted from inside run(), so the thread is still
            # alive while it does its cleanup. Treat that as not busy, or an
            # immediate disconnect after a run gets refused.
            r, rec = hub.runner, hub.recorder
            return ((r is not None and r.is_alive() and r.state != "finished")
                    or (rec is not None and rec.is_alive()))

        def settle():
            """Let a just-finished runner complete its cleanup."""
            r = hub.runner
            if r is not None and r.is_alive() and r.state == "finished":
                r.join(timeout=8)

        def err(m):
            hub.send_to(sock, {"ev": "error", "msg": m})

        for raw in ws_frames(sock):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            cmd = msg.get("cmd")

            # ---------------------------------------------------- connection
            if cmd == "connect":
                if busy():
                    err("cannot reconnect while a run or reference is active")
                    continue
                settle()
                try:
                    info = hub.session.connect(msg.get("resource") or resource,
                                               msg.get("channel") or channel)
                    hub.emit({"ev": "connection", **info})
                    hub.emit({"ev": "log", "msg": f"connected: {info.get('idn')}",
                              "t": storage.now_iso()})
                except Exception as e:
                    hub.emit({"ev": "connection", **hub.session.info()})
                    err(f"connect failed: {type(e).__name__}: {e}")
            elif cmd == "disconnect":
                if busy():
                    err("stop the run before disconnecting")
                    continue
                settle()
                hub.emit({"ev": "connection", **hub.session.disconnect()})
                hub.emit({"ev": "log", "msg": "disconnected (acquisition settings "
                                              "restored)", "t": storage.now_iso()})
            elif cmd == "status":
                hub.send_to(sock, {"ev": "connection",
                                   **hub.session.refresh_state()})

            # ----------------------------------------------- reference session
            elif cmd == "record_ref":
                if not hub.session.connected:
                    err("connect to the analyzer first")
                    continue
                if busy():
                    err("a run or reference session is already active")
                    continue
                try:
                    cfg = coerce(msg.get("config"))
                except (TypeError, ValueError) as e:
                    err(f"bad configuration: {e}")
                    continue
                hub.recorder = ReferenceRecorder(hub.session, cfg, hub.emit)
                hub.recorder.start()
            elif cmd == "cancel_ref":
                if hub.recorder and hub.recorder.is_alive():
                    hub.recorder.stop()
            elif cmd == "set_ref":
                try:
                    hub.ref_peak_ma = (None if msg.get("ref_peak_ma") in (None, "")
                                       else float(msg["ref_peak_ma"]))
                except (TypeError, ValueError):
                    err("reference must be a number")
                    continue
                hub.emit({"ev": "ref_set", "ref_peak_ma": hub.ref_peak_ma})

            # ------------------------------------------------------------ run
            elif cmd == "start":
                if not hub.session.connected:
                    err("connect to the analyzer first")
                    continue
                if busy():
                    err("a run or reference session is already active")
                    continue
                try:
                    cfg = coerce(msg.get("config"))
                except (TypeError, ValueError) as e:
                    err(f"bad configuration: {e}")
                    continue
                ref = msg.get("ref_peak_ma", hub.ref_peak_ma)
                try:
                    ref = None if ref in (None, "") else float(ref)
                except (TypeError, ValueError):
                    ref = None
                hub.ref_peak_ma = ref
                hub.history.clear()
                hub.runner = CurrentTestRunner(cfg, hub.session, hub.emit,
                                               results_root, ref)
                hub.emit({"ev": "started", "config": cfg, "ref_peak_ma": ref,
                          "stop_level_ma": (None if ref is None
                                            else ref - cfg["drop_delta_ma"])})
                hub.runner.start()
            elif cmd == "stop":
                if hub.runner and hub.runner.is_alive():
                    hub.runner.stop()
                else:
                    err("no run in progress")
    except (ConnectionError, OSError):
        pass
    finally:
        hub.remove(sock)


def handle_client(sock, hub, resource, channel, results_root):
    try:
        sock.settimeout(20)
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
            return handle_ws(sock, head, hub, resource, channel, results_root)

        if parsed.path == "/api/resources":
            try:
                import pyvisa
                rm = pyvisa.ResourceManager()
                res = list(rm.list_resources())
                rm.close()
            except Exception as e:
                return json_response(sock, {"resources": [],
                                            "error": f"{type(e).__name__}: {e}"})
            return json_response(sock, {"resources": res})
        if parsed.path == "/api/runs":
            return json_response(sock, {"runs": storage.list_runs(results_root)})
        if parsed.path == "/api/run":
            r = storage.read_run(qs.get("name", [""])[0], results_root)
            return json_response(sock, r or {"error": "not found"},
                                 "200 OK" if r else "404 Not Found")
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


def serve(bind, port, resource=RESOURCE, channel=CHANNEL, results_root=None):
    hub = Hub()
    hub.session = Session(resource, channel)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # being listened on, so a stray second instance silently steals connections
    # instead of failing. SO_EXCLUSIVEADDRUSE is the correct flag there.
    if os.name == "nt":
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind, port))
    except OSError as e:
        srv.close()
        print(f"Cannot listen on {bind}:{port} - {e.strerror or e}")
        print("Another copy of this app is probably already running. Close it,")
        print(f"or start this one on a different port:  --port {port + 1}")
        return
    srv.listen(8)
    # A blocking accept() on Windows is not interrupted by Ctrl+C: the signal is
    # only delivered when the interpreter regains control, which never happens
    # while it waits. A short timeout gives it that chance.
    srv.settimeout(0.5)

    print(f"TX current decay test  ->  http://{bind}:{port}/")
    print(f"  instrument : {resource}  channel {channel}")
    print(f"  results    : {storage.results_root(results_root)}")
    print("  measure-only: EMUL / OUTP / VOLT / CURR:LIM / CURR:RANG are never written")
    print("  Ctrl+C to stop\n")

    # Ctrl+C is handled through a flag rather than by catching KeyboardInterrupt:
    # the exception can surface at any bytecode boundary, including inside the
    # handler or the cleanup, which is how it escaped as a traceback. A signal
    # handler makes shutdown deterministic and keeps the console clean.
    shutdown = threading.Event()
    previous = {}

    def on_signal(signum, frame):
        if shutdown.is_set():
            # a second Ctrl+C means "stop waiting for cleanup"
            if signum in previous:
                signal.signal(signum, previous[signum])
            raise KeyboardInterrupt
        shutdown.set()

    # SIGBREAK is Ctrl+Break on Windows, which does not raise SIGINT
    for signame in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, on_signal)
        except (ValueError, OSError, RuntimeError):
            pass                 # not the main thread; fall back to the except

    try:
        while not shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(target=handle_client,
                             args=(conn, hub, resource, channel, results_root),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, RuntimeError):
                pass
        _shutdown(hub, srv)


def _shutdown(hub, srv):
    """Stop work and close the instrument link.

    Client threads are daemons and would be killed mid-command, so the run is
    stopped and the session closed here - that restores the acquisition settings
    now rather than leaving them for the next connect to recover.
    """
    print("\nStopping...")
    try:
        if hub.runner is not None and hub.runner.is_alive():
            print("  ending the run in progress")
            hub.runner.stop()
            hub.runner.join(timeout=20)
        if hub.recorder is not None and hub.recorder.is_alive():
            print("  ending the reference recording")
            hub.recorder.stop()
            hub.recorder.join(timeout=20)
        if hub.session is not None and hub.session.connected:
            hub.session.disconnect()
            print("  instrument released, acquisition settings restored")
    except KeyboardInterrupt:
        print("  cleanup interrupted - the instrument may keep this run's "
              "acquisition settings until the next connect")
    except Exception as e:
        print(f"  cleanup problem: {type(e).__name__}: {e}")
    finally:
        try:
            srv.close()
        except OSError:
            pass
    print("Stopped.")
