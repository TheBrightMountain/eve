"""`eve aws project` - the things you deploy, and which one is current."""

from __future__ import annotations

from pathlib import Path

import rich_click as click
from rich.table import Table

from eve.aws import state as statefile
from eve.aws import types as projtypes
from eve.aws.account import profiles
from eve.aws.config import Project, ids_file
from eve.aws.context import header, identity, reporting, runner
from eve.aws.runner import AwsError
from eve.console import console, info, success, warning


@click.group("project")
def project_group():
    """The projects you work on, each tied to an account.

    - `ls` - every project, which one is current
    - `add <name> --path P --type T --account A` - remember a project
    - `enter <name>` - make it current (and switch to its account)
    - `leave` - clear the current project
    - `rm <name>` - forget it (touches nothing in AWS or on disk)
    - `types` - the project types and what each adds

    A project's details (cluster, services, bucket...) are read from
    `<path>/aws/ids.txt`.
    """


@project_group.command("ls")
def ls():
    """List projects."""
    state = statefile.load()
    if not state["projects"]:
        info("No projects yet - eve aws project add <name> --path P --type T --account A")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("", "NAME", "TYPE", "ACCOUNT", "PATH"):
        table.add_column(col)
    for name, p in sorted(state["projects"].items()):
        current = "[accent]*[/accent]" if name == state.get("current_project") else ""
        table.add_row(current, name, p["type"], p.get("account") or "-", p["path"])
    console.print(table)


@project_group.command("add")
@click.argument("name")
@click.option("--path", "path", required=True, type=click.Path(file_okay=False), help="The project's folder.")
@click.option("--type", "kind", default="ecs", show_default=True, type=click.Choice(projtypes.names()))
@click.option("--account", required=True, help="Account (from `eve aws account ls`) the project lives in.")
@click.pass_context
def add(ctx, name, path, kind, account):
    """Remember project `NAME`."""
    with reporting():
        folder = Path(path).expanduser().resolve()
        if not ids_file(folder).exists():
            raise AwsError(f"{ids_file(folder)} not found.", "A project needs an aws/ids.txt with its IDs.")
        if account not in profiles(ctx):
            warning(f"No account {account!r} yet - add it with `eve aws account add {account} --region <region>`.")
        state = statefile.load()
        if name in state["projects"]:
            raise AwsError(f"Project {name!r} already exists.", f"eve aws project rm {name} first.")
        statefile.add_project(state, name, folder, kind, account)
        statefile.save(state)
        project = Project.load(name, state["projects"][name])
        if not project.get("ACCOUNT_ID"):
            warning("ids.txt has no ACCOUNT_ID - add it so eve can refuse to act on the wrong account.")
        success(f"Project {name} added ({kind}, account {account}).")


@project_group.command("rm")
@click.argument("name")
def rm(name):
    """Forget project `NAME`."""
    with reporting():
        state = statefile.load()
        if name not in state["projects"]:
            raise AwsError(f"Unknown project {name!r}.", "eve aws project ls")
        statefile.save(statefile.remove_project(state, name))
        success(f"Project {name} forgotten.")


@project_group.command("enter")
@click.argument("name")
@click.pass_context
def enter(ctx, name):
    """Make `NAME` the current project and switch to its account."""
    with reporting():
        state = statefile.load()
        if name not in state["projects"]:
            raise AwsError(f"Unknown project {name!r}.", "eve aws project ls")
        project = Project.load(name, state["projects"][name])
        state["current_project"] = name
        if project.account:
            state["current_account"] = project.account
        statefile.save(state)
        r = runner(ctx, project.account)
        header(project, r)
        who = identity(r)
        if who:
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
