"""Actions on the current project: status, park, unpark, logs, exec, run, push, files."""

from __future__ import annotations

import shlex
import time
import urllib.error
import urllib.request

import rich_click as click
from rich.table import Table

from eve.aws import pick
from eve.aws import types as projtypes
from eve.aws.config import short_name
from eve.aws.context import PROJECT_OPTION, header, project_and_runner, reporting, safety_check
from eve.aws.runner import AwsError
from eve.console import console, info, step, success, warning

POLL_SECONDS = 30


def sleep(seconds):
    """Tests replace this."""
    time.sleep(seconds)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_status(url):
    """HTTP status of `url` without following redirects, or an error string. Tests replace this."""
    try:
        with urllib.request.build_opener(_NoRedirect).open(urllib.request.Request(url), timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return f"unreachable ({exc.__class__.__name__})"


# -- shared lookups --------------------------------------------------------------------------------


def _db_status(project, r):
    db = project.get("db_id")
    if not db:
        return None
    return r.aws_text(
        "rds", "describe-db-instances", "--db-instance-identifier", db, "--query", "DBInstances[0].DBInstanceStatus"
    )


def _set_desired(project, r, count):
    cluster = project.require("cluster")
    for service in project.services:
        r.aws(
            "ecs", "update-service", "--cluster", cluster, "--service", service,
            "--desired-count", str(count), "--query", "service.serviceName", mutating=True,
        )  # fmt: skip
    r.aws("ecs", "wait", "services-stable", "--cluster", cluster, "--services", *project.services, mutating=True)


def _site_check(project):
    url = project.get("site_url")
    if url:
        code = http_status(url)
        good = isinstance(code, int) and code < 500
        (success if good else warning)(f"{url} → {code}")


def _pick_service(project, given):
    default = project.get("default_service")
    return pick.choose(
        project.services,
        "service",
        given=given,
        default=default,
        label=lambda s: f"{short_name(s)} ({s})",
        match=lambda s, g: g in (s, short_name(s)),
    )


def _pick_task(project, r, service, given):
    cluster = project.require("cluster")
    arns = r.aws_json(
        "ecs", "list-tasks", "--cluster", cluster, "--service-name", service, "--desired-status", "RUNNING",
        "--query", "taskArns",
    )  # fmt: skip
    if not arns:
        raise AwsError(f"{service} has no running task.", "eve aws unpark   (or check `eve aws status`)")
    labels = {arn: arn.rsplit("/", 1)[-1] for arn in arns}
    if len(arns) > 1 and not given:
        described = r.aws_json(
            "ecs", "describe-tasks", "--cluster", cluster, "--tasks", *arns,
            "--query", "tasks[].[taskArn,taskDefinitionArn,availabilityZone,startedAt]",
        )  # fmt: skip
        for arn, taskdef, az, started in described or []:
            labels[arn] = f"{arn.rsplit('/', 1)[-1][:12]}  rev {taskdef.rsplit(':', 1)[-1]}  {az}  started {started}"
    return pick.choose(
        arns, "task", given=given, label=labels.get, match=lambda a, g: a.rsplit("/", 1)[-1].startswith(g)
    )


def _pick_container(project, r, service, task, given):
    default = project.container_for(service) or projtypes.get(project.kind)["container"]
    if given or default:
        return given or default
    names = r.aws_json(
        "ecs", "describe-tasks", "--cluster", project.require("cluster"), "--tasks", task,
        "--query", "tasks[0].containers[].name",
    )  # fmt: skip
    return pick.choose(names or [], "container")


def _exec(project, r, service, task_given, container_given, command, user):
    safety_check(project, r)
    task = _pick_task(project, r, service, task_given)
    container = _pick_container(project, r, service, task, container_given)
    if user:
        command = f"su -s /bin/sh {user} -c {shlex.quote(command)}"
    r._need("session-manager-plugin")
    r.interactive(
        r.aws_argv([
            "ecs", "execute-command", "--cluster", project.require("cluster"), "--task", task,
            "--container", container, "--interactive", "--command", command,
        ]),
        mutating=True,
    )  # fmt: skip


# -- commands --------------------------------------------------------------------------------------


@click.command("status")
@PROJECT_OPTION
@click.pass_context
def status(ctx, project_name):
    """Services, database, load balancer targets and the site, in one view."""
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        header(project, r)
        data = r.aws_json(
            "ecs", "describe-services", "--cluster", project.require("cluster"), "--services", *project.services
        )
        table = Table(box=None, pad_edge=False)
        for col in ("SERVICE", "REV", "RUNNING", "ROLLOUT"):
            table.add_column(col)
        for svc in data.get("services", []):
            primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), {})
            table.add_row(
                svc["serviceName"],
                svc["taskDefinition"].rsplit(":", 1)[-1],
                f"{svc['runningCount']}/{svc['desiredCount']}",
                primary.get("rolloutState", "-"),
            )
        console.print(table)
        db = _db_status(project, r)
        if db is not None:
            (success if db == "available" else info)(f"database {project.get('db_id')}: {db}")
        tg = project.get("target_group_arn")
        if tg:
            targets = r.aws_json(
                "elbv2", "describe-target-health", "--target-group-arn", tg,
                "--query", "TargetHealthDescriptions[].[Target.Id,TargetHealth.State]",
            )  # fmt: skip
            if not targets:
                info("load balancer: no targets registered")
            for target, state in targets or []:
                (success if state == "healthy" else warning)(f"target {target}: {state}")
        _site_check(project)


