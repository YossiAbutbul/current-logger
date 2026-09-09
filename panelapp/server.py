"""HTTP + WebSocket server for the FSC3 control panel. Standard library only."""

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

from .config import (DETECTORS, INSTRUMENT_HOST, INSTRUMENT_PORT, MARKERS,
                     SWEEP_PERIODS, TRACE_MODES, WEB_DIRS)
from .session import Session, Sweeper, now_iso

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MHZ = 1e6


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
        self.session = None
        self.sweeper = None

    def add(self, s):
        with self.lock:
            self.clients.append(s)

    def remove(self, s):
        with self.lock:
            if s in self.clients:
                self.clients.remove(s)

    def send_to(self, sock, msg):
        """Send to one client under the lock emit() uses: the sweeper thread
        broadcasts while a socket's own thread replies, and unsynchronised
        writes interleave into a corrupt frame that wedges the client parser."""
        payload = json.dumps(msg)
        with self.lock:
            try:
                ws_send(sock, payload)
            except OSError:
                if sock in self.clients:
                    self.clients.remove(sock)

    def emit(self, msg):
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
    """Look the file up in each static root in turn (panel first, then the
    shared design system)."""
    rel = url_path.lstrip("/") or "index.html"
    for web_dir in WEB_DIRS:
        root = os.path.abspath(web_dir)
        full = os.path.normpath(os.path.join(root, rel))
        if not (full == os.path.join(root, "index.html")
                or full.startswith(root + os.sep)):
            return respond(sock, b"", "403 Forbidden", "text/plain")
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",
                                                      "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                return respond(sock, f.read(), "200 OK", ctype)
    respond(sock, b"not found", "404 Not Found", "text/plain")


def handle_ws(sock, head, hub, host, port):
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

    ses = hub.session

    def log(msg):
        hub.emit({"ev": "log", "msg": msg, "t": now_iso()})

    def err(msg):
        hub.send_to(sock, {"ev": "error", "msg": msg})

    def push_state():
        hub.emit({"ev": "state", "state": ses.refresh(),
                  "connection": {"connected": ses.connected, "idn":
                                 ses.an.idn if ses.an else ""}})

    def num(m, key, scale=1.0):
        v = m.get(key)
        if v in (None, ""):
            return None
        return float(v) * scale

    def act(fn, *a, **kw):
        """Run one instrument operation atomically, then republish the state."""
        if not ses.connected:
            err("connect to the analyzer first")
            return
        try:
            with ses.lock:
                problems = fn(*a, **kw) or []
        except Exception as e:
            err(f"{type(e).__name__}: {e}")
            return
        for p in problems:
            log(p)
        if problems:
            hub.emit({"ev": "rejected", "problems": problems})
        push_state()

    try:
        hub.send_to(sock, {"ev": "hello", "host": ses.host, "port": ses.port,
                           "markers": MARKERS, "trace_modes": list(TRACE_MODES),
                           "detectors": list(DETECTORS),
                           "periods": list(SWEEP_PERIODS)})
        hub.send_to(sock, {"ev": "connection", **ses.info()})
        if hub.sweeper:
            hub.send_to(sock, {"ev": "live", "live": hub.sweeper.live,
                               "period": hub.sweeper.period})

        for raw in ws_frames(sock):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            cmd = msg.get("cmd")

            # -------------------------------------------------- connection
            if cmd == "connect":
                try:
                    info = ses.connect(msg.get("host") or host,
                                       msg.get("port") or port)
                    ses.refresh()
                    hub.emit({"ev": "connection", **ses.info()})
                    log(f"connected: {info.get('idn')}")
                except Exception as e:
                    hub.emit({"ev": "connection", **ses.info()})
                    err(f"connect failed: {type(e).__name__}: {e}")
            elif cmd == "disconnect":
                if hub.sweeper:
                    hub.sweeper.live = False
                    hub.emit({"ev": "live", "live": False})
                hub.emit({"ev": "connection", **ses.disconnect()})
                log("disconnected - settings left as they are")
            elif cmd == "status":
                push_state()
            elif cmd == "restore":
                act(lambda: ses.an.restore())
                log("restored the state captured at connect")

            # ------------------------------------------------------- sweep
            elif cmd == "live":
                if not ses.connected:
                    err("connect to the analyzer first")
                    continue
                want = bool(msg.get("live"))
                # Going live puts the analyzer back into continuous sweep: the
                # panel then only reads the trace it is already producing.
                if want:
                    act(lambda: ses.an.set_continuous(True))
                hub.sweeper.live = want
                hub.emit({"ev": "live", "live": hub.sweeper.live,
                          "period": hub.sweeper.period})
            elif cmd == "rate":
                try:
                    p = float(msg.get("seconds"))
                except (TypeError, ValueError):
                    err("refresh rate must be a number")
                    continue
                hub.sweeper.period = max(0.2, min(p, 10.0))
                hub.emit({"ev": "live", "live": hub.sweeper.live,
                          "period": hub.sweeper.period})
            elif cmd == "sweep":
                if not ses.connected:
                    err("connect to the analyzer first")
                    continue
                try:
                    hub.sweeper.sweep(emit_state=True, single=True)
                except Exception as e:
                    err(f"sweep failed: {type(e).__name__}: {e}")
            elif cmd == "continuous":
                act(lambda: ses.an.set_continuous(bool(msg.get("on"))))

            # --------------------------------------------------- frequency
            elif cmd == "frequency":
                act(lambda: ses.an.set_frequency(
                    center_hz=num(msg, "center_mhz", MHZ),
                    span_hz=num(msg, "span_mhz", MHZ),
                    start_hz=num(msg, "start_mhz", MHZ),
                    stop_hz=num(msg, "stop_mhz", MHZ)))
            elif cmd == "full_span":
                act(lambda: ses.an.full_span())

            # --------------------------------------------------- amplitude
            elif cmd == "ref_level":
                v = num(msg, "dbm")
                if v is None:
                    err("reference level must be a number")
                    continue
                act(lambda: ses.an.set_ref_level(v))
            elif cmd == "ref_offset":
                v = num(msg, "db")
                if v is None:
                    err("reference offset must be a number")
                    continue
                act(lambda: ses.an.set_ref_offset(v))
            elif cmd == "attenuation":
                auto = bool(msg.get("auto"))
                v = num(msg, "db")
                if not auto and v is None:
                    err("attenuation must be a number")
                    continue
                act(lambda: ses.an.set_attenuation(v, auto))

            # ------------------------------------------------------- trace
            elif cmd == "trace_mode":
                act(lambda: ses.an.set_trace_mode(msg.get("mode", "WRIT")))
            elif cmd == "trace_restart":
                act(lambda: ses.an.restart_trace(
                    ses.state.get("trace_mode", "WRIT")))
                log("trace restarted")
            elif cmd == "detector":
                act(lambda: ses.an.set_detector(msg.get("detector", "POS")))

            # -------------------------------------------------- bandwidths
            elif cmd == "rbw":
                act(lambda: ses.an.set_rbw(num(msg, "hz"),
                                           bool(msg.get("auto"))))
            elif cmd == "vbw":
                act(lambda: ses.an.set_vbw(num(msg, "hz"),
                                           bool(msg.get("auto"))))
            elif cmd == "sweep_time":
                act(lambda: ses.an.set_sweep_time(num(msg, "seconds"),
                                                  bool(msg.get("auto"))))

            # ----------------------------------------------------- markers
            elif cmd == "markers":
                act(lambda: ses.an.set_marker_count(msg.get("count", 0)))
            elif cmd == "marker_x":
                v = num(msg, "x")
                if v is None:
                    err("marker position must be a number")
                    continue
                act(lambda: ses.an.set_marker_x(int(msg.get("n", 1)), v,
                                                bool(msg.get("seconds"))))
            elif cmd == "marker_action":
                act(lambda: ses.an.marker_action(int(msg.get("n", 1)),
                                                 msg.get("action", "peak")))
            elif cmd == "markers_off":
                act(lambda: ses.an.set_marker_count(0))
    except (ConnectionError, OSError):
        pass
    finally:
        hub.remove(sock)


