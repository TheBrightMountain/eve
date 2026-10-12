"""Shared plumbing for the `eve aws` commands: options, project lookup, errors."""

from __future__ import annotations

import contextlib
import json
import os
import sys

import rich_click as click

from eve.aws import state as statefile
from eve.aws.config import Project
from eve.aws.runner import AwsError, Runner
from eve.console import console, error, info, warning

PROJECT_OPTION = click.option(
    "-P", "--project", "project_name", metavar="NAME", help="Act on this project instead of the current one."
)


@contextlib.contextmanager
def reporting():
    """Turn an AwsError into a message and exit code 1 instead of a traceback."""
    try:
        yield
    except AwsError as exc:
        error(str(exc))
        if exc.hint:
            info(exc.hint)
        sys.exit(1)


def settings(ctx):
    obj = ctx.obj or {}
    return obj.get("quiet", False), obj.get("dry_run", False)


def runner(ctx, profile=None):
    quiet, dry_run = settings(ctx)
    return Runner(quiet=quiet, dry_run=dry_run, profile=profile)


def current_project_name(state, given=None):
    name = given or os.environ.get("EVE_AWS_PROJECT") or state.get("current_project")
    if not name:
        raise AwsError("No project selected.", "eve aws project enter <name>   (or pass -P <name>)")
    return name


def project_and_runner(ctx, given=None):
    state = statefile.load()
    project = Project(statefile.load_project(current_project_name(state, given)))
    profile = project.account or state.get("current_account")
    return project, runner(ctx, profile)


def header(project, runner_):
    console.print(f"[step]▸[/step] [bold]{project.name}[/bold] [dim]({project.kind} · account {runner_.profile})[/dim]")


def identity(runner_):
    """The signed-in identity for the runner's profile, or None."""
    res = runner_.aws("sts", "get-caller-identity", "--output", "json", check=False)
    if res is None or res.returncode != 0:
        return None
    return json.loads(res.stdout)


def safety_check(project, runner_):
    """Refuse to change anything when signed in to the wrong account."""
    expected = project.get("account_id")
    who = identity(runner_)
    if who is None:
        raise AwsError(
            f"Not signed in to account {runner_.profile!r}.", f"eve aws account login {runner_.profile or '<name>'}"
        )
    if not expected:
        warning(f"account_id is not set for {project.name} - skipping the wrong-account check.")
        info(f"eve aws project set {project.name} account_id {who['Account']}")
        return who
    if who["Account"] != expected:
        raise AwsError(
            f"Signed in to account {who['Account']}, but project {project.name} lives in {expected}.",
            f"eve aws account login <the account for {expected}>",
        )
    return who
