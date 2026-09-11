"""Derive the system eve wants from the ledger, then make reality match.

`desired` is deliberately pure - ledger in, description out - so the whole
model is testable without root, without a network, on either platform.
"""

from __future__ import annotations

from eve.route import hosts, ledger, paths


def desired(book):
    """What the machine should look like for this ledger."""
    pins = {host: entry["address"] for host, entry in ledger.by_method(book, "pin").items() if entry.get("address")}
    dpi_entries = ledger.by_method(book, "dpi")

    # One daemon serves every DPI host, so it runs one strategy: the most
    # recently verified one, which is the one we last saw working.
    strategy = None
    if dpi_entries:
        # Latest verification wins; ties (same second) break toward the entry
        # updated most recently, which `reversed` picks out of `max`.
        newest = max(reversed(list(dpi_entries.values())), key=lambda e: e.get("verified", ""))
        strategy = newest.get("strategy")

    return {
        "pins": pins,
        "dpi_hosts": sorted(dpi_entries),
        "dpi_active": bool(dpi_entries),
        "strategy": strategy,
        "qnum": ledger.qnum(book),
    }


def plan(book, backend, hosts_path=None):
    """Human-readable lines describing what `apply` would do. Changes nothing."""
    state = desired(book)
    lines = [f"hosts block in {hosts_path or paths.hosts_path()}:"]
    lines += [f"    {ip}  {host}" for host, ip in sorted(state["pins"].items())] or ["    (empty)"]
    if state["dpi_active"]:
        lines.append(f"DPI hostlist: {', '.join(state['dpi_hosts'])}")
        lines += backend.plan(state["strategy"], state["dpi_hosts"])
    else:
        lines.append("DPI bypass: not needed, would be removed")
    return lines


def apply(book, backend, hosts_path=None, make_backup=True):
    """Make the machine match the ledger. Safe to run repeatedly."""
    state = desired(book)
    hosts.write_block(state["pins"], path=hosts_path, make_backup=make_backup)

    if state["dpi_active"]:
        backend.install()
        backend.write_hostlist(state["dpi_hosts"])
        backend.apply(state["strategy"])
    elif backend.present():
        backend.teardown()
    return state
