"""`eve device ...`, `eve wake` and `eve ssh`."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import ClassVar

import rich_click as click
from rich.markup import escape
from rich.table import Table

from eve.console import console, error, info, success, warning
from eve.device import net, store
from eve.device.store import FIELDS, DeviceError


@contextlib.contextmanager
def reporting():
    try:
        yield
    except DeviceError as exc:
        error(str(exc))
        if exc.hint:
            console.print(f"  [dim]{exc.hint}[/dim]")
        sys.exit(1)


def field_options(fn):
    """The `--mac`, `--ip`, ... options shared by `add` and `edit`."""
    for field in reversed(list(FIELDS)):
        metavar = {"port": "N", "mac": "MAC"}.get(field, field.upper())
        fn = click.option(f"--{field}", field, metavar=metavar, help=FIELDS[field].capitalize() + ".")(fn)
    return fn


def _given(values):
    return {field: store.clean(field, value) for field, value in values.items() if value is not None}


def _check_aliases(devices, aliases, owner):
    for alias in aliases:
        store.check_name(alias, "alias")
        clash = store.taken(devices, alias, ignore=owner)
        if clash:
            raise DeviceError(f"{alias!r} already belongs to {clash}.")


def _ssh_host(device):
    return device.get("host") or device.get("ip")


def _describe(name, device):
    console.print(f"[accent]{name}[/accent]")
    if device.get("aliases"):
        console.print(f"  [dim]{'aliases':<10}[/dim] {', '.join(device['aliases'])}")
    for field in FIELDS:
        if field in device:
            console.print(f"  [dim]{field:<10}[/dim] {device[field]}")


# -- eve device ------------------------------------------------------------------------------------


class AliasedGroup(click.RichGroup):
    ALIASES: ClassVar[dict] = {"rm": "remove", "list": "ls"}

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self.ALIASES.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        name, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd else name), cmd, rest


@click.group("device", cls=AliasedGroup)
def device_group():
    """The machines you wake and ssh into, known by name.

    - `ls` · `show <name>` - list them / print one
    - `add <name> [-a ALIAS]... [--mac M] [--ip IP] [--host H] [--user U] ...` - remember one
    - `edit <name> [--rename N] [-a ALIAS] [--drop-alias A] [--mac M] ... [--unset FIELD]` - change it
    - `remove <name>` (or `rm`) - forget it

    Then `eve wake <name>` and `eve ssh <name>`. A device answers to its name or
    any alias. Kept in `~/.config/eve/devices.json`.
    """


@device_group.command("ls")
def ls():
    """List devices."""
    with reporting():
        devices = store.load()
    if not devices:
        info("No devices yet - eve device add <name> --mac M --ip IP")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("NAME", "ALIASES", "MAC", "IP", "SSH"):
        table.add_column(col, style="accent" if col == "NAME" else None)
    for name in sorted(devices):
        d = devices[name]
        host = _ssh_host(d)
        target = f"{d['user']}@{host}" if host and d.get("user") else (host or "-")
        if host and d.get("port", 22) != 22:
            target += f":{d['port']}"
        table.add_row(name, ", ".join(d.get("aliases", [])) or "-", d.get("mac", "-"), d.get("ip", "-"), target)
    console.print(table)


@device_group.command("show")
@click.argument("name")
def show(name):
    """Print device `NAME`."""
    with reporting():
        _describe(*store.resolve(store.load(), name))


@device_group.command("add")
@click.argument("name")
@click.option("-a", "--alias", "aliases", multiple=True, metavar="ALIAS", help="Another name for it (repeatable).")
@field_options
def add(name, aliases, **values):
    """Remember device `NAME`.

    Every field is optional, but `eve wake` needs `--mac` and `eve ssh` needs
    `--host` or `--ip`.
    """
    with reporting():
        devices = store.load()
        store.check_name(name)
        clash = store.taken(devices, name)
        if clash:
            raise DeviceError(f"{name!r} is already taken by {clash}.", f"eve device edit {clash}")
        _check_aliases(devices, aliases, None)
        device = _given(values)
        if aliases:
            device["aliases"] = list(dict.fromkeys(aliases))
        devices[name] = device
        where = store.save(devices)
        success(f"Device {name} added.")
        _describe(name, device)
        console.print(f"[dim]{where}[/dim]")


@device_group.command("edit")
@click.argument("name")
@click.option("--rename", metavar="NEW", help="Give it a new name.")
@click.option("-a", "--alias", "aliases", multiple=True, metavar="ALIAS", help="Add an alias (repeatable).")
@click.option("--drop-alias", "dropped", multiple=True, metavar="ALIAS", help="Remove an alias (repeatable).")
@click.option(
    "--unset",
    "unset",
    multiple=True,
    type=click.Choice(list(FIELDS)),
    help="Clear a field (repeatable).",
)
@field_options
def edit(name, rename, aliases, dropped, unset, **values):
    """Change device `NAME` (or alias). Only the options you give change."""
    with reporting():
        devices = store.load()
        name, device = store.resolve(devices, name)
        changes = _given(values)
        both = set(changes) & set(unset)
        if both:
            raise DeviceError(f"Cannot both set and unset {', '.join(sorted(both))}.")
        if not (rename or aliases or dropped or unset or changes):
            raise DeviceError("Nothing to change.", "eve device edit -h")

        device = dict(device)
        device.update(changes)
        for field in unset:
            device.pop(field, None)

        current = list(device.get("aliases", []))
        missing = [a for a in dropped if a.lower() not in (c.lower() for c in current)]
        if missing:
            raise DeviceError(f"{name} has no alias {', '.join(missing)}.")
        current = [c for c in current if c.lower() not in (a.lower() for a in dropped)]

        new_name = name
        if rename:
            store.check_name(rename)
            clash = store.taken(devices, rename, ignore=name)
            if clash:
                raise DeviceError(f"{rename!r} is already taken by {clash}.")
            new_name = rename
        _check_aliases(devices, aliases, name)
        current = list(dict.fromkeys(current + [a for a in aliases if a.lower() != new_name.lower()]))
        if current:
            device["aliases"] = current
        else:
            device.pop("aliases", None)

        del devices[name]
        devices[new_name] = device
        store.save(devices)
        success(f"Device {new_name} updated." if new_name == name else f"Device {name} is now {new_name}.")
        _describe(new_name, device)


@device_group.command("remove")
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Don't ask first.")
def remove(name, yes):
    """Forget device `NAME` (or alias)."""
    with reporting():
        devices = store.load()
        name, _ = store.resolve(devices, name)
        if not yes and not click.confirm(f"Forget {name}?", default=False):
            info("Kept it.")
            return
        del devices[name]
        store.save(devices)
        success(f"Device {name} removed.")


# -- eve wake / eve ssh ----------------------------------------------------------------------------


@click.command("wake")
@click.argument("name")
@click.option("-w", "--wait", is_flag=True, help="Then wait until it answers on the network.")
@click.option("-t", "--timeout", default=180, show_default=True, metavar="SECONDS", help="How long `--wait` waits.")
def wake(name, wait, timeout):
    """Send a wake-on-LAN packet to device `NAME` (or alias).

    The packet is a LAN broadcast, so this machine must be on the same network
    as the device - Tailscale and other VPNs don't carry it. The device needs
    wake-on-LAN enabled in its BIOS/UEFI and, on Windows, in the network
    adapter's power settings.
    """
    with reporting():
        name, device = store.resolve(store.load(), name)
        if not device.get("mac"):
            raise DeviceError(f"{name} has no MAC address, so it can't be woken.", f"eve device edit {name} --mac M")
        targets = net.broadcast_targets(device)
        try:
            net.send_magic_packet(device["mac"], targets)
        except OSError as exc:
            raise DeviceError(f"Could not send the wake packet: {exc}") from exc
        success(f"Wake packet sent to {name} ({device['mac']}) via {', '.join(targets)}.")

        if not wait:
            return
        hosts = list(dict.fromkeys(h for h in (device.get("ip"), device.get("host")) if h))
        if not hosts:
            warning(f"{name} has no ip or host to watch - not waiting.")
            return
        deadline = time.monotonic() + timeout
        with console.status(f"Waiting for {name} to come up..."):
            while time.monotonic() < deadline:
                for host in hosts:
                    if net.ping(host):
                        success(f"{name} is up ({host}).")
                        return
                time.sleep(2)
        error(f"{name} did not answer within {timeout}s.")
        console.print("  [dim]Is wake-on-LAN on in its BIOS and network adapter, and is it plugged in by cable?[/dim]")
        sys.exit(1)


@click.command(
    "ssh",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.argument("name")
@click.argument("ssh_args", nargs=-1, type=click.UNPROCESSED)
@click.option("-n", "--dry-run", is_flag=True, help="Print the ssh command instead of running it.")
def ssh(name, ssh_args, dry_run):
    """Open ssh to device `NAME` (or alias).

    Anything after the name goes to ssh as is: `eve ssh bs uptime`,
    `eve ssh bs -L 8080:localhost:80`.
    """
    with reporting():
        name, device = store.resolve(store.load(), name)
        host = _ssh_host(device)
        if not host:
            raise DeviceError(f"{name} has no host or ip to ssh to.", f"eve device edit {name} --host H")
        target = f"{device['user']}@{host}" if device.get("user") else host
        cmd = ["ssh"]
        if device.get("port") and device["port"] != 22:
            cmd += ["-p", str(device["port"])]
        cmd += [target, *ssh_args]
        console.print(f"[dim]$ {escape(shlex.join(cmd))}[/dim]", highlight=False, emoji=False)
        if dry_run:
            return
        if not shutil.which("ssh"):
            raise DeviceError("No ssh client found on PATH.")

    sys.stdout.flush()
    if os.name == "nt":
        sys.exit(subprocess.call(cmd))
    os.execvp("ssh", cmd)
