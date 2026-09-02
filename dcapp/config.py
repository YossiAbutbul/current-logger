"""Defaults and paths for the TX current decay test."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "webdc")
RESULTS_DIR = os.path.join(ROOT, "results-current")

# Agilent N6705B, N6781A module in slot 3. Keysight IO Libraries provides VISA.
RESOURCE = "USB0::0x0957::0x0F07::MY50000200::INSTR"
CHANNEL = 3

DEFAULTS = {
    "test_name": "tx-current",

    # --- what the DUT does -------------------------------------------
    "interval_s": 33.0,        # DUT message interval: one TX every this often
    "trig_level_ma": 500.0,    # current level that marks the start of a TX

    # --- reference session --------------------------------------------
    "ref_samples": 3,          # transmissions recorded to set the reference

    # --- stop condition ------------------------------------------------
    "drop_delta_ma": 200.0,    # stop once peak <= reference - this
    "consecutive": 3,          # ...on this many transmissions in a row

    # --- capture (rarely needs changing) -------------------------------
    "window_ms": 600.0,        # capture length; must exceed the TX burst
    "tint_us": 143.36,         # sample interval; quantised to 20.48 us steps
    "pretrigger_pct": 10.0,
    "trig_slope": "POS",
    "trig_timeout_s": 45.0,    # abandon a TX after this; must exceed interval
    "decimate_points": 500,

    # --- limits ---------------------------------------------------------
    "max_measurements": 20000,
    "max_duration_s": 172800,  # 48 h
    "ref_retries": 5,
}

FLOAT_KEYS = ("interval_s", "trig_level_ma", "drop_delta_ma", "window_ms",
              "tint_us", "pretrigger_pct", "trig_timeout_s", "max_duration_s")
INT_KEYS = ("ref_samples", "consecutive", "max_measurements", "ref_retries",
            "decimate_points")


def coerce(cfg):
    out = dict(DEFAULTS)
    out.update(cfg or {})
    # accept the older name so saved configs keep loading
    if "threshold_ma" in (cfg or {}) and "drop_delta_ma" not in (cfg or {}):
        out["drop_delta_ma"] = cfg["threshold_ma"]
    for k in FLOAT_KEYS:
        out[k] = float(out[k])
    for k in INT_KEYS:
        out[k] = int(out[k])
    out["trig_slope"] = str(out["trig_slope"]).upper()[:3]
    out["consecutive"] = max(1, out["consecutive"])
    out["ref_samples"] = max(1, min(50, out["ref_samples"]))
    out["decimate_points"] = max(50, min(20000, out["decimate_points"]))
    out["drop_delta_ma"] = abs(out["drop_delta_ma"])
    return out
