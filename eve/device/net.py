"""Wake-on-LAN and reachability checks - nothing but the standard library."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess

WOL_PORT = 9


def magic_packet(mac):
    """Six 0xFF bytes, then the MAC sixteen times."""
    raw = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def broadcast_targets(device):
    """Where to send the wake packet, most specific first, without repeats."""
    targets = []
    if device.get("broadcast"):
        targets.append(device["broadcast"])
    ip = device.get("ip")
    if ip and ipaddress.ip_address(ip).version == 4:
        targets.append(str(ipaddress.ip_network(f"{ip}/24", strict=False).broadcast_address))
    targets.append("255.255.255.255")
    return list(dict.fromkeys(targets))


def send_magic_packet(mac, targets, port=WOL_PORT, repeat=3):
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for target in targets:
                sock.sendto(packet, (target, port))


def ping(host, timeout=1):
    """True if `host` answers one ping within `timeout` seconds."""
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
    except OSError:
        return False


def port_open(host, port, timeout=1):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
