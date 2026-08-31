/* Saved-run browser: pick a run, scroll its screens, see the decay curve. */
const Review = (() => {
  const $ = id => document.getElementById(id);
  const fmt = (v, d = 2) =>
    (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

  let rows = [], name = null, idx = 0, timer = null;
  const cache = new Map();

  async function loadList(keepSelection) {
    let r;
    try {
      r = await Api.tests();
    } catch (e) {
      $("vMeta").textContent = "could not list runs: " + e.message;
      return;
    }
    const sel = $("vTest");
    const prev = keepSelection ? sel.value : null;
    sel.innerHTML = "";
    const tests = (r.tests || []).slice().reverse();
    $("nSaved").textContent = tests.length ? String(tests.length) : "";

    if (!tests.length) {
      $("vMeta").textContent = "no saved runs yet";
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "— no runs —";
      sel.appendChild(o);
      rows = [];
      blank();
      return;
    }
    tests.forEach(t => {
      const o = document.createElement("option");
      o.value = t.name;
      o.textContent = t.name + " (" + t.captures + ")";
      sel.appendChild(o);
    });
    const target = (prev && tests.some(t => t.name === prev)) ? prev : tests[0].name;
    sel.value = target;
    await open(target);
  }

  async function open(n) {
    name = n;
    cache.clear();
    let r;
    try {
      r = await Api.test(n);
    } catch (e) {
      $("vMeta").textContent = "could not open " + n;
      return;
    }
    rows = (r.rows || []).map(x => ({
      index: +x.index,
      timestamp: x.timestamp,
      elapsed_s: parseFloat(x.elapsed_s),
      peak_dbm: x.peak_dbm === "" ? null : parseFloat(x.peak_dbm),
      delta_db: x.delta_db === "" ? null : parseFloat(x.delta_db),
      triggered: x.triggered === "yes",
      file: x.file,
    }));

    const cfg = (r.config || {}).config || {};
    const th = Math.abs(cfg.threshold_db || 3);
    const missed = rows.filter(x => !x.triggered).length;
    $("vMeta").textContent = rows.length + " captures · " + missed + " missed"
      + (cfg.center_hz ? " · " + (cfg.center_hz / 1e6).toFixed(4) + " MHz" : "")
      + (cfg.interval_s ? " · every " + (cfg.interval_s / 60).toFixed(2) + " min" : "");

    const res = r.result;
    if (res && res.reached) {
      $("vResult").hidden = false;
      $("vResultText").innerHTML =
        "Fell " + fmt(res.final_delta_db) + " dB (" + fmt(res.ref_peak_dbm)
        + " → " + fmt(res.final_peak_dbm) + " dBm) in <b>" + fmt(res.elapsed_s, 1)
        + " s = " + fmt(res.elapsed_s / 60, 2) + " min</b> over "
        + res.measurements + " measurements.";
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
    const ref = t.length ? t[0].peak_dbm : null;
    const lines = ref === null ? [] : [
      { v: ref, color: Theme.css("--accent"), label: "reference" },
      { v: ref - th, color: Theme.css("--bad"), label: "−" + th + " dB target" },
    ];
    Plot.line($("vdecay"), t.map(x => x.elapsed_s), t.map(x => x.peak_dbm), {
      dots: true, color: Theme.css("--ok"), hlines: lines,
      xfmt: v => v >= 60 ? (v / 60).toFixed(1) + "m" : v.toFixed(0) + "s",
      xlabel: "elapsed", empty: "no triggered captures",
    });
    idx = 0;
    show(0);
  }

  function buildRow(m) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const cells = [
      m.index,
      (m.timestamp || "").replace("T", " ").slice(11),
      fmt(m.elapsed_s, 1),
      m.triggered ? fmt(m.peak_dbm) : "—",
      m.triggered ? ((m.delta_db >= 0 ? "+" : "") + fmt(m.delta_db)) : "—",
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i === 0 && m.index === 0) td.className = "accent";
      tr.appendChild(td);
    });
    const td = document.createElement("td");
    td.innerHTML = m.triggered
      ? '<span class="pill ok">OK</span>'
      : '<span class="pill bad">MISS</span>';
    tr.appendChild(td);
    return tr;
  }

  async function show(i) {
    if (!rows.length) {
      blank();
      return;
    }
    idx = Math.max(0, Math.min(rows.length - 1, i));
    $("vSlider").value = idx;
    $("vPos").textContent = (idx + 1) + " / " + rows.length;
    const trs = $("vtbl").tBodies[0].rows;
    for (let k = 0; k < trs.length; k++) trs[k].classList.toggle("sel", k === idx);

    const row = rows[idx];
    if (!row.triggered) {
      Plot.line($("vscreen"), [], [], { empty: "#" + row.index + " — burst missed" });
      $("vInfo").textContent = "#" + row.index + " · " + row.timestamp + " · no trigger";
      return;
    }

    let c = cache.get(row.file);
    if (!c) {
      try {
        c = await Api.capture(name, row.file);
      } catch (e) {
        $("vInfo").textContent = "could not read " + row.file;
        return;
      }
      cache.set(row.file, c);
      if (cache.size > 40) cache.delete(cache.keys().next().value);
    }

    const refP = parseFloat(c.meta.ref_peak_dbm);
    const lines = isNaN(refP) ? [] : [{
      v: refP, color: Theme.css("--accent"),
      label: "ref " + refP.toFixed(2) + " dBm",
    }];
    Plot.line($("vscreen"), c.time_s.map(v => v * 1000), c.power_dbm, {
      xfmt: v => v.toFixed(1), xlabel: "ms", markPeak: true, hlines: lines,
      color: Theme.css("--accent"),
    });
    $("vInfo").textContent =
      "#" + c.meta.index + " · " + c.meta.timestamp + " · t+"
      + fmt(parseFloat(c.meta.elapsed_s), 1) + "s · peak " + c.meta.peak_dbm
      + " dBm · Δ " + c.meta.delta_db + " dB · " + row.file;
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
    if (timer) {
      clearInterval(timer);
      timer = null;
      b.textContent = "▶ Play";
      return;
    }
    b.textContent = "⏸ Pause";
    timer = setInterval(() => {
      if (idx >= rows.length - 1) {
        clearInterval(timer);
        timer = null;
        b.textContent = "▶ Play";
        return;
      }
      show(idx + 1);
    }, 700);
  }

  function init() {
    $("vRefresh").onclick = () => loadList(true);
    $("vTest").onchange = e => { if (e.target.value) open(e.target.value); };
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
