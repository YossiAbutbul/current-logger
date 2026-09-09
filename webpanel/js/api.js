/* Backend client for the FSC3 panel.

   openSocket() = the browser's WebSocket to this app.
   connect()    = tell the backend to open the SCPI socket to the analyzer.
   Two different connections; keep the names distinct. */
const Api = (() => {
  let ws = null;
  const handlers = {};

  function on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); }
  function fire(ev, m) { (handlers[ev] || []).forEach(f => f(m)); }

  function openSocket() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/ws");
    ws.onopen = () => fire("open", {});
    ws.onclose = () => { fire("close", {}); setTimeout(openSocket, 2000); };
    ws.onmessage = e => {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      fire(m.ev, m);
      fire("*", m);
    };
  }

  function send(o) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(o));
    else fire("error", { msg: "not connected to the panel server" });
  }

  return {
    openSocket, on, send,
    // link to the analyzer
    connect: (host, port) => send({ cmd: "connect", host, port }),
    disconnect: () => send({ cmd: "disconnect" }),
    status: () => send({ cmd: "status" }),
    restore: () => send({ cmd: "restore" }),
    // sweep
    live: on_ => send({ cmd: "live", live: on_ }),
    sweep: () => send({ cmd: "sweep" }),
    rate: seconds => send({ cmd: "rate", seconds }),
    continuous: on_ => send({ cmd: "continuous", on: on_ }),
    // frequency (MHz in, Hz on the wire)
    frequency: o => send({ cmd: "frequency", ...o }),
    fullSpan: () => send({ cmd: "full_span" }),
    // amplitude
    refLevel: dbm => send({ cmd: "ref_level", dbm }),
    refOffset: db => send({ cmd: "ref_offset", db }),
    attenuation: (db, auto) => send({ cmd: "attenuation", db, auto }),
    // trace
    traceMode: mode => send({ cmd: "trace_mode", mode }),
    traceRestart: () => send({ cmd: "trace_restart" }),
    detector: d => send({ cmd: "detector", detector: d }),
    // bandwidths
    rbw: (hz, auto) => send({ cmd: "rbw", hz, auto }),
    vbw: (hz, auto) => send({ cmd: "vbw", hz, auto }),
    sweepTime: (seconds, auto) => send({ cmd: "sweep_time", seconds, auto }),
    // markers
    markers: count => send({ cmd: "markers", count }),
    markersOff: () => send({ cmd: "markers_off" }),
    // x is in the analyzer's own units: Hz normally, seconds in zero span
    markerX: (n, x, seconds) => send({ cmd: "marker_x", n, x, seconds }),
    markerAction: (n, action) => send({ cmd: "marker_action", n, action }),
  };
})();
