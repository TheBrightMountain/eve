"""`eve aws project` - the things you deploy, and which one is current.

Each project is one JSON file in ~/.config/eve/aws/projects/ holding its
account, type and the AWS IDs eve needs. Nothing lives in the project's repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import rich_click as click
from rich.table import Table

from eve.aws import state as statefile
from eve.aws import types as projtypes
from eve.aws.account import REMOTE, _login, profiles
from eve.aws.config import KEYS, Project, find_ids_file, from_ids_values, parse_ids, set_value, unset_value
from eve.aws.context import header, identity, reporting, runner
from eve.aws.runner import AwsError
from eve.console import console, info, success, warning

# Machine-specific fields left out of `export`.
NOT_SHARED = ("path",)


def _current_or(name):
    name = name or statefile.load().get("current_project")
    if not name:
        raise AwsError("No project given and none is current.", "eve aws project enter <name>")
    return name


def _check_account(ctx, account):
    if account and account not in profiles(ctx):
        warning(f"No account {account!r} yet - add it with `eve aws account add {account} --region <region>`.")


def _warn_missing_account_id(data):
    if not data.get("account_id"):
        warning(
            "account_id is not set - add it so eve refuses to act on the wrong account: "
            f"eve aws project set {data['name']} account_id <12-digit id>"
        )


def _read_source(source):
    """A JSON project file, or an old KEY=VALUE ids.txt."""
    path = Path(source).expanduser()
    if not path.exists():
        raise AwsError(f"{path} not found.")
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_ids": parse_ids(text)}


@click.group("project")
def project_group():
    """The projects you work on, each tied to an account.

    - `ls` · `show [<name>]` · `keys` - list projects / print one as JSON / the keys a project can have
    - `add <name> --type T --account A [--path P] [--from FILE]` - create one
    - `set <name> <key> <value...>` · `unset <name> <key>` - change its IDs
    - `edit <name> [--name N] [--path P] [--type T] [--account A]` - no options opens it in your editor
    - `enter <name>` · `leave` - make it current (switching to and signing in to its account) / clear that
    - `export <name>` · `import <file>` - share a project as JSON
    - `rm <name>` · `types` - forget one / list project types

    Projects are stored in `~/.config/eve/aws/projects/<name>.json` - not in
    the project's repo, so nothing about your AWS setup gets committed.
    """


@project_group.command("ls")
def ls():
    """List projects."""
    names = statefile.project_names()
    if not names:
        info("No projects yet - eve aws project add <name> --type T --account A")
        return
    current = statefile.load().get("current_project")
    table = Table(box=None, pad_edge=False)
    for col in ("", "NAME", "TYPE", "ACCOUNT", "CLUSTER", "PATH"):
        table.add_column(col)
    for name in names:
        p = Project(statefile.load_project(name))
        mark = "[accent]*[/accent]" if name == current else ""
        table.add_row(mark, name, p.kind, p.account or "-", p.get("cluster", "-"), str(p.path or "-"))
    console.print(table)


@project_group.command("show")
@click.argument("name", required=False)
def show(name):
    """Print project `NAME` (default: the current one) as JSON."""
    with reporting():
        data = statefile.load_project(_current_or(name))
        console.print_json(json.dumps(data))
        console.print(f"[dim]{statefile.project_path(data['name'])}[/dim]")


@project_group.command("keys")
def keys():
    """List the keys a project can have."""
    for key, (kind, meaning) in KEYS.items():
        console.print(f"[accent]{key:<17}[/accent] [dim]{kind:<4}[/dim] {meaning}")


@project_group.command("add")
@click.argument("name")
@click.option("--type", "kind", default="ecs", show_default=True, type=click.Choice(projtypes.names()))
@click.option("--account", required=True, help="Account (from `eve aws account ls`) the project lives in.")
@click.option("--path", "path", type=click.Path(file_okay=False), help="Project folder (git repo), used by `push`.")
@click.option("--from", "source", type=click.Path(dir_okay=False), help="Start from a project JSON or an old ids.txt.")
@click.pass_context
def add(ctx, name, kind, account, path, source):
    """Create project `NAME`.

    Without `--from`, eve also picks up an old `ids.txt` in `--path` if there is one.
    """
    with reporting():
        if name in statefile.project_names():
            raise AwsError(f"Project {name!r} already exists.", f"eve aws project edit {name}")
        _check_account(ctx, account)
        folder = str(Path(path).expanduser().resolve()) if path else None
        if not source and folder and find_ids_file(folder):
            source = str(find_ids_file(folder))
            info(f"Importing {source}")
        loaded = _read_source(source) if source else {}
        if "_ids" in loaded:
            data = from_ids_values(name, kind, account, folder, loaded["_ids"])
        else:
            data = {**loaded, "name": name, "type": kind, "account": account}
            if folder:
                data["path"] = folder
        statefile.save_project(data)
        success(f"Project {name} added ({kind}, account {account}).")
        _warn_missing_account_id(data)
        if not data.get("cluster"):
            info(f"Fill it in with `eve aws project set {name} <key> <value>` or `eve aws project edit {name}`.")


@project_group.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("values", nargs=-1, required=True)
def set_(name, key, values):
    """Set `KEY` on project `NAME` (see `eve aws project keys`).

    \b
    eve aws project set moodle-lab cluster moodle
    eve aws project set moodle-lab services moodle-web moodle-cron
    eve aws project set moodle-lab containers.web php
    eve aws project set moodle-lab recipes.hello.cmd echo hi
    """
    with reporting():
        data = statefile.load_project(name)
        set_value(data, key, values)
        statefile.save_project(data)
        success(f"{name}: {key} = {' '.join(values)}")


@project_group.command("unset")
@click.argument("name")
@click.argument("key")
def unset(name, key):
    """Remove `KEY` from project `NAME`."""
    with reporting():
        data = statefile.load_project(name)
        unset_value(data, key)
        statefile.save_project(data)
        success(f"{name}: {key} removed")


@project_group.command("edit")
@click.argument("name")
@click.option("--name", "new_name", help="Rename the project.")
@click.option("--path", "path", type=click.Path(file_okay=False), help="Change its folder.")
@click.option("--type", "kind", type=click.Choice(projtypes.names()), help="Change its type.")
@click.option("--account", help="Tie it to another account.")
@click.pass_context
def edit(ctx, name, new_name, path, kind, account):
    """Change project `NAME`. With no options, open its JSON in your editor."""
    with reporting():
        data = statefile.load_project(name)
        if not any((new_name, path, kind, account)):
            _edit_in_editor(name)
            return
        if new_name and new_name != name and new_name in statefile.project_names():
            raise AwsError(f"A project called {new_name!r} already exists.")
        changes = []
        if path:
            folder = str(Path(path).expanduser().resolve())
            changes.append(f"path {data.get('path')} → {folder}")
            data["path"] = folder
        if kind:
            changes.append(f"type {data.get('type')} → {kind}")
            data["type"] = kind
        if account:
            _check_account(ctx, account)
            changes.append(f"account {data.get('account')} → {account}")
            data["account"] = account
        state = statefile.load()
        if new_name and new_name != name:
            changes.insert(0, f"name {name} → {new_name}")
            statefile.delete_project(name)
            data["name"] = new_name
        statefile.save_project(data)
        if state.get("current_project") == name:
            state["current_project"] = data["name"]
            if account:
                state["current_account"] = account
            statefile.save(state)
        success(f"Project {data['name']} updated")
        for change in changes:
            info(change)


def _edit_in_editor(name):
    path = statefile.project_path(name)
    before = path.read_text(encoding="utf-8")
    click.edit(filename=str(path))
    after = path.read_text(encoding="utf-8")
    try:
        data = json.loads(after)
    except json.JSONDecodeError as exc:
        path.write_text(before, encoding="utf-8")
        raise AwsError(
            f"That wasn't valid JSON ({exc}) - your edit was undone.", f"eve aws project edit {name}"
        ) from exc
    if data.get("name", name) != name:
        path.write_text(before, encoding="utf-8")
        raise AwsError("Rename with `eve aws project edit <name> --name NEW`, not in the file - edit undone.")
    success(f"Saved {name}" if after != before else "No changes.")


@project_group.command("export")
@click.argument("name")
@click.option("-o", "--output", type=click.Path(dir_okay=False), help="Write to this file instead of the screen.")
def export(name, output):
    """Print project `NAME` as shareable JSON (without this machine's folder path)."""
    with reporting():
        data = {k: v for k, v in statefile.load_project(name).items() if k not in NOT_SHARED}
        text = json.dumps(data, indent=2) + "\n"
        if output:
            Path(output).expanduser().write_text(text, encoding="utf-8")
            success(f"Wrote {output}")
        else:
            click.echo(text, nl=False)


@project_group.command("import")
@click.argument("source", type=click.Path(dir_okay=False))
@click.option("--name", help="Save it under this name (default: the name in the file).")
@click.option("--account", help="Use this account instead of the one in the file.")
@click.option("--path", "path", type=click.Path(file_okay=False), help="This machine's folder for the project.")
@click.option("--force", is_flag=True, help="Replace a project with the same name.")
@click.pass_context
def import_(ctx, source, name, account, path, force):
    """Add a project from a JSON file made by `eve aws project export`."""
    with reporting():
        loaded = _read_source(source)
        if "_ids" in loaded:
            raise AwsError("That's an old ids.txt.", "Use `eve aws project add <name> --from <file> ...` for those.")
        data = dict(loaded)
        data["name"] = name or data.get("name")
        if not data["name"]:
            raise AwsError("The file has no name.", "Pass --name.")
        if data["name"] in statefile.project_names() and not force:
            raise AwsError(f"Project {data['name']!r} already exists.", "Pass --force to replace it, or --name.")
        if account:
            data["account"] = account
        if path:
            data["path"] = str(Path(path).expanduser().resolve())
        data.setdefault("type", "ecs")
        _check_account(ctx, data.get("account"))
        statefile.save_project(data)
        success(f"Project {data['name']} imported ({data['type']}, account {data.get('account')}).")
        _warn_missing_account_id(data)


@project_group.command("rm")
@click.argument("name")
def rm(name):
    """Forget project `NAME` (deletes eve's file only - nothing in AWS or your repo)."""
    with reporting():
        statefile.load_project(name)
        statefile.delete_project(name)
        state = statefile.load()
        if state.get("current_project") == name:
            state["current_project"] = None
            statefile.save(state)
        success(f"Project {name} forgotten.")


@project_group.command("enter")
@click.argument("name")
@click.option("--no-login", is_flag=True, help="Don't sign in automatically if the account's session has expired.")
@REMOTE
@click.pass_context
def enter(ctx, name, no_login, remote):
    """Make `NAME` the current project, switch to its account, and sign in if needed."""
    with reporting():
        project = Project(statefile.load_project(name))
        if project.account and project.account not in profiles(ctx):
            raise AwsError(
                f"Project {name} uses account {project.account!r}, which doesn't exist.",
                f"eve aws account add {project.account} --region <region>   (or eve aws project edit {name} --account A)",
            )
        state = statefile.load()
        state["current_project"] = name
        if project.account:
            state["current_account"] = project.account
        statefile.save(state)
        r = runner(ctx, project.account)
        header(project, r)
        who = identity(r)
        if who is None and project.account and not no_login:
            info(f"Account {project.account} isn't signed in - signing in now")
            _login(ctx, project.account, remote)
            who = identity(r)
        if who:
            expected = project.get("account_id")
            if expected and who["Account"] != expected:
                warning(f"Signed in to {who['Account']}, but {name} lives in {expected} - actions will refuse to run.")
            success(f"Entered {name} - signed in to {who['Account']} as {who['Arn'].rsplit('/', 1)[-1]}")
        else:
            warning(f"Entered {name}, but account {project.account} is not signed in.")
            info(f"eve aws account login {project.account}")


@project_group.command("leave")
def leave():
    """Clear the current project."""
    state = statefile.load()
    name = state.get("current_project")
    state["current_project"] = None
    statefile.save(state)
    success(f"Left {name}." if name else "No project was current.")


@project_group.command("types")
def types_():
    """List project types and what each adds."""
    for name in projtypes.names():
        kind = projtypes.get(name)
        console.print(f"[accent]{name}[/accent] - {kind['description']}")
        for rname, recipe in kind["recipes"].items():
            console.print(f"    run {rname:<15} {recipe.get('help', '')}")
