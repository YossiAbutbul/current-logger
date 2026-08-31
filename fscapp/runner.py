"""The burst-decay test state machine.

Timing lives here, in the backend, so a closed tab or a sleeping page cannot
disturb a run that may last hours. Capture #0 is taken immediately and its peak
becomes the reference; capture k is then armed at

    t_ref + k * interval + arm_offset

Anchoring every slot to t_ref rather than to the previous capture means the
schedule cannot drift as capture durations vary.
"""

import threading
import time
import traceback

from . import storage
from .scpi import Instrument, Scpi


class TestRunner(threading.Thread):
    def __init__(self, cfg, host, scpi_port, emit, results_root=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.host, self.scpi_port = host, scpi_port
        self.emit = emit
        self.results_root = results_root
        # Must not be named _stop: that shadows Thread._stop(), which
        # is_alive() calls internally.
        self._abort = threading.Event()
        self.state = "idle"
        self.folder = None
        self.captures = []
        self.ref_peak = None
        self.t_ref = None
        self.result = None

    # -- control ----------------------------------------------------------
    def stop(self):
        self._abort.set()

    def stopped(self):
        return self._abort.is_set()

    def log(self, msg):
        self.emit({"ev": "log", "msg": msg, "t": storage.now_iso()})

    def _sleep_until(self, when):
        """Interruptible wait that keeps the UI countdown honest."""
        while not self.stopped():
            remain = when - time.time()
            if remain <= 0:
                return True
            self.emit({"ev": "waiting", "remaining_s": round(remain, 1),
                       "next_index": len(self.captures)})
            time.sleep(min(0.5, remain))
        return False

    def _record(self, idx, ts, elapsed, vals, peak, delta, triggered, extra=None):
        fname = storage.save_capture(self.folder, self.cfg, idx, ts, elapsed,
                                     vals or [], peak, delta, self.ref_peak, triggered)
        row = {"index": idx, "timestamp": ts, "elapsed_s": elapsed,
               "peak_dbm": peak, "delta_db": delta, "triggered": triggered,
               "file": fname}
        self.captures.append(row)
        storage.write_summary(self.folder, self.captures, self.result)
        self.emit({"ev": "capture", **row, **(extra or {})})
        return row

    # -- main loop --------------------------------------------------------
    def run(self):
        cfg = self.cfg
        scpi = inst = None
        try:
            self.state = "connecting"
            self.emit({"ev": "state", "state": self.state})
            scpi = Scpi(self.host, self.scpi_port)
            inst = Instrument(scpi)
            self.log(f"connected: {inst.idn()}")

            self.folder = storage.prepare_folder(
                cfg["test_name"], cfg, f"{self.host}:{self.scpi_port}",
                self.results_root)
            self.log(f"saving captures to {self.folder}")

            problems = inst.apply_setup(cfg, self.log)
            if problems:
                self.emit({"ev": "setup_warning", "problems": problems})
            self.log(f"zero span @ {cfg['center_hz']/1e6:.6g} MHz, "
                     f"sweep {cfg['sweep_time_s']*1000:.3g} ms, "
                     f"trigger {cfg['trigger']}")

            self.state = "running"
            self.emit({"ev": "state", "state": self.state})

            idx = slot = ref_attempts = 0
            t_start = time.time()

            while not self.stopped():
                if idx >= cfg["max_measurements"]:
                    return self._finish("stopped: reached max measurements")
                if time.time() - t_start > cfg["max_duration_s"]:
                    return self._finish("stopped: reached max duration")

                due = (time.time() if self.ref_peak is None
                       else self.t_ref + slot * cfg["interval_s"] + cfg["arm_offset_s"])
                if not self._sleep_until(due):
                    break

                self.emit({"ev": "arming", "index": idx})
                vals, triggered = inst.capture(cfg["trig_timeout_s"], self.stopped)
                ts, cap_time = storage.now_iso(), time.time()

                if not triggered:
                    self.log(f"#{idx}: no trigger within "
                             f"{cfg['trig_timeout_s']:.0f}s - burst missed")
                    if self.ref_peak is None:
                        ref_attempts += 1
                        if ref_attempts > cfg["ref_retries"]:
                            return self._finish(
                                "aborted: could not capture a reference burst - "
                                "check trigger level, frequency, and that the "
                                "device is transmitting")
                        continue                       # retry the reference now
                    self._record(idx, ts, cap_time - self.t_ref, [], None, None,
                                 False, {"trace": []})
                    slot += 1
                    idx += 1
                    continue

                peak = max(vals)
                if self.ref_peak is None:
                    self.ref_peak, self.t_ref, slot, delta = peak, cap_time, 1, 0.0
                    self.log(f"reference power {peak:.2f} dBm at {ts}")
                    self.emit({"ev": "reference", "peak_dbm": peak, "timestamp": ts})
                else:
                    delta = peak - self.ref_peak
                    slot += 1

                elapsed = cap_time - self.t_ref
                self._record(idx, ts, elapsed, vals, peak, delta, True,
                             {"trace": [round(v, 2) for v in vals],
                              "sweep_time_s": cfg["sweep_time_s"],
                              "ref_peak_dbm": self.ref_peak})
                self.log(f"#{idx}: peak {peak:.2f} dBm  ({delta:+.2f} dB)  "
                         f"t+{elapsed:.1f}s")

                if delta <= -abs(cfg["threshold_db"]):
                    self.result = {
                        "reached": True, "threshold_db": cfg["threshold_db"],
                        "ref_peak_dbm": self.ref_peak, "final_peak_dbm": peak,
                        "final_delta_db": delta, "elapsed_s": elapsed,
                        "measurements": len(self.captures),
                        "started": self.captures[0]["timestamp"], "ended": ts,
                    }
                    return self._finish(
                        f"threshold reached: {delta:+.2f} dB after {elapsed:.1f}s")
                idx += 1

            self._finish("stopped by user")

        except Exception as e:
            self.log("ERROR: " + "".join(
                traceback.format_exception_only(type(e), e)).strip())
            self._finish("aborted: " + str(e))
        finally:
            if inst:
                inst.release()
            if scpi:
                scpi.close()

    def _finish(self, message):
        import os
        self.state = "finished"
        if self.folder:
            storage.write_summary(self.folder, self.captures, self.result)
        self.log(message)
        self.emit({"ev": "finished", "message": message, "result": self.result,
                   "folder": os.path.basename(self.folder) if self.folder else None})
