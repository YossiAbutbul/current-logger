#!/usr/bin/env python3
"""Launcher for the FSC3 burst-decay test console.

    python run-test.py
    python run-test.py --host 172.16.10.1 --scpi-port 5555 --port 8770

Then open http://localhost:8770/
"""

import argparse

from fscapp.config import INSTRUMENT_HOST, INSTRUMENT_PORT
from fscapp.server import serve


def main():
    ap = argparse.ArgumentParser(description="FSC3 periodic burst decay test")
    ap.add_argument("--host", default=INSTRUMENT_HOST, help="instrument IP")
    ap.add_argument("--scpi-port", type=int, default=INSTRUMENT_PORT)
    ap.add_argument("--bind", default="127.0.0.1", help="web UI interface")
    ap.add_argument("--port", type=int, default=8770, help="web UI port")
    ap.add_argument("--results-dir", default=None,
                    help="where run folders are written (default: ./results)")
    args = ap.parse_args()
    serve(args.bind, args.port, args.host, args.scpi_port, args.results_dir)


if __name__ == "__main__":
    main()
