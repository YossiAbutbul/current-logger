"""FSC3 operations for the control panel.

Every command below was checked against the real analyzer (R&S FSC3, firmware
V2.22) before being exposed in the UI. The measured quirks that shaped this
module:

  * Markers must be switched on in order. With M1 off, CALC:MARK2:STAT ON is
    rejected with -200 "Execution error", so markers are addressed as a count
    (1..6) and enabled from 1 upwards.
  * A marker with nothing to read answers 99.1e+36 - the 9.91e37 "invalid"
    sentinel - not an error. Anything above INVALID is reported as no reading.
  * CALC:MARK<n>:X? is Hz in a frequency sweep but SECONDS in zero span, so the
    caller is told which axis it is looking at rather than guessing.
  * Querying CALC:MARK<n>:X? or :Y? on a marker that is OFF turns that marker
    ON. Only STAT? may be asked about an inactive marker, so the active count is
    always re-read from STAT? before any position is read.
  * DISP:TRAC:Y:SCAL is accepted and then ignored (it reads back 100 dB no
    matter what is written), so the display range is exposed read-only.
  * CALC:MARK<n>:FUNC:REF is accepted and does nothing, so "marker to reference
    level" is not offered. CALC:MARK<n>:FUNC:CENT does work.
  * Detectors: APE (auto peak), POS (max peak), NEG, SAMP and RMS are accepted.
    MAXP is not a token on this firmware (-141) - POS is the max-peak detector -
    and AVER and QPE are rejected with -221.
  * DISP:TRAC:Y:RLEV:OFFS is settable and reads back, and the displayed
    reference level follows it one for one - measured: offset 20 -> 30 dB moved
    DISP:TRAC:Y:RLEV from -20 to -10 dBm. So the offset is never written without
    reading the reference level back afterwards.
  * SWE:POIN? and INP:GAIN:STAT? are not implemented and never answer, which
    desyncs the stream - so the trace length is taken from the data itself and
    nothing outside the verified query list below is ever queried.
  * A blocking *OPC? stalls for the whole sweep. INIT:IMM;*OPC plus *ESR?
    polling stays responsive and lets ABOR recover.

Verified queries (the only ones this module sends):
  *IDN? *ESR? SYST:ERR? FREQ:CENT? FREQ:SPAN? FREQ:STAR? FREQ:STOP?
  DISP:TRAC:Y:RLEV? DISP:TRAC:Y:RLEV:OFFS? DISP:TRAC:Y:SCAL? DISP:TRAC:MODE?
  BAND:RES? BAND:VID? INP:ATT? INP:ATT:AUTO? DET? SWE:TIME? INIT:CONT?
  CALC:MARK<n>:STAT? CALC:MARK<n>:X? CALC:MARK<n>:Y? TRAC:DATA?
"""

import time

# The socket transport is shared with the burst-decay app rather than copied:
# same instrument, same line protocol, already proven against the hardware.
from fscapp.scpi import Scpi

from .config import DETECTORS, INVALID, MARKERS, TRACE_MODES


