"""Default test configuration and shared paths."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
RESULTS_DIR = os.path.join(ROOT, "results")

# Instrument defaults - R&S handhelds (FSC/FSH) listen for SCPI on 5555.
INSTRUMENT_HOST = "172.16.10.1"
INSTRUMENT_PORT = 5555

DEFAULTS = {
    "test_name": "burst-decay",
    "center_hz": 2.404e9,
    "sweep_time_s": 0.01,
    "rbw_hz": 0,             # 0 = auto
    "vbw_hz": 0,             # 0 = auto
    "ref_level_dbm": -20.0,
    "atten_auto": True,
    "atten_db": 0.0,
    "detector": "POS",       # POS | RMS | SAMP | NEG  (AVER is rejected by the FSC3)
    "trigger": "VID",        # VID (zero span only) | EXT | IMM (free run)
    "trig_level_pct": 50.0,
    "trig_slope": "POS",
    "trig_timeout_s": 30.0,  # how long to wait for a burst before calling it missed
    "interval_s": 300.0,     # device transmits once per this period
    "arm_offset_s": 2.0,     # arm this long after each interval boundary (may be negative)
    "threshold_db": 3.0,     # stop once peak has fallen this far below the reference
    "max_measurements": 500,
    "max_duration_s": 86400,
    "ref_retries": 5,
}

FLOAT_KEYS = ("center_hz", "sweep_time_s", "rbw_hz", "vbw_hz", "ref_level_dbm",
              "atten_db", "trig_level_pct", "trig_timeout_s", "interval_s",
              "arm_offset_s", "threshold_db", "max_duration_s")
INT_KEYS = ("max_measurements", "ref_retries")


def coerce(cfg):
    """Merge over DEFAULTS and force numeric types coming from JSON."""
    out = dict(DEFAULTS)
    out.update(cfg or {})
    for k in FLOAT_KEYS:
        out[k] = float(out[k])
    for k in INT_KEYS:
        out[k] = int(out[k])
    out["atten_auto"] = bool(out["atten_auto"])
    return out
