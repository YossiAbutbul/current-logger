"""SCPI transport and the FSC3 operations this app needs.

Every command here was verified against a real R&S FSC3 (firmware V2.22).
Notable instrument quirks that shaped this module:

  * TRIG:SOUR VID is rejected outside zero span ("TraceVideo not allowed in
    normal..."), so the video trigger is only usable with FREQ:SPAN 0.
  * SWE:TIME is only settable in zero span; SWE:TIME:AUTO is rejected there.
  * DET AVER is rejected ("Requested detector is not allowed").
  * SWE:POIN? and INP:GAIN:STAT? are not implemented and simply never answer,
    so they must never be queried - a query with no reply desyncs the stream.
  * A blocking *OPC? would stall for the whole trigger wait. INIT;*OPC plus
    *ESR? polling stays responsive and lets ABOR recover cleanly.
"""

import socket
import threading
import time


class Scpi:
    """Line-oriented SCPI over a raw socket."""

    def __init__(self, host, port=5555, timeout=6.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.lock = threading.Lock()
        self.host, self.port = host, port

    def write(self, cmd):
        with self.lock:
            self.sock.sendall(cmd.encode() + b"\n")

    def query(self, cmd, timeout=6.0):
        with self.lock:
            self.sock.settimeout(timeout)
            self.sock.sendall(cmd.encode() + b"\n")
            while b"\n" not in self.buf:
                chunk = self.sock.recv(262144)
                if not chunk:
                    raise ConnectionError("instrument closed the connection")
                self.buf += chunk
            line, _, self.buf = self.buf.partition(b"\n")
            return line.decode("latin-1").strip()

    def error(self):
        """Return an error string, or None when the error queue is clean."""
        e = self.query("SYST:ERR?")
        return None if e.startswith("0,") else e

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Instrument:
    """The FSC3 operations used by the burst-decay test."""

    def __init__(self, scpi):
        self.s = scpi

    def idn(self):
        return self.s.query("*IDN?")

    def apply_setup(self, cfg, log):
        """Push the whole test setup. Returns a list of rejected commands."""
        s = self.s
        problems = []

        def send(cmd):
            s.write(cmd)
            time.sleep(0.12)
            e = s.error()
            if e:
                problems.append(f"{cmd}  ->  {e}")
                log(f"setup rejected: {cmd}  ->  {e}")

        send("*CLS")
        send("INIT:CONT OFF")
        send(f"FREQ:CENT {cfg['center_hz']:.0f}")
        send("FREQ:SPAN 0")                      # zero span: the x axis is time
        send(f"SWE:TIME {cfg['sweep_time_s']:.9g}")   # only legal in zero span
        if cfg["rbw_hz"] > 0:
            send(f"BAND:RES {cfg['rbw_hz']:.0f}")
        else:
            send("BAND:AUTO ON")
        if cfg["vbw_hz"] > 0:
            send(f"BAND:VID {cfg['vbw_hz']:.0f}")
        else:
            send("BAND:VID:AUTO ON")
        send(f"DISP:TRAC:Y:RLEV {cfg['ref_level_dbm']:.6g}")
        if cfg["atten_auto"]:
            send("INP:ATT:AUTO ON")
        else:
            send(f"INP:ATT {cfg['atten_db']:.0f}")
        send(f"DET {cfg['detector']}")
        send("DISP:TRAC:MODE WRIT")

        trig = cfg["trigger"].upper()
        send(f"TRIG:SOUR {trig}")
        if trig == "VID":
            send(f"TRIG:LEV:VID {cfg['trig_level_pct']:.6g}")
        if trig in ("VID", "EXT"):
            send(f"TRIG:SLOP {cfg['trig_slope']}")
        return problems

    def capture(self, timeout_s, stop_flag, poll=0.25):
        """Arm one triggered sweep and wait for it.

        Returns (values, triggered). If the burst never arrives the sweep is
        aborted and (None, False) is returned, leaving the stream in sync.
        """
        s = self.s
        s.write("ABOR")
        time.sleep(0.05)
        s.write("*CLS")
        s.write("INIT:CONT OFF")
        time.sleep(0.05)
        s.write("INIT:IMM;*OPC")

        deadline = time.time() + timeout_s
        triggered = False
        while time.time() < deadline:
            if stop_flag():
                break
            time.sleep(poll)
            try:
                esr = s.query("*ESR?", timeout=5)
            except Exception:
                break
            try:
                if int(float(esr)) & 1:          # ESR bit 0 = operation complete
                    triggered = True
                    break
            except ValueError:
                continue

        if not triggered:
            s.write("ABOR")
            time.sleep(0.15)
            s.error()                            # drain anything the abort queued
            return None, False

        raw = s.query("TRAC:DATA? TRACE1", timeout=25)
        return [float(v) for v in raw.split(",") if v.strip()], True

    def release(self):
        """Leave the analyzer usable: free run, continuous sweep."""
        for cmd in ("ABOR", "TRIG:SOUR IMM", "INIT:CONT ON"):
            try:
                self.s.write(cmd)
                time.sleep(0.1)
            except Exception:
                pass
