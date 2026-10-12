"""`eve aws account` - the AWS accounts you sign in to.

Each account is a normal AWS CLI profile in ~/.aws/config, signed in with
`aws login`. eve only remembers which one is current.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import rich_click as click
from rich.table import Table

from eve.aws import state as statefile
from eve.aws.context import identity, reporting, runner
from eve.aws.runner import AwsError
from eve.console import console, info, success, warning


def config_path():
    return Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")


def profiles(ctx):
    res = runner(ctx).aws("configure", "list-profiles", profile=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _headless():
    if os.environ.get("SSH_CONNECTION"):
        return True
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return True
    return False


def _login(ctx, name, remote):
    use_remote = _headless() if remote is None else remote
    args = ["login", "--profile", name] + (["--remote"] if use_remote else [])
    runner(ctx).interactive(["aws", *args], mutating=True)


def remove_profile_section(path, name):
    """Drop `[profile name]` (or `[default]`) and its lines from the config file."""
    header = "[default]" if name == "default" else f"[profile {name}]"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, skipping, removed = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == header
            removed = removed or skipping
        if not skipping:
            out.append(line)
    if removed:
        path.write_text("".join(out), encoding="utf-8")
    return removed


REMOTE = click.option(
    "--remote/--browser",
    default=None,
    help="Sign in by pasting a code (remote) or with a local browser. Default: remote when there is no display.",
)


@click.group("account")
def account_group():
    """The AWS accounts you sign in to (AWS CLI profiles).

    - `ls` - every account, which one is current, and whether it's signed in
    - `add <name> --region R` - save a new account and sign in
    - `login <name>` - make it current, signing in only if needed
    - `logout [<name>]` - sign out
    - `rm <name>` - forget an account
    """


@account_group.command("ls")
@click.pass_context
def ls(ctx):
    """List accounts, their account IDs and whether each is signed in."""
    with reporting():
        state = statefile.load()
        table = Table(box=None, pad_edge=False)
        for col in ("", "NAME", "ACCOUNT", "REGION", "STATUS"):
            table.add_column(col)
        for name in profiles(ctx):
            r = runner(ctx, name)
            region = r.aws_text("configure", "get", "region", check=False) or "-"
            who = identity(r)
            status = "[success]signed in[/success]" if who else "[warning]signed out / expired[/warning]"
            current = "[accent]*[/accent]" if name == state.get("current_account") else ""
            table.add_row(current, name, who["Account"] if who else "-", region, status)
        console.print(table)


@account_group.command("add")
@click.argument("name")
@click.option("--region", required=True, help="Default region for this account, e.g. ap-southeast-2.")
@REMOTE
@click.pass_context
def add(ctx, name, region, remote):
    """Save a new account as profile `NAME` and sign in to it."""
    with reporting():
        if name in profiles(ctx):
            raise AwsError(f"Account {name!r} already exists.", f"eve aws account login {name}")
        runner(ctx, name).aws("configure", "set", "region", region, mutating=True)
        _login(ctx, name, remote)
        state = statefile.load()
        state["current_account"] = name
        if not runner(ctx).dry_run:
            statefile.save(state)
        success(f"Account {name} saved and current.")


@account_group.command("login")
@click.argument("name")
@REMOTE
@click.pass_context
def login(ctx, name, remote):
    """Make `NAME` the current account. Signs in only if its session is missing or expired."""
    with reporting():
        if name not in profiles(ctx):
            raise AwsError(f"No account {name!r}.", f"eve aws account add {name} --region <region>")
        who = identity(runner(ctx, name))
        if who:
            info(f"Already signed in as {who['Arn']}")
        else:
            _login(ctx, name, remote)
        state = statefile.load()
        state["current_account"] = name
        if not runner(ctx).dry_run:
            statefile.save(state)
        success(f"Current account: {name}")


@account_group.command("logout")
@click.argument("name", required=False)
@click.pass_context
def logout(ctx, name):
    """Sign out of `NAME` (default: the current account)."""
    with reporting():
        name = name or statefile.load().get("current_account")
        if not name:
            raise AwsError("No account given and none is current.", "eve aws account logout <name>")
        runner(ctx, name).aws("logout", mutating=True)
        success(f"Signed out of {name}")


@account_group.command("rm")
@click.argument("name")
@click.option("--force", is_flag=True, help="Remove it even if a project still uses it.")
@click.pass_context
def rm(ctx, name, force):
    """Forget account `NAME`: sign out and remove its profile from ~/.aws/config."""
    with reporting():
        state = statefile.load()
        users = statefile.projects_using(state, name)
        if users and not force:
            raise AwsError(f"Projects still use {name}: {', '.join(users)}.", "Remove them first, or pass --force.")
        r = runner(ctx, name)
        r.aws("logout", mutating=True, check=False)
        if r.dry_run:
            r.show(["edit", str(config_path()), f"(remove [profile {name}])"], prefix="would run:")
            return
        if not remove_profile_section(config_path(), name):
            warning(f"No [profile {name}] section found in {config_path()}.")
        if state.get("current_account") == name:
            state["current_account"] = None
            statefile.save(state)
        success(f"Account {name} removed.")
