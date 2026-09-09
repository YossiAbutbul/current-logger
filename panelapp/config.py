"""Defaults and static roots for the FSC3 control panel."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Static files are looked up in order, so the panel keeps only what is its own
# (index.html, css/panel.css, js/api.js, js/panel.js) and inherits the design
# system - theme.css, app.css, theme.js, plot.js, palettes.json - from the
# current-test app. One stylesheet, one palette set, one chart engine for both
# consoles; nothing is duplicated and webdc is never written to.
WEB_DIRS = [os.path.join(ROOT, "webpanel"), os.path.join(ROOT, "webdc")]

# R&S handhelds (FSC/FSH) listen for SCPI on 5555, not the usual 5025.
INSTRUMENT_HOST = "172.16.10.1"
INSTRUMENT_PORT = 5555

# The FSC3 refuses CALC:MARK<n>:STAT ON unless every lower marker is already
# on, so markers are addressed as a count 1..MARKERS rather than a free set.
MARKERS = 6

# Levels the analyzer cannot resolve come back as the 9.91e37 sentinel.
INVALID = 1e30

TRACE_MODES = ("WRIT", "MAXH", "MINH", "AVER", "VIEW")
TRACE_MODE_NAMES = {
    "WRIT": "Clear write",
    "MAXH": "Max hold",
    "MINH": "Min hold",
    "AVER": "Average",
    "VIEW": "View (frozen)",
}
# Measured on the instrument: APE/POS/NEG/SAMP/RMS are accepted, MAXP is not a
# token here (-141), and AVER and QPE are rejected with -221 "Requested detector
# is not allowed". POS is the max-peak detector; APE (auto peak) is the default.
DETECTORS = ("APE", "POS", "NEG", "SAMP", "RMS")
DETECTOR_NAMES = {"APE": "Auto peak", "POS": "Max peak", "NEG": "Min peak",
                  "SAMP": "Sample", "RMS": "RMS"}

# How often the live view reads the analyzer. Every SCPI query interrupts the
# instrument's own sweep and display, so this is a load dial, not just a frame
# rate: 1 s is comfortable, 0.3 s is visibly heavier on the FSC3's own screen.
SWEEP_PERIOD_S = 1.0
SWEEP_PERIODS = (0.3, 0.5, 1.0, 2.0, 5.0)
# How often the live view re-counts the markers from scratch, to notice markers
# switched on or off from the front panel.
MARKER_VERIFY_EVERY = 12
