/* Backend client.

   openSocket()  = the browser's WebSocket to this app.
   connect()     = tell the backend to open the VISA link to the analyzer.
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
    else fire("error", { msg: "not connected to the test server" });
  }

  const get = async path => {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    return r.json();
  };

  return {
    openSocket, on, send,
    // instrument link
    connect: (resource, channel) => send({ cmd: "connect", resource, channel }),
    disconnect: () => send({ cmd: "disconnect" }),
    status: () => send({ cmd: "status" }),
    // reference session
    recordRef: cfg => send({ cmd: "record_ref", config: cfg }),
    cancelRef: () => send({ cmd: "cancel_ref" }),
    setRef: v => send({ cmd: "set_ref", ref_peak_ma: v }),
    // run
    start: (cfg, ref) => send({ cmd: "start", config: cfg, ref_peak_ma: ref }),
    stop: () => send({ cmd: "stop" }),
    // rest
    resources: () => get("/api/resources"),
    runs: () => get("/api/runs"),
    run: name => get("/api/run?name=" + encodeURIComponent(name)),
    capture: (name, file) => get("/api/capture?name=" + encodeURIComponent(name)
      + "&file=" + encodeURIComponent(file)),
  };
})();
