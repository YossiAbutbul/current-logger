#!/usr/bin/env python3
"""
find-fsc.py - locate an R&S instrument reachable over the network.

Useful after binding the RNDIS driver to the FSC3's USB port (the instrument then
appears as a USB network adapter), or when it is plugged into LAN. Scans the local
subnets for anything answering SCPI on port 5555 or 5025 and asks it *IDN?.

    python find-fsc.py                 scan every local subnet
    python find-fsc.py 172.16.10       scan one /24
    python find-fsc.py 172.16.10.10    probe one host

Standard library only.
"""

import concurrent.futures as cf
import ipaddress
import socket
import subprocess
import sys

PORTS = (5555, 5025)   # R&S handhelds answer on 5555


def local_subnets():
    """Every IPv4 /24 this PC has an address on, most-specific interfaces first."""
    nets = []
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        out = ""
    adapter = "?"
    for line in out.splitlines():
        s = line.strip()
        if s.endswith(":") and "adapter" in line.lower():
            adapter = s.rstrip(":")
        if "IPv4 Address" in s or "IPv4-Adresse" in s:
            ip = s.split(":")[-1].strip().rstrip("(Preferred)").strip()
            try:
                a = ipaddress.IPv4Address(ip)
            except ValueError:
                continue
            if a.is_loopback:
                continue
            nets.append((adapter, str(ipaddress.IPv4Network(f"{ip}/24", strict=False))))
    return nets


def probe(ip, timeout=0.35):
    """Return '<port>: <idn>' if anything SCPI-ish answers on a known port."""
    for port in PORTS:
        r = probe_port(ip, port, timeout)
        if r:
            return f"{port}: {r}"
    return None


def probe_port(ip, port, timeout=0.35):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(1.5)
            s.sendall(b"*IDN?\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.decode("latin-1").strip() or "(open, but no *IDN? reply)"
    except OSError:
        return None


def main():
    targets, labels = [], {}
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.count(".") == 3:
            targets = [arg]
        else:
            targets = [f"{arg}.{i}" for i in range(1, 255)]
        labels = {t: "(specified)" for t in targets}
    else:
        nets = local_subnets()
        if not nets:
            print("Could not read any local IPv4 address from ipconfig.")
            return
        print("Local subnets:")
        for adapter, net in nets:
            print(f"  {net:<20} {adapter}")
            for host in ipaddress.IPv4Network(net).hosts():
                targets.append(str(host))
                labels.setdefault(str(host), adapter)
        print()

    print(f"Probing {len(targets)} addresses on TCP {PORTS} ...")
    found = []
    with cf.ThreadPoolExecutor(max_workers=256) as ex:
        for ip, idn in zip(targets, ex.map(probe, targets)):
            if idn:
                found.append((ip, idn))
                print(f"\n  *** {ip}  [{labels.get(ip,'')}]\n      {idn}")

    if not found:
        print("\nNothing answered on ports 5555/5025.")
        print("If the FSC3 is on USB, check that the RNDIS driver is bound and that a")
        print("new network adapter shows an IP (ipconfig). On LAN, check")
        print("Setup > Instrument Setup > Network on the instrument.")
        return

    print("\nUse it with the panel:")
    for ip, _ in found:
        print(f"  python fsc-bridge.py --open tcp://{ip}:5555")


if __name__ == "__main__":
    main()
