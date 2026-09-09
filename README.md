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

## FSC3 control panel

`python run-panel.py` then <http://localhost:8772/> - a live control panel for
the analyzer: frequency, reference level and offset, trace mode, detector, peak
search and up to six markers, over the same LAN SCPI link.

Unlike the tests, this one **does not put the analyzer back**. What you set is
meant to stay set; the state as found at connect is captured and `Restore`
writes it back on request.

### Layout

```
run-panel.py          launcher
panelapp/
  config.py           host/port, detectors, trace modes, poll rates
  analyzer.py         the verified SCPI operations, and only those
  session.py          shared link + the live sweeper thread
  server.py           HTTP + WebSocket, stdlib only
webpanel/             index.html, css/panel.css, js/api.js, js/panel.js
```

The panel serves its own files first and falls back to `webdc/`, so the design
system - `theme.css`, `app.css`, `theme.js`, `plot.js`, `palettes.json` - is
shared with the current test rather than copied. The SCPI socket comes from
`fscapp.scpi` for the same reason.

Live mode leaves the analyzer **free-running** and only reads the trace. Arming
a sweep per refresh would put the instrument into single-sweep and make its own
display step along with the panel, and would break max hold and averaging.
Every query still costs the analyzer sweep time, so the refresh rate is a dial
(0.3-5 s, default 1 s) and marker reads are kept to one confirmation plus two
queries per active marker.

### FSC3 quirks (firmware V2.22), all measured

| Detail | Value |
|---|---|
| Marker order | `CALC:MARK2:STAT ON` fails `-200` while M1 is off. Markers are a count 1..6, enabled from 1 up |
| Reading an inactive marker | `CALC:MARK<n>:X?`/`:Y?` **switches that marker on**. Only `STAT?` is safe to ask |
| Unset marker level | Answers `99.1e+36` - the 9.91e37 invalid sentinel - not an error |
| Marker x unit | Hz in a frequency sweep, **seconds** in zero span |
| Detectors | `APE` `POS` `NEG` `SAMP` `RMS` accepted. `MAXP` is `-141`; `AVER` and `QPE` are `-221`. `POS` is max peak |
| Reference offset | `DISP:TRAC:Y:RLEV:OFFS` works, and the reference level follows it 1:1 - so write the offset first, the level second |
| Display range | `DISP:TRAC:Y:SCAL` is accepted and ignored (reads back 100 dB), so it is exposed read-only |
| Marker to reference | `CALC:MARK<n>:FUNC:REF` is accepted and does nothing. `FUNC:CENT` works |
| Trace point count | Taken from the data: `SWE:POIN?` is not implemented and never answers, which desyncs the stream |

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

## TX current decay test

A battery/HLC-powered unit transmits periodically. The N6781A captures each TX
with a **current-level trigger**, so the test never has to predict when a burst
happens - it arms and waits.

```
python run-current-test.py
```

Then open <http://localhost:8771/>. Flags: `--resource`, `--channel`, `--port`,
`--results-dir`, `--list` (enumerate VISA resources).

### The flow

1. **Connect** - pick the VISA resource and channel, press Connect. This only
   reads: identity, module, emulation mode, current range, terminal voltage and
   output state are shown so you can confirm the instrument is set up the way
   you want.
2. **Reference** - set the DUT message interval and the TX detect level, then
   press *Record reference*. It captures a few transmissions and takes the mean
   of their peaks as the reference current, showing the spread so you can see
   how repeatable they are. You can also type a reference in by hand.
3. **Drop delta** - say how far the current may fall, in mA. The stop level
   (reference - drop delta) is shown as you type: reference 700 mA with a drop
   delta of 200 mA stops the run at 500 mA. It must hold for N transmissions in
   a row (default 3) so one odd burst cannot end a long run.
4. **Run** - pressing Start stamps **t0**. The run ends at **tn**, either when
   the stop level is confirmed or when you press Stop, and the result reports
   **tn - t0** along with the reference, final current and transmission counts.

**Measure-only.** The app never writes `EMUL`, `OUTP`, `VOLT`, `CURR:LIM` or
`SENS:CURR:RANG` - the instrument's mode and range are whatever you set on the
front panel. It does write acquisition settings (`SENS:FUNC`, `SENS:SWE:*`,
`TRIG:ACQ:*`); those are snapshotted on connect, mirrored to
`.acq-restore.json`, and restored on disconnect. If the server is killed while
connected, the next connect finds that file and restores from it before taking
a fresh snapshot, so a crash cannot leave your settings changed.

### Layout

```
run-current-test.py    launcher
dcapp/
  config.py            defaults, VISA resource, paths
  instrument.py        N6705B/N6781A access; instrument quirks documented here
  session.py           the shared connection, plus the reference recorder
  runner.py            run state machine; owns t0/tn
  storage.py           two-tier results built for 24h+ runs
  server.py            HTTP + WebSocket + REST
webdc/                 UI (same design system as the RF test, kept independent)
results-current/<run>/ summary.csv + capture_NNNNN.csv + _config/_state/_result
```

### Storage for long runs

A day at 33 s intervals is ~2,600 transmissions. `summary.csv` gets one appended
and flushed row per TX (~145 B, so ~380 KB/day) and alone drives the decay
curve. Each waveform is decimated peak-preserving from 4,185 points to ~500
(~11 KB, so ~29 MB/day) instead of ~260 MB. `_state.json` is rewritten
atomically after every TX, so an interrupted run loses at most one measurement.

### N6781A quirks (firmware D.02.08), all measured

| Detail | Value |
|---|---|
| Acquisition trigger source | `CURR<channel>` - `CURR3`. Plain `CURR` gives `-224`; `CURR1` gives `+310` (empty slot) |
| Completion | `STAT:OPER:COND?` = `+1` idle, `+41` armed, `+1` complete. Bit 5 (32) = waiting for trigger |
| `SENS:SWE:TINT` | Quantised to 20.48 us steps - 150 us becomes 143.36 us. Use the readback for the time axis |
| Current sign | `MEAS:CURR?` and `FETC:ARR:CURR?` are negative for sourced current; `abs()` matches the front panel |
| Pre-trigger | `SENS:SWE:OFFS:POIN` accepts negative values |
| `MEAS:CURR?` polling rate | ~616 ms per reading over USBTMC here - far too slow to catch a 450 ms burst, hence the digitiser |