@click.command("park")
@PROJECT_OPTION
@click.pass_context
def park(ctx, project_name):
    """Stop paying for compute: services to 0, then stop the database.

    The load balancer, storage and images stay, so `unpark` brings it all back.
    """
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        header(project, r)
        safety_check(project, r)
        step("Services → 0")
        _set_desired(project, r, 0)
        db = _db_status(project, r)
        if db is not None:
            step(f"Database {project.get('db_id')}")
            if db == "available":
                r.aws("rds", "stop-db-instance", "--db-instance-identifier", project.get("db_id"),
                      "--query", "DBInstance.DBInstanceStatus", mutating=True)  # fmt: skip
                warning("AWS starts a stopped database again by itself after 7 days.")
            else:
                info(f"already {db} - leaving it")
        success("Parked." if not r.dry_run else "Dry run - nothing changed.")


@click.command("unpark")
@PROJECT_OPTION
@click.option("--desired", default=1, show_default=True, type=click.IntRange(min=1), help="Tasks per service.")
@click.pass_context
def unpark(ctx, project_name, desired):
    """Start the database, wait for it, then bring the services back.

    Waits if the database is still stopping, and refuses to start the services
    if the database never becomes available.
    """
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        header(project, r)
        safety_check(project, r)
        db = _db_status(project, r)
        if db is not None:
            name = project.get("db_id")
            step(f"Database {name}")
            while db == "stopping" and not r.dry_run:
                info(f"still stopping - checking again in {POLL_SECONDS}s")
                sleep(POLL_SECONDS)
                db = _db_status(project, r)
            if db == "stopped" or (db == "stopping" and r.dry_run):
                r.aws("rds", "start-db-instance", "--db-instance-identifier", name,
                      "--query", "DBInstance.DBInstanceStatus", mutating=True)  # fmt: skip
            if db != "available":
                try:
                    r.aws("rds", "wait", "db-instance-available", "--db-instance-identifier", name, mutating=True)
                except AwsError as exc:
                    raise AwsError(f"Database never became available ({exc}) - NOT starting services.") from exc
            if not r.dry_run:
                success("database available")
        step(f"Services → {desired}")
        _set_desired(project, r, desired)
        if not r.dry_run:
            _site_check(project)
        success("Unparked." if not r.dry_run else "Dry run - nothing changed.")


@click.command("logs")
@click.argument("service", required=False)
@PROJECT_OPTION
@click.option("--since", default="10m", show_default=True, help="How far back, e.g. 5m, 2h, 1d.")
@click.option("-f", "--follow", is_flag=True, help="Keep printing new lines.")
@click.option("-g", "--grep", "pattern", help="CloudWatch filter pattern, e.g. objectfs or ERROR.")
@click.pass_context
def logs(ctx, service, project_name, since, follow, pattern):
    """Tail a service's CloudWatch logs (`SERVICE` = web, cron, ... or the full name)."""
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        svc = _pick_service(project, service)
        args = ["logs", "tail", project.require("log_group"), "--since", since,
                "--log-stream-name-prefix", short_name(svc)]  # fmt: skip
        if follow:
            args.append("--follow")
        if pattern:
            args += ["--filter-pattern", pattern]
        r.interactive(r.aws_argv(args))


