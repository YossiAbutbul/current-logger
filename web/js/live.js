/* The running-test view: stats, the zero-span screen, the decay curve and the
   measurement table. State is mirrored from backend events, never computed
   from timers here - the backend owns the schedule. */
const Live = (() => {
  const $ = id => document.getElementById(id);
  const fmt = (v, d = 2) =>
    (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

  let caps = [], refPeak = null, threshold = 3, last = null, running = false;

  function reset(cfg) {
    caps = []; refPeak = null; last = null;
    if (cfg) threshold = Math.abs(+cfg.threshold_db) || 3;
    $("tbl").tBodies[0].innerHTML = "";
    ["sRef", "sPeak", "sDelta", "sElapsed", "sNext"].forEach(id => {
      $(id).textContent = "—"; $(id).className = "v";
    });
    $("sMissed").textContent = "0";
    $("cCount").textContent = "0";
    $("nLive").textContent = "";
    $("screenInfo").textContent = "no capture yet";
    banner(null);
    draw();
  }

  function banner(text, kind) {
    const b = $("banner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false;
    b.className = "notice " + (kind || "");
    $("bannerText").innerHTML = text;
  }

  function setRunning(on) {
    running = on;
    ["btnStart", "btnStart2"].forEach(id => $(id).disabled = on);
    ["btnStop", "btnStop2"].forEach(id => $(id).disabled = !on);
  }

  function readConfig() {
    const num = id => parseFloat($(id).value);
    const cfg = {
      test_name: $("test_name").value,
      center_hz: num("center_mhz") * 1e6,
      sweep_time_s: num("sweep_ms") / 1000,
      rbw_hz: num("rbw_khz") * 1000,
      vbw_hz: num("vbw_khz") * 1000,
      ref_level_dbm: num("ref_level_dbm"),
      atten_auto: $("atten_auto").value === "1",
      atten_db: num("atten_db"),
      detector: $("detector").value,
      trigger: $("trigger").value,
      trig_level_pct: num("trig_level_pct"),
      trig_slope: $("trig_slope").value,
      trig_timeout_s: num("trig_timeout_s"),
      interval_s: num("interval_min") * 60,
      arm_offset_s: num("arm_offset_s"),
      threshold_db: num("threshold_db"),
      max_measurements: parseInt($("max_measurements").value, 10),
      max_duration_s: num("max_duration_min") * 60,
      ref_retries: parseInt($("ref_retries").value, 10),
    };
    const bad = Object.entries(cfg).find(([, v]) => typeof v === "number" && isNaN(v));
    if (bad) throw new Error("check the value for " + bad[0]);
    if (!cfg.test_name.trim()) throw new Error("test name is empty");
    return cfg;
  }

  function row(tbl, m, clickable) {
    const tr = tbl.tBodies[0].insertRow();
    if (clickable) tr.className = "clickable";
    const cells = [
      m.index,
      (m.timestamp || "").replace("T", " ").slice(11),
      fmt(m.elapsed_s, 1),
      m.triggered ? fmt(m.peak_dbm) : "—",
      m.triggered ? ((m.delta_db >= 0 ? "+" : "") + fmt(m.delta_db)) : "—",
      null,
    ];
    cells.forEach((c, i) => {
      const td = tr.insertCell();
      if (i === 5) {
        td.innerHTML = m.triggered
          ? '<span class="pill ok">OK</span>'
          : '<span class="pill bad">MISS</span>';
      } else {
        td.textContent = c;
        if (i === 4 && m.triggered && m.delta_db <= -threshold) td.className = "bad";
        if (i === 0 && m.index === 0) td.className = "accent";
      }
    });
    return tr;
  }

  function onCapture(m) {
    caps.push(m);
    last = m.triggered ? m : last;
    const missed = caps.filter(c => !c.triggered).length;
    $("cCount").textContent = caps.length;
    $("nLive").textContent = caps.length ? String(caps.length) : "";
    $("sMissed").textContent = missed;
    $("sElapsed").textContent = fmt(m.elapsed_s, 1) + " s";

    if (m.triggered) {
      if (m.ref_peak_dbm != null) refPeak = m.ref_peak_dbm;
      $("sRef").textContent = fmt(refPeak) + " dBm";
      $("sPeak").textContent = fmt(m.peak_dbm) + " dBm";
      const d = m.delta_db;
      $("sDelta").textContent = (d >= 0 ? "+" : "") + fmt(d) + " dB";
      $("sDelta").className = "v " +
        (d <= -threshold ? "bad" : d <= -threshold / 2 ? "warn" : "ok");
      drawScreen(m);
    }
    row($("tbl"), m, false);
    $("tbl").parentElement.scrollTop = $("tbl").parentElement.scrollHeight;
    drawDecay();
  }

  function drawScreen(m) {
    const n = m.trace.length, st = m.sweep_time_s || 0;
    const xs = m.trace.map((_, i) => n > 1 ? i * st / (n - 1) * 1000 : 0);
    const lines = [];
    if (refPeak != null) {
      lines.push({ v: refPeak, color: Theme.css("--accent"), label: "ref " + fmt(refPeak) + " dBm" });
      lines.push({ v: refPeak - threshold, color: Theme.css("--bad"), label: `ref −${threshold} dB` });
    }
    Plot.line($("screen"), xs, m.trace, {
      xfmt: v => v.toFixed(1), xlabel: "ms", markPeak: true, hlines: lines,
      color: Theme.css("--accent"),
    });
    $("screenInfo").textContent =
      `#${m.index} · t+${fmt(m.elapsed_s, 1)}s · peak ${fmt(m.peak_dbm)} dBm · `
      + `${n} pts / ${fmt(st * 1000, 3)} ms · ${m.file}`;
  }

  function drawDecay() {
    const t = caps.filter(c => c.triggered);
    const lines = [];
    if (refPeak != null) {
      lines.push({ v: refPeak, color: Theme.css("--accent"), label: "reference" });
      lines.push({ v: refPeak - threshold, color: Theme.css("--bad"), label: `−${threshold} dB target` });
    }
    Plot.line($("decay"), t.map(c => c.elapsed_s), t.map(c => c.peak_dbm), {
      dots: true, color: Theme.css("--ok"), hlines: lines,
      xfmt: v => v >= 60 ? (v / 60).toFixed(1) + "m" : v.toFixed(0) + "s",
      xlabel: "elapsed", empty: "no triggered captures yet",
    });
  }

  function draw() {
    if (last) drawScreen(last);
    else Plot.line($("screen"), [], [], { empty: "no capture yet" });
    drawDecay();
  }

  function onFinished(m) {
    const prev = !!m.replay;
    setRunning(false);
    $("sState").textContent = prev ? "idle" : "finished";
    $("cState").textContent = prev ? "idle" : "finished";
    if (!prev) $("sNext").textContent = "—";
    $("liveSub").textContent = prev ? "no test running" : (m.message || "finished");

    if (m.result && m.result.reached) {
      const r = m.result;
      banner((prev ? "<b>Previous run</b> (not started here) — " : "<b>Threshold reached.</b> ")
        + `power fell ${fmt(r.final_delta_db)} dB `
        + `(${fmt(r.ref_peak_dbm)} → ${fmt(r.final_peak_dbm)} dBm) in `
        + `<b>${fmt(r.elapsed_s, 1)} s = ${fmt(r.elapsed_s / 60, 2)} min</b> `
        + `over ${r.measurements} measurements · results/${m.folder}`, "ok");
    } else if (prev) {
      banner(`<b>Previous run</b> (not started here): ${m.message || "ended"}`
        + (m.folder ? ` · results/${m.folder}` : ""), "ok muted");
    } else {
      banner(m.message || "test ended", "bad");
    }
  }

  return {
    reset, readConfig, setRunning, onCapture, onFinished, banner, draw,
    get running() { return running; },
    setRef(p) { refPeak = p; $("sRef").textContent = fmt(p) + " dBm"; },
    setThreshold(t) { threshold = Math.abs(t) || 3; },
  };
})();
