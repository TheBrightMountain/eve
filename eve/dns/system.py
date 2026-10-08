"""Read and change which DNS servers each network interface uses.

Windows goes through the DnsClient PowerShell module; Linux through
`resolvectl`, which is runtime-only - systemd-resolved forgets it when the link
or NetworkManager restarts. Both backends speak the same shape: an interface is
`{"name", "id", "servers", "manual"}`, where `manual` is the list of servers
configured by hand, empty when they come from DHCP.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)


def _ps_list(values):
    return "@(" + ",".join(f"'{v}'" for v in values) + ")"


# Only interfaces that are up and carry a default route: those are the ones
# whose DNS actually gets used. A disconnected adapter or a VPN's virtual one
# would just collect settings nobody reads.
WINDOWS_LIST = r"""
$ErrorActionPreference = 'SilentlyContinue'
$base = 'HKLM:\SYSTEM\CurrentControlSet\Services'
@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | ForEach-Object {
  $guid = $_.NetAdapter.InterfaceGuid
  $v4 = (Get-ItemProperty "$base\Tcpip\Parameters\Interfaces\$guid" -Name NameServer).NameServer
  $v6 = (Get-ItemProperty "$base\Tcpip6\Parameters\Interfaces\$guid" -Name NameServer).NameServer
  [pscustomobject]@{
    name = $_.InterfaceAlias
    id = [string]$_.InterfaceIndex
    servers = @((Get-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex).ServerAddresses)
    manual = @(@($v4, $v6) -join ',' -split '[, ]+' | Where-Object { $_ })
  }
}) | ConvertTo-Json -Compress -Depth 3
"""


class WindowsBackend:
    name = "windows"

    def _ps(self, script):
        done = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout or "powershell failed").strip())
        return done.stdout

    def interfaces(self):
        raw = self._ps(WINDOWS_LIST).strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        # ConvertTo-Json unwraps a one-element array into a bare object.
        rows = parsed if isinstance(parsed, list) else [parsed]
        return [
            {"name": r["name"], "id": r["id"], "servers": list(r["servers"] or []), "manual": list(r["manual"] or [])}
            for r in rows
        ]

    def set_servers(self, iface, servers):
        self._ps(f"Set-DnsClientServerAddress -InterfaceIndex {int(iface['id'])} -ServerAddresses {_ps_list(servers)}")

    def set_automatic(self, iface):
        self._ps(f"Set-DnsClientServerAddress -InterfaceIndex {int(iface['id'])} -ResetServerAddresses")

    def flush(self):
        self._ps("Clear-DnsClientCache")


class LinuxBackend:
    name = "linux"

    def _resolvectl(self, *args):
        if not shutil.which("resolvectl"):
            raise RuntimeError("resolvectl not found - eve dns needs systemd-resolved on Linux.")
        done = _run(["resolvectl", *args])
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout or "resolvectl failed").strip())
        return done.stdout

    def interfaces(self):
        routes = _run(["ip", "-j", "route", "show", "default"])
        devices = []
        for route in json.loads(routes.stdout or "[]"):
            if route.get("dev") and route["dev"] not in devices:
                devices.append(route["dev"])
        rows = []
        for dev in devices:
            out = self._resolvectl("dns", dev)
            servers = re.sub(r"^Link \d+ \([^)]*\):", "", out.strip()).split()
            # resolved cannot say where a link's servers came from, but whoever
            # set them - DHCP, netplan, NetworkManager - `revert` hands the link
            # back to them. Recording them as manual would make reset pin
            # today's list instead, and miss whatever the network hands out next.
            rows.append({"name": dev, "id": dev, "servers": servers, "manual": []})
        return rows

    def set_servers(self, iface, servers):
        self._resolvectl("dns", iface["id"], *servers)

    def set_automatic(self, iface):
        self._resolvectl("revert", iface["id"])

    def flush(self):
        self._resolvectl("flush-caches")


def backend(name=None):
    name = name or ("windows" if os.name == "nt" else "linux")
    return WindowsBackend() if name == "windows" else LinuxBackend()
