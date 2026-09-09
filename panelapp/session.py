"""Shared analyzer session and the live sweeper.

One SCPI link is shared by the sweeper thread and by every command arriving
over the WebSocket, so all instrument access goes through Session.lock and is
taken for a WHOLE operation, not per command. Locking per SCPI line would let
a control command land between the sweeper's INIT and its TRAC:DATA?, which
returns a trace that belongs to neither setting. A sweep is ~0.2 s, so a
control command waits well under half a second for its turn.
"""

import threading
import time

from .analyzer import Analyzer
from .config import (INSTRUMENT_HOST, INSTRUMENT_PORT,
                     MARKER_VERIFY_EVERY, SWEEP_PERIOD_S)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Session:
    def __init__(self, host=INSTRUMENT_HOST, port=INSTRUMENT_PORT):
        self.host, self.port = host, port
        self.lock = threading.RLock()
        self.an = None
        self.state = {}

    @property
    def connected(self):
        return self.an is not None and self.an.s is not None

    def info(self):
        return {"connected": self.connected, "host": self.host,
                "port": self.port, "idn": self.an.idn if self.an else "",
                "state": self.state}

    def connect(self, host=None, port=None):
        with self.lock:
            self.disconnect()
            self.host = host or self.host
            self.port = int(port or self.port)
            an = Analyzer(self.host, self.port)
            an.connect()
            self.an = an
            self.state = dict(an.saved or {})
            return self.info()

    def disconnect(self):
        with self.lock:
            if self.an is not None:
                # Deliberately NOT restoring the captured state: this is a
                # control panel, so what the operator set is meant to stay set.
                # Rolling back is an explicit button instead.
                try:
                    self.an.send("INIT:CONT ON")     # leave it sweeping freely
                except Exception:
                    pass
                self.an.close()
                self.an = None
            self.state = {}
            return self.info()

    def refresh(self):
        with self.lock:
            if not self.connected:
                return {}
            self.state = self.an.read_state()
            return self.state


class Sweeper(threading.Thread):
    """Pushes a fresh trace while the panel is live."""

    def __init__(self, session, emit):
        super().__init__(daemon=True)
        self.s = session
        self.emit = emit
        self.live = False
        self.period = SWEEP_PERIOD_S         # the instrument-load dial
        self._cycle = 0
        self._abort = threading.Event()      # not _stop: shadows Thread._stop()

    def stop(self):
        self._abort.set()

    def run(self):
        while not self._abort.is_set():
            if not (self.live and self.s.connected):
                time.sleep(0.1)
                continue
            try:
                self.sweep()
            except Exception as e:
                self.emit({"ev": "log", "t": now_iso(),
                           "msg": f"sweep stopped: {type(e).__name__}: {e}"})
                self.live = False
                self.emit({"ev": "live", "live": False})
            time.sleep(max(0.1, self.period))

    def sweep(self, emit_state=False, single=False):
        """Read the trace and its markers as one atomic operation.

        Live mode does NOT arm sweeps. The analyzer is left free-running and
        the current trace is simply read, which is how its own display behaves
        and what max hold and averaging need. Arming a sweep per refresh would
        instead put the instrument in single-sweep and make its screen step
        along with the panel. `single` is the deliberate one-shot path behind
        the Sweep once button.
        """
        with self.s.lock:
            if not self.s.connected:
                return
            an = self.s.an
            ok = an.sweep_once() if single else True
            values, start, stop = an.trace()
            # Trust the count we last read authoritatively, and re-count from
            # scratch only now and then: every query on this path costs the
            # analyzer sweep time. A one-off read always verifies.
            verify = single or emit_state or (self._cycle % MARKER_VERIFY_EVERY == 0)
            self._cycle += 1
            markers = an.read_markers(None if verify
                                      else self.s.state.get("markers", 0))
            if verify:
                self.s.state["markers"] = len(markers)
            if emit_state:
                self.s.state = an.read_state()
                state = dict(self.s.state)
            else:
                state = None
        msg = {"ev": "trace", "t": now_iso(), "swept": ok,
               "start_hz": start, "stop_hz": stop,
               "zero_span": bool(start is not None and stop is not None
                                 and stop <= start),
               "values": [round(v, 2) for v in values],
               "markers": markers}
        if state is not None:
            msg["state"] = state
        self.emit(msg)
