/* The FSC3 control panel.

   The analyzer is the single source of truth: every control sends a command
   and then redraws from the state the backend reads back, so what you see is
   what the instrument actually took - not what was typed. A field being edited
   is never overwritten underneath the cursor. */
(() => {
  const $ = id => document.getElementById(id);
  const MODE_NAMES = { WRIT: "clear write", MAXH: "max hold", MINH: "min hold",
                       AVER: "average", VIEW: "view (frozen)" };
  const DET_NAMES = { APE: "auto peak", POS: "max peak", NEG: "min peak",
                      SAMP: "sample", RMS: "RMS" };
  const GAP = 26;          // --s5, the gap between panels
  const SPLIT_PX = 14;     // the draggable handle track between them
  const MIN_TABLE = 208;   // room kept for the marker table under the screen
  const LEFT_MIN = 380;    // the settings column never gets narrower than this
  const PANEL_PAD = 52;    // --s5 either side of a panel

  let st = {};            // last state read from the analyzer
  let connected = false;
  let live = false;
  let last = null;        // last trace message
  let sel = 1;            // marker the action buttons apply to

  /* ─────────────────────────── formatting ─────────────────────────── */
  const trim = s => s.indexOf(".") < 0 ? s
    : s.replace(/0+$/, "").replace(/\.$/, "");
  const isNum = v => v !== null && v !== undefined && !isNaN(v);

  function fmtFreq(hz) {
    if (!isNum(hz)) return "—";
    const a = Math.abs(hz);
    if (a >= 1e9) return trim((hz / 1e9).toFixed(6)) + " GHz";
    if (a >= 1e6) return trim((hz / 1e6).toFixed(4)) + " MHz";
    if (a >= 1e3) return trim((hz / 1e3).toFixed(3)) + " kHz";
    return trim(hz.toFixed(1)) + " Hz";
  }
  const fmtDb = (v, d = 1) => isNum(v) ? trim(v.toFixed(d)) + " dB" : "—";
  const fmtDbm = (v, d = 1) => isNum(v) ? trim(v.toFixed(d)) + " dBm" : "—";
  const toMHz = hz => isNum(hz) ? trim((hz / 1e6).toFixed(6)) : "";
  const toKHz = hz => isNum(hz) ? trim((hz / 1e3).toFixed(3)) : "";
  const toMs = s => isNum(s) ? trim((s * 1000).toFixed(3)) : "";

  /* There is no log panel; the last thing the backend said goes in the footer. */
  function log(msg) { $("footRight").textContent = msg; }

  const BANG = { ok: "✓", bad: "!", "": "i" };

  function banner(text, kind) {
    const b = $("banner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false;
    b.className = "notice " + (kind || "");
    b.querySelector(".bang").textContent = BANG[kind || ""] || "i";
    $("bannerText").innerHTML = text;
  }

  /* ───────────────────────────── views ────────────────────────────── */
  const VIEWS = { connect: "viewConnect", spectrum: "viewSpectrum",
                  sweep: "viewSweep" };
  const VIEW_KEY = "fsc3.view";

  function showView(v) {
    if (!VIEWS[v]) v = "connect";
    Object.entries(VIEWS).forEach(([k, id]) => { $(id).hidden = (k !== v); });
    document.querySelectorAll(".rail-item").forEach(b =>
      b.setAttribute("aria-current", String(b.dataset.view === v)));
    // only the spectrum page pins its right column; the others scroll normally
    document.querySelector(".main").classList.toggle("fixed", v === "spectrum");
    // reopen on the same page after a refresh
    try { localStorage.setItem(VIEW_KEY, v); } catch (e) { /* private mode */ }
    if (v === "spectrum") { sizeScreen(); drawTrace(); }
  }

  function savedView() {
    try {
      const v = localStorage.getItem(VIEW_KEY);
      return VIEWS[v] ? v : "connect";
    } catch (e) { return "connect"; }
  }

  /* ─────────────────────── the screen's geometry ──────────────────── */
  /* The screen fills its box: full width from the stylesheet, and whatever
     height the splitters leave once the panel's own chrome is accounted for.

     Everything measured here is deliberately independent of the canvas. The
     panel is a grid row of a definite-height column, so its height is fixed by
     the splitters and content larger than the row overflows instead of growing
     it - which makes panel.clientHeight a safe budget. (scrollHeight is NOT:
     it is floored at clientHeight, so it reports the whole row as chrome and
     starves the canvas to nothing.) */
  function sizeScreen() {
    const cv = $("screen");
    if (!cv || $("viewSpectrum").hidden) return;
    const panel = cv.closest(".panel");
    const main = document.querySelector(".main");
    if (!panel || !main) return;

    cv.style.width = "";            // the stylesheet's 100% of the panel
    cv.style.height = "0px";        // collapse, so the chrome can be measured

    let h = 340;                    // fallback when the page scrolls instead
    if (main.classList.contains("fixed") && panel.clientHeight) {
      const cs = getComputedStyle(panel);
      const gap = parseFloat(cs.rowGap) || 0;
      // measured in place, so the faceplate header's negative margin is exact
      const above = cv.getBoundingClientRect().top
        - panel.getBoundingClientRect().top
        - (parseFloat(cs.borderTopWidth) || 0);
      let below = parseFloat(cs.paddingBottom) || 0;
      let after = false;
      for (const el of panel.children) {
        if (el === cv) { after = true; continue; }
        if (after) below += gap + el.offsetHeight;
      }
      h = panel.clientHeight - above - below;
    }
    cv.style.height = Math.round(Math.max(140, h)) + "px";
  }

  /* ──────────────────────── the marker table ─────────────────────── */
  const TABLE_KEY = "fsc3.table";
  const tableShown = () => {
    const col = document.querySelector(".work-right");
    return !col || !col.classList.contains("no-table");
  };
  function showTable(on) {
    const col = document.querySelector(".work-right");
    if (col) col.classList.toggle("no-table", !on);
    const b = $("btnTable");
    if (b) {
      b.textContent = on ? "Hide table" : "Show table";
      b.setAttribute("aria-pressed", String(!on));
    }
    try { localStorage.setItem(TABLE_KEY, on ? "1" : "0"); } catch (e) {}
    sizeScreen();
    drawTrace();
  }

  /* ───────────────────────────── splitters ───────────────────────── */
  const SPLIT_KEY = "fsc3.split";
  const loadSplit = () => {
    try { return JSON.parse(localStorage.getItem(SPLIT_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  };
  const saveSplit = o => {
    try { localStorage.setItem(SPLIT_KEY, JSON.stringify(o)); } catch (e) {}
  };

  /* Coalesce redraws onto one frame, with a timer as a backstop.
     requestAnimationFrame is throttled to nothing in a background tab or a
     hidden pane, so on its own it leaves the screen stale at the wrong size
     until something else nudges it; whichever of the two fires first wins and
     cancels the other. The pending frame is tracked by id rather than by a
     boolean, which would latch on and kill every later redraw. */
  let redrawId = 0, redrawTimer = 0;
  function runRedraw() {
    if (redrawId) { cancelAnimationFrame(redrawId); redrawId = 0; }
    if (redrawTimer) { clearTimeout(redrawTimer); redrawTimer = 0; }
    sizeScreen();
    drawTrace();
  }
  function requestRedraw() {
    if (redrawId) cancelAnimationFrame(redrawId);
    if (redrawTimer) clearTimeout(redrawTimer);
    redrawId = requestAnimationFrame(runRedraw);
    redrawTimer = setTimeout(runRedraw, 120);
  }

  function initSplitters() {
    const wb = document.querySelector(".workbench");
    const col = document.querySelector(".work-right");
    if (!wb || !col) return;

    const saved = loadSplit();
    if (saved.left) wb.style.setProperty("--left-w", saved.left + "px");
    if (saved.table) wb.style.setProperty("--table-h", saved.table + "px");

    const setLeft = px => {
      const max = Math.max(LEFT_MIN, wb.clientWidth - 420);
      const w = Math.round(Math.max(LEFT_MIN, Math.min(px, max)));
      wb.style.setProperty("--left-w", w + "px");
      return { left: w };
    };
    const setTable = px => {
      const max = Math.max(MIN_TABLE, col.clientHeight - 240);
      const h = Math.round(Math.max(MIN_TABLE, Math.min(px, max)));
      wb.style.setProperty("--table-h", h + "px");
      return { table: h };
    };

    handle($("splitV"), {
      move: e => setLeft(e.clientX - wb.getBoundingClientRect().left),
      key: (e, step) => {
        if (e.key === "ArrowLeft") return setLeft(wb.clientWidth * 0 + leftNow(wb) - step);
        if (e.key === "ArrowRight") return setLeft(leftNow(wb) + step);
      },
      reset: () => { wb.style.removeProperty("--left-w"); return { left: 0 }; },
    });
    handle($("splitH"), {
      move: e => setTable(col.getBoundingClientRect().bottom - e.clientY),
      key: (e, step) => {
        if (e.key === "ArrowUp") return setTable(tableNow(wb) + step);
        if (e.key === "ArrowDown") return setTable(tableNow(wb) - step);
      },
      reset: () => { wb.style.removeProperty("--table-h"); return { table: 0 }; },
    });
  }

  const px = v => parseFloat(v) || 0;
  const leftNow = wb => px(getComputedStyle(wb).getPropertyValue("--left-w")) || 400;
  const tableNow = wb => px(getComputedStyle(wb).getPropertyValue("--table-h")) || 224;
  function tableHeight() {
    const wb = document.querySelector(".workbench");
    return wb ? tableNow(wb) : 224;
  }

  /* One drag handler for both handles: pointer capture so the drag survives
     the cursor leaving the 14px strip, and the size is only persisted when the
     drag ends rather than on every move. */
  function handle(el, opts) {
    if (!el) return;
    let active = false, pending = null;

    el.addEventListener("pointerdown", e => {
      active = true;
      try { el.setPointerCapture(e.pointerId); } catch (err) {}
      el.classList.add("dragging");
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    el.addEventListener("pointermove", e => {
      if (!active) return;
      pending = opts.move(e) || pending;
      requestRedraw();
    });
    const end = e => {
      if (!active) return;
      active = false;
      el.classList.remove("dragging");
      document.body.style.userSelect = "";
      try { el.releasePointerCapture(e.pointerId); } catch (err) {}
      if (pending) { saveSplit({ ...loadSplit(), ...pending }); pending = null; }
      requestRedraw();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("lostpointercapture", end);

    el.addEventListener("dblclick", () => {
      const patch = opts.reset();
      const all = loadSplit();
      Object.keys(patch).forEach(k => delete all[k]);
      saveSplit(all);
      requestRedraw();
    });
    el.addEventListener("keydown", e => {
      const patch = opts.key(e, e.shiftKey ? 40 : 12);
      if (!patch) return;
      e.preventDefault();
      saveSplit({ ...loadSplit(), ...patch });
      requestRedraw();
    });
  }

  /* ──────────────────────── controls plumbing ─────────────────────── */
  /* A field commits on Enter or on losing focus, and only when it changed.
     Anything unparseable snaps back to the instrument's own value. */
  function field(id, commit) {
    const el = $(id);
    el.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); el.blur(); }
      if (e.key === "Escape") { el.value = el._last || ""; el.blur(); }
    });
    el.addEventListener("blur", () => {
      const raw = el.value.trim();
      if (raw === (el._last || "")) return;
      const v = parseFloat(raw);
      if (isNaN(v)) { el.value = el._last || ""; return; }
      commit(v);
    });
  }

  function setVal(id, text) {
    const el = $(id);
    if (!el) return;
    el._last = text;
    if (document.activeElement !== el) el.value = text;
  }

  function seg(id, key, pick) {
    document.querySelectorAll("#" + id + " .btn").forEach(b => {
      b.onclick = () => pick(b.dataset[key], b);
    });
  }
  function segSet(id, key, value) {
    document.querySelectorAll("#" + id + " .btn").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset[key] === String(value))));
  }

  /* ───────────────────────────── state ────────────────────────────── */
  function applyState(state) {
    if (!state || !Object.keys(state).length) return;
    st = state;
    const zero = !!st.zero_span;

    // header
    $("cCenter").textContent = fmtFreq(st.center_hz);
    $("cSpan").textContent = zero ? "zero span" : fmtFreq(st.span_hz);
    $("cRef").textContent = fmtDbm(st.ref_level_dbm);
    $("cMode").textContent = MODE_NAMES[st.trace_mode] || st.trace_mode || "—";

    // connect page readout
    $("iCenter").textContent = fmtFreq(st.center_hz);
    $("iSpan").textContent = zero ? "0 Hz (zero span)" : fmtFreq(st.span_hz);
    $("iRef").textContent = fmtDbm(st.ref_level_dbm);
    $("iOffset").textContent = fmtDb(st.ref_offset_db);
    $("iRange").textContent = fmtDb(st.range_db, 0);
    $("iMode").textContent = MODE_NAMES[st.trace_mode] || "—";
    $("iDet").textContent = st.detector || "—";
    $("iBw").textContent = fmtFreq(st.rbw_hz) + "  /  " + fmtFreq(st.vbw_hz);
    $("iAtt").textContent = (isNum(st.atten_db) ? st.atten_db.toFixed(0) + " dB" : "—")
      + (st.atten_auto ? "  (auto)" : "");
    $("iSwt").textContent = isNum(st.sweep_time_s)
      ? trim((st.sweep_time_s * 1000).toFixed(3)) + " ms" : "—";

    // frequency
    setVal("f_center", toMHz(st.center_hz));
    setVal("f_span", toMHz(st.span_hz));
    setVal("f_start", toMHz(st.start_hz));
    setVal("f_stop", toMHz(st.stop_hz));
    $("freqAside").textContent = zero ? "zero span — the x axis is time" : "MHz";

    // amplitude
    setVal("a_ref", isNum(st.ref_level_dbm) ? trim(st.ref_level_dbm.toFixed(2)) : "");
    setVal("a_offset", isNum(st.ref_offset_db) ? trim(st.ref_offset_db.toFixed(2)) : "");
    setVal("a_att", isNum(st.atten_db) ? st.atten_db.toFixed(0) : "");
    $("a_range").value = fmtDb(st.range_db, 0);
    $("ampAside").textContent = st.atten_auto ? "attenuation auto" : "attenuation manual";

    // trace
    segSet("modeSeg", "mode", st.trace_mode);
    $("modeAside").textContent = MODE_NAMES[st.trace_mode] || "—";

    // sweep page
    setVal("s_rbw", toKHz(st.rbw_hz));
    setVal("s_vbw", toKHz(st.vbw_hz));
    setVal("s_time", toMs(st.sweep_time_s));
    segSet("detSeg", "det", st.detector);
    segSet("contSeg", "cont", st.continuous ? "1" : "0");
    $("detAside").textContent = DET_NAMES[st.detector] || st.detector || "—";
    $("bwAside").textContent = fmtFreq(st.rbw_hz) + " / " + fmtFreq(st.vbw_hz);
    $("swAside").textContent = st.continuous ? "continuous" : "single";

    // markers
    const n = st.markers || 0;
    segSet("mkSeg", "count", n);
    if (sel > n) sel = Math.max(1, n);
    $("mkSelLabel").textContent = n ? sel : "—";
    $("mkAside").textContent = n ? n + " active" : "off";
    $("nMark").textContent = n ? "M" + n : "";
    $("mkXHead").textContent = zero ? "Time ms" : "Frequency MHz";
    $("mkX").placeholder = zero ? "set ms" : "set MHz";
    markerTable();

    $("specSub").textContent = connected
      ? fmtFreq(st.center_hz) + (zero ? " · zero span" : " · " + fmtFreq(st.span_hz) + " span")
        + " · ref " + fmtDbm(st.ref_level_dbm)
        + " · " + (MODE_NAMES[st.trace_mode] || "")
      : "not connected";
  }

  /* ───────────────────────────── markers ──────────────────────────── */
  function markerTable() {
    const tb = $("mkTbl").tBodies[0];
    tb.innerHTML = "";
    const ms = (last && last.markers) || [];
    const n = st.markers || 0;
    if (!n) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5; td.textContent = "no markers on";
      td.style.textAlign = "center";
      tr.appendChild(td); tb.appendChild(tr);
      return;
    }
    const zero = !!st.zero_span;
    const m1 = ms.find(m => m.n === 1);
    for (let i = 1; i <= n; i++) {
      const m = ms.find(x => x.n === i) || { n: i, x: null, y: null };
      const tr = document.createElement("tr");
      tr.className = "clickable" + (i === sel ? " pick" : "");
      tr.onclick = () => { sel = i; applyState(st); drawTrace(); };
      const xTxt = m.x === null ? "—"
        : zero ? trim((m.x * 1000).toFixed(3)) : trim((m.x / 1e6).toFixed(6));
      const delta = (i > 1 && m1 && isNum(m1.y) && isNum(m.y))
        ? (m.y - m1.y >= 0 ? "+" : "") + (m.y - m1.y).toFixed(2) : "—";
      [i, xTxt, m.y === null ? "—" : m.y.toFixed(2), delta].forEach((c, k) => {
        const td = document.createElement("td");
        td.textContent = c;
        if (k === 0 && i === sel) td.className = "accent";
        tr.appendChild(td);
      });
      const td = document.createElement("td");
      if (i === sel) td.innerHTML = '<span class="pill ok">SEL</span>';
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  /* ───────────────────────────── trace ────────────────────────────── */
  function drawTrace() {
    if (!last || !last.values || !last.values.length) {
      Plot.line($("screen"), [], [], { empty: "" });   // no placeholder caption
      return;
    }
    const v = last.values, n = v.length, zero = !!last.zero_span;
    let xs, xlabel, xfmt;
    if (zero) {
      const span = (st.sweep_time_s || 0) * 1000;      // the x axis is time
      xs = v.map((_, i) => n > 1 ? span * i / (n - 1) : 0);
      xlabel = "ms";
      xfmt = t => trim(t.toFixed(t >= 10 ? 1 : 3));
    } else {
      const a = last.start_hz / 1e6, b = last.stop_hz / 1e6;
      xs = v.map((_, i) => n > 1 ? a + (b - a) * i / (n - 1) : a);
      xlabel = "MHz";
      xfmt = f => trim(f.toFixed(3));
    }
    const vlines = (last.markers || [])
      .filter(m => m.x !== null)
      .map(m => ({
        v: zero ? m.x * 1000 : m.x / 1e6,
        y: m.y,
        color: m.n === sel ? "--accent" : "--warn",
        label: "M" + m.n + (m.y === null ? "" : "  " + m.y.toFixed(1)),
      }));
    /* The y axis is the analyzer's own: the reference level is the top of the
       screen and the display range sets the bottom, so a signal sits exactly
       where it sits on the instrument instead of being auto-scaled. */
    const yrange = (isNum(st.ref_level_dbm) && isNum(st.range_db)
                    && st.range_db > 0)
      ? [st.ref_level_dbm - st.range_db, st.ref_level_dbm] : null;

    Plot.line($("screen"), xs, v, {
      color: "--ok", unit: "dBm", xlabel, xfmt, vlines, yrange,
    });

    $("traceInfo").textContent =
      (zero ? trim(((st.sweep_time_s || 0) * 1000).toFixed(2)) + " ms sweep"
              : fmtFreq(last.start_hz) + " – " + fmtFreq(last.stop_hz))
      + " · " + (DET_NAMES[st.detector] || st.detector || "")
      + " · RBW " + fmtFreq(st.rbw_hz)
      + (last.swept === false ? " · sweep timed out" : "");
  }

  /* ─────────────────────────── connection ─────────────────────────── */
  function showConnection(m) {
    connected = !!m.connected;
    $("cLink").className = "dot " + (connected ? (live ? "busy" : "on") : "err");
    $("cInstr").textContent = connected ? (m.host + ":" + m.port)
                                        : "not connected";
    $("btnConnect").disabled = connected;
    $("btnDisconnect").disabled = !connected;
    $("btnRestore").disabled = !connected;
    $("nConn").textContent = connected ? "✓" : "";
    $("iIdn").textContent = m.idn || "—";
    if (!connected) {
      $("specSub").textContent = "not connected";
      return;
    }
    if (m.state) applyState(m.state);
  }

  function setLive(on) {
    live = on;
    $("btnLive").textContent = on ? "Stop" : "Go live";
    $("btnLive").className = "btn" + (on ? " danger" : "");
    $("cLink").className = "dot " + (connected ? (on ? "busy" : "on") : "err");
  }

  /* ───────────────────────────── boot ─────────────────────────────── */
  document.addEventListener("DOMContentLoaded", async () => {
    await Theme.init();

    document.querySelectorAll(".rail-item").forEach(b => {
      b.onclick = () => showView(b.dataset.view);
    });

    // link
    $("btnConnect").onclick = () =>
      Api.connect($("host").value.trim(), parseInt($("port").value, 10) || 5555);
    $("btnDisconnect").onclick = () => Api.disconnect();
    $("btnRestore").onclick = () => Api.restore();

    // sweep
    $("btnLive").onclick = () => Api.live(!live);
    $("btnSweep").onclick = () => Api.sweep();
    $("btnSweep2").onclick = () => Api.sweep();
    seg("contSeg", "cont", c => Api.continuous(c === "1"));

    // frequency
    field("f_center", v => Api.frequency({ center_mhz: v }));
    field("f_span", v => Api.frequency({ span_mhz: v }));
    field("f_start", v => Api.frequency({ start_mhz: v }));
    field("f_stop", v => Api.frequency({ stop_mhz: v }));
    $("btnFull").onclick = () => Api.fullSpan();
    $("btnZero").onclick = () => Api.frequency({ span_mhz: 0 });
    $("btnHalf").onclick = () => {
      if (isNum(st.span_hz) && st.span_hz > 0)
        Api.frequency({ span_mhz: st.span_hz / 2e6 });
    };
    $("btnDouble").onclick = () => {
      if (isNum(st.span_hz) && st.span_hz > 0)
        Api.frequency({ span_mhz: st.span_hz * 2 / 1e6 });
    };

    // amplitude
    field("a_ref", v => Api.refLevel(v));
    field("a_offset", v => Api.refOffset(v));
    field("a_att", v => Api.attenuation(v, false));
    $("btnAttAuto").onclick = () => Api.attenuation(null, true);
    $("btnRefUp").onclick = () => {
      if (isNum(st.ref_level_dbm)) Api.refLevel(st.ref_level_dbm + 10);
    };
    $("btnRefDown").onclick = () => {
      if (isNum(st.ref_level_dbm)) Api.refLevel(st.ref_level_dbm - 10);
    };

    // trace
    seg("modeSeg", "mode", m => Api.traceMode(m));
    $("btnRestart").onclick = () => Api.traceRestart();
    seg("detSeg", "det", d => Api.detector(d));

    // bandwidths
    field("s_rbw", v => Api.rbw(v * 1e3, false));
    field("s_vbw", v => Api.vbw(v * 1e3, false));
    field("s_time", v => Api.sweepTime(v / 1e3, false));
    $("btnRbwAuto").onclick = () => Api.rbw(null, true);
    $("btnVbwAuto").onclick = () => Api.vbw(null, true);
    $("btnSwtAuto").onclick = () => Api.sweepTime(null, true);

    // markers
    seg("mkSeg", "count", c => {
      const n = parseInt(c, 10);
      if (n > 0) sel = Math.min(sel, n) || 1;
      Api.markers(n);
    });
    document.querySelectorAll("[data-act]").forEach(b => {
      b.onclick = () => {
        if (!(st.markers > 0)) { Api.markers(1); sel = 1; }
        Api.markerAction(sel || 1, b.dataset.act);
      };
    });
    $("btnPeak").onclick = () => {
      if (!(st.markers > 0)) { Api.markers(1); sel = 1; }
      Api.markerAction(sel || 1, "peak");
    };
    $("btnMkSet").onclick = () => {
      const v = parseFloat($("mkX").value);
      if (isNaN(v)) { banner("Enter a number for the marker position.", "bad"); return; }
      banner(null);
      if (!(st.markers > 0)) { Api.markers(1); sel = 1; }
      // zero span puts the marker on the time axis, so send seconds there
      Api.markerX(sel || 1, st.zero_span ? v / 1000 : v * 1e6, !!st.zero_span);
    };
    $("btnMkOff").onclick = () => Api.markersOff();
    $("btnTable").onclick = () => showTable(!tableShown());
    $("rateSel").onchange = e => Api.rate(parseFloat(e.target.value));

    initSplitters();
    document.addEventListener("theme:changed", drawTrace);
    window.addEventListener("resize", requestRedraw);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) requestRedraw();
    });

    /* ---- socket ---- */
    Api.on("close", () => {
      $("cLink").className = "dot err";
      $("cInstr").textContent = "panel server lost — retrying";
    });
    Api.on("hello", m => {
      $("footLeft").textContent = "FSC3 at " + m.host + ":" + m.port;
    });
    Api.on("connection", m => { showConnection(m); if (m.state) applyState(m.state); });
    Api.on("state", m => {
      if (m.connection) {
        connected = !!m.connection.connected;
        $("iIdn").textContent = m.connection.idn || "—";
      }
      applyState(m.state);
      drawTrace();
    });
    Api.on("trace", m => {
      last = m;
      if (m.state) applyState(m.state);
      else markerTable();
      drawTrace();
    });
    Api.on("live", m => {
      setLive(!!m.live);
      if (m.period) $("rateSel").value = String(m.period);
    });
    Api.on("log", m => log(m.msg));
    Api.on("error", m => banner(m.msg, "bad"));
    Api.on("rejected", m => banner("The analyzer refused: <b>"
      + (m.problems || []).join("</b>, <b>") + "</b>", "bad"));

    let tableOn = true;
    try { tableOn = localStorage.getItem(TABLE_KEY) !== "0"; } catch (e) {}
    showTable(tableOn);

    Api.openSocket();
    drawTrace();
    showView(savedView());
    // the canvas can only be measured once the restored page has been laid out
    requestAnimationFrame(() => { sizeScreen(); drawTrace(); });
  });
})();
