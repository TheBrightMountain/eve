"""The five things you can do to a route."""

from __future__ import annotations

import contextlib
import json as jsonlib
import os
import sys

import rich_click as click

from eve.console import console, error, info, step, success, warning
from eve.route import dpi, ledger, paths, probe, reconcile, render, strategy

TIMEOUT = click.option("-t", "--timeout", default=6.0, show_default=True, help="Per-connection timeout (seconds).")
DRY_RUN = click.option("-n", "--dry-run", is_flag=True, help="Say what would change, change nothing.")

FIXABLE = {probe.DNS_POISONED: "pin", probe.SNI_BLOCKED: "dpi"}

# What `auto` falls back to when the diagnosis found nothing wrong. The checker
# is not infallible - an intermittent block, a lucky handshake, or a resolver
# that answers differently for one query can all make a blocked host look fine
# - so a clean verdict is a reason to warn, not a reason to refuse.
WHEN_NOTHING_WRONG = "pin"


def _choose_method(rep, requested):
    """Decide which fix to apply, or bail out if there is nothing sensible.

    An explicit `--method` always wins: it exists precisely for the times the
    diagnosis is wrong, so it must not be second-guessed here.
    """
    verdict = rep["verdict"]

    if requested != "auto":
        warning(f"Forcing [accent]{requested}[/accent] - ignoring the diagnosis ({verdict}).")
        return requested

    if verdict in FIXABLE:
        return FIXABLE[verdict]

    if verdict == probe.OPEN:
        warning(f"{rep['host']} looks reachable, but the check is not infallible - continuing anyway.")
        return WHEN_NOTHING_WRONG

    error(f"{probe.VERDICT_TEXT[verdict]} - this needs a tunnel or VPN, which eve will not set up for you.")
    info("If you know better, force one with `--method pin` or `--method dpi`.")
    sys.exit(1)


def _require_admin(action):
    if paths.is_admin():
        return
    hint = "start an elevated PowerShell and re-run" if os.name == "nt" else "re-run with sudo"
    error(f"{action} needs administrator rights - {hint}.")
    sys.exit(1)


RECOVERY_HINT = (
    "The ledger still records what eve was trying to do - `eve route ls` shows it, "
    "`eve route sync` retries, `eve route rm <host>` undoes it."
)


@contextlib.contextmanager
def _reporting(hint=None):
    """Turn a backend failure into an error message rather than a traceback."""
    try:
        yield
    except (RuntimeError, OSError) as exc:
        error(str(exc))
        if hint:
            info(hint)
        sys.exit(1)


def _apply(book, backend, dry_run, action):
    if dry_run:
        step("Would change")
        for line in reconcile.plan(book, backend):
            console.print(f"  {line}")
        return False
    _require_admin(action)

    # Record the intent before touching the machine. If applying then fails
    # part-way, the ledger still knows what eve was doing, so `ls` can show it
    # and `rm` can undo it. The other order strands real system state - a
    # running service, an edited hosts file - with no record of it anywhere.
    ledger.save(book)
    with _reporting(RECOVERY_HINT):
        reconcile.apply(book, backend)
    return True


@click.command("check")
@click.argument("hosts", nargs=-1, required=True)
@TIMEOUT
@click.option("-a", "--addresses", is_flag=True, help="Show every address and how each one answered.")
@click.option(
    "-n",
    "--samples",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Probe this many times and summarise. Exposes an intermittent block.",
)
@click.option("--no-vn", is_flag=True, help="Skip the Vietnamese ISP resolvers.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def check(hosts, timeout, addresses, samples, no_vn, as_json):
    """Work out what stands between you and `HOSTS`. Changes nothing.

    Asks several resolvers what the name means, then opens a real TLS
    connection to each address it gets back. When nothing completes, it runs
    one more handshake to the **same address** announcing a harmless server
    name: if that one succeeds, the route is open and the block is keyed on the
    name. That is what separates **sni-blocked** from **ip-blocked**.

    A single probe measures something that is not stable - a host can read
    `open` and then `sni-blocked` a minute later. Pass `-n` to sample several
    times; the worst verdict seen is the one reported, because an intermittent
    block is still a block.

    Pure Python sockets - no privileges, same behaviour on Windows and Linux.
    """
    reports = [_sample(host, timeout, not no_vn, samples) for host in hosts]

    if as_json:
        console.print_json(jsonlib.dumps(reports, ensure_ascii=False))
        return reports
    for rep in reports:
        render.report(rep, addresses=addresses)
    return reports


def _sample(host, timeout, include_vn, samples):
    """Probe `host` `samples` times and fold the runs into one report.

    The report returned is the worst run seen, so the caller reads it exactly
    like a single probe. Extra keys describe the spread, and are left off
    entirely for a single sample so the output shape does not change.
    """
    runs = []
    for attempt in range(samples):
        label = f"probing {host}..." if samples == 1 else f"probing {host} ({attempt + 1}/{samples})..."
        with console.status(f"[info]{label}[/info]", spinner="dots"):
            runs.append(probe.diagnose(host, timeout=timeout, include_vn=include_vn))

    if samples == 1:
        return runs[0]

    verdicts = [run["verdict"] for run in runs]
    headline = probe.worst(verdicts)
    report = next(run for run in runs if run["verdict"] == headline)
    distribution = {v: verdicts.count(v) for v in dict.fromkeys(verdicts)}
    return {**report, "samples": verdicts, "distribution": distribution}


@click.command("add")
@click.argument("host")
@click.option(
    "--method",
    "requested",
    type=click.Choice(["auto", "pin", "dpi"]),
    default="auto",
    show_default=True,
    help="Which fix to apply. `auto` follows the diagnosis.",
)
@TIMEOUT
@DRY_RUN
def add(host, requested, timeout, dry_run):
    """Make `HOST` reachable, and remember how.

    Diagnoses first, then applies the fix that diagnosis calls for: a poisoned
    name gets its real address pinned, a name-keyed DPI block gets the
    packet-level bypass. The result is recorded so `eve route sync` can keep it
    working when a CDN moves.

    A clean verdict does **not** stop it. The checker can be wrong - an
    intermittent block or one lucky handshake is enough - so a host that looks
    reachable is pinned anyway, with a warning. Use `--method` when you do not
    trust the diagnosis at all.
    """
    step(f"eve route add {host}")
    with console.status(f"[info]diagnosing {host}...[/info]", spinner="dots"):
        rep = probe.diagnose(host, timeout=timeout)

    verdict = rep["verdict"]
    console.print(f"Diagnosis: [bold]{verdict}[/bold] - {probe.VERDICT_TEXT[verdict]}")

    method = _choose_method(rep, requested)
    book = ledger.load()
    backend = dpi.backend()

    if method == "pin":
        if not rep["best"]:
            error(f"No working address for {host} to pin - nothing resolved or answered.")
            sys.exit(1)
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
            with _reporting():
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
