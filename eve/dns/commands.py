"""Show, set and reset the DNS servers the machine uses."""

from __future__ import annotations

import ipaddress
import sys

import rich_click as click
from rich.table import Table

from eve.console import console, error, info, step, success, warning
from eve.dns import state, system
from eve.route import dns as resolver
from eve.route.commands import DRY_RUN, _reporting, _require_admin

# A name every working resolver can answer, used to test a server before
# trusting the whole machine's lookups to it.
TEST_NAME = "example.com"

INTERFACE = click.option(
    "-i",
    "--interface",
    "names",
    multiple=True,
    metavar="NAME",
    help="Only this interface (repeatable). Default: every connected one.",
)


def _pick(interfaces, names):
    if not names:
        return interfaces
    known = {iface["name"]: iface for iface in interfaces}
    missing = [n for n in names if n not in known]
    if missing:
        error(f"No connected interface called {', '.join(missing)}. Have: {', '.join(known) or 'none'}.")
        sys.exit(1)
    return [known[n] for n in names]


def _answers(server, timeout=2.5):
    """True/False for an IPv4 server; None for IPv6, which the probe cannot ask."""
    if ipaddress.ip_address(server).version != 4:
        return None
    return bool(resolver.query_a(TEST_NAME, server, timeout))


@click.command("show")
def show():
    """Which DNS servers each connected interface uses, and where they came from."""
    backend = system.backend()
    with _reporting():
        interfaces = backend.interfaces()
    saved = state.load()

    if not interfaces:
        info("No connected interface with a default route.")
        return interfaces

    table = Table(box=None, pad_edge=False)
    for column in ("interface", "servers", "set by"):
        table.add_column(column, style="accent" if column == "interface" else None)
    for iface in interfaces:
        if iface["name"] in saved:
            origin = f"eve (was {state.describe(saved[iface['name']])})"
        else:
            origin = "hand" if iface["manual"] else "automatic"
        table.add_row(iface["name"], ", ".join(iface["servers"]) or "-", origin)
    console.print(table)
    return interfaces


@click.command("set")
@click.argument("servers", nargs=-1, required=True)
@INTERFACE
@click.option("-f", "--force", is_flag=True, help="Set them even if none of them answers a test lookup.")
@DRY_RUN
def set_(servers, names, force, dry_run):
    """Point the machine's DNS at `SERVERS`, in order of preference.

        eve dns set 1.1.1.1 8.8.8.8

    Each server is asked to resolve a test name first: one that does not answer
    is warned about, and if none answers nothing is changed, because a typo
    here takes every lookup on the machine down with it. `--force` skips that.

    What each interface used before is kept the first time, so
    `eve dns reset` puts it back - even after several `set`s.

    On Linux this goes through `resolvectl` and lasts until the link or
    NetworkManager restarts.
    """
    bad = []
    for server in servers:
        try:
            ipaddress.ip_address(server)
        except ValueError:
            bad.append(server)
    if bad:
        error(f"Not an IP address: {', '.join(bad)}. DNS servers are given by address, e.g. 1.1.1.1.")
        sys.exit(1)

    step("Testing the servers")
    verdicts = {server: _answers(server) for server in servers}
    for server, ok in verdicts.items():
        if ok is None:
            info(f"{server}: IPv6 - not tested")
        elif ok:
            success(f"{server} answers")
        else:
            warning(f"{server} did not answer a lookup for {TEST_NAME}")
    if not force and verdicts and all(ok is False for ok in verdicts.values()):
        error("None of these servers answered. Nothing was changed - pass --force if you are sure.")
        sys.exit(1)

    backend = system.backend()
    with _reporting():
        targets = _pick(backend.interfaces(), names)
    if not targets:
        error("No connected interface with a default route to set.")
        sys.exit(1)

    if dry_run:
        step("Would change")
        for iface in targets:
            console.print(f"  {iface['name']}: {', '.join(iface['servers']) or '-'} → {', '.join(servers)}")
        return targets
    _require_admin("Changing DNS servers")

    # Keep the originals before touching anything, so a failure part-way still
    # leaves `reset` knowing what to put back.
    saved = state.load()
    for iface in targets:
        if state.remember(saved, iface):
            info(f"{iface['name']}: keeping {state.describe(saved[iface['name']])} for `eve dns reset`")
    state.save(saved)

    with _reporting("`eve dns reset` puts back what each interface had before."):
        for iface in targets:
            backend.set_servers(iface, list(servers))
        backend.flush()
        after = {iface["name"]: iface for iface in backend.interfaces()}

    for iface in targets:
        now = after.get(iface["name"], {}).get("servers", [])
        if now[: len(servers)] == list(servers):
            success(f"{iface['name']} now uses {', '.join(servers)}")
        else:
            warning(f"{iface['name']} reports {', '.join(now) or 'nothing'} - the change may not have taken.")
    return targets


@click.command("reset")
@INTERFACE
@DRY_RUN
def reset(names, dry_run):
    """Put back whatever each interface used before `eve dns set`.

    Only interfaces eve changed are touched. One with servers set by hand gets
    exactly those again; one whose servers were automatic is handed back to
    whatever set them - DHCP on Windows; DHCP, netplan or NetworkManager on
    Linux - so it picks up anything they hand out later.
    """
    saved = state.load()
    chosen = names or list(saved)
    unknown = [n for n in chosen if n not in saved]
    if unknown:
        error(f"eve never changed {', '.join(unknown)} - nothing to put back.")
        sys.exit(1)
    if not chosen:
        info("eve has not changed any interface's DNS - nothing to reset.")
        return []

    if dry_run:
        step("Would change")
        for name in chosen:
            console.print(f"  {name} → {state.describe(saved[name])}")
        return chosen
    _require_admin("Changing DNS servers")

    backend = system.backend()
    with _reporting():
        for name in chosen:
            original = saved[name]
            iface = {"name": name, "id": original["id"]}
            if original["manual"]:
                backend.set_servers(iface, original["manual"])
            else:
                backend.set_automatic(iface)
            del saved[name]
            state.save(saved)
            success(f"{name} is back to {state.describe(original)}")
        backend.flush()
    return chosen
