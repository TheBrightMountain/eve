"""The machines eve knows by name, kept in ~/.config/eve/devices.json.

    {
      "battlestation": {
        "aliases": ["bs"], "mac": "9C:6B:00:36:2B:BC", "ip": "10.0.0.73",
        "host": "battlestation.bicorn-luma.ts.net", "user": "nms", "port": 22,
        "broadcast": "10.0.0.255"
      }
    }

`ip` is the LAN address (what wake-on-LAN aims at); `host` is what `ssh`
dials when it differs from `ip` - a Tailscale name, say. Every field but the
name is optional.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path

FIELDS = {
    "mac": "hardware address of the network card that wakes it (wake)",
    "ip": "LAN address - the subnet broadcast for wake, and ssh's fallback host",
    "host": "what ssh dials if not the ip (a Tailscale or DNS name)",
    "user": "ssh user",
    "port": "ssh port (default 22)",
    "broadcast": "address the wake packet goes to (default: the ip's /24 broadcast, then 255.255.255.255)",
    "note": "anything worth remembering about it",
}

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DeviceError(Exception):
    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "eve" / "devices.json"


def load(location=None):
    location = Path(location) if location else path()
    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise DeviceError(f"{location} is not valid JSON: {exc}", "Fix or remove the file.") from exc
    return data if isinstance(data, dict) else {}


def save(devices, location=None):
    location = Path(location) if location else path()
    location.parent.mkdir(parents=True, exist_ok=True)
    tmp = location.with_suffix(".tmp")
    tmp.write_text(json.dumps(devices, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(location)
    return location


def resolve(devices, name_or_alias):
    """The (name, device) that `name_or_alias` refers to, matching names before aliases."""
    key = name_or_alias.lower()
    for name, device in devices.items():
        if name.lower() == key:
            return name, device
    for name, device in devices.items():
        if key in (a.lower() for a in device.get("aliases", [])):
            return name, device
    known = ", ".join(sorted(devices)) or "none yet"
    raise DeviceError(f"No device called {name_or_alias!r} (known: {known}).", "eve device ls")


def taken(devices, word, ignore=None):
    """The device already answering to `word` as name or alias, other than `ignore`."""
    key = word.lower()
    for name, device in devices.items():
        if name == ignore:
            continue
        if name.lower() == key or key in (a.lower() for a in device.get("aliases", [])):
            return name
    return None


# -- validation ------------------------------------------------------------------------------------


def check_name(word, what="name"):
    if not _NAME.match(word):
        raise DeviceError(f"{what.capitalize()} {word!r} must be letters, digits, '.', '_' or '-'.")
    return word


def normalize_mac(value):
    digits = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(digits) != 12 or not re.fullmatch(r"[0-9A-Fa-f:.\- ]+", value.strip()):
        raise DeviceError(f"{value!r} is not a MAC address.", "Like 9C:6B:00:36:2B:BC or 9c-6b-00-36-2b-bc.")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()


def check_ip(value, what="IP"):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise DeviceError(f"{value!r} is not an {what} address.") from exc


def check_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise DeviceError(f"{value!r} is not a port (1-65535).")
    return port


def clean(field, value):
    """`value` checked and normalized for `field`."""
    if field == "mac":
        return normalize_mac(value)
    if field == "ip":
        return check_ip(value)
    if field == "broadcast":
        return check_ip(value, "broadcast")
    if field == "port":
        return check_port(value)
    value = str(value).strip()
    if not value:
        raise DeviceError(f"{field} cannot be empty - use --unset {field} to clear it.")
    return value
