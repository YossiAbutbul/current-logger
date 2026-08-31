#!/usr/bin/env python3
"""
arpsweep.py - find a device on a directly-connected Ethernet link by ARP.

Useful when an instrument is plugged straight into the PC with no DHCP server, so
both ends self-assign a 169.254.x.x (APIPA) address and the instrument's IP is
unknown. ARP works below IP, so it finds the device even if its address is on a
completely different subnet than ours.

    python arpsweep.py                  sweep 169.254.0.0/16 (APIPA)
    python arpsweep.py 172.16.10.0/24   sweep a specific range

Any host that answers is then probed for SCPI on port 5025. Standard library only.
"""

import concurrent.futures as cf
import ctypes
import ipaddress
import socket
import struct
import sys
from ctypes import wintypes

iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
SendARP = iphlpapi.SendARP
SendARP.argtypes = [wintypes.ULONG, wintypes.ULONG,
                    ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]

PORTS = (5555, 5025)


def arp(ip):
    """Return the MAC for `ip` if it answers ARP on a local link, else None."""
    dest = struct.unpack("<L", socket.inet_aton(ip))[0]
    mac = (ctypes.c_ubyte * 6)()
    ln = wintypes.ULONG(6)
    if SendARP(dest, 0, ctypes.byref(mac), ctypes.byref(ln)) == 0 and ln.value:
        return "-".join(f"{b:02X}" for b in mac[:ln.value])
    return None


def scpi(ip, timeout=1.0):
    for port in PORTS:
        r = scpi_port(ip, port, timeout)
        if r:
            return f"port {port}: {r}"
    return None


def scpi_port(ip, port, timeout=1.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(2.0)
            s.sendall(b"*IDN?\n")
            data = b""
            while b"\n" not in data:
                c = s.recv(4096)
                if not c:
                    break
                data += c
            return data.decode("latin-1").strip() or "(port open, no *IDN? reply)"
    except OSError:
        return None


def main():
    net = ipaddress.IPv4Network(sys.argv[1] if len(sys.argv) > 1 else "169.254.0.0/16")
    hosts = [str(h) for h in net.hosts()]
    mine = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)
            if i[0] == socket.AF_INET}
    hosts = [h for h in hosts if h not in mine]

    print(f"ARP-sweeping {net} ({len(hosts)} addresses). This can take a few minutes.")
    found = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=512) as ex:
        for ip, mac in zip(hosts, ex.map(arp, hosts)):
            done += 1
            if done % 8192 == 0:
                print(f"  ... {done}/{len(hosts)} checked, {len(found)} found",
                      flush=True)
            if mac:
                found.append((ip, mac))
                print(f"  HOST {ip:<16} {mac}", flush=True)

    if not found:
        print("\nNo device answered ARP.")
        print("The instrument may use a static IP outside this range - read it from")
        print("the analyzer: SETUP > Instrument Setup > Network / LAN settings.")
        return

    print(f"\n{len(found)} host(s) found. Probing SCPI on ports {PORTS} ...")
    for ip, mac in found:
        idn = scpi(ip)
        print(f"  {ip:<16} {mac}  ->  {idn if idn else 'no SCPI on 5555/5025'}")
        if idn:
            print(f"\n  *** Use: python fsc-bridge.py --open tcp://{ip}:5555")


if __name__ == "__main__":
    main()
