"""Agilent N6705B / N6781A access for the TX current test.

MEASURE-ONLY BY DESIGN. This module never writes EMUL, OUTP, VOLT, CURR:LIM or
SENS:CURR:RANG - the DUT is battery/HLC powered and the instrument's mode and
range are whatever the operator set on the front panel. The only writes are
acquisition settings (SENS:FUNC, SENS:SWE:*, TRIG:ACQ:*), which are saved on
connect and put back on disconnect.

Verified against N6705B firmware D.02.08 with an N6781A in slot 3:

  * TRIG:ACQ:SOUR takes CURR<channel> - "CURR3". Plain "CURR" is rejected with
    -224, and "CURR1" gives +310 because slot 1 is empty.
  * STAT:OPER:COND? reads +1 idle, +41 armed, +1 again once the sweep completes.
    Bit 5 (32) is "waiting for the acquisition trigger"; it clearing means done.
  * SENS:SWE:TINT quantises to multiples of 20.48 us (150 us becomes 143.36 us),
    so the readback - never the request - defines the time axis.
  * MEAS:CURR? and FETC:ARR:CURR? are negative for sourced current on this
    module; abs() is applied to match the front panel.
"""

import time

import pyvisa

WAITING_FOR_TRIGGER = 0x20          # STAT:OPER bit 5


class InstrumentError(Exception):
    pass


