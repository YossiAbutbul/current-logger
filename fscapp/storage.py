"""Reading and writing test results.

Layout on disk, one folder per run under results/:

    results/<test-name>/
        _config.json     the exact configuration the run used
        _summary.csv     one row per measurement
        _result.json     written when the threshold is reached
        capture_0000.csv one screen: metadata header + time/power samples
"""

import csv
import datetime as dt
import json
import os
import re

from .config import RESULTS_DIR

CAPTURE_RE = re.compile(r"capture_\d+\.csv")


def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def safe_name(name):
    """Filesystem-safe folder name - also the defence against path traversal."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return s or "test"


def results_root(root=None):
    path = root or RESULTS_DIR
    os.makedirs(path, exist_ok=True)
    return path


def prepare_folder(test_name, cfg, instrument, root=None):
    """Create a fresh folder for a run, never overwriting an earlier one."""
    base = os.path.join(results_root(root), safe_name(test_name))
    folder, n = base, 2
    while os.path.exists(folder):
        folder = f"{base}_{n}"
        n += 1
    os.makedirs(folder)
    with open(os.path.join(folder, "_config.json"), "w", encoding="utf-8") as f:
        json.dump({"config": cfg, "started": now_iso(), "instrument": instrument},
                  f, indent=2)
    return folder


def save_capture(folder, cfg, idx, ts, elapsed, vals, peak, delta, ref_peak, triggered):
    """Write one screen as CSV. Returns the file name."""
    fname = f"capture_{idx:04d}.csv"
    n = len(vals or [])
    step = (cfg["sweep_time_s"] / (n - 1)) if n > 1 else 0
    with open(os.path.join(folder, fname), "w", newline="", encoding="utf-8") as f:
        for k, v in (
            ("test", cfg["test_name"]), ("index", idx), ("timestamp", ts),
            ("elapsed_s", f"{elapsed:.3f}"), ("center_hz", f"{cfg['center_hz']:.0f}"),
            ("span_hz", "0"), ("sweep_time_s", f"{cfg['sweep_time_s']:.9g}"),
            ("rbw_hz", cfg["rbw_hz"] or "auto"), ("vbw_hz", cfg["vbw_hz"] or "auto"),
            ("ref_level_dbm", cfg["ref_level_dbm"]), ("detector", cfg["detector"]),
            ("trigger", cfg["trigger"]), ("trig_level_pct", cfg["trig_level_pct"]),
            ("triggered", "yes" if triggered else "no"), ("points", n),
            ("peak_dbm", "" if peak is None else f"{peak:.3f}"),
            ("ref_peak_dbm", "" if ref_peak is None else f"{ref_peak:.3f}"),
            ("delta_db", "" if delta is None else f"{delta:.3f}"),
        ):
            f.write(f"# {k},{v}\n")
        w = csv.writer(f)
        w.writerow(["time_s", "power_dbm"])
        for i, v in enumerate(vals or []):
            w.writerow([f"{i * step:.9f}", f"{v:.3f}"])
    return fname


def write_summary(folder, captures, result=None):
    with open(os.path.join(folder, "_summary.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "timestamp", "elapsed_s", "peak_dbm", "delta_db",
                    "triggered", "file"])
        for c in captures:
            w.writerow([c["index"], c["timestamp"], f"{c['elapsed_s']:.3f}",
                        "" if c["peak_dbm"] is None else f"{c['peak_dbm']:.3f}",
                        "" if c["delta_db"] is None else f"{c['delta_db']:.3f}",
                        "yes" if c["triggered"] else "no", c["file"]])
    if result:
        with open(os.path.join(folder, "_result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


# ─────────────────────────────── reading back ──────────────────────────────


def list_tests(root=None):
    base = results_root(root)
    out = []
    for name in sorted(os.listdir(base)):
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        if not os.path.exists(os.path.join(folder, "_summary.csv")):
            continue
        info = {
            "name": name,
            "captures": sum(1 for f in os.listdir(folder) if CAPTURE_RE.fullmatch(f)),
        }
        for key, fname in (("result", "_result.json"), ("config", "_config.json")):
            path = os.path.join(folder, fname)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if key == "config":
                        info["started"] = data.get("started")
                        info["center_hz"] = (data.get("config") or {}).get("center_hz")
                    else:
                        info["result"] = data
                except (OSError, ValueError):
                    pass
        out.append(info)
    return out


def read_test(name, root=None):
    folder = os.path.join(results_root(root), safe_name(name))
    if not os.path.isdir(folder):
        return None
    rows = []
    summary = os.path.join(folder, "_summary.csv")
    if os.path.exists(summary):
        with open(summary, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    info = {"name": name, "rows": rows}
    for key, fname in (("result", "_result.json"), ("config", "_config.json")):
        path = os.path.join(folder, fname)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    info[key] = json.load(f)
            except (OSError, ValueError):
                pass
    return info


def read_capture(name, fname, root=None):
    """Parse one capture CSV back into metadata plus two parallel arrays."""
    if not CAPTURE_RE.fullmatch(fname or ""):
        return None
    path = os.path.join(results_root(root), safe_name(name), fname)
    if not os.path.exists(path):
        return None
    meta, times, powers = {}, [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                k, _, v = line[1:].strip().partition(",")
                meta[k.strip()] = v.strip()
            elif line and not line.startswith("time_s"):
                a, _, b = line.partition(",")
                try:
                    times.append(float(a))
                    powers.append(float(b))
                except ValueError:
                    pass
    return {"meta": meta, "time_s": times, "power_dbm": powers}
