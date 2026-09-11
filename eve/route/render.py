"""Everything that turns a report or a ledger into something readable."""

from __future__ import annotations

from rich.table import Table

from eve.console import console, error, info, success, warning
from eve.route import dns, probe

ADVICE = {
    probe.OPEN: "Nothing to do.",
    probe.DNS_POISONED: "Run `eve route add {host}` to pin the working address.",
    probe.SNI_BLOCKED: "hosts and DNS cannot help here. Run `eve route add {host}` for the DPI bypass.",
    probe.IP_BLOCKED: "The address answers TCP but nothing completes - you need a tunnel or VPN.",
    probe.UNREACHABLE: "No address resolved or answered. Check the name, then your link.",
}

VERDICT_STYLE = {
    probe.OPEN: "success",
    probe.DNS_POISONED: "warning",
    probe.SNI_BLOCKED: "error",
    probe.IP_BLOCKED: "error",
    probe.UNREACHABLE: "error",
}


def _table(*columns):
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column(columns[0], style="accent")
    for column in columns[1:]:
        table.add_column(column)
    return table


def _fmt_ips(ips):
    if not ips:
        return "[dim]empty[/dim]"
    return ", ".join(f"[error]{ip}[/error]" if dns.is_bogus(ip) else ip for ip in ips)


def _tls_note(row):
    if row["reset"]:
        return "[error]reset[/error]"
    if row["error"] and "timeout" in row["error"]:
        return "[error]timeout[/error]"
    return "[error]fail[/error]"


def report(rep, addresses=False):
    host, verdict = rep["host"], rep["verdict"]
    console.print()
    console.print(
        f"[bold]{host}[/bold]  [{VERDICT_STYLE[verdict]}]{verdict}[/{VERDICT_STYLE[verdict]}]"
        f" - {probe.VERDICT_TEXT[verdict]}"
    )

    if addresses:
        resolvers = _table("resolver", "answer")
        system = rep["system_ips"]
        resolvers.add_row("system", _fmt_ips(system) if system else "[error]no answer[/error]")
        for label, ips in rep["answers"].items():
            resolvers.add_row(label, "[dim]no reply[/dim]" if ips is None else _fmt_ips(ips))
        console.print()
        console.print(resolvers)

        if rep["probes"]:
            probes = _table("address", "tcp", "tls", "time")
            for row in rep["probes"]:
                probes.add_row(
                    row["ip"],
                    "[success]open[/success]" if row["tcp"] else "[error]no[/error]",
                    "[success]ok[/success]" if row["tls"] else _tls_note(row),
                    f"{row['ms']:.0f}ms" if row["ms"] is not None else "-",
                )
            console.print()
            console.print(probes)

    control = rep.get("control")
    if control is not None:
        console.print()
        if control["tls"]:
            info(
                f"Control test: the same address accepts SNI [accent]{control['sni']}[/accent]"
                " - the path is open, the name is what gets you blocked."
            )
        else:
            info(f"Control test: SNI [accent]{control['sni']}[/accent] fails too - the address is the problem.")

    message = ADVICE[verdict].format(host=host)
    console.print()
    if verdict == probe.OPEN:
        success(f"{host} is reachable. {message}")
    elif verdict == probe.DNS_POISONED:
        warning(f"DNS hands back a bogus address for {host}, but {rep['best']} works. {message}")
    else:
        error(f"{host}: {probe.VERDICT_TEXT[verdict]}. {message}")


def ledger_table(book, dpi_state):
    entries = book["entries"]
    if not entries:
        info("Nothing pinned or bypassed - eve is not holding any route open.")
    else:
        table = _table("host", "how", "detail", "last verified")
        for host in sorted(entries):
            entry = entries[host]
            detail = entry.get("address") or entry.get("strategy") or "-"
            table.add_row(host, entry["method"], detail, entry.get("verified", "-"))
        console.print()
        console.print(table)

    console.print()
    if dpi_state.get("running"):
        success("DPI bypass is running")
    elif dpi_state.get("installed"):
        warning("DPI bypass is installed but not running")
    else:
        info("DPI bypass is not installed.")