def _f(v):
    """Parse an FSC3 number, mapping the invalid sentinel to None."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if abs(x) >= INVALID else x


class Analyzer:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.s = None
        self.idn = ""
        self.saved = None          # state as found on connect, for Restore

    # ------------------------------------------------------------- link
    def connect(self):
        self.s = Scpi(self.host, self.port)
        self.idn = self.s.query("*IDN?")
        self.s.error()                       # drain whatever was queued
        self.saved = self.read_state()
        return self.idn

    def close(self):
        if self.s is not None:
            try:
                self.s.close()
            finally:
                self.s = None

    # --------------------------------------------------------- plumbing
    def send(self, cmd):
        """Write and return the instrument's complaint, or None if clean."""
        self.s.write(cmd)
        time.sleep(0.05)
        return self.s.error()

    def apply(self, cmds, log=None):
        """Send a sequence, collecting whatever the analyzer rejected."""
        problems = []
        for cmd in cmds:
            err = self.send(cmd)
            if err:
                problems.append(f"{cmd}  ->  {err}")
                if log:
                    log(f"rejected: {cmd}  ->  {err}")
        return problems

    def q(self, cmd, timeout=6.0):
        return self.s.query(cmd, timeout)

    # ----------------------------------------------------- state probing
    def read_state(self):
        """Everything the panel shows. Only verified queries are used."""
        st = {}
        for key, cmd in (
                ("center_hz", "FREQ:CENT?"), ("span_hz", "FREQ:SPAN?"),
                ("start_hz", "FREQ:STAR?"), ("stop_hz", "FREQ:STOP?"),
                ("ref_level_dbm", "DISP:TRAC:Y:RLEV?"),
                ("ref_offset_db", "DISP:TRAC:Y:RLEV:OFFS?"),
                ("range_db", "DISP:TRAC:Y:SCAL?"),
                ("rbw_hz", "BAND:RES?"), ("vbw_hz", "BAND:VID?"),
                ("atten_db", "INP:ATT?"), ("sweep_time_s", "SWE:TIME?")):
            st[key] = _f(self.q(cmd))
        st["trace_mode"] = self.q("DISP:TRAC:MODE?").upper()
        st["detector"] = self.q("DET?").upper()
        st["atten_auto"] = self.q("INP:ATT:AUTO?").strip() in ("1", "ON")
        st["continuous"] = self.q("INIT:CONT?").strip() in ("1", "ON")
        st["zero_span"] = bool(st["span_hz"] is not None and st["span_hz"] <= 0)
        st["markers"] = self.marker_count()
        return st

    def restore(self):
        """Put back the state found at connect. Explicit, never automatic:
        this is a control panel, so the operator's changes are meant to stick."""
        s = self.saved
        if not s:
            return ["nothing was captured at connect"]
        # The offset drags the reference level with it, so it goes FIRST and the
        # level is written last - the other order lands the level off by the
        # difference between the two offsets.
        cmds = [f"FREQ:CENT {s['center_hz']:.0f}",
                f"FREQ:SPAN {s['span_hz']:.0f}",
                f"DISP:TRAC:Y:RLEV:OFFS {s['ref_offset_db']:.6g}",
                f"DISP:TRAC:Y:RLEV {s['ref_level_dbm']:.6g}",
                f"BAND:RES {s['rbw_hz']:.0f}", f"BAND:VID {s['vbw_hz']:.0f}",
                f"DET {s['detector']}", f"DISP:TRAC:MODE {s['trace_mode']}"]
        cmds.append("INP:ATT:AUTO ON" if s["atten_auto"]
                    else f"INP:ATT {s['atten_db']:.0f}")
        if s["zero_span"] and s["sweep_time_s"]:
            cmds.append(f"SWE:TIME {s['sweep_time_s']:.9g}")
        cmds.append("INIT:CONT ON" if s["continuous"] else "INIT:CONT OFF")
        return self.apply(cmds)

    # ------------------------------------------------------- frequency
    def set_frequency(self, center_hz=None, span_hz=None,
                      start_hz=None, stop_hz=None):
        cmds = []
        if center_hz is not None:
            cmds.append(f"FREQ:CENT {center_hz:.0f}")
        if span_hz is not None:
            cmds.append(f"FREQ:SPAN {span_hz:.0f}")
        # start/stop are applied after centre/span so they win if both are given
        if start_hz is not None:
            cmds.append(f"FREQ:STAR {start_hz:.0f}")
        if stop_hz is not None:
            cmds.append(f"FREQ:STOP {stop_hz:.0f}")
        return self.apply(cmds)

    def full_span(self):
        return self.apply(["FREQ:SPAN:FULL"])

    # ------------------------------------------------------- amplitude
    def set_ref_level(self, dbm):
        return self.apply([f"DISP:TRAC:Y:RLEV {dbm:.6g}"])

    def set_ref_offset(self, db):
        """Reference offset in dB. The displayed reference level moves with it,
        so the caller must re-read the state rather than assume RLEV held."""
        return self.apply([f"DISP:TRAC:Y:RLEV:OFFS {db:.6g}"])

    def set_attenuation(self, db=None, auto=False):
        return self.apply(["INP:ATT:AUTO ON"] if auto
                          else [f"INP:ATT {db:.0f}"])

    # ----------------------------------------------------------- trace
    def set_trace_mode(self, mode):
        mode = mode.upper()
        if mode not in TRACE_MODES:
            return [f"unknown trace mode {mode}"]
        return self.apply([f"DISP:TRAC:MODE {mode}"])

    def restart_trace(self, mode):
        """Clear an accumulating trace. Dropping to clear-write and back is
        what actually empties a max/min hold; re-sending the mode does not."""
        mode = (mode or "WRIT").upper()
        problems = self.apply(["DISP:TRAC:MODE WRIT"])
        time.sleep(0.15)
        if mode != "WRIT":
            problems += self.apply([f"DISP:TRAC:MODE {mode}"])
        return problems

    def set_detector(self, det):
        det = det.upper()
        if det not in DETECTORS:
            return [f"detector {det} is not available on this analyzer"]
        return self.apply([f"DET {det}"])

    # ------------------------------------------------- sweep / bandwidth
    def set_rbw(self, hz=None, auto=False):
        return self.apply(["BAND:AUTO ON"] if auto else [f"BAND:RES {hz:.0f}"])

    def set_vbw(self, hz=None, auto=False):
        return self.apply(["BAND:VID:AUTO ON"] if auto
                          else [f"BAND:VID {hz:.0f}"])

    def set_sweep_time(self, seconds=None, auto=False):
        # SWE:TIME:AUTO is rejected in zero span; SWE:TIME is only settable there
        return self.apply(["SWE:TIME:AUTO ON"] if auto
                          else [f"SWE:TIME {seconds:.9g}"])

    def set_continuous(self, on):
        return self.apply([f"INIT:CONT {'ON' if on else 'OFF'}"])

    # --------------------------------------------------------- markers
    def marker_count(self):
        """How many markers are on. They are contiguous from 1 by construction,
        so the first one that is off ends the run."""
        n = 0
        for i in range(1, MARKERS + 1):
            if self.q(f"CALC:MARK{i}:STAT?").strip() not in ("1", "ON"):
                break
            n = i
        return n

    def set_marker_count(self, n):
        """Enable markers 1..n and switch the rest off. Enabling out of order
        is refused by the analyzer, so the whole set is rebuilt from 1."""
        n = max(0, min(int(n), MARKERS))
        problems = self.apply(["CALC:MARK:AOFF"])
        for i in range(1, n + 1):
            problems += self.apply([f"CALC:MARK{i}:STAT ON"])
        return problems

    def read_markers(self, count=None):
        """[{n, x, y}] for the active markers. x is Hz, or seconds in zero span.

        Reading a marker that is OFF switches it ON - measured - so a position
        is never asked for without knowing the marker is active. Re-counting
        from scratch costs up to six queries, and on the live path every query
        steals time from the analyzer's own sweep, so a caller that already
        knows the count may pass it: markers are contiguous from 1, so
        confirming the HIGHEST one confirms the whole set for a single query,
        and only a stale count pays for a full re-count.
        """
        if count is None:
            n = self.marker_count()
        else:
            n = max(0, min(int(count), MARKERS))
            if n and self.q(f"CALC:MARK{n}:STAT?").strip() not in ("1", "ON"):
                n = self.marker_count()
        out = []
        for i in range(1, n + 1):
            out.append({"n": i,
                        "x": _f(self.q(f"CALC:MARK{i}:X?")),
                        "y": _f(self.q(f"CALC:MARK{i}:Y?"))})
        return out

    def set_marker_x(self, n, x, seconds=False):
        """Place a marker. x is Hz, or seconds when the sweep is zero span -
        which is also why the two are formatted differently: a frequency is a
        whole number of Hz, a sweep position is a fraction of a second."""
        return self.apply([f"CALC:MARK{n}:X {x:.9g}" if seconds
                           else f"CALC:MARK{n}:X {x:.0f}"])

    def marker_action(self, n, action):
        cmd = {"peak": f"CALC:MARK{n}:MAX",
               "next": f"CALC:MARK{n}:MAX:NEXT",
               "min": f"CALC:MARK{n}:MIN",
               "center": f"CALC:MARK{n}:FUNC:CENT"}.get(action)
        if cmd is None:
            return [f"unknown marker action {action}"]
        return self.apply([cmd])

    # ----------------------------------------------------------- sweep
    def sweep_once(self, timeout_s=10.0, poll=0.05):
        """Run one sweep to completion. True if it finished inside the timeout.

        *ESR? polling rather than a blocking *OPC?, so a slow sweep never holds
        the connection hostage and ABOR can always get control back.
        """
        self.s.write("ABOR")
        time.sleep(0.02)
        self.s.write("*CLS")
        self.s.write("INIT:CONT OFF")
        time.sleep(0.02)
        self.s.write("INIT:IMM;*OPC")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(poll)
            try:
                if int(float(self.q("*ESR?", 5))) & 1:
                    return True
            except (ValueError, OSError):
                break
        self.s.write("ABOR")
        time.sleep(0.1)
        self.s.error()
        return False

    def trace(self):
        """The current trace as (values_dbm, start_hz, stop_hz).

        The point count comes from the data - SWE:POIN? is not implemented on
        this firmware and querying it would never answer.
        """
        raw = self.q("TRAC:DATA? TRACE1", 25)
        values = [float(v) for v in raw.split(",") if v.strip()]
        start = _f(self.q("FREQ:STAR?"))
        stop = _f(self.q("FREQ:STOP?"))
        return values, start, stop