@click.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("service", required=False)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@PROJECT_OPTION
@click.option("-t", "--task", "task", help="Task ID (or its start) when a service runs several.")
@click.option("-c", "--container", help="Container name (default: from ids.txt / project type).")
@click.option("-u", "--as", "user", help="Run as this user inside the container (wraps the command in su).")
@click.pass_context
def exec_(ctx, service, command, project_name, task, container, user):
    """Open a shell, or run `COMMAND`, inside a running container (ECS Exec).

    Put the command after `--`: `eve aws exec web -u www-data -- php admin/cli/cron.php`
    """
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        header(project, r)
        svc = _pick_service(project, service)
        cmd = shlex.join(command) if command else "bash"
        _exec(project, r, svc, task, container, cmd, user)


@click.command("run")
@click.argument("recipe", required=False)
@PROJECT_OPTION
@click.option("-s", "--service", help="Service to run it in (default: the recipe's, then DEFAULT_SERVICE).")
@click.option("-t", "--task", "task", help="Task ID when the service runs several.")
@click.pass_context
def run(ctx, recipe, project_name, service, task):
    """Run a named recipe inside a container. With no name, list the recipes.

    Recipes come from the project type (e.g. `moodle`: purge, upgrade, cron,
    reset-password) and from `RECIPE_<name>=...` lines in ids.txt.
    """
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        recipes = projtypes.recipes_for(project)
        if not recipe:
            if not recipes:
                info(f"No recipes. Add one: eve aws project set {project.name} recipes.<name>.cmd <command>")
                return
            table = Table(box=None, pad_edge=False)
            for col in ("RECIPE", "FROM", "RUNS"):
                table.add_column(col)
            for name, rec in sorted(recipes.items()):
                table.add_row(name, rec.get("source", ""), rec.get("help") or rec["cmd"])
            console.print(table)
            return
        if recipe not in recipes:
            raise AwsError(f"No recipe {recipe!r}.", "eve aws run   (lists them)")
        rec = recipes[recipe]
        answers = {key: click.prompt(key, hide_input=secret) for key, secret in rec.get("prompts", [])}
        command = projtypes.fill(rec, answers) if answers else rec["cmd"]
        header(project, r)
        svc = _pick_service(project, service or rec.get("service"))
        _exec(project, r, svc, task, None, command, rec.get("user"))


@click.command("push")
@PROJECT_OPTION
@click.option("-t", "--tag", help="Image tag (default: the project repo's short git commit).")
@click.option("--allow-dirty", is_flag=True, help="Push even with uncommitted changes.")
@click.pass_context
def push(ctx, project_name, tag, allow_dirty):
    """Tag the local images with the git commit and push them to ECR."""
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        header(project, r)
        safety_check(project, r)
        if not tag:
            if project.path is None:
                raise AwsError("push needs the project's folder for the git commit tag.",
                               f"eve aws project edit {project.name} --path <folder>   (or pass --tag)")  # fmt: skip
            repo = str(project.path)
            dirty = r.run(["git", "-C", repo, "status", "--porcelain"]).stdout.strip()
            if dirty and not allow_dirty:
                raise AwsError(
                    "Uncommitted changes - the tag wouldn't match the code.", "Commit first, or --allow-dirty."
                )
            tag = r.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"]).stdout.strip()
        registry = project.require("registry")
        local = project.get("local_image_tag", "local")
        step(f"Push tag {tag}")
        r.pipe(
            r.aws_argv(["ecr", "get-login-password"]),
            ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        )
        for name in project.list_of("repos"):
            target = f"{registry}/{name}:{tag}"
            r.run(["docker", "tag", f"{name}:{local}", target], mutating=True)
            r.run(["docker", "push", target], mutating=True, capture=False)
        success(f"Pushed {tag}" if not r.dry_run else "Dry run - nothing pushed.")


@click.command("files")
@click.argument("prefix", required=False, default="")
@PROJECT_OPTION
@click.pass_context
def files(ctx, prefix, project_name):
    """List objects in the project's files bucket (optionally under `PREFIX`, e.g. ecs/)."""
    with reporting():
        project, r = project_and_runner(ctx, project_name)
        bucket = project.require("files_bucket")
        r.interactive(
            r.aws_argv(["s3", "ls", f"s3://{bucket}/{prefix}", "--recursive", "--human-readable", "--summarize"])
        )


ACTIONS = (status, park, unpark, logs, exec_, run, push, files)
