/* Saved-run browser. Waveforms are fetched one at a time and cached, so a
   2,600-transmission run costs no more to open than a 10-transmission one. */
const Review = (() => {
  const $ = id => document.getElementById(id);
  const fmt = (v, d = 1) =>
    (v === null || v === undefined || v === "" || isNaN(v)) ? "—" : Number(v).toFixed(d);
  const num = v => (v === "" || v === undefined || v === null) ? null : parseFloat(v);
  const clock = ts => (ts || "").replace("T", " ").slice(11);
  const hhmm = s => s >= 3600 ? (s / 3600).toFixed(2) + " h"
    : s >= 60 ? (s / 60).toFixed(1) + " m" : s.toFixed(0) + " s";

  let rows = [], name = null, idx = 0, timer = null, threshold = 50;
  const cache = new Map();

  async function loadList(keep) {
    let r;
    try { r = await Api.runs(); }
    catch (e) { $("vMeta").textContent = "could not list runs: " + e.message; return; }
    const sel = $("vRun");
    const prev = keep ? sel.value : null;
    sel.innerHTML = "";
    const runs = (r.runs || []).slice().reverse();
    $("nSaved").textContent = runs.length ? String(runs.length) : "";
    if (!runs.length) {
      $("vMeta").textContent = "no saved runs yet";
      const o = document.createElement("option");
      o.value = ""; o.textContent = "— no runs —";
      sel.appendChild(o);
      rows = []; blank();
      return;
    }
    runs.forEach(t => {
      const o = document.createElement("option");
      o.value = t.name;
      o.textContent = t.name + " (" + t.captures + ")";
      sel.appendChild(o);
    });
    const target = (prev && runs.some(t => t.name === prev)) ? prev : runs[0].name;
    sel.value = target;
    await open(target);
  }

  async function open(n) {
    name = n; cache.clear();
    let r;
    try { r = await Api.run(n); }
    catch (e) { $("vMeta").textContent = "could not open " + n; return; }

    rows = (r.rows || []).map(x => ({
      index: +x.index, timestamp: x.timestamp,
      elapsed_s: parseFloat(x.elapsed_s),
      peak_ma: num(x.peak_ma), delta_ma: num(x.delta_ma),
      burst_ms: num(x.burst_ms), charge_mc: num(x.charge_mc),
      idle_ua: num(x.idle_ua),
      triggered: x.triggered === "yes", file: x.file,
    }));

    const cfg = (r.config || {}).config || {};
    threshold = Math.abs(cfg.drop_delta_ma || cfg.threshold_ma || 200);
    const missed = rows.filter(x => !x.triggered).length;
    const span = rows.length ? rows[rows.length - 1].elapsed_s : 0;
    $("vMeta").textContent =
      (r.total_rows || rows.length) + " transmissions · " + missed + " missed · "
      + hhmm(span) + (cfg.trig_level_ma ? " · trig " + cfg.trig_level_ma + " mA" : "")
      + (r.decimated ? "  (table downsampled for display)" : "");

    const res = r.result;
    if (res && res.reached) {
      $("vResult").hidden = false;
      $("vResultText").innerHTML =
        "Peak fell to " + fmt(res.final_peak_ma) + " mA (" + fmt(res.ref_peak_ma)
        + " → " + fmt(res.final_peak_ma) + " mA) on " + res.consecutive
        + " consecutive TX after <b>" + (res.elapsed_s / 3600).toFixed(2)
        + " h</b> over " + res.measurements + " transmissions.";
    } else {
      $("vResult").hidden = true;
    }

    const tb = $("vtbl").tBodies[0];
    tb.innerHTML = "";
    rows.forEach((m, i) => {
      const tr = buildRow(m);
      tr.onclick = () => show(i);
      tb.appendChild(tr);
    });

    $("vSlider").max = Math.max(0, rows.length - 1);
    $("vSlider").value = 0;

    const t = rows.filter(x => x.triggered);
    const ref = t.length ? t[0].peak_ma : null;
    const lines = ref === null ? [] : [
      { v: ref, color: "--accent", label: "reference" },
      { v: ref - threshold, color: "--bad", label: "−" + threshold + " mA target" },
    ];
    Plot.line($("vdecay"), t.map(x => x.elapsed_s), t.map(x => x.peak_ma), {
      dots: t.length <= 200, color: "--ok", hlines: lines,
      xfmt: v => v >= 3600 ? (v / 3600).toFixed(1) + "h"
        : v >= 60 ? (v / 60).toFixed(0) + "m" : v.toFixed(0) + "s",
      xlabel: "elapsed", empty: "no transmissions captured",
    });
    idx = 0;
    show(0);
  }

  function buildRow(m) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const cells = [
      m.index, clock(m.timestamp), hhmm(m.elapsed_s),
      m.triggered ? fmt(m.peak_ma) : "—",
      m.delta_ma === null ? "—" : (m.delta_ma >= 0 ? "+" : "") + fmt(m.delta_ma),
      m.triggered ? fmt(m.burst_ms, 0) : "—",
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i === 4 && m.triggered && m.delta_ma <= -threshold) td.className = "bad";
      if (i === 0 && m.index === 0) td.className = "accent";
      tr.appendChild(td);
    });
    const td = document.createElement("td");
    td.innerHTML = m.triggered ? '<span class="pill ok">OK</span>'
      : '<span class="pill bad">MISS</span>';
    tr.appendChild(td);
    return tr;
  }

  async function show(i) {
    if (!rows.length) { blank(); return; }
    idx = Math.max(0, Math.min(rows.length - 1, i));
    $("vSlider").value = idx;
    $("vPos").textContent = (idx + 1) + " / " + rows.length;
    const trs = $("vtbl").tBodies[0].rows;
    for (let k = 0; k < trs.length; k++) trs[k].classList.toggle("sel", k === idx);

    const row = rows[idx];
    if (!row.triggered || !row.file) {
      Plot.line($("vscreen"), [], [], { empty: "#" + row.index + " — TX missed" });
      $("vInfo").textContent = "#" + row.index + " · " + row.timestamp + " · no trigger";
      return;
    }
    let c = cache.get(row.file);
    if (!c) {
      try { c = await Api.capture(name, row.file); }
      catch (e) { $("vInfo").textContent = "could not read " + row.file; return; }
      cache.set(row.file, c);
      if (cache.size > 40) cache.delete(cache.keys().next().value);
    }
    const refP = parseFloat(c.meta.ref_peak_ma);
    const lines = isNaN(refP) ? [] : [
      { v: refP, color: "--accent", label: "ref " + refP.toFixed(1) + " mA" },
      { v: refP - threshold, color: "--bad", label: "−" + threshold + " mA" },
    ];
    Plot.line($("vscreen"), c.time_s.map(v => v * 1000), c.current_ma, {
      xfmt: v => v.toFixed(0), xlabel: "ms", markPeak: true, unit: "mA", hlines: lines,
      color: "--accent",
    });
    $("vInfo").textContent =
      "#" + c.meta.index + " · " + c.meta.timestamp + " · peak " + c.meta.peak_ma
      + " mA · burst " + c.meta.burst_ms + " ms · "
      + c.meta.stored_points + "/" + c.meta.raw_points + " pts (" + c.meta.decimation
      + ") · " + row.file;
  }

  function blank() {
    Plot.line($("vscreen"), [], [], { empty: "pick a run" });
    Plot.line($("vdecay"), [], [], { empty: "no data" });
    $("vPos").textContent = "—";
    $("vInfo").textContent = "pick a run";
    $("vResult").hidden = true;
  }

  function play() {
    const b = $("vPlay");
    if (timer) { clearInterval(timer); timer = null; b.textContent = "Play"; return; }
    b.textContent = "Pause";
    timer = setInterval(() => {
      if (idx >= rows.length - 1) {
        clearInterval(timer); timer = null; b.textContent = "Play"; return;
      }
      show(idx + 1);
    }, 600);
  }

  function init() {
    $("vRefresh").onclick = () => loadList(true);
    $("vRun").onchange = e => { if (e.target.value) open(e.target.value); };
    $("vPrev").onclick = () => show(idx - 1);
    $("vNext").onclick = () => show(idx + 1);
    $("vPlay").onclick = play;
    $("vSlider").oninput = e => show(+e.target.value);
    document.addEventListener("keydown", e => {
      if ($("viewReview").hidden) return;
      if (e.key === "ArrowLeft") show(idx - 1);
      if (e.key === "ArrowRight") show(idx + 1);
    });
    blank();
  }

  return { init, loadList, redraw: () => show(idx) };
})();
