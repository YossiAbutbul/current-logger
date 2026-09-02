/* Bootstrap and the three steps: connect, reference, run. */
(() => {
  const $ = id => document.getElementById(id);
  const fmt = (v, d = 1) =>
    (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

  let connected = false;
  let refPeak = null;          // mA, from the reference session or typed in
  let refSamples = [];
  let total = 0;

  function log(msg, cls) {
    const el = $("log"), d = document.createElement("div");
    if (cls) d.className = cls;
    d.textContent = msg;
    el.appendChild(d);
    while (el.childElementCount > 400) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }

  const VIEWS = { connect: "viewConnect", setup: "viewSetup",
                  live: "viewLive", review: "viewReview" };

  function showView(v) {
    Object.entries(VIEWS).forEach(([k, id]) => { $(id).hidden = (k !== v); });
    document.querySelectorAll(".rail-item").forEach(b =>
      b.setAttribute("aria-current", String(b.dataset.view === v)));
    if (v === "review") Review.loadList(true);
    if (v === "live") Live.draw();
  }

  /* ─────────────── configuration ─────────────── */
  function readConfig() {
    const num = id => parseFloat($(id).value);
    const cfg = {
      test_name: $("test_name").value,
      interval_s: num("interval_s"),
      trig_level_ma: num("trig_level_ma"),
      ref_samples: parseInt($("ref_samples").value, 10),
      drop_delta_ma: num("drop_delta_ma"),
      consecutive: parseInt($("consecutive").value, 10),
      window_ms: num("window_ms"),
      tint_us: num("tint_us"),
      pretrigger_pct: num("pretrigger_pct"),
      trig_slope: $("trig_slope").value,
      trig_timeout_s: num("trig_timeout_s"),
      decimate_points: parseInt($("decimate_points").value, 10),
      max_duration_s: num("max_duration_min") * 60,
      max_measurements: parseInt($("max_measurements").value, 10),
      ref_retries: parseInt($("ref_retries").value, 10),
    };
    const bad = Object.entries(cfg).find(([, v]) => typeof v === "number" && isNaN(v));
    if (bad) throw new Error("check the value for " + bad[0]);
    if (!cfg.test_name.trim()) throw new Error("run name is empty");
    if (cfg.trig_timeout_s <= cfg.interval_s)
      throw new Error("wait timeout must be longer than the DUT message interval");
    return cfg;
  }

  function setupBanner(text, kind) {
    const b = $("banner");
    if (!text) { b.hidden = true; return; }
    b.hidden = false;
    b.className = "notice " + (kind || "");
    $("bannerText").innerHTML = text;
  }

  /* stop level = reference − drop delta, shown live so the numbers are concrete */
  function refreshStopLevel() {
    const drop = parseFloat($("drop_delta_ma").value);
    const n = parseInt($("consecutive").value, 10) || 1;
    if (refPeak === null || isNaN(drop)) {
      $("stopLevel").value = "—";
      $("stopExplain").textContent = refPeak === null
        ? "Record a reference (or type one in) to see the stop level."
        : "Enter a drop delta.";
      Live.setRef(refPeak, null);
      return null;
    }
    const stop = refPeak - Math.abs(drop);
    $("stopLevel").value = stop.toFixed(1) + " mA";
    $("stopExplain").innerHTML =
      "Reference is <b>" + fmt(refPeak) + " mA</b>. The run stops when the peak "
      + "current falls to <b>" + fmt(stop) + " mA</b> or below, on <b>" + n
      + "</b> transmissions in a row.";
    Live.setRef(refPeak, stop);
    return stop;
  }

  function setRef(v, note) {
    refPeak = (v === null || v === undefined || isNaN(v)) ? null : Number(v);
    $("rRef").textContent = refPeak === null ? "—" : fmt(refPeak) + " mA";
    $("nRef").textContent = refPeak === null ? "" : fmt(refPeak, 0);
    $("refStatus").textContent = note || (refPeak === null ? "not recorded yet" : "set");
    Api.setRef(refPeak);
    refreshStopLevel();
  }

  /* ─────────────── connection panel ─────────────── */
  async function loadResources() {
    const sel = $("resource");
    try {
      const r = await Api.resources();
      sel.innerHTML = "";
      (r.resources || []).forEach(x => {
        const o = document.createElement("option");
        o.value = x; o.textContent = x;
        sel.appendChild(o);
      });
      if (!sel.options.length) {
        const o = document.createElement("option");
        o.value = ""; o.textContent = "— no VISA resources found —";
        sel.appendChild(o);
      }
      // prefer the N6705B (Keysight vendor 0x0957, product 0x0F07)
      const n6705 = [...sel.options].find(o => o.value.includes("0x0F07"));
      if (n6705) sel.value = n6705.value;
      if (r.error) log("VISA: " + r.error, "w");
    } catch (e) {
      log("could not list VISA resources: " + e.message, "e");
    }
  }

  function showConnection(m) {
    connected = !!m.connected;
    $("cLink").className = "dot " + (connected ? "on" : "err");
    $("cInstr").textContent = connected
      ? (m.resource || "").replace(/^USB0::/, "") + " ch" + m.channel
      : "not connected";
    $("btnConnect").disabled = connected;
    $("btnDisconnect").disabled = !connected;
    $("nConn").textContent = connected ? "✓" : "";
    $("iIdn").textContent = m.idn || "—";
    $("iModule").textContent = m.module || "—";
    $("iEmul").textContent = m.emulation || "—";
    $("iRange").textContent = m.range_a ? parseFloat(m.range_a) + " A" : "—";
    $("iVolt").textContent = m.voltage_v ? parseFloat(m.voltage_v).toFixed(4) + " V" : "—";
    $("iOutput").textContent = m.output === "1" ? "ON" : m.output === "0" ? "OFF" : "—";
    const b = $("connBanner");
    if (!connected) { b.hidden = true; return; }
    b.hidden = false;
    b.className = "notice " + (m.output === "0" ? "" : "ok");
    $("connText").innerHTML = m.output === "0"
      ? "Connected, but the channel output relay is <b>OFF</b>. If the N6781A is in "
        + "series as an ammeter it must be ON to pass current."
      : "Connected and reading. Nothing you configured has been changed.";
  }

  /* ─────────────── start ─────────────── */
  function start() {
    if (!connected) {
      setupBanner("Connect to the analyzer first (step 1).", "bad");
      showView("connect");
      return;
    }
    let cfg;
    try { cfg = readConfig(); }
    catch (e) { log("! " + e.message, "e"); setupBanner(e.message, "bad"); return; }
    if (refPeak === null) {
      setupBanner("No reference current yet — record one, or type a value in step 2.",
                  "bad");
      showView("setup");
      return;
    }
    setupBanner(null);
    total = 0;
    Live.reset(cfg, refPeak, refPeak - Math.abs(cfg.drop_delta_ma));
    Live.setRunning(true);
    Api.start(cfg, refPeak);
    showView("live");
  }

  /* ─────────────── boot ─────────────── */
  document.addEventListener("DOMContentLoaded", async () => {
    await Theme.init();
    Review.init();
    await loadResources();

    document.querySelectorAll(".rail-item").forEach(b => {
      b.onclick = () => showView(b.dataset.view);
    });
    $("btnConnect").onclick = () =>
      Api.connect($("resource").value, parseInt($("channel").value, 10) || 3);
    $("btnDisconnect").onclick = () => Api.disconnect();
    $("btnStart").onclick = start;
    $("btnStop").onclick = () => Api.stop();

    $("btnRecordRef").onclick = () => {
      if (!connected) {
        setupBanner("Connect to the analyzer first (step 1).", "bad");
        return;
      }
      let cfg;
      try { cfg = readConfig(); }
      catch (e) { setupBanner(e.message, "bad"); return; }
      setupBanner(null);
      refSamples = [];
      $("rCount").textContent = "0";
      $("rSpread").textContent = "—";
      $("refStatus").textContent = "recording…";
      $("btnRecordRef").disabled = true;
      $("btnCancelRef").disabled = false;
      Api.recordRef(cfg);
    };
    $("btnCancelRef").onclick = () => Api.cancelRef();
    $("btnUseManual").onclick = () => {
      const v = parseFloat($("ref_manual").value);
      if (isNaN(v)) { setupBanner("Enter a number for the reference.", "bad"); return; }
      setupBanner(null);
      setRef(v, "entered by hand");
    };
    ["drop_delta_ma", "consecutive"].forEach(id =>
      $(id).addEventListener("input", refreshStopLevel));

    document.addEventListener("theme:changed", () => {
      Live.draw();
      if (!$("viewReview").hidden) Review.redraw();
    });

    /* ---- socket + events ---- */
    Api.on("close", () => {
      $("cLink").className = "dot err";
      $("cInstr").textContent = "server disconnected — retrying";
    });
    Api.on("hello", m => { $("footLeft").textContent = m.instrument || ""; });
    Api.on("connection", showConnection);
    Api.on("log", m => log("[" + (m.t || "").slice(11) + "] " + m.msg));
    Api.on("error", m => { log("! " + m.msg, "e"); setupBanner(m.msg, "bad"); });
    Api.on("setup_warning", m =>
      (m.problems || []).forEach(p => log("setup: " + p, "w")));

    /* ---- reference session ---- */
    Api.on("ref_state", m => {
      const rec = m.state === "recording";
      $("btnRecordRef").disabled = rec;
      $("btnCancelRef").disabled = !rec;
    });
    Api.on("ref_sample", m => {
      refSamples.push(m.peak_ma);
      $("rCount").textContent = m.n + " / " + m.want;
      $("refStatus").textContent = "recording… " + m.n + " of " + m.want;
      Plot.line($("refScreen"), m.trace_ms, m.trace_ma, {
        xfmt: v => v.toFixed(0), xlabel: "ms", markPeak: true,
        color: "--accent",
      });
      log("reference TX " + m.n + ": peak " + fmt(m.peak_ma) + " mA, burst "
        + fmt(m.burst_ms, 0) + " ms");
    });
    Api.on("ref_miss", m =>
      log("reference: no TX seen (" + m.misses + ")", "w"));
    Api.on("ref_done", m => {
      $("btnRecordRef").disabled = false;
      $("btnCancelRef").disabled = true;
      if (!m.ok) {
        $("refStatus").textContent = "failed";
        setupBanner(m.msg || "reference recording failed", "bad");
        return;
      }
      if (m.spread_ma !== undefined)
        $("rSpread").textContent = fmt(m.spread_ma) + " mA";
      setRef(m.ref_peak_ma, m.restored ? "from earlier this session"
        : "average of " + (m.peaks ? m.peaks.length : 1) + " transmissions");
      if (!m.restored)
        setupBanner("Reference set to <b>" + fmt(m.ref_peak_ma)
          + " mA</b>. Now set the drop delta below and start the run.", "ok");
    });
    Api.on("ref_set", m => { /* echo; the UI already knows */ });

    /* ---- run ---- */
    Api.on("state", m => {
      if (m.state === "running") {
        Live.setRunning(true);
        Live.startClock(m.t0);
        $("liveSub").textContent = "run in progress";
      }
    });
    Api.on("started", m => {
      total = 0;
      Live.reset(m.config, m.ref_peak_ma, m.stop_level_ma);
      Live.setRunning(true);
      $("liveSub").textContent = "starting…";
      $("footRight").textContent =
        "results-current/" + ((m.config && m.config.test_name) || "");
    });
    Api.on("folder", m => { $("footRight").textContent = "results-current/" + m.folder; });
    Api.on("reference", m => Live.setRef(m.peak_ma, m.stop_level_ma));
    Api.on("streak", m => Live.setStreak(m.streak, m.need));
    Api.on("capture", m => {
      total += 1;
      Live.setCounts(total);
      Live.onCapture(m);
    });
    Api.on("finished", m => { Live.onFinished(m); Review.loadList(true); });

    Api.openSocket();
    Live.draw();
    Plot.line($("refScreen"), [], [], { empty: "no reference recorded yet" });
    refreshStopLevel();
    showView("connect");
  });
})();
