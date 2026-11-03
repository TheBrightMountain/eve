"""`eve update`: bring eve itself up to date, from wherever it was installed.

uv records the source of every install in the package's direct_url.json:

- **git** (`uv tool install git+https://...`) - `uv tool upgrade` re-fetches the branch
- **a clone** (`./install.sh`, `uv tool install .`) - `git pull` there, then reinstall from it
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

import rich_click as click
from rich.markup import escape

from eve import __version__
from eve.console import console, error, info, step, success, warning


class UpdateError(Exception):
    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


def install_source():
    """How eve was installed: {"kind": "git"|"dir"|"other", ...}."""
    try:
        raw = distribution("eve").read_text("direct_url.json")
    except PackageNotFoundError:
        raw = None
    data = json.loads(raw) if raw else {}
    url = data.get("url", "")
    if "vcs_info" in data:
        return {"kind": "git", "url": url, "commit": data["vcs_info"].get("commit_id")}
    if "dir_info" in data and url.startswith("file:"):
        path = Path(unquote(urlparse(url).path))
        if sys.platform == "win32" and str(path).startswith("\\"):
            path = Path(str(path).lstrip("\\"))
        return {"kind": "dir", "path": path, "editable": bool(data["dir_info"].get("editable"))}
    return {"kind": "other"}


def plan(source, uv="uv", pull=True):
    """The commands that update this install, in order."""
    if source["kind"] == "git":
        return [[uv, "tool", "upgrade", "--reinstall", "eve"]]
    if source["kind"] == "dir":
        path = str(source["path"])
        cmds = []
        if pull and (source["path"] / ".git").exists():
            cmds.append(["git", "-C", path, "pull", "--ff-only"])
        install = [uv, "tool", "install", "--force", "--reinstall"]
        if source["editable"]:
            install.append("--editable")
        cmds.append([*install, path])
        return cmds
    raise UpdateError(
        "Can't tell where this eve was installed from, so there is nothing to update from.",
        "uv tool install --force git+https://github.com/TheBrightMountain/eve",
    )


def _show(cmd):
    console.print(f"  [dim]$ {escape(shlex.join(cmd))}[/dim]", highlight=False, emoji=False, soft_wrap=True)


def _new_version():
    exe = shutil.which("eve")
    if not exe:
        return None
    proc = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    return proc.stdout.strip().split()[-1] if proc.returncode == 0 and proc.stdout.strip() else None


@click.command("update")
@click.option("-n", "--dry-run", is_flag=True, help="Print the commands instead of running them.")
@click.option("--no-pull", is_flag=True, help="From a clone: reinstall what is checked out, without `git pull`.")
def update(dry_run, no_pull):
    """Update eve to the latest version, from wherever it was installed.

    Installed from GitHub: `uv tool upgrade` fetches the latest `main`. Installed
    from a clone: `git pull` there (fast-forward only), then reinstall from it.
    Either way the tool is rebuilt, which also repairs it after a Python upgrade.
    """
    try:
        source = install_source()
        uv = shutil.which("uv")
        if not uv and not dry_run:
            raise UpdateError("uv is not on PATH.", "https://docs.astral.sh/uv/getting-started/installation/")
        cmds = plan(source, uv or "uv", pull=not no_pull)
    except UpdateError as exc:
        error(str(exc))
        if exc.hint:
            console.print(f"  [dim]{exc.hint}[/dim]")
        sys.exit(1)

    where = source.get("url") or str(source.get("path"))
    info(f"eve {__version__}, installed from {where}")
    if source["kind"] == "dir" and no_pull:
        info("Skipping git pull - reinstalling what is checked out.")
    elif source["kind"] == "dir" and len(cmds) == 1:
        warning(f"{where} is not a git clone - reinstalling it as is.")

    for cmd in cmds:
        step(cmd[0] if cmd[0] == "git" else "uv tool")
        _show(cmd)
        if dry_run:
            continue
        sys.stdout.flush()
        if subprocess.run(cmd, check=False).returncode != 0:
            error(f"{Path(cmd[0]).name} failed - eve is unchanged from {__version__}.")
            sys.exit(1)

    if dry_run:
        return
    console.print()
    new = _new_version()
    if new and new != __version__:
        success(f"eve updated: {__version__} → {new}")
    elif new:
        success(f"eve is up to date ({new}), reinstalled.")
    else:
        success("eve reinstalled.")
