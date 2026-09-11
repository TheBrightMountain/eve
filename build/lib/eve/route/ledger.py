"""The record of which hosts eve is keeping reachable, and how.

This is the only source of truth. Commands mutate the ledger; `reconcile` makes
the machine match it. Nothing writes to a backend directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from eve.route import paths

VERSION = 1
ZAPRET_VERSION = "v72.13"
DEFAULT_QNUM = 200


def blank():
    return {
        "version": VERSION,
        "entries": {},
        "zapret": {"version": ZAPRET_VERSION, "qnum": DEFAULT_QNUM},
    }


def load(path=None):
    path = Path(path) if path else paths.ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return blank()
    try:
        book = json.loads(raw)
    except ValueError:
        return blank()
    # Fill in anything a older/newer writer left out, but keep every key it had.
    for key, value in blank().items():
        book.setdefault(key, value)
    return book


def save(book, path=None):
    path = Path(path) if path else paths.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def add_entry(book, host, method, verdict, address=None, strategy=None):
    entry = {"method": method, "verdict": verdict, "verified": _now()}
    if address:
        entry["address"] = address
    if strategy:
        entry["strategy"] = strategy
    entry.setdefault("added", book["entries"].get(host, {}).get("added", entry["verified"]))
    # Re-seat the host at the end so insertion order tracks recency of update,
    # not first sighting. Timestamps are second-granular and tie; order does not.
    book["entries"].pop(host, None)
    book["entries"][host] = entry
    return entry


def remove_entry(book, host):
    return book["entries"].pop(host, None) is not None


def by_method(book, method):
    return {host: entry for host, entry in book["entries"].items() if entry.get("method") == method}


def qnum(book):
    return book.get("zapret", {}).get("qnum", DEFAULT_QNUM)
