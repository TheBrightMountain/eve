"""The things you can do to a route."""

from __future__ import annotations

import contextlib
import json as jsonlib
import os
import sys

import rich_click as click

from eve.console import console, error, info, step, success, warning
from eve.route import dpi, ledger, paths, preset, probe, reconcile, render, strategy

TIMEOUT = click.option("-t", "--timeout", default=6.0, show_default=True, help="Per-connection timeout (seconds).")
DRY_RUN = click.option("-n", "--dry-run", is_flag=True, help="Say what would change, change nothing.")

FIXABLE = {probe.DNS_POISONED: "pin", probe.SNI_BLOCKED: "dpi"}

# What `auto` falls back to when the diagnosis found nothing wrong. The checker
# is not infallible - an intermittent block, a lucky handshake, or a resolver
# that answers differently for one query can all make a blocked host look fine
# - so a clean verdict is a reason to warn, not a reason to refuse.
WHEN_NOTHING_WRONG = "pin"


class Refused(Exception):
    """One host eve will not or cannot add. `add` stops on it; `import` moves on."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


def _choose_method(rep, requested, whole_domain=False):
    """Decide which fix to apply, or bail out if there is nothing sensible.

    An explicit `--method` always wins: it exists precisely for the times the
    diagnosis is wrong, so it must not be second-guessed here.

    A whole-domain entry can only be dpi - a pin covers one exact name, so it
    could never reach the subdomains the entry exists for.
    """
    verdict = rep["verdict"]

    if requested != "auto":
        warning(f"Forcing [accent]{requested}[/accent] - ignoring the diagnosis ({verdict}).")
        return requested

    if whole_domain and verdict in (probe.OPEN, probe.DNS_POISONED, probe.SNI_BLOCKED):
        if verdict != probe.SNI_BLOCKED:
            warning(f"{rep['host']} looks {verdict}, but only the DPI bypass covers a whole domain - using it anyway.")
        return "dpi"

    if verdict in FIXABLE:
        return FIXABLE[verdict]

    if verdict == probe.OPEN:
        warning(f"{rep['host']} looks reachable, but the check is not infallible - continuing anyway.")
        return WHEN_NOTHING_WRONG

    raise Refused(
        f"{probe.VERDICT_TEXT[verdict]} - this needs a tunnel or VPN, which eve will not set up for you.",
        hint="If you know better, force one with `--method pin` or `--method dpi`.",
    )


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

    A host eve is already holding open is called out as such, so a reachable
    verdict never gets mistaken for "nothing was ever wrong here".

    A single probe measures something that is not stable - a host can read
    `open` and then `sni-blocked` a minute later. Pass `-n` to sample several
    times; the worst verdict seen is the one reported, because an intermittent
    block is still a block.

    Pure Python sockets - no privileges, same behaviour on Windows and Linux.
    """
    # The ledger is world-readable on purpose, so this stays privilege-free.
    book = ledger.load()
    reports = [{**_sample(host, timeout, not no_vn, samples), "held": ledger.holding(book, host)} for host in hosts]

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
@click.option(
    "--probe",
    "probe_name",
    metavar="NAME",
    help="Cover all of `HOST`'s subdomains, testing this real one in its place. DPI only.",
)
@TIMEOUT
@DRY_RUN
def add(host, requested, probe_name, timeout, dry_run):
    """Make `HOST` reachable, and remember how.

    Diagnoses first, then applies the fix that diagnosis calls for: a poisoned
    name gets its real address pinned, a name-keyed DPI block gets the
    packet-level bypass. The result is recorded so `eve route sync` can keep it
    working when a CDN moves.

    A clean verdict does **not** stop it. The checker can be wrong - an
    intermittent block or one lucky handshake is enough - so a host that looks
    reachable is pinned anyway, with a warning. Use `--method` when you do not
    trust the diagnosis at all.

    To cover a whole domain whose bare name is not a site - a CDN like
    `steamcontent.com` with rotating `cacheN-...` hosts - pass `--probe` with
    one real subdomain. The DPI entry then covers every subdomain, and that one
    is what gets tested:

        eve route add steamcontent.com --probe cache1-hkg1.steamcontent.com
    """
    book = ledger.load()
    backend = dpi.backend()
    try:
        rep = _admit(book, backend, host, requested, probe_name, timeout, dry_run)
    except Refused as exc:
        error(str(exc))
        if exc.hint:
            info(exc.hint)
        sys.exit(1)

    if _apply(book, backend, dry_run, "Editing hosts"):
        step("Verify")
        _verify(host, probe_name, timeout)
    return rep


