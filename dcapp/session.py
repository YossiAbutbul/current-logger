"""A single shared connection to the analyzer.

The connection panel, the reference recorder and the test run all use the same
Session, so connecting is something you do once and can see the state of.

Connecting only READS. No acquisition settings are written until you actually
record a reference or start a run, and whatever was there is put back when the
session disconnects.
"""

import json
import os
import threading
import time

from .config import ROOT
from .instrument import N6705

# The acquisition snapshot is mirrored here so that a server killed mid-run
# does not lose the operator's original settings: the next connect finds this
# file, restores from it, and only then takes a fresh snapshot.
SNAPSHOT = os.path.join(ROOT, ".acq-restore.json")


class Session:
    def __init__(self, resource, channel):
        self.resource = resource
        self.channel = channel
        self.inst = None
        self.lock = threading.RLock()
        self.idn = None
        self.state = {}
        self.busy = ""            # "" | "reference" | "run"

    # ------------------------------------------------------------- connect
    def connect(self, resource=None, channel=None):
        with self.lock:
            if self.inst is not None:
                self.disconnect()
            self.resource = resource or self.resource
            self.channel = int(channel or self.channel)
            inst = N6705(self.resource, self.channel)
            self.idn = inst.connect()
            self.inst = inst

            stale = _load_snapshot()
            if stale:
                # a previous session did not shut down cleanly - the settings
                # on the instrument are ours, not the operator's
                inst._saved = stale
                inst.restore_acquisition()
            inst.save_acquisition()          # so we can put it back later
            _save_snapshot(inst._saved)
            self.state = inst.read_state()
            return self.info()

    def disconnect(self):
        with self.lock:
            if self.inst is not None:
                try:
                    self.inst.abort()
                    self.inst.restore_acquisition()
                    _clear_snapshot()        # clean shutdown: nothing to recover
                except Exception:
                    pass
                self.inst.close()
            self.inst = None
            self.idn = None
            self.state = {}
            return self.info()

    @property
    def connected(self):
        return self.inst is not None

    def refresh_state(self):
        with self.lock:
            if self.inst is None:
                return self.info()
            try:
                self.state = self.inst.read_state()
            except Exception:
                pass
            return self.info()

    def info(self):
        d = {
            "connected": self.connected,
            "resource": self.resource,
            "channel": self.channel,
            "idn": self.idn,
            "busy": self.busy,
        }
        d.update(self.state or {})
        return d

    # --------------------------------------------------------- acquisition
    def configure(self, cfg, log):
        with self.lock:
            return self.inst.configure(cfg, log)

    def capture_one(self, cfg, stop_flag):
        """Arm, wait for a TX, fetch. Returns (values, tint_s, triggered)."""
        inst = self.inst
        with self.lock:
            inst.arm()
        got = inst.wait_for_capture(cfg["trig_timeout_s"], stop_flag)
        if not got:
            with self.lock:
                inst.abort()
            return None, inst.tint_actual, False
        with self.lock:
            values = inst.fetch()
        return values, inst.tint_actual, True

    def idle_current_ua(self):
        try:
            with self.lock:
                return self.inst.measure_current() * 1e6
        except Exception:
            try:
                self.inst.recover()
            except Exception:
                pass
            return None


def analyse(values, tint_s, floor_frac=0.5):
    """Peak / mean / charge / burst width for one captured TX."""
    if not values:
        return None
    peak = max(values)
    mean = sum(values) / len(values)
    thresh = peak * floor_frac
    above = [i for i, v in enumerate(values) if v >= thresh]
    burst_s = (above[-1] - above[0] + 1) * tint_s if above else 0.0
    in_burst = values[above[0]:above[-1] + 1] if above else []
    return {
        "peak_ma": peak * 1000.0,
        "mean_ma": mean * 1000.0,
        "charge_mc": sum(in_burst) * tint_s * 1000.0,
        "burst_ms": burst_s * 1000.0,
    }


class ReferenceRecorder(threading.Thread):
    """Record a short session of transmissions to establish the reference current.

    Captures `ref_samples` transmissions and reports each peak; the reference is
    the mean of them, so one odd burst does not define the whole run.
    """

    def __init__(self, session, cfg, emit):
        super().__init__(daemon=True)
        self.s = session
        self.cfg = cfg
        self.emit = emit
        self._abort = threading.Event()
        self.peaks = []

    def stop(self):
        self._abort.set()

    def stopped(self):
        return self._abort.is_set()

    def run(self):
        cfg = self.cfg
        want = max(1, int(cfg.get("ref_samples", 3)))
        self.s.busy = "reference"
        self.emit({"ev": "ref_state", "state": "recording", "want": want})
        try:
            problems = self.s.configure(cfg, lambda m: self.emit(
                {"ev": "log", "msg": m, "t": time.strftime("%Y-%m-%dT%H:%M:%S")}))
            if problems:
                self.emit({"ev": "setup_warning", "problems": problems})

            misses = 0
            while len(self.peaks) < want and not self.stopped():
                values, tint, got = self.s.capture_one(cfg, self.stopped)
                if not got:
                    misses += 1
                    self.emit({"ev": "ref_miss", "misses": misses})
                    if misses > max(2, want):
                        self.emit({"ev": "ref_done", "ok": False,
                                   "msg": "no transmission seen - check the trigger "
                                          "level against the real TX current"})
                        return
                    continue
                m = analyse(values, tint)
                if not m:
                    continue
                self.peaks.append(m["peak_ma"])
                keep_i, keep_v = _decimate(values, 400)
                self.emit({"ev": "ref_sample", "n": len(self.peaks), "want": want,
                           "peak_ma": m["peak_ma"], "burst_ms": m["burst_ms"],
                           "charge_mc": m["charge_mc"],
                           "trace_ms": [round(i * tint * 1000, 4) for i in keep_i],
                           "trace_ma": [round(v * 1000, 3) for v in keep_v]})

            if self.stopped() and not self.peaks:
                self.emit({"ev": "ref_done", "ok": False, "msg": "cancelled"})
                return

            ref = sum(self.peaks) / len(self.peaks)
            self.emit({"ev": "ref_done", "ok": True, "ref_peak_ma": ref,
                       "peaks": self.peaks,
                       "spread_ma": (max(self.peaks) - min(self.peaks))
                       if len(self.peaks) > 1 else 0.0})
        except Exception as e:
            self.emit({"ev": "ref_done", "ok": False,
                       "msg": f"{type(e).__name__}: {e}"})
        finally:
            self.s.busy = ""
            self.emit({"ev": "ref_state", "state": "idle"})


def _decimate(values, target):
    from .storage import decimate_peak
    return decimate_peak(values, target)


def _save_snapshot(saved):
    try:
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2)
    except OSError:
        pass


def _load_snapshot():
    try:
        with open(SNAPSHOT, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("func") else None
    except (OSError, ValueError):
        return None


def _clear_snapshot():
    try:
        os.remove(SNAPSHOT)
    except OSError:
        pass
