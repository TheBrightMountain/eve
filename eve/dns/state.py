"""What each interface used before eve touched it, so `eve dns reset` can put it back.

Saved once, on the first `set` for an interface, and never overwritten by a
later one - otherwise a second `set` would record eve's own servers as the
original and the real one would be lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from eve.route import paths


def path():
    return paths.state_dir() / "dns.json"


def load(location=None):
    location = Path(location) if location else path()
    try:
        return json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(saved, location=None):
    location = Path(location) if location else path()
    if not saved:
        location.unlink(missing_ok=True)
        return location
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(json.dumps(saved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return location


def remember(saved, iface):
    """Record `iface`'s current setting unless one is already kept. True if it recorded."""
    if iface["name"] in saved:
        return False
    saved[iface["name"]] = {"id": iface["id"], "manual": list(iface["manual"])}
    return True


def describe(original):
    return ", ".join(original["manual"]) if original["manual"] else "automatic"
