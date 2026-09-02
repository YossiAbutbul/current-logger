/* Palette plumbing.
   palettes.json holds the ten directions x three modes from the design study,
   extracted verbatim. Here we flatten one of them onto CSS custom properties. */
const Theme = (() => {
  const KEY = "fsc3.palette";
  let dirs = [], dir = "anodized", mode = "mid";

  function setVars(p) {
    const r = document.documentElement.style;
    r.setProperty("--app-bar", p.appBar);
    r.setProperty("--app-bar-border", p.appBarBorder);
    r.setProperty("--content-bg", p.contentBg);
    r.setProperty("--paper", p.paper);
    r.setProperty("--log-bg", p.logBg);

    r.setProperty("--ok", p.data.ok);
    r.setProperty("--warn", p.data.warn);
    r.setProperty("--bad", p.data.bad);
    r.setProperty("--highlight", p.data.highlight);

    const s = p.sidebar;
    r.setProperty("--sidebar-bg", s.bg);
    r.setProperty("--border", s.border);
    r.setProperty("--text", s.text);
    r.setProperty("--text-dim", s.textDim);
    r.setProperty("--accent", s.accent);
    r.setProperty("--accent-soft", s.accentSoft);
    r.setProperty("--accent-soft-hover", s.accentSoftHover);
    r.setProperty("--hover", s.hover);
    r.setProperty("--success", s.success);
    r.setProperty("--success-off", s.successOff);

    for (const [name, a] of Object.entries(p.actions)) {
      r.setProperty(`--${name}-bg`, a.bg);
      r.setProperty(`--${name}-bg-hover`, a.bgHover);
      r.setProperty(`--${name}-fg`, a.fg);
      r.setProperty(`--${name}-border`, a.border);
    }
    document.documentElement.style.colorScheme = mode === "light" ? "light" : "dark";
  }

  function apply() {
    const d = dirs.find(x => x.key === dir) || dirs[0];
    if (!d) return;
    setVars(d.modes[mode] || d.modes.mid);
    try { localStorage.setItem(KEY, JSON.stringify({ dir, mode })); } catch (e) {}
    document.querySelectorAll("#palModes button").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    const sel = document.getElementById("palDir");
    if (sel) sel.value = dir;
    document.dispatchEvent(new CustomEvent("theme:changed"));
  }

  async function init() {
    try {
      dirs = await (await fetch("js/palettes.json")).json();
    } catch (e) { return; }
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "null");
      if (saved && dirs.some(d => d.key === saved.dir)) {
        dir = saved.dir;
        if (["light", "mid", "dark"].includes(saved.mode)) mode = saved.mode;
      }
    } catch (e) {}

    const sel = document.getElementById("palDir");
    if (sel) {
      let group = null;
      dirs.forEach(d => {
        const label = d.family === "hue" ? "With an accent hue" : "Achromatic";
        if (label !== group) {
          group = label;
          sel.appendChild(Object.assign(document.createElement("optgroup"),
            { label: group }));
        }
        const o = document.createElement("option");
        o.value = d.key;
        o.textContent = `${d.name} — ${d.blurb}`;
        sel.lastChild.appendChild(o);
      });
      sel.onchange = () => { dir = sel.value; apply(); };
    }
    document.querySelectorAll("#palModes button").forEach(b => {
      b.onclick = () => { mode = b.dataset.mode; apply(); };
    });
    apply();
  }

  /* Chart colours read from the live tokens so plots follow the palette. */
  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return v || fallback;
  }

  return { init, apply, css, get mode() { return mode; }, get dir() { return dir; } };
})();
