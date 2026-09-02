#!/usr/bin/env python3
"""Launcher for the TX current decay test console.

    python run-current-test.py
    python run-current-test.py --resource "USB0::0x0957::0x0F07::MY50000200::INSTR" --channel 3

Then open http://localhost:8771/

Measure-only: the app never writes EMUL, OUTP, VOLT, CURR:LIM or SENS:CURR:RANG.
Acquisition settings are saved on connect and restored when the run ends.
"""

import argparse

from dcapp.config import CHANNEL, RESOURCE
from dcapp.server import serve


def main():
    ap = argparse.ArgumentParser(description="TX current decay test (N6705B/N6781A)")
    ap.add_argument("--resource", default=RESOURCE, help="VISA resource string")
    ap.add_argument("--channel", type=int, default=CHANNEL)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--results-dir", default=None,
                    help="where run folders are written (default: ./results-current)")
    ap.add_argument("--list", action="store_true",
                    help="list VISA resources and exit")
    args = ap.parse_args()

    if args.list:
        import pyvisa
        rm = pyvisa.ResourceManager()
        for r in rm.list_resources():
            print(" ", r)
        return
    serve(args.bind, args.port, args.resource, args.channel, args.results_dir)


if __name__ == "__main__":
    main()