def handle_client(sock, hub, host, port):
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

        if "upgrade: websocket" in head.lower():
            return handle_ws(sock, head, hub, host, port)
        if parsed.path == "/api/state":
            return json_response(sock, hub.session.info())
        serve_static(sock, "/index.html" if parsed.path == "/" else parsed.path)
    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def serve(bind, port, host=INSTRUMENT_HOST, scpi_port=INSTRUMENT_PORT):
    hub = Hub()
    hub.session = Session(host, scpi_port)
    hub.sweeper = Sweeper(hub.session, hub.emit)
    hub.sweeper.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # being listened on, so a stray instance silently steals connections instead
    # of failing. SO_EXCLUSIVEADDRUSE is the correct flag there.
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
        print("Another copy of this panel is probably already running. Close it,")
        print(f"or start this one on a different port:  --port {port + 1}")
        return
    srv.listen(8)
    # A blocking accept() on Windows is not interrupted by Ctrl+C: the signal is
    # only delivered when the interpreter regains control, which never happens
    # while it waits. A short timeout gives it that chance.
    srv.settimeout(0.5)

    print(f"FSC3 control panel  ->  http://{bind}:{port}/")
    print(f"  analyzer : {host}:{scpi_port}")
    print("  the panel writes only what you change; nothing is restored on exit")
    print("  Ctrl+C to stop\n")

    shutdown = threading.Event()
    previous = {}

    def on_signal(signum, frame):
        if shutdown.is_set():
            if signum in previous:                 # a second Ctrl+C gives up
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
            pass

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
                             args=(conn, hub, host, scpi_port),
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
    print("\nStopping...")
    try:
        if hub.sweeper is not None:
            hub.sweeper.live = False
            hub.sweeper.stop()
            hub.sweeper.join(timeout=5)
        if hub.session is not None and hub.session.connected:
            hub.session.disconnect()
            print("  analyzer released, sweeping freely, settings left as set")
    except KeyboardInterrupt:
        print("  cleanup interrupted")
    except Exception as e:
        print(f"  cleanup problem: {type(e).__name__}: {e}")
    finally:
        try:
            srv.close()
        except OSError:
            pass
    print("Stopped.")
