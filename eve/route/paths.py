"""Where eve keeps the state it owns.

Every location here is eve's alone, so `eve route rm --all` can put the machine
back exactly as it was. Nothing is shared with another tool.
"""

from __future__ import annotations

import os
from pathlib import Path


def _is_windows():
    return os.name == "nt"


def state_dir():
    if _is_windows():
        root = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(root) / "eve"
    return Path("/etc/eve")


def ledger_path():
    return state_dir() / "route.json"


def dpi_hostlist_path():
    return state_dir() / "route-dpi.txt"


def zapret_dir():
    if _is_windows():
        root = os.environ.get("ProgramFiles", r"C:\Program Files")
        return Path(root) / "eve" / "zapret"
    return Path("/opt/eve/zapret")


def hosts_path():
    if _is_windows():
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def is_admin():
    if _is_windows():
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False