def _admit(book, backend, host, requested, probe_name, timeout, dry_run):
    """Diagnose `host`, pick its fix and record it in `book` - but apply nothing.

    The caller applies once afterwards, so `import` can admit a whole list and
    reconcile the machine a single time. Raises `Refused` for a host that
    cannot be added; `book` is left as it was in that case.
    """
    if "*" in host:
        raise Refused(
            f"{host}: wildcards are not needed - a DPI entry already covers every subdomain.",
            hint="Name the domain and a real host under it, e.g. "
            "`eve route add steamcontent.com --probe cache1-hkg1.steamcontent.com`.",
        )
    if probe_name:
        if probe_name != host and not probe_name.endswith(f".{host}"):
            raise Refused(f"--probe {probe_name} is not under {host}, so it cannot stand in for it.")
        if requested == "pin":
            raise Refused(
                "--probe covers a whole domain, which only the DPI bypass can do - a pin covers one exact name."
            )
    target = probe_name or host

    step(f"eve route add {host}" + (f" (testing {target})" if probe_name else ""))
    with console.status(f"[info]diagnosing {target}...[/info]", spinner="dots"):
        rep = probe.diagnose(target, timeout=timeout)

    verdict = rep["verdict"]
    console.print(f"Diagnosis: [bold]{verdict}[/bold] - {probe.VERDICT_TEXT[verdict]}")

    method = _choose_method(rep, requested, whole_domain=bool(probe_name))

    if method == "pin":
        if not rep["best"]:
            raise Refused(f"No working address for {host} to pin - nothing resolved or answered.")
        ledger.add_entry(book, host, method="pin", verdict=verdict, address=rep["best"])
        return rep

    ok, reason = backend.available()
    if not ok:
        raise Refused(f"The DPI bypass is not available here: {reason}")
    if dry_run:
        ledger.add_entry(book, host, method="dpi", verdict=verdict, strategy=strategy.LADDER[0], probe=probe_name)
        return rep

    _require_admin("Installing the DPI bypass")
    with _reporting():
        backend.install()
        hosts = sorted(set(ledger.by_method(book, "dpi")) | {host})
        step("Finding a strategy that gets through")
        found = strategy.find(
            target,
            backend,
            hosts,
            verify=lambda h: probe.reachable(h, timeout=timeout),
            preferred=reconcile.desired(book)["strategy"],
            on_try=lambda c: info(f"trying [accent]{c}[/accent]"),
        )
    if not found:
        # The ledger is untouched, so reconciling against it puts back
        # exactly what was running before - the hosts already being
        # bypassed keep working, rather than losing the service too.
        with _reporting(RECOVERY_HINT):
            reconcile.apply(book, backend)
        raise Refused(f"No strategy in the ladder got through to {target}. Nothing else was changed.")
    success(f"Strategy that works: [accent]{found}[/accent]")
    ledger.add_entry(book, host, method="dpi", verdict=verdict, strategy=found, probe=probe_name)
    return rep


def _verify(host, probe_name, timeout):
    target = probe_name or host
    with console.status(f"[info]re-checking {target}...[/info]", spinner="dots"):
        ok = probe.reachable(target, timeout=timeout)
    if ok:
        success(f"{target} is reachable now" + (f" - and so is the rest of {host}" if probe_name else ""))
    else:
        warning(f"{target} still does not answer. Run `eve route check {target} -a` for detail.")
    return ok


@click.command("import")
@click.argument("source", required=False)
@click.option("-l", "--list", "list_presets", is_flag=True, help="Show the presets that ship with eve.")
@TIMEOUT
@DRY_RUN
def import_(source, list_presets, timeout, dry_run):
    """Add every route in a preset or a file, in one go.

    `SOURCE` is the name of a preset that ships with eve (`eve route import
    --list` shows them) or the path to a file of your own in the same format,
    one entry per line:

        steamcontent.com  cache1-hkg1.steamcontent.com
        steamcdn-a.akamaihd.net

    A second column is a probe, exactly like `add --probe`: the entry covers
    the whole domain and that real host is what gets tested. `#` starts a
    comment.

    Each entry is diagnosed and fixed just as `add` would. One that cannot be
    added is reported and skipped rather than stopping the rest, and hosts eve
    already holds are left alone - `eve route sync` re-checks those.
    """
    if list_presets:
        for name, summary in preset.available():
            console.print(f"[accent]{name}[/accent]  {summary}")
        return None
    if not source:
        error("Name a preset or a file - `eve route import --list` shows the presets.")
        sys.exit(1)

    try:
        entries = preset.load(source)
    except (ValueError, OSError) as exc:
        error(str(exc))
        sys.exit(1)
    if not dry_run:
        _require_admin("Importing routes")

    book = ledger.load()
    backend = dpi.backend()
    added, failed = [], []
    for host, probe_name in entries:
        if host in book["entries"]:
            info(f"{host} is already held - skipping. `eve route sync` re-checks it.")
            continue
        try:
            _admit(book, backend, host, "auto", probe_name, timeout, dry_run)
            added.append((host, probe_name))
        except Refused as exc:
            error(f"{host}: {exc}")
            failed.append(host)

    if added and _apply(book, backend, dry_run, "Importing routes"):
        step("Verify")
        for host, probe_name in added:
            _verify(host, probe_name, timeout)

    summary = f"{len(added)} added, {len(entries) - len(added) - len(failed)} already held, {len(failed)} failed"
    (warning if failed else success)(f"Imported {source}: {summary}.")
    if failed:
        sys.exit(1)
    return added


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
        target = ledger.probe_host(host, entry)
        with console.status(f"[info]re-checking {target}...[/info]", spinner="dots"):
            rep = probe.diagnose(target, timeout=timeout)
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
