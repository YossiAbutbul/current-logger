#!/usr/bin/env python3
"""Launcher for the FSC3 spectrum analyzer control panel.

    python run-panel.py
    python run-panel.py --host 172.16.10.1 --scpi-port 5555 --port 8772

Then open http://localhost:8772/

Frequency, reference level and offset, trace mode, peak search and markers,
with a live trace. Unlike the burst-decay test this panel does not put the
analyzer back the way it found it - the settings you make are meant to stay.
The state as found at connect is captured, and "Restore" puts it back on
request.
"""

import argparse

from panelapp.config import INSTRUMENT_HOST, INSTRUMENT_PORT
from panelapp.server import serve


def main():
    ap = argparse.ArgumentParser(description="R&S FSC3 control panel")
    ap.add_argument("--host", default=INSTRUMENT_HOST,
                    help="analyzer IP address")
    ap.add_argument("--scpi-port", type=int, default=INSTRUMENT_PORT,
                    help="analyzer SCPI port (R&S handhelds use 5555)")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8772, help="web UI port")
    args = ap.parse_args()

    serve(args.bind, args.port, args.host, args.scpi_port)


if __name__ == "__main__":
    main()
