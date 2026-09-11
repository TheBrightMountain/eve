"""The five things you can do to a route."""

from __future__ import annotations

import json as jsonlib
import os
import sys

import rich_click as click

from eve.console import console, error, info, step, success, warning
from eve.route import dpi, ledger, paths, probe, reconcile, render, strategy

TIMEOUT = click.option("-t", "--timeout", default=6.0, show_default=True, help="Per-connection timeout (seconds).")
DRY_RUN = click.option("-n", "--dry-run", is_flag=True, help="Say what would change, change nothing.")

FIXABLE = {probe.DNS_POISONED: "pin", probe.SNI_BLOCKED: "dpi"}


def _require_admin(action):
    if paths.is_admin():
        return
    hint = "start an elevated PowerShell and re-run" if os.name == "nt" else "re-run with sudo"
    error(f"{action} needs administrator rights - {hint}.")
    sys.exit(1)


def _apply(book, backend, dry_run, action):
    if dry_run:
        step("Would change")
        for line in reconcile.plan(book, backend):
            console.print(f"  {line}")
        return False
    _require_admin(action)
    reconcile.apply(book, backend)
    ledger.save(book)
    return True


@click.command("check")
@click.argument("hosts", nargs=-1, required=True)
@TIMEOUT
@click.option("-a", "--addresses", is_flag=True, help="Show every address and how each one answered.")
@click.option("--no-vn", is_flag=True, help="Skip the Vietnamese ISP resolvers.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def check(hosts, timeout, addresses, no_vn, as_json):
    """Work out what stands between you and `HOSTS`. Changes nothing.

    Asks several resolvers what the name means, then opens a real TLS
    connection to each address it gets back. When nothing completes, it runs
    one more handshake to the **same address** announcing a harmless server
    name: if that one succeeds, the route is open and the block is keyed on the
    name. That is what separates **sni-blocked** from **ip-blocked**.

    Pure Python sockets - no privileges, same behaviour on Windows and Linux.
    """
    reports = []
    for host in hosts:
        with console.status(f"[info]probing {host}...[/info]", spinner="dots"):
            reports.append(probe.diagnose(host, timeout=timeout, include_vn=not no_vn))

    if as_json:
        console.print_json(jsonlib.dumps(reports, ensure_ascii=False))
        return reports
    for rep in reports:
        render.report(rep, addresses=addresses)
    return reports


@click.command("add")
@click.argument("host")
@TIMEOUT
@DRY_RUN
def add(host, timeout, dry_run):
    """Make `HOST` reachable, and remember how.

    Diagnoses first, then applies only the fix the diagnosis calls for: a
    poisoned name gets its real address pinned, a name-keyed DPI block gets the
    packet-level bypass. The result is recorded so `eve route sync` can keep it
    working when a CDN moves.
    """
    step(f"eve route add {host}")
    with console.status(f"[info]diagnosing {host}...[/info]", spinner="dots"):
        rep = probe.diagnose(host, timeout=timeout)

    verdict = rep["verdict"]
    console.print(f"Diagnosis: [bold]{verdict}[/bold] - {probe.VERDICT_TEXT[verdict]}")

    if verdict == probe.OPEN:
        success(f"{host} is already reachable - nothing to do.")
        return rep
    if verdict not in FIXABLE:
        error(f"{probe.VERDICT_TEXT[verdict]} - this needs a tunnel or VPN, which eve will not set up for you.")
        sys.exit(1)

    method = FIXABLE[verdict]
    book = ledger.load()
    backend = dpi.backend()

    if method == "pin":
        ledger.add_entry(book, host, method="pin", verdict=verdict, address=rep["best"])
    else:
        ok, reason = backend.available()
        if not ok:
            error(f"The DPI bypass is not available here: {reason}")
            sys.exit(1)
        if dry_run:
            ledger.add_entry(book, host, method="dpi", verdict=verdict, strategy=strategy.LADDER[0])
        else:
            _require_admin("Installing the DPI bypass")
            backend.install()
            hosts = sorted(set(ledger.by_method(book, "dpi")) | {host})
            step("Finding a strategy that gets through")
            found = strategy.find(
                host,
                backend,
                hosts,
                verify=lambda h: probe.reachable(h, timeout=timeout),
                preferred=reconcile.desired(book)["strategy"],
                on_try=lambda c: info(f"trying [accent]{c}[/accent]"),
            )
            if not found:
                error("No strategy in the ladder got through. The bypass has been removed again.")
                sys.exit(1)
            success(f"Strategy that works: [accent]{found}[/accent]")
            ledger.add_entry(book, host, method="dpi", verdict=verdict, strategy=found)

    if not _apply(book, backend, dry_run, "Editing hosts"):
        return rep

    step("Verify")
    with console.status(f"[info]re-checking {host}...[/info]", spinner="dots"):
        ok = probe.reachable(host, timeout=timeout)
    if ok:
        success(f"{host} is reachable now")
    else:
        warning(f"{host} still does not answer. Run `eve route check {host} -a` for detail.")
    return rep


@click.command("rm")
@click.argument("host", required=False)
@click.option("--all", "drop_all", is_flag=True, help="Forget every route eve is holding open.")
@DRY_RUN
def rm(host, drop_all, dry_run):
    """Undo what `eve route add` put in place, and forget it.

    Only touches what eve owns - the marked hosts block, eve's own service and
    firewall table. Everything else is left exactly as it was.
    """
    if not host and not drop_all:
        error("Nothing to do - name a HOST, or pass --all.")
        sys.exit(1)

    step("eve route rm")
    book = ledger.load()

    if drop_all:
        removed = len(book["entries"])
        book["entries"] = {}
        info(f"Forgetting {removed} route(s)")
    elif not ledger.remove_entry(book, host):
        info(f"{host} was not one of eve's routes - nothing changed.")
        return
    else:
        info(f"Forgetting {host}")

    if _apply(book, dpi.backend(), dry_run, "Editing hosts"):
        success("Done")


@click.command("ls")
@click.option("--json", "as_json", is_flag=True, help="Print the ledger as JSON.")
def ls(as_json):
    """Show every route eve is holding open, and how.

    A pinned address goes stale when a CDN moves, and a pin that outlives its
    reason looks exactly like a broken site - so it is worth being able to see
    them without going digging in `hosts`.
    """
    book = ledger.load()
    backend = dpi.backend()
    state = backend.state() if backend.present() else {"installed": False, "running": False}

    if as_json:
        console.print_json(jsonlib.dumps({**book, "dpi": state}, ensure_ascii=False))
        return book
    render.ledger_table(book, state)
    return book


@click.command("sync")
@TIMEOUT
@DRY_RUN
def sync(timeout, dry_run):
    """Re-check every route and repair the ones that drifted.

    Pinned addresses rot when a CDN moves. `sync` re-resolves each one and
    re-pins it, which is the difference between a stale pin and a mystery.
    """
    book = ledger.load()
    if not book["entries"]:
        info("Nothing to sync.")
        return book

    step("eve route sync")
    for host, entry in list(book["entries"].items()):
        with console.status(f"[info]re-checking {host}...[/info]", spinner="dots"):
            rep = probe.diagnose(host, timeout=timeout)
        if entry["method"] == "pin" and rep["best"] and rep["best"] != entry.get("address"):
            info(f"{host}: [accent]{entry.get('address')}[/accent] → [accent]{rep['best']}[/accent]")
            ledger.add_entry(book, host, method="pin", verdict=rep["verdict"], address=rep["best"])
        elif rep["verdict"] in probe.REACHABLE:
            success(f"{host} still reachable")
        else:
            warning(f"{host} reports {rep['verdict']} - run `eve route add {host}` to re-fix it.")

    if _apply(book, dpi.backend(), dry_run, "Editing hosts"):
        success("In sync")
    return book
