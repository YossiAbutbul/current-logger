/* WebSocket to the test backend, plus the REST calls the review view uses. */
const Api = (() => {
  let ws = null;
  const handlers = {};

  function on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); }
  function fire(ev, m) { (handlers[ev] || []).forEach(f => f(m)); }

  function connect() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/ws");
    ws.onopen = () => fire("open", {});
    ws.onclose = () => { fire("close", {}); setTimeout(connect, 2000); };
    ws.onmessage = e => {
      let m; try { m = JSON.parse(e.data); } catch (err) { return; }
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
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  };

  return {
    connect, on, send,
    start: cfg => send({ cmd: "start", config: cfg }),
    stop: () => send({ cmd: "stop" }),
    tests: () => get("/api/tests"),
    test: name => get("/api/test?name=" + encodeURIComponent(name)),
    capture: (name, file) => get("/api/capture?name=" + encodeURIComponent(name)
      + "&file=" + encodeURIComponent(file)),
  };
})();
