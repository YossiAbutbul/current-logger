"""TX current decay run.

Flow:

  1. Connect (connection panel) - read only.
  2. Record a reference session - a few transmissions whose mean peak becomes
     the reference current.
  3. Start. t0 is stamped the moment the run starts.
  4. Every transmission is captured on a current trigger, so the run never has
     to predict when the DUT talks; it arms and waits.
  5. The run ends when the peak has dropped to (reference - drop delta) on
     `consecutive` transmissions in a row. tn is stamped, and the reported
     result is tn - t0.
"""

import os
import threading
import time
import traceback

from . import storage
from .session import analyse


class CurrentTestRunner(threading.Thread):
    def __init__(self, cfg, session, emit, results_root=None, ref_peak_ma=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.s = session
        self.emit = emit
        self.results_root = results_root
        self.ref_peak_ma = ref_peak_ma      # from the reference session, if any
        self._abort = threading.Event()     # not _stop: shadows Thread._stop()
        self.state = "idle"
        self.store = None
        self.t0 = None
        self.tn = None
        self.result = None
        self.below_streak = 0
        self.count = 0
        self.missed = 0

    # ------------------------------------------------------------ control
    def stop(self):
        self._abort.set()

    def stopped(self):
        return self._abort.is_set()

    def log(self, msg):
        self.emit({"ev": "log", "msg": msg, "t": storage.now_iso()})

    @property
    def stop_level_ma(self):
        if self.ref_peak_ma is None:
            return None
        return self.ref_peak_ma - abs(self.cfg["drop_delta_ma"])

    # --------------------------------------------------------- main loop
    def run(self):
        cfg = self.cfg
        try:
            self.s.busy = "run"
            self.t0 = time.time()
            t0_iso = storage.now_iso()
            self.state = "running"
            self.emit({"ev": "state", "state": self.state, "t0": t0_iso})
            self.log(f"t0 = {t0_iso}")

            problems = self.s.configure(cfg, self.log)
            if problems:
                self.emit({"ev": "setup_warning", "problems": problems})
            inst = self.s.inst
            self.log(f"window {inst.points} pts x {inst.tint_actual*1e6:.2f} us "
                     f"= {inst.points*inst.tint_actual*1000:.1f} ms, "
                     f"trigger {cfg['trig_level_ma']:.0f} mA")

            self.store = storage.prepare_folder(
                cfg["test_name"], cfg, f"{self.s.resource} ch{self.s.channel}",
                self.results_root)
            self.store.save_state({"t0": t0_iso, "ref_peak_ma": self.ref_peak_ma,
                                   "finished": False})
            self.log(f"recording to {self.store.folder}")
            self.emit({"ev": "folder", "folder": os.path.basename(self.store.folder)})

            if self.ref_peak_ma is not None:
                self.log(f"reference {self.ref_peak_ma:.1f} mA -> stop at "
                         f"{self.stop_level_ma:.1f} mA "
                         f"(drop {cfg['drop_delta_ma']:.0f} mA) on "
                         f"{cfg['consecutive']} consecutive TX")
            else:
                self.log("no reference recorded - the first captured TX will set it")

            ref_attempts = 0
            while not self.stopped():
                if self.count >= cfg["max_measurements"]:
                    return self._finish("stopped: reached max transmissions")
                if time.time() - self.t0 > cfg["max_duration_s"]:
                    return self._finish("stopped: reached max duration")

                self.emit({"ev": "arming", "index": self.count})
                try:
                    values, tint, got = self.s.capture_one(cfg, self.stopped)
                except Exception as e:
                    self.log(f"acquisition error: {type(e).__name__}: {e}")
                    try:
                        self.s.inst.recover()
                    except Exception:
                        pass
                    values, tint, got = None, None, False

                if self.stopped():
                    break

                ts, now = storage.now_iso(), time.time()
                elapsed = now - self.t0

                if not got:
                    self.missed += 1
                    self.log(f"#{self.count}: no TX above "
                             f"{cfg['trig_level_ma']:.0f} mA within "
                             f"{cfg['trig_timeout_s']:.0f}s")
                    if self.ref_peak_ma is None:
                        ref_attempts += 1
                        if ref_attempts > cfg["ref_retries"]:
                            return self._finish(
                                "aborted: no transmission captured - check the "
                                "trigger level and that the DUT is transmitting")
                        continue
                    self._record(ts, elapsed, None, None, None)
                    continue

                m = analyse(values, tint)
                if m is None:
                    self.missed += 1
                    continue

                if self.ref_peak_ma is None:          # fallback: no ref session
                    self.ref_peak_ma = m["peak_ma"]
                    self.log(f"reference from first TX: {m['peak_ma']:.1f} mA "
                             f"-> stop at {self.stop_level_ma:.1f} mA")
                    self.emit({"ev": "reference", "peak_ma": self.ref_peak_ma,
                               "stop_level_ma": self.stop_level_ma})

                self._record(ts, elapsed, m, values, tint)

                if m["peak_ma"] <= self.stop_level_ma:
                    self.below_streak += 1
                else:
                    self.below_streak = 0
                self.emit({"ev": "streak", "streak": self.below_streak,
                           "need": cfg["consecutive"]})

                if self.below_streak >= cfg["consecutive"]:
                    self.tn = now
                    self.result = {
                        "reached": True,
                        "t0": t0_iso, "tn": ts,
                        "elapsed_s": self.tn - self.t0,
                        "drop_delta_ma": cfg["drop_delta_ma"],
                        "consecutive": cfg["consecutive"],
                        "ref_peak_ma": self.ref_peak_ma,
                        "stop_level_ma": self.stop_level_ma,
                        "final_peak_ma": m["peak_ma"],
                        "final_delta_ma": m["peak_ma"] - self.ref_peak_ma,
                        "measurements": self.count,
                        "missed": self.missed,
                    }
                    self.store.save_result(self.result)
                    return self._finish(
                        f"stop level reached: {m['peak_ma']:.1f} mA after "
                        f"{self._hms(self.tn - self.t0)}")

            self._finish("stopped by user")

        except Exception as e:
            self.log("ERROR: " + "".join(
                traceback.format_exception_only(type(e), e)).strip())
            self._finish("aborted: " + str(e))
        finally:
            # Take the session lock: a disconnect arriving right now would
            # otherwise talk to the same VISA session concurrently.
            try:
                with self.s.lock:
                    if self.s.inst is not None:
                        self.s.inst.abort()
            except Exception:
                pass
            self.s.busy = ""

    # ------------------------------------------------------------ records
    @staticmethod
    def _hms(sec):
        h, rem = divmod(int(sec), 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}h {m:02d}m {s:02d}s" if h else f"{m:d}m {s:02d}s"

    def _record(self, ts, elapsed, m, values, tint_s=None):
        idx = self.count
        delta = (m["peak_ma"] - self.ref_peak_ma) if (
            m and self.ref_peak_ma is not None) else None
        fname = ""
        if m and values:
            meta = {
                "run": self.cfg["test_name"], "index": idx, "timestamp": ts,
                "elapsed_s": f"{elapsed:.3f}",
                "peak_ma": f"{m['peak_ma']:.4f}", "mean_ma": f"{m['mean_ma']:.4f}",
                "burst_ms": f"{m['burst_ms']:.3f}",
                "ref_peak_ma": ("" if self.ref_peak_ma is None
                                else f"{self.ref_peak_ma:.4f}"),
                "stop_level_ma": ("" if self.stop_level_ma is None
                                  else f"{self.stop_level_ma:.4f}"),
                "delta_ma": ("" if delta is None else f"{delta:.4f}"),
                "trig_level_ma": self.cfg["trig_level_ma"],
            }
            fname = self.store.save_capture(idx, meta, values, tint_s)

        row = {"index": idx, "timestamp": ts, "elapsed_s": elapsed,
               "peak_ma": m["peak_ma"] if m else None,
               "mean_ma": m["mean_ma"] if m else None,
               "burst_ms": m["burst_ms"] if m else None,
               "delta_ma": delta,
               "triggered": bool(m), "file": fname}
        self.store.append(row)
        self.store.save_state({
            "t0": storage.now_iso() if self.t0 is None else None,
            "index": idx, "ref_peak_ma": self.ref_peak_ma,
            "stop_level_ma": self.stop_level_ma, "count": self.count + 1,
            "missed": self.missed, "below_streak": self.below_streak,
            "last": ts, "finished": False,
        })
        self.count += 1

        payload = dict(row)
        payload["ref_peak_ma"] = self.ref_peak_ma
        payload["stop_level_ma"] = self.stop_level_ma
        if m and values:
            keep_i, keep_v = storage.decimate_peak(values, 400)
            payload["trace_ms"] = [round(i * tint_s * 1000.0, 4) for i in keep_i]
            payload["trace_ma"] = [round(v * 1000.0, 3) for v in keep_v]
        self.emit({"ev": "capture", **payload})
        if m:
            self.log(f"#{idx}: peak {m['peak_ma']:.1f} mA "
                     f"({'' if delta is None else format(delta, '+.1f')} mA)  "
                     f"burst {m['burst_ms']:.0f} ms  t+{self._hms(elapsed)}")

    def _finish(self, message):
        self.state = "finished"
        if self.tn is None:
            self.tn = time.time()
        elapsed = (self.tn - self.t0) if self.t0 else 0.0
        if self.store:
            self.store.save_state({
                "ref_peak_ma": self.ref_peak_ma, "count": self.count,
                "missed": self.missed, "elapsed_s": elapsed,
                "finished": True, "message": message,
            })
            if not self.result:
                self.store.save_result({
                    "reached": False, "elapsed_s": elapsed,
                    "ref_peak_ma": self.ref_peak_ma,
                    "stop_level_ma": self.stop_level_ma,
                    "measurements": self.count, "missed": self.missed,
                    "message": message,
                })
        self.log(f"{message}  |  total time {self._hms(elapsed)}")
        self.emit({"ev": "finished", "message": message, "result": self.result,
                   "elapsed_s": elapsed,
                   "folder": os.path.basename(self.store.folder) if self.store else None})