class N6705:
    def __init__(self, resource, channel=3, timeout_ms=15000):
        self.resource = resource
        self.ch = channel
        self.timeout_ms = timeout_ms
        self.rm = None
        self.dev = None
        self._saved = None
        self.tint_actual = None
        self.points = None
        self.pre_points = 0

    # ---------------------------------------------------------------- link
    def connect(self):
        self.rm = pyvisa.ResourceManager()
        self.dev = self.rm.open_resource(self.resource)
        self.dev.timeout = self.timeout_ms
        try:
            self.dev.clear()
        except Exception:
            pass
        self.drain_errors()
        return self.query("*IDN?")

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:
                pass
            self.rm = None

    def query(self, cmd, timeout_ms=None):
        old = self.dev.timeout
        if timeout_ms:
            self.dev.timeout = timeout_ms
        try:
            return self.dev.query(cmd).strip()
        finally:
            self.dev.timeout = old

    def write(self, cmd):
        self.dev.write(cmd)
        time.sleep(0.06)

    def write_checked(self, cmd):
        """Write and return the instrument's complaint, or None if clean."""
        self.write(cmd)
        err = self.query("SYST:ERR?")
        if err.startswith("+0") or err.startswith("0,"):
            return None
        return err

    def drain_errors(self, limit=15):
        out = []
        for _ in range(limit):
            try:
                e = self.query("SYST:ERR?", 4000)
            except Exception:
                break
            if e.startswith("+0") or e.startswith("0,"):
                break
            out.append(e)
        return out

    def recover(self):
        """Clear a wedged VISA session after a timeout."""
        try:
            self.dev.clear()
        except Exception:
            pass
        time.sleep(0.3)
        self.drain_errors()

    # ------------------------------------------------------- state probing
    def read_state(self):
        """Read-only snapshot of what the operator has configured."""
        ch = self.ch
        out = {}
        for key, cmd in (("output", f"OUTP:STAT? (@{ch})"),
                         ("emulation", f"EMUL? (@{ch})"),
                         ("range_a", f"SENS:CURR:RANG? (@{ch})"),
                         ("voltage_v", f"MEAS:VOLT? (@{ch})"),
                         ("module", f"SYST:CHAN:MOD? (@{ch})")):
            try:
                out[key] = self.query(cmd, 5000)
            except Exception:
                self.recover()
                out[key] = "?"
        return out

    def save_acquisition(self):
        ch = self.ch
        self._saved = {}
        for key, cmd in (("func", f"SENS:FUNC? (@{ch})"),
                         ("tint", f"SENS:SWE:TINT? (@{ch})"),
                         ("poin", f"SENS:SWE:POIN? (@{ch})"),
                         ("offs", f"SENS:SWE:OFFS:POIN? (@{ch})"),
                         ("tsrc", f"TRIG:ACQ:SOUR? (@{ch})"),
                         ("tlev", f"TRIG:ACQ:CURR:LEV? (@{ch})"),
                         ("tslp", f"TRIG:ACQ:CURR:SLOP? (@{ch})")):
            try:
                self._saved[key] = self.query(cmd, 5000)
            except Exception:
                self.recover()
        return dict(self._saved)

    def restore_acquisition(self):
        if not self._saved or self.dev is None:
            return
        s, ch = self._saved, self.ch
        try:
            self.write(f"ABOR:ACQ (@{ch})")
            for cmd in (f'SENS:FUNC {s.get("func", chr(34) + "VOLT" + chr(34))},(@{ch})',
                        f'SENS:SWE:TINT {s.get("tint", "2.048E-05")},(@{ch})',
                        f'SENS:SWE:POIN {s.get("poin", "4883")},(@{ch})',
                        f'SENS:SWE:OFFS:POIN {s.get("offs", "0")},(@{ch})',
                        f'TRIG:ACQ:SOUR {s.get("tsrc", "BUS")},(@{ch})',
                        f'TRIG:ACQ:CURR:LEV {s.get("tlev", "0")},(@{ch})',
                        f'TRIG:ACQ:CURR:SLOP {s.get("tslp", "POS")},(@{ch})'):
                self.write(cmd)
            self.drain_errors()
        except Exception:
            pass

    # -------------------------------------------------------- acquisition
    def configure(self, cfg, log):
        """Set up a current-triggered sweep. Returns a list of rejected commands."""
        ch = self.ch
        problems = []

        def send(cmd):
            try:
                err = self.write_checked(cmd)
            except Exception as e:
                err = f"{type(e).__name__}"
                self.recover()
            if err:
                problems.append(f"{cmd}  ->  {err}")
                log(f"setup rejected: {cmd}  ->  {err}")

        send(f"ABOR:ACQ (@{ch})")
        send(f'SENS:FUNC "CURR",(@{ch})')
        send(f"SENS:SWE:TINT {cfg['tint_us'] * 1e-6:.9g},(@{ch})")

        # the module quantises TINT, so read back what it actually took
        self.tint_actual = float(self.query(f"SENS:SWE:TINT? (@{ch})"))
        points = max(2, int(round(cfg["window_ms"] * 1e-3 / self.tint_actual)))
        send(f"SENS:SWE:POIN {points},(@{ch})")
        self.points = int(float(self.query(f"SENS:SWE:POIN? (@{ch})")))

        self.pre_points = int(self.points * cfg["pretrigger_pct"] / 100.0)
        send(f"SENS:SWE:OFFS:POIN {-self.pre_points},(@{ch})")

        send(f"TRIG:ACQ:SOUR CURR{ch},(@{ch})")
        send(f"TRIG:ACQ:CURR:LEV {cfg['trig_level_ma'] / 1000.0:.9g},(@{ch})")
        send(f"TRIG:ACQ:CURR:SLOP {cfg['trig_slope']},(@{ch})")

        src = self.query(f"TRIG:ACQ:SOUR? (@{ch})")
        if src.upper() != f"CURR{ch}":
            problems.append(f"trigger source is {src}, expected CURR{ch}")
        return problems

    def measure_current(self):
        """One immediate current reading in amps (absolute)."""
        return abs(float(self.query(f"MEAS:CURR? (@{self.ch})", 6000)))

    def arm(self):
        self.write(f"ABOR:ACQ (@{self.ch})")
        self.write(f"INIT:ACQ (@{self.ch})")

    def wait_for_capture(self, timeout_s, stop_flag, poll=0.25, arm_grace=2.0):
        """True once the sweep has triggered and completed, False on timeout.

        Completion is "the waiting-for-trigger bit was set and then cleared".
        A burst arriving inside the first poll interval would clear the bit
        before we ever saw it set, so if the bit is still not set after
        arm_grace seconds we treat the sweep as already complete rather than
        waiting out the whole timeout for a capture that has already happened.
        """
        deadline = time.time() + timeout_s
        armed_deadline = time.time() + arm_grace
        armed_seen = False
        while time.time() < deadline:
            if stop_flag():
                return False
            try:
                cond = int(float(self.query(f"STAT:OPER:COND? (@{self.ch})", 5000)))
            except Exception:
                self.recover()
                time.sleep(poll)
                continue
            if cond & WAITING_FOR_TRIGGER:
                armed_seen = True
            elif armed_seen:
                return True                      # was waiting, now is not
            elif time.time() > armed_deadline:
                return True                      # triggered before we first looked
            time.sleep(poll)
        return False

    def abort(self):
        try:
            self.write(f"ABOR:ACQ (@{self.ch})")
            self.drain_errors()
        except Exception:
            self.recover()

    def fetch(self):
        """The captured waveform as a list of amps (absolute)."""
        raw = self.query(f"FETC:ARR:CURR? (@{self.ch})", 30000)
        return [abs(float(v)) for v in raw.split(",") if v.strip()]
