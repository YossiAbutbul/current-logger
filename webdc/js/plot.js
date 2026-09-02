/* Canvas charts, interactive.

   Drawn at devicePixelRatio and sized from the element's real box, so text is
   rendered at its true size instead of being authored at 1600px and scaled
   down by CSS. Colours are read from the live palette tokens at draw time.

   Interaction, on every chart:
     hover         crosshair on the nearest sample, with its x/y in a tooltip
     drag          select an x range; live min/max/delta while dragging
     release       zoom to that range; stats then track the visible window
     double-click  reset the zoom
*/
const Plot = (() => {
  const drawn = new WeakMap();      // canvas -> [xs, ys, opts]
  const state = new WeakMap();      // canvas -> {hover, drag, zoom, geom}
  const live = new Set();           // every canvas drawn, for redraws

  const PAD = { l: 58, r: 18, t: 18, b: 34 };

  function mono() {
    return getComputedStyle(document.documentElement)
      .getPropertyValue("--font-mono").trim() || "monospace";
  }

  /* A colour may be given as a token name ("--accent"); resolve it now rather
     than when the caller built its options, or a redraw after a palette change
     would replay the previous palette's colours. */
  function col(c, fallback) {
    if (typeof c === "string" && c.startsWith("--")) return Theme.css(c, fallback);
    return c || fallback;
  }

  function st(cv) {
    let s = state.get(cv);
    if (!s) { s = { hover: null, drag: null, zoom: null, geom: null }; state.set(cv, s); }
    return s;
  }

  /* "nice" tick steps so labels land on round numbers rather than 1/8ths */
  function ticks(lo, hi, want) {
    const span = hi - lo;
    if (!(span > 0)) return [lo];
    const raw = span / want;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step)
      out.push(+v.toFixed(10));
    return out.length >= 2 ? out : [lo, hi];
  }

  function fmtTick(v, all) {
    const span = all && all.length > 1
      ? Math.abs(all[all.length - 1] - all[0]) : Math.abs(v);
    const d = span >= 100 ? 0 : span >= 10 ? 1 : span >= 1 ? 2 : 3;
    return v.toFixed(d);
  }

  function fmtVal(v) {
    const a = Math.abs(v);
    return v.toFixed(a >= 100 ? 2 : a >= 1 ? 3 : 4);
  }

  function hexA(c, a) {
    c = (c || "").trim();
    if (c.startsWith("#")) {
      let h = c.slice(1);
      if (h.length === 3) h = h.split("").map(x => x + x).join("");
      const n = parseInt(h, 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(",").map(s => parseFloat(s));
      return `rgba(${p[0]},${p[1]},${p[2]},${a})`;
    }
    return c;
  }

  /* ── stats over an x range ────────────────────────────────────────── */
  function rangeStats(xs, ys, xa, xb) {
    const lo = Math.min(xa, xb), hi = Math.max(xa, xb);
    let min = Infinity, max = -Infinity, sum = 0, n = 0;
    for (let i = 0; i < xs.length; i++) {
      if (xs[i] < lo || xs[i] > hi) continue;
      const v = ys[i];
      if (v < min) min = v;
      if (v > max) max = v;
      sum += v; n++;
    }
    if (!n) return null;
    return { lo, hi, min, max, mean: sum / n, delta: max - min, n };
  }

  /* ── draw ─────────────────────────────────────────────────────────── */
  function line(cv, xs, ys, opts = {}) {
    drawn.set(cv, [xs, ys, opts]);
    live.add(cv);
    attach(cv);
    render(cv);
  }

  function render(cv) {
    const rec = drawn.get(cv);
    if (!rec) return;
    const [xs, ys, opts] = rec;
    const s = st(cv);

    const dpr = window.devicePixelRatio || 1;
    const rect = cv.getBoundingClientRect();
    const W = Math.max(240, Math.round(rect.width));
    const H = Math.max(120, Math.round(rect.height));
    if (cv.width !== W * dpr || cv.height !== H * dpr) {
      cv.width = W * dpr; cv.height = H * dpr;
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);

    const dim = Theme.css("--text-dim", "#949AA0");
    const ink = Theme.css("--text", "#E4E7EA");
    const grid = Theme.css("--border", "#41464B");
    const bg = Theme.css("--content-bg", "#2E3236");
    const M = mono();

    g.clearRect(0, 0, W, H);
    g.fillStyle = bg;
    g.fillRect(0, 0, W, H);

    if (!xs || !xs.length) {
      g.fillStyle = dim; g.font = "12px " + M;
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(opts.empty || "no data", W / 2, H / 2);
      g.textAlign = "left"; g.textBaseline = "alphabetic";
      s.geom = null;
      return;
    }

    /* x domain honours the zoom; y is scaled to what is actually visible */
    let x0 = Math.min(...xs), x1 = Math.max(...xs);
    if (s.zoom) { x0 = s.zoom[0]; x1 = s.zoom[1]; }
    const vis = [];
    for (let i = 0; i < xs.length; i++)
      if (xs[i] >= x0 && xs[i] <= x1) vis.push(i);
    const src = vis.length ? vis : xs.map((_, i) => i);
    let y0 = Infinity, y1 = -Infinity;
    src.forEach(i => { if (ys[i] < y0) y0 = ys[i]; if (ys[i] > y1) y1 = ys[i]; });
    (opts.hlines || []).forEach(h => {
      if (isFinite(h.v)) { y0 = Math.min(y0, h.v); y1 = Math.max(y1, h.v); }
    });
    const pad = Math.max((y1 - y0) * 0.14, Math.abs(y1) * 1e-6, 1e-9);
    y0 -= pad; y1 += pad;
    if (x1 === x0) x1 = x0 + 1;
    if (y1 === y0) { y0 -= 0.5; y1 += 0.5; }

    const px = v => PAD.l + (W - PAD.l - PAD.r) * (v - x0) / (x1 - x0);
    const py = v => H - PAD.b - (H - PAD.b - PAD.t) * (v - y0) / (y1 - y0);
    const ix = p => x0 + (p - PAD.l) * (x1 - x0) / (W - PAD.l - PAD.r);
    const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    s.geom = { W, H, x0, x1, y0, y1, px, py, ix, plotW, plotH };

    /* ---- grid ---- */
    g.font = "11px " + M; g.lineWidth = 1;
    const yt = ticks(y0, y1, Math.max(3, Math.min(6, Math.round(plotH / 44))));
    g.strokeStyle = grid; g.globalAlpha = 0.5;
    yt.forEach(v => {
      const y = Math.round(py(v)) + 0.5;
      if (y < PAD.t || y > H - PAD.b) return;
      g.beginPath(); g.moveTo(PAD.l, y); g.lineTo(W - PAD.r, y); g.stroke();
    });
    const xt = ticks(x0, x1, Math.max(3, Math.min(8, Math.round(plotW / 110))));
    xt.forEach(v => {
      const x = Math.round(px(v)) + 0.5;
      if (x < PAD.l || x > W - PAD.r) return;
      g.beginPath(); g.moveTo(x, PAD.t); g.lineTo(x, H - PAD.b); g.stroke();
    });
    g.globalAlpha = 1;

    /* ---- axis labels ---- */
    g.fillStyle = dim; g.textAlign = "right"; g.textBaseline = "middle";
    yt.forEach(v => {
      const y = py(v);
      if (y < PAD.t - 1 || y > H - PAD.b + 1) return;
      g.fillText(fmtTick(v, yt), PAD.l - 10, y);
    });
    g.textBaseline = "top";
    xt.forEach((v, i) => {
      const x = px(v);
      if (x < PAD.l - 1 || x > W - PAD.r + 1) return;
      g.textAlign = i === 0 ? "left" : i === xt.length - 1 ? "right" : "center";
      g.fillText(opts.xfmt ? opts.xfmt(v) : fmtTick(v, xt), x, H - PAD.b + 9);
    });
    if (opts.xlabel) {
      g.textAlign = "right";
      g.fillText(opts.xlabel, W - PAD.r, H - 12);
    }

    /* ---- reference lines ---- */
    (opts.hlines || []).forEach(h => {
      if (!isFinite(h.v) || h.v < y0 || h.v > y1) return;
      const y = Math.round(py(h.v)) + 0.5;
      const hc = col(h.color, dim);
      g.save();
      g.setLineDash([5, 4]); g.strokeStyle = hc; g.globalAlpha = 0.85; g.lineWidth = 1;
      g.beginPath(); g.moveTo(PAD.l, y); g.lineTo(W - PAD.r, y); g.stroke();
      g.restore();
      if (!h.label) return;
      g.font = "10px " + M;
      const tw = g.measureText(h.label).width;
      g.fillStyle = bg;
      g.fillRect(PAD.l + 6, y - 13, tw + 10, 14);
      g.fillStyle = hc; g.textAlign = "left"; g.textBaseline = "bottom";
      g.fillText(h.label, PAD.l + 10, y - 1);
      g.font = "11px " + M;
    });

    /* ---- selection band, under the trace ---- */
    const color = col(opts.color, Theme.css("--accent", "#C6CED5"));
    const acc = Theme.css("--accent", "#C6CED5");
    const sel = s.drag && Math.abs(s.drag.a - s.drag.b) > 2 ? s.drag : null;
    if (sel) {
      const xa = Math.min(sel.a, sel.b), xb = Math.max(sel.a, sel.b);
      g.fillStyle = hexA(acc, 0.14);
      g.fillRect(xa, PAD.t, xb - xa, plotH);
      g.strokeStyle = hexA(acc, 0.7); g.lineWidth = 1;
      g.beginPath();
      g.moveTo(Math.round(xa) + .5, PAD.t); g.lineTo(Math.round(xa) + .5, H - PAD.b);
      g.moveTo(Math.round(xb) + .5, PAD.t); g.lineTo(Math.round(xb) + .5, H - PAD.b);
      g.stroke();
    }

    /* ---- trace ---- */
    g.save();
    g.beginPath(); g.rect(PAD.l, PAD.t, plotW, plotH); g.clip();

    if (opts.fill !== false && xs.length > 8) {
      const grad = g.createLinearGradient(0, PAD.t, 0, H - PAD.b);
      grad.addColorStop(0, hexA(color, 0.20));
      grad.addColorStop(1, hexA(color, 0.02));
      g.beginPath();
      g.moveTo(px(xs[0]), H - PAD.b);
      xs.forEach((x, i) => g.lineTo(px(x), py(ys[i])));
      g.lineTo(px(xs[xs.length - 1]), H - PAD.b);
      g.closePath(); g.fillStyle = grad; g.fill();
    }

    g.strokeStyle = color; g.lineWidth = opts.lw || 1.6;
    g.lineJoin = "round"; g.lineCap = "round";
    g.beginPath();
    xs.forEach((x, i) => i ? g.lineTo(px(x), py(ys[i])) : g.moveTo(px(x), py(ys[i])));
    g.stroke();

    if (opts.dots && src.length <= 300) {
      g.fillStyle = color;
      src.forEach(i => { g.beginPath(); g.arc(px(xs[i]), py(ys[i]), 2.6, 0, 7); g.fill(); });
    }
    g.restore();

    /* ---- peak marker, of what is visible ---- */
    if (opts.markPeak) {
      let bi = src[0];
      src.forEach(i => { if (ys[i] > ys[bi]) bi = i; });
      const x = px(xs[bi]), y = py(ys[bi]);
      const warn = Theme.css("--warn", "#E2A63F");
      g.strokeStyle = warn; g.lineWidth = 1.4;
      g.beginPath(); g.arc(x, y, 4.5, 0, 7); g.stroke();
      g.beginPath(); g.moveTo(x, y - 8); g.lineTo(x, PAD.t + 2); g.stroke();
      g.font = "10px " + M; g.fillStyle = warn;
      g.textAlign = x > W - PAD.r - 70 ? "right" : "left";
      g.textBaseline = "top";
      g.fillText(fmtTick(ys[bi], ys) + (opts.unit ? " " + opts.unit : ""),
                 x + (g.textAlign === "right" ? -6 : 6), PAD.t + 1);
    }

    /* ---- axes ---- */
    g.strokeStyle = grid; g.globalAlpha = 0.9; g.lineWidth = 1;
    g.beginPath();
    g.moveTo(PAD.l + 0.5, PAD.t);
    g.lineTo(PAD.l + 0.5, H - PAD.b + 0.5);
    g.lineTo(W - PAD.r, H - PAD.b + 0.5);
    g.stroke();
    g.globalAlpha = 1;

    /* ---- stats: the selection while dragging, else the visible window ---- */
    let stats = null, title = "";
    if (sel) {
      stats = rangeStats(xs, ys, ix(Math.min(sel.a, sel.b)), ix(Math.max(sel.a, sel.b)));
      title = "selection";
    } else if (s.zoom) {
      stats = rangeStats(xs, ys, x0, x1);
      title = "visible";
    }
    if (stats) drawStats(g, W, H, M, stats, opts, title, bg, ink, dim, grid);

    /* ---- hover crosshair and tooltip ---- */
    if (s.hover !== null && !sel) {
      const i = s.hover;
      if (i >= 0 && i < xs.length && xs[i] >= x0 && xs[i] <= x1) {
        const x = px(xs[i]), y = py(ys[i]);
        g.save();
        g.strokeStyle = hexA(ink, 0.35); g.lineWidth = 1; g.setLineDash([3, 3]);
        g.beginPath();
        g.moveTo(Math.round(x) + .5, PAD.t);
        g.lineTo(Math.round(x) + .5, H - PAD.b);
        g.stroke();
        g.restore();
        g.fillStyle = color;
        g.beginPath(); g.arc(x, y, 3.5, 0, 7); g.fill();
        g.strokeStyle = bg; g.lineWidth = 1.5;
        g.beginPath(); g.arc(x, y, 3.5, 0, 7); g.stroke();
        drawTip(g, W, H, M, x, y, xs[i], ys[i], i, opts, bg, ink, dim, grid);
      }
    }

    /* ---- reset control, shown only while zoomed ---- */
    s.resetRect = null;
    if (s.zoom) {
      const label = "Reset view";
      g.font = "10px " + M;
      const tw = g.measureText(label).width;
      const bw = tw + 18, bh = 17;
      const bx = W - PAD.r - bw, by = 2;
      s.resetRect = { x: bx, y: by, w: bw, h: bh };
      const hot = s.overReset;
      g.fillStyle = hot ? hexA(acc, 0.18) : bg;
      g.fillRect(bx, by, bw, bh);
      g.strokeStyle = hot ? acc : grid; g.lineWidth = 1;
      g.strokeRect(bx + .5, by + .5, bw - 1, bh - 1);
      g.fillStyle = hot ? ink : dim;
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(label, bx + bw / 2, by + bh / 2 + .5);
    }

    g.textAlign = "left"; g.textBaseline = "alphabetic";
  }

  /* ── tooltip near the cursor ──────────────────────────────────────── */
  function drawTip(g, W, H, M, x, y, xv, yv, i, opts, bg, ink, dim, grid) {
    let rows;
    if (opts.tip) {
      rows = opts.tip(xv, yv, i);
    } else {
      const xTxt = opts.xfmt ? opts.xfmt(xv) : fmtTick(xv, [xv]);
      const hasUnit = /[a-z%]$/i.test(xTxt);
      rows = [
        xTxt + (hasUnit || !opts.xlabel ? "" : " " + opts.xlabel),
        fmtVal(yv) + (opts.unit ? " " + opts.unit : ""),
      ];
    }

    g.font = "11px " + M;
    const wid = Math.max(...rows.map(r => g.measureText(r).width)) + 16;
    const hgt = rows.length * 15 + 10;
    let bx = x + 12, by = y - hgt - 12;
    if (bx + wid > W - PAD.r) bx = x - wid - 12;
    if (bx < PAD.l) bx = PAD.l + 2;
    if (by < PAD.t) by = y + 14;
    if (by + hgt > H - PAD.b) by = H - PAD.b - hgt - 2;

    g.fillStyle = bg; g.globalAlpha = 0.96;
    g.fillRect(bx, by, wid, hgt);
    g.globalAlpha = 1;
    g.strokeStyle = grid; g.lineWidth = 1;
    g.strokeRect(bx + .5, by + .5, wid - 1, hgt - 1);
    g.textAlign = "left"; g.textBaseline = "top";
    rows.forEach((r, k) => {
      g.fillStyle = k === 0 ? dim : ink;
      g.fillText(r, bx + 8, by + 6 + k * 15);
    });
  }

  /* ── stats box, top-right inside the plot ─────────────────────────── */
  function drawStats(g, W, H, M, s, opts, title, bg, ink, dim, grid) {
    const u = opts.unit ? " " + opts.unit : "";
    const span = opts.xfmt
      ? `${opts.xfmt(s.lo)} → ${opts.xfmt(s.hi)}`
      : `${fmtTick(s.lo, [s.lo, s.hi])} → ${fmtTick(s.hi, [s.lo, s.hi])}`;
    const rows = [
      ["max", fmtVal(s.max) + u],
      ["min", fmtVal(s.min) + u],
      ["Δ", fmtVal(s.delta) + u],
      ["mean", fmtVal(s.mean) + u],
      ["span", span],
      ["n", String(s.n)],
    ];
    g.font = "10px " + M;
    const kw = Math.max(...rows.map(r => g.measureText(r[0]).width));
    const vw = Math.max(...rows.map(r => g.measureText(r[1]).width));
    const wid = kw + vw + 26, hgt = rows.length * 13 + 21;
    const bx = W - PAD.r - wid - 6, by = PAD.t + 6;

    g.fillStyle = bg; g.globalAlpha = 0.94;
    g.fillRect(bx, by, wid, hgt);
    g.globalAlpha = 1;
    g.strokeStyle = grid; g.lineWidth = 1;
    g.strokeRect(bx + .5, by + .5, wid - 1, hgt - 1);

    g.textBaseline = "top";
    g.textAlign = "left"; g.fillStyle = dim;
    g.fillText(title, bx + 9, by + 5);
    rows.forEach((r, k) => {
      const yy = by + 20 + k * 13;
      g.textAlign = "left"; g.fillStyle = dim;
      g.fillText(r[0], bx + 9, yy);
      g.textAlign = "right"; g.fillStyle = ink;
      g.fillText(r[1], bx + wid - 9, yy);
    });
  }

  /* ── interaction ──────────────────────────────────────────────────── */
  function nearest(cv, clientX) {
    const rec = drawn.get(cv), s = st(cv);
    if (!rec || !s.geom) return null;
    const [xs] = rec;
    const rect = cv.getBoundingClientRect();
    const xv = s.geom.ix(clientX - rect.left);
    let best = -1, bd = Infinity;
    for (let i = 0; i < xs.length; i++) {
      if (xs[i] < s.geom.x0 || xs[i] > s.geom.x1) continue;
      const d = Math.abs(xs[i] - xv);
      if (d < bd) { bd = d; best = i; }
    }
    return best < 0 ? null : best;
  }

  function localX(cv, clientX) {
    const rect = cv.getBoundingClientRect();
    return Math.max(PAD.l, Math.min(rect.width - PAD.r, clientX - rect.left));
  }

  function onReset(cv, e) {
    const r = st(cv).resetRect;
    if (!r) return false;
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    return x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
  }

  function inPlot(cv, e) {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    return x >= PAD.l && x <= rect.width - PAD.r
        && y >= PAD.t && y <= rect.height - PAD.b;
  }

  function attach(cv) {
    if (cv.__plotWired) return;
    cv.__plotWired = true;
    cv.style.cursor = "crosshair";
    cv.style.touchAction = "none";

    cv.addEventListener("pointermove", e => {
      const s = st(cv);
      if (s.drag) { s.drag.b = localX(cv, e.clientX); render(cv); return; }

      const over = onReset(cv, e);
      if (over !== !!s.overReset) {
        s.overReset = over;
        cv.style.cursor = over ? "pointer" : "crosshair";
        render(cv);
      }
      if (over) {
        if (s.hover !== null) { s.hover = null; render(cv); }
        return;
      }
      if (!inPlot(cv, e)) {
        if (s.hover !== null) { s.hover = null; render(cv); }
        return;
      }
      const i = nearest(cv, e.clientX);
      if (i !== s.hover) { s.hover = i; render(cv); }
    });

    cv.addEventListener("pointerleave", () => {
      const s = st(cv);
      if (s.hover !== null || s.drag) { s.hover = null; s.drag = null; render(cv); }
    });

    cv.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      const s = st(cv);
      if (onReset(cv, e)) {              // the Reset view control
        s.zoom = null; s.drag = null; s.hover = null; s.overReset = false;
        cv.style.cursor = "crosshair";
        render(cv);
        return;
      }
      if (!inPlot(cv, e)) return;
      const x = localX(cv, e.clientX);
      s.drag = { a: x, b: x };
      s.hover = null;
      try { cv.setPointerCapture(e.pointerId); } catch (err) {}
      render(cv);
    });

    cv.addEventListener("pointerup", e => {
      const s = st(cv);
      if (!s.drag) return;
      const { a, b } = s.drag;
      s.drag = null;
      try { cv.releasePointerCapture(e.pointerId); } catch (err) {}
      if (Math.abs(a - b) > 6 && s.geom) {
        const lo = s.geom.ix(Math.min(a, b)), hi = s.geom.ix(Math.max(a, b));
        if (hi > lo) s.zoom = [lo, hi];
      }
      render(cv);
    });

    cv.addEventListener("dblclick", () => {
      const s = st(cv);
      s.zoom = null; s.drag = null;
      render(cv);
    });
  }

  /* ── redraws: palette change, resize ──────────────────────────────── */
  function redrawAll() {
    live.forEach(cv => {
      if (!cv.isConnected) { live.delete(cv); return; }
      if (drawn.get(cv)) render(cv);
    });
  }
  document.addEventListener("theme:changed", redrawAll);

  if (window.ResizeObserver) {
    const ro = new ResizeObserver(entries => {
      entries.forEach(e => { if (drawn.get(e.target)) render(e.target); });
    });
    document.addEventListener("DOMContentLoaded", () => {
      document.querySelectorAll("canvas.screen").forEach(c => ro.observe(c));
    });
  }

  function resetZoom(cv) { const s = st(cv); s.zoom = null; s.drag = null; render(cv); }

  return { line, redrawAll, resetZoom };
})();
