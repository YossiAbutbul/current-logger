# spectrum-log

Tools for a Rohde & Schwarz **FSC3** spectrum analyzer.

The analyzer is reached over LAN at **172.16.10.1**, SCPI on **port 5555**
(R&S handhelds use 5555, not the 5025 used by many other R&S instruments).
Plugged in directly, the instrument hands this PC `172.16.10.2`.

## Burst decay test

A device under test transmits a burst every N minutes. The test captures each
burst in zero span using the analyzer's trigger, takes the first capture's peak
as the reference power, then keeps capturing once per interval until the peak
has dropped by a configurable amount (default 3 dB), and reports how long that
took. Every capture is saved as CSV and can be scrolled through afterwards.

```
python run-test.py
```

Then open <http://localhost:8770/>. Point it elsewhere with
`--host`, `--scpi-port`, `--port`, `--results-dir`.

### Layout

```
run-test.py            launcher
fscapp/
  config.py            defaults, paths, config coercion
  scpi.py              socket transport + FSC3 operations (instrument quirks documented here)
  runner.py            test state machine; owns timing, runs in the backend
  storage.py           results/ layout: capture CSVs, summary, config, result
  server.py            HTTP + WebSocket, static file serving, REST for the viewer
web/
  index.html           app shell
  css/theme.css        design tokens (defaults = Anodized / mid)
  css/app.css          layout and components
  js/palettes.json     10 palette directions x 3 modes
  js/theme.js          applies a palette to CSS custom properties
  js/plot.js           canvas charts, coloured from the live tokens
  js/api.js            WebSocket + REST client
  js/live.js           running-test view
  js/review.js         saved-run browser
  js/app.js            bootstrap and event wiring
results/<run-name>/    one folder per run, never overwritten
```

A run folder holds `_config.json`, `_summary.csv`, `_result.json` and one
`capture_NNNN.csv` per measurement. Each capture CSV carries a `#` metadata
header followed by `time_s,power_dbm` samples.

## Instrument panel

`fsc3-panel.html` is a general-purpose control panel (frequency, markers,
RBW/VBW, SCPI console, trace plot). It talks to the instrument through
`fsc-bridge.py`, which relays a browser WebSocket to TCP or a COM port:

```
python fsc-bridge.py --open tcp://172.16.10.1:5555
```

## Finding the instrument

```
python find-fsc.py          scan local subnets for SCPI on 5555/5025
python arpsweep.py          ARP sweep a directly-connected link
```

## Note on USB

The FSC3's USB port enumerates as CDC class 02 / subclass 02 / **protocol 0xFF**
with no Union functional descriptor, so Windows binds the generic `usbser.sys`,
which cannot drive it: `CreateFile` succeeds but `SetCommState` and
`EscapeCommFunction(SETDTR)` both fail with `ERROR_GEN_FAILURE`. COM5 therefore
appears in Device Manager but cannot be opened by anything, and Chrome's Web
Serial picker correctly hides it. Using USB requires the R&S driver (ships with
R&S InstrumentView). LAN avoids the issue entirely.
