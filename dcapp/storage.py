"""Results on disk, built for runs that can last well over 24 hours.

Two tiers, because a day at 33 s intervals is ~2,600 transmissions:

  summary.csv    one row per TX, appended and flushed immediately. A few
                 hundred KB per day. This alone drives the decay curve.
  capture_*.csv  the waveform, decimated peak-preserving to ~500 points
                 (~10 KB), so a day costs ~26 MB rather than ~260 MB.
  _state.json    rewritten after every TX so an interrupted run can resume
                 instead of losing the hours already recorded.

Decimation keeps the maximum of each bucket, so the peak - the number the test
actually decides on - survives. The exact full-resolution peak is also written
into the capture header, so the reported figure is never a decimation artifact.
"""

import csv
import datetime as dt
import json
import os
import re

from .config import RESULTS_DIR

CAPTURE_RE = re.compile(r"capture_\d+\.csv")
# charge_mc and idle_ua are no longer recorded. Runs written before that change
# still carry those columns; the readers use DictReader and tolerate them.
SUMMARY_COLS = ["index", "timestamp", "elapsed_s", "peak_ma", "mean_ma",
                "burst_ms", "delta_ma", "triggered", "file"]


def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return s or "run"


def results_root(root=None):
    path = root or RESULTS_DIR
    os.makedirs(path, exist_ok=True)
    return path


def decimate_peak(values, target):
    """Peak-preserving decimation: the max of each bucket, plus bucket centres."""
    n = len(values)
    if n <= target:
        return list(range(n)), list(values)
    step = n / float(target)
    idx, out = [], []
    for b in range(target):
        lo = int(b * step)
        hi = max(lo + 1, int((b + 1) * step))
        chunk = values[lo:hi]
        if not chunk:
            continue
        peak = max(chunk)
        out.append(peak)
        idx.append(lo + chunk.index(peak))
    return idx, out


class Run:
    """One test run's folder. Append-only so a crash costs at most one TX."""

    def __init__(self, folder, cfg, instrument, resume=False):
        self.folder = folder
        self.cfg = cfg
        self.rows = []
        self._summary_path = os.path.join(folder, "summary.csv")
        if resume:
            self.rows = read_summary(folder)
        else:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "_config.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"config": cfg, "started": now_iso(),
                           "instrument": instrument}, f, indent=2)
            with open(self._summary_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(SUMMARY_COLS)

    # -------------------------------------------------------------- write
    def append(self, row):
        self.rows.append(row)
        with open(self._summary_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                row["index"], row["timestamp"], f"{row['elapsed_s']:.3f}",
                _n(row["peak_ma"]), _n(row["mean_ma"]), _n(row["burst_ms"]),
                _n(row["delta_ma"]), "yes" if row["triggered"] else "no",
                row["file"],
            ])
            f.flush()
            os.fsync(f.fileno())

    def save_capture(self, idx, meta, values, tint_s):
        """Write one decimated waveform. Returns the file name."""
        fname = f"capture_{idx:05d}.csv"
        target = self.cfg["decimate_points"]
        keep_i, keep_v = decimate_peak(values, target) if values else ([], [])
        with open(os.path.join(self.folder, fname), "w", newline="",
                  encoding="utf-8") as f:
            for k, v in meta.items():
                f.write(f"# {k},{v}\n")
            f.write(f"# raw_points,{len(values)}\n")
            f.write(f"# stored_points,{len(keep_v)}\n")
            f.write(f"# decimation,peak-per-bucket\n")
            f.write(f"# tint_s,{tint_s:.9g}\n")
            w = csv.writer(f)
            w.writerow(["time_s", "current_ma"])
            for i, v in zip(keep_i, keep_v):
                w.writerow([f"{i * tint_s:.9f}", f"{v * 1000.0:.4f}"])
        return fname

    def save_state(self, state):
        path = os.path.join(self.folder, "_state.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic, so the file is never half-written

    def save_result(self, result):
        with open(os.path.join(self.folder, "_result.json"), "w",
                  encoding="utf-8") as f:
            json.dump(result, f, indent=2)


def _n(v, d=3):
    return "" if v is None else f"{v:.{d}f}"


def prepare_folder(test_name, cfg, instrument, root=None):
    base = os.path.join(results_root(root), safe_name(test_name))
    folder, n = base, 2
    while os.path.exists(folder):
        folder = f"{base}_{n}"
        n += 1
    return Run(folder, cfg, instrument)


# ─────────────────────────────── reading back ──────────────────────────────


def read_summary(folder):
    path = os.path.join(folder, "summary.csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(r)
    return out


def list_runs(root=None):
    base = results_root(root)
    out = []
    for name in sorted(os.listdir(base)):
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        if not os.path.exists(os.path.join(folder, "summary.csv")):
            continue
        info = {"name": name,
                "captures": sum(1 for f in os.listdir(folder)
                                if CAPTURE_RE.fullmatch(f))}
        for key, fname in (("result", "_result.json"), ("state", "_state.json"),
                           ("config", "_config.json")):
            path = os.path.join(folder, fname)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                if key == "config":
                    info["started"] = data.get("started")
                else:
                    info[key] = data
        out.append(info)
    return out


def read_run(name, root=None, max_rows=6000):
    folder = os.path.join(results_root(root), safe_name(name))
    if not os.path.isdir(folder):
        return None
    rows = read_summary(folder)
    info = {"name": name, "total_rows": len(rows)}
    # A 48 h run is ~5,000 rows; cap what the browser receives and say so.
    if len(rows) > max_rows:
        step = len(rows) / float(max_rows)
        info["rows"] = [rows[int(i * step)] for i in range(max_rows)]
        info["decimated"] = True
    else:
        info["rows"] = rows
        info["decimated"] = False
    for key, fname in (("result", "_result.json"), ("config", "_config.json"),
                       ("state", "_state.json")):
        path = os.path.join(folder, fname)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    info[key] = json.load(f)
            except (OSError, ValueError):
                pass
    return info


def read_capture(name, fname, root=None):
    if not CAPTURE_RE.fullmatch(fname or ""):
        return None
    path = os.path.join(results_root(root), safe_name(name), fname)
    if not os.path.exists(path):
        return None
    meta, times, cur = {}, [], []
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
                    cur.append(float(b))
                except ValueError:
                    pass
    return {"meta": meta, "time_s": times, "current_ma": cur}
