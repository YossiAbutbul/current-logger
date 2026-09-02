/* The running-test view. State is mirrored from backend events; the backend
   owns the schedule and the timing. A 24 h run is ~2,600 transmissions, so the
   table keeps a tail and the decay chart downsamples before drawing. */
const Live = (() => {
  const $ = id => document.getElementById(id);
  const fmt = (v, d = 1) =>
    (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);
  const MAX_ROWS = 300;
  const MAX_PLOT = 1200;

  let pts = [], refPeak = null, stopLevel = null, last = null;
  let running = false, t0ms = null, ticker = null;

  const clock = ts => (ts || "").replace("T", " ").slice(11);

  function hms(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return "—";
    const s = Math.floor(sec);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m ${String(r).padStart(2, "0")}s`
      : `${m}m ${String(r).padStart(2, "0")}s`;
  }

  function reset(cfg, ref, stop) {
    pts = []; last = null;
    refPeak = (ref === undefined) ? refPeak : ref;
    stopLevel = (stop === undefined) ? stopLevel : stop;
    $("tbl").tBodies[0].innerHTML = "";
    ["sPeak", "sDelta"].forEach(id => {
      $(id).textContent = "—"; $(id).className = "v";
    });
    $("sStreak").textContent = "0";
    $("cCount").textContent = "0";
    $("nLive").textContent = "";
    $("screenInfo").textContent = "no capture yet";
    setRef(refPeak, stopLevel);
    banner(null);
    draw();
  }

  function banner(text, kind) {
    const b = $("runBanner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false;
    b.className = "notice " + (kind || "");
    b.style.opacity = "1";
    $("runBannerText").innerHTML = text;
  }

  function setRunning(on) {
    running = on;
    $("btnStart").disabled = on;
    $("btnStop").disabled = !on;
    // the name picks the results folder, so it is fixed once a run starts
    const name = $("test_name");
    if (name) name.disabled = on;
  }

  function startClock(t0iso) {
    $("sT0").textContent = clock(t0iso) || "—";
    t0ms = Date.now();
    if (ticker) clearInterval(ticker);
    ticker = setInterval(() => {
      if (!t0ms) return;
      const e = (Date.now() - t0ms) / 1000;
      $("sElapsed").textContent = hms(e);
      $("cElapsed").textContent = hms(e);
    }, 1000);
  }

  function stopClock(finalSec) {
    if (ticker) { clearInterval(ticker); ticker = null; }
    if (finalSec !== undefined && finalSec !== null) {
      $("sElapsed").textContent = hms(finalSec);
      $("cElapsed").textContent = hms(finalSec);
    }
    t0ms = null;
  }

  function setRef(ref, stop) {
    refPeak = (ref === undefined || ref === null) ? refPeak : ref;
    stopLevel = (stop === undefined || stop === null) ? stopLevel : stop;
    $("sRef").textContent = refPeak === null ? "—" : fmt(refPeak) + " mA";
    $("sStop").textContent = stopLevel === null ? "—" : fmt(stopLevel) + " mA";
    $("cRef").textContent = refPeak === null ? "—" : fmt(refPeak, 0) + " mA";
    $("cStop").textContent = stopLevel === null ? "—" : fmt(stopLevel, 0) + " mA";
  }

  function row(m) {
    const tr = document.createElement("tr");
    const cells = [
      m.index, clock(m.timestamp), hms(m.elapsed_s),
      m.triggered ? fmt(m.peak_ma) : "—",
      m.delta_ma === null || m.delta_ma === undefined ? "—"
        : (m.delta_ma >= 0 ? "+" : "") + fmt(m.delta_ma),
      m.triggered ? fmt(m.burst_ms, 0) : "—",
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i === 3 && m.triggered && stopLevel !== null && m.peak_ma <= stopLevel)
        td.className = "bad";
      if (i === 0 && m.index === 0) td.className = "accent";
      tr.appendChild(td);
    });
    const td = document.createElement("td");
    td.innerHTML = m.triggered ? '<span class="pill ok">OK</span>'
      : '<span class="pill bad">MISS</span>';
    tr.appendChild(td);
    return tr;
  }

  function onCapture(m) {
    if (m.ref_peak_ma != null || m.stop_level_ma != null)
      setRef(m.ref_peak_ma, m.stop_level_ma);

    if (m.triggered) {
      pts.push({ elapsed_s: m.elapsed_s, peak_ma: m.peak_ma });
      last = m;
      $("sPeak").textContent = fmt(m.peak_ma) + " mA";
      const d = m.delta_ma;
      if (d !== null && d !== undefined) {
        $("sDelta").textContent = (d >= 0 ? "+" : "") + fmt(d) + " mA";
        const frac = stopLevel !== null && refPeak !== null
          ? (refPeak - m.peak_ma) / (refPeak - stopLevel) : 0;
        $("sDelta").className = "v " + (frac >= 1 ? "bad" : frac >= 0.5 ? "warn" : "ok");
      }
      if (m.trace_ms) drawScreen(m);
    }

    const tb = $("tbl").tBodies[0];
    tb.appendChild(row(m));
    while (tb.rows.length > MAX_ROWS) tb.deleteRow(0);
    $("tblNote").textContent = "showing last " + tb.rows.length
      + " · full history in results-current/";
    $("tbl").parentElement.scrollTop = $("tbl").parentElement.scrollHeight;
    drawDecay();
  }

  function setCounts(total) { $("cCount").textContent = total; $("nLive").textContent = total || ""; }
  function setStreak(s, need) { $("sStreak").textContent = s + " / " + need; }

  function guides() {
    const g = [];
    if (refPeak !== null)
      g.push({ v: refPeak, color: "--accent", label: "reference " + fmt(refPeak) + " mA" });
    if (stopLevel !== null)
      g.push({ v: stopLevel, color: "--bad", label: "stop at " + fmt(stopLevel) + " mA" });
    return g;
  }

  function drawScreen(m) {
    Plot.line($("screen"), m.trace_ms, m.trace_ma, {
      xfmt: v => v.toFixed(0), xlabel: "ms", markPeak: true, unit: "mA",
      hlines: guides(), color: "--accent",
    });
    $("screenInfo").textContent =
      "#" + m.index + " · " + clock(m.timestamp) + " · peak " + fmt(m.peak_ma)
      + " mA · burst " + fmt(m.burst_ms, 0) + " ms · " + m.file;
  }

  function drawDecay() {
    let xs = pts.map(p => p.elapsed_s), ys = pts.map(p => p.peak_ma);
    if (xs.length > MAX_PLOT) {
      const step = xs.length / MAX_PLOT, nx = [], ny = [];
      for (let b = 0; b < MAX_PLOT; b++) {
        const lo = Math.floor(b * step);
        const hi = Math.max(lo + 1, Math.floor((b + 1) * step));
        let mi = lo;
        for (let i = lo; i < hi && i < ys.length; i++) if (ys[i] < ys[mi]) mi = i;
        nx.push(xs[mi]); ny.push(ys[mi]);      // keep the minimum: the decay edge
      }
      xs = nx; ys = ny;
    }
    Plot.line($("decay"), xs, ys, {
      dots: xs.length <= 200, color: "--ok", hlines: guides(),
      xfmt: v => v >= 3600 ? (v / 3600).toFixed(1) + "h"
        : v >= 60 ? (v / 60).toFixed(0) + "m" : v.toFixed(0) + "s",
      xlabel: "elapsed", empty: "no transmissions captured yet",
    });
  }

  function draw() {
    if (last && last.trace_ms) drawScreen(last);
    else Plot.line($("screen"), [], [], { empty: "no capture yet" });
    drawDecay();
  }

  function onFinished(m) {
    const prev = !!m.replay;
    setRunning(false);
    stopClock(m.elapsed_s);
    $("liveSub").textContent = prev ? "not started" : (m.message || "finished");

    const r = m.result;
    if (r && r.reached) {
      banner((prev ? "<b>Previous run</b> (not started here) — " : "<b>Stop level reached.</b> ")
        + "current fell from <b>" + fmt(r.ref_peak_ma) + " mA</b> to <b>"
        + fmt(r.final_peak_ma) + " mA</b> (stop level " + fmt(r.stop_level_ma)
        + " mA, drop " + fmt(r.drop_delta_ma, 0) + " mA) confirmed over "
        + r.consecutive + " transmissions.<br>"
        + "<b>t0</b> " + r.t0 + " &nbsp; <b>tn</b> " + r.tn
        + " &nbsp; <b>Δt = " + hms(r.elapsed_s) + "</b> ("
        + (r.elapsed_s / 3600).toFixed(3) + " h) over " + r.measurements
        + " transmissions, " + r.missed + " missed · results-current/" + m.folder, "ok");
    } else if (prev) {
      banner("<b>Previous run</b> (not started here): " + (m.message || "ended"), "ok");
      $("runBanner").style.opacity = ".65";
    } else {
      banner((m.message || "run ended")
        + (m.elapsed_s ? " · <b>Δt = " + hms(m.elapsed_s) + "</b>" : ""), "bad");
    }
  }

  return {
    reset, setRunning, onCapture, onFinished, banner, draw, setCounts, setStreak,
    setRef, startClock, stopClock, hms,
    get running() { return running; },
  };
})();
