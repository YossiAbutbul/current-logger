/* Bootstrap: theme, socket, view switching, start/stop, log strip. */
(() => {
  const $ = id => document.getElementById(id);

  function log(msg, cls) {
    const el = $("log"), d = document.createElement("div");
    if (cls) d.className = cls;
    d.textContent = msg;
    el.appendChild(d);
    while (el.childElementCount > 400) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }

  const VIEWS = { setup: "viewSetup", live: "viewLive", review: "viewReview" };

  function showView(v) {
    Object.entries(VIEWS).forEach(([k, id]) => { $(id).hidden = (k !== v); });
    document.querySelectorAll(".rail-item").forEach(b =>
      b.setAttribute("aria-current", String(b.dataset.view === v)));
    if (v === "review") Review.loadList(true);
    if (v === "live") Live.draw();
  }

  function start() {
    let cfg;
    try {
      cfg = Live.readConfig();
    } catch (e) {
      log("! " + e.message, "e");
      Live.banner(e.message, "bad");
      return;
    }
    Live.setThreshold(cfg.threshold_db);
    Api.start(cfg);
    showView("live");
  }

  document.addEventListener("DOMContentLoaded", async () => {
    await Theme.init();
    Review.init();

    document.querySelectorAll(".rail-item").forEach(b => {
      b.onclick = () => showView(b.dataset.view);
    });
    $("btnStart").onclick = start;
    $("btnStart2").onclick = start;
    $("btnStop").onclick = () => Api.stop();
    $("btnStop2").onclick = () => Api.stop();

    document.addEventListener("theme:changed", () => {
      Live.draw();
      if (!$("viewReview").hidden) Review.redraw();
    });

    Api.on("open", () => { $("cLink").className = "dot on"; });
    Api.on("close", () => {
      $("cLink").className = "dot err";
      $("cInstr").textContent = "disconnected — retrying";
    });
    Api.on("hello", m => {
      $("cInstr").textContent = m.instrument;
      $("footLeft").textContent = "instrument " + m.instrument;
    });
    Api.on("log", m => log("[" + (m.t || "").slice(11) + "] " + m.msg));
    Api.on("error", m => log("! " + m.msg, "e"));
    Api.on("setup_warning", m =>
      (m.problems || []).forEach(p => log("setup: " + p, "w")));

    Api.on("state", m => {
      $("cState").textContent = m.state;
      $("sState").textContent = m.state;
      Live.setRunning(m.state === "running" || m.state === "connecting");
      if (m.state === "running") $("liveSub").textContent = "test running";
    });
    Api.on("started", m => {
      Live.reset(m.config);
      Live.setRunning(true);
      $("liveSub").textContent = "starting…";
      $("footRight").textContent =
        "results/" + ((m.config && m.config.test_name) || "");
    });
    Api.on("reference", m => Live.setRef(m.peak_dbm));
    Api.on("waiting", m => {
      $("sNext").textContent =
        m.remaining_s > 0 ? m.remaining_s.toFixed(0) + " s" : "now";
    });
    Api.on("arming", () => {
      $("sNext").textContent = "arming…";
      $("cLink").className = "dot busy";
    });
    Api.on("capture", m => {
      Live.onCapture(m);
      $("cLink").className = "dot on";
    });
    Api.on("finished", m => {
      Live.onFinished(m);
      Review.loadList(true);
    });

    Api.connect();
    Live.draw();
    showView("setup");
  });
})();
