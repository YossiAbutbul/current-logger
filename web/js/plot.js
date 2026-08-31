/* Canvas plots. Colours come from the active palette tokens, so charts change
   with the theme instead of hard-coding a blue. */
const Plot = (() => {

  function line(cv, xs, ys, opts = {}) {
    const g = cv.getContext("2d");
    const W = cv.width, H = cv.height;
    const L = 74, R = 20, T = 18, B = 46;
    const ink = Theme.css("--text", "#E4E7EA");
    const dim = Theme.css("--text-dim", "#949AA0");
    const grid = Theme.css("--border", "#41464B");
    const bg = Theme.css("--content-bg", "#2E3236");

    g.fillStyle = bg; g.fillRect(0, 0, W, H);

    if (!xs || !xs.length) {
      g.fillStyle = dim;
      g.font = "22px " + mono();
      g.textAlign = "center";
      g.fillText(opts.empty || "no data", W / 2, H / 2);
      g.textAlign = "start";
      return;
    }

    let x0 = Math.min(...xs), x1 = Math.max(...xs);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    (opts.hlines || []).forEach(h => {
      if (isFinite(h.v)) { y0 = Math.min(y0, h.v); y1 = Math.max(y1, h.v); }
    });
    const pad = Math.max(2, (y1 - y0) * 0.12);
    y0 -= pad; y1 += pad;
    if (x1 === x0) x1 = x0 + 1;
    if (y1 === y0) y1 = y0 + 1;

    const px = v => L + (W - L - R) * (v - x0) / (x1 - x0);
    const py = v => H - B - (H - B - T) * (v - y0) / (y1 - y0);

    g.strokeStyle = grid; g.lineWidth = 1;
    g.fillStyle = dim; g.font = "17px " + mono();
    for (let k = 0; k <= 8; k++) {
      const v = y0 + (y1 - y0) * k / 8, yy = Math.round(py(v)) + .5;
      g.beginPath(); g.moveTo(L, yy); g.lineTo(W - R, yy); g.stroke();
      g.fillText(v.toFixed(0), 8, yy + 6);
    }
    g.textAlign = "center";
    for (let k = 0; k <= 8; k++) {
      const v = x0 + (x1 - x0) * k / 8, xx = Math.round(px(v)) + .5;
      g.beginPath(); g.moveTo(xx, T); g.lineTo(xx, H - B); g.stroke();
      g.fillText(opts.xfmt ? opts.xfmt(v) : v.toFixed(2), xx, H - 18);
    }
    g.textAlign = "start";
    if (opts.xlabel) {
      g.fillStyle = dim; g.font = "15px " + mono();
      g.textAlign = "end"; g.fillText(opts.xlabel, W - R, H - 4); g.textAlign = "start";
    }

    (opts.hlines || []).forEach(h => {
      if (!isFinite(h.v) || h.v < y0 || h.v > y1) return;
      g.save();
      g.setLineDash([7, 5]);
      g.strokeStyle = h.color; g.lineWidth = 1.5;
      const yy = Math.round(py(h.v)) + .5;
      g.beginPath(); g.moveTo(L, yy); g.lineTo(W - R, yy); g.stroke();
      g.restore();
      g.fillStyle = h.color; g.font = "16px " + mono();
      g.fillText(h.label, L + 10, py(h.v) - 7);
    });

    g.strokeStyle = opts.color || Theme.css("--accent", "#C6CED5");
    g.lineWidth = opts.lw || 2;
    g.beginPath();
    xs.forEach((x, i) => i ? g.lineTo(px(x), py(ys[i])) : g.moveTo(px(x), py(ys[i])));
    g.stroke();

    if (opts.dots) {
      g.fillStyle = opts.color || Theme.css("--accent", "#C6CED5");
      xs.forEach((x, i) => { g.beginPath(); g.arc(px(x), py(ys[i]), 4.5, 0, 7); g.fill(); });
    }
    if (opts.markPeak) {
      let bi = 0;
      ys.forEach((v, i) => { if (v > ys[bi]) bi = i; });
      g.strokeStyle = Theme.css("--warn", "#E2A63F");
      g.lineWidth = 2;
      g.beginPath(); g.arc(px(xs[bi]), py(ys[bi]), 8, 0, 7); g.stroke();
    }
    if (opts.highlightX !== undefined && isFinite(opts.highlightX)) {
      g.save();
      g.strokeStyle = Theme.css("--accent", "#C6CED5");
      g.lineWidth = 1.5; g.setLineDash([3, 4]);
      const xx = Math.round(px(opts.highlightX)) + .5;
      g.beginPath(); g.moveTo(xx, T); g.lineTo(xx, H - B); g.stroke();
      g.restore();
    }
    g.strokeStyle = grid; g.lineWidth = 1;
    g.strokeRect(L + .5, T + .5, W - L - R, H - B - T);
  }

  function mono() {
    return getComputedStyle(document.documentElement)
      .getPropertyValue("--font-mono").trim() || "monospace";
  }

  return { line };
})();
