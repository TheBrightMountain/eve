"""eve aws: config, state, types, picking, and every command's real argv.

A fake replaces subprocess, so nothing here talks to AWS or Docker.
"""

import json

import pytest
from click.testing import CliRunner

from eve.aws import actions, pick, runner, types
from eve.aws import state as statefile
from eve.aws.config import Project, parse, short_name
from eve.aws.runner import AwsError, Result
from eve.cli import cli

ACCOUNT = "111122223333"
IDS = f"""# lab project
ACCOUNT_ID={ACCOUNT}
CLUSTER=moodle
SERVICES=moodle-web moodle-cron
DEFAULT_SERVICE=web
DB_ID=moodle-db
TG_ARN=arn:aws:elasticloadbalancing:ap-southeast-2:{ACCOUNT}:targetgroup/moodle-web-tg/abc
SITE_URL=https://mdl.example.com
LOG_GROUP=/ecs/moodle
REGISTRY={ACCOUNT}.dkr.ecr.ap-southeast-2.amazonaws.com
REPOS=moodle-php moodle-nginx
FILES_BUCKET=moodle-files-{ACCOUNT}
RECIPE_hello=echo hi=there
RECIPE_hello_SERVICE=cron
"""


class FakeExec:
    """Records argv; answers from rules (latest matching rule wins)."""

    def __init__(self):
        self.calls = []
        self.rules = []

    def on(self, *tokens, out="", rc=0, err="", seq=None):
        self.rules.append((tokens, list(seq) if seq else [Result(rc, out, err)]))

    def __call__(self, argv, capture=True, stdin_from=None):
        self.calls.append({"argv": list(argv), "stdin_from": stdin_from, "capture": capture})
        for tokens, answers in reversed(self.rules):
            if all(t in argv for t in tokens):
                return answers.pop(0) if len(answers) > 1 else answers[0]
        return Result(0, "", "")

    def ran(self, *tokens):
        return [c["argv"] for c in self.calls if all(t in c["argv"] for t in tokens)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("EVE_AWS_PROJECT", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    proj = tmp_path / "moodle-aws"
    (proj / "aws").mkdir(parents=True)
    (proj / "aws" / "ids.txt").write_text(IDS, encoding="utf-8")

    fake = FakeExec()
    fake.on(
        "sts", "get-caller-identity", out=json.dumps({"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/nms"})
    )
    fake.on("configure", "list-profiles", out="default\nlab\n")
    monkeypatch.setattr(runner, "_execute", fake)
    monkeypatch.setattr(runner, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(actions, "sleep", lambda s: None)
    monkeypatch.setattr(actions, "http_status", lambda url: 303)

    st = statefile.empty()
    statefile.add_project(st, "moodle", proj, "moodle", "lab")
    st["current_project"] = "moodle"
    st["current_account"] = "lab"
    statefile.save(st)
    return {"fake": fake, "proj": proj, "tmp": tmp_path}


def invoke(*args, input=None):
    return CliRunner().invoke(cli, ["aws", *args], input=input)


# -- config / state / types / pick ---------------------------------------------------------------


def test_parse_skips_comments_and_keeps_equals_in_values():
    values = parse("# c\n\nA=1\nB = two words \nC=x=y\nnot a pair\n")
    assert values == {"A": "1", "B": "two words", "C": "x=y"}


def test_service_short_names_and_resolution(env):
    project = Project.load("moodle", statefile.load()["projects"]["moodle"])
    assert short_name("moodle-web") == "web"
    assert project.resolve_service("cron") == "moodle-cron"
    assert project.resolve_service("moodle-web") == "moodle-web"
    with pytest.raises(AwsError):
        project.resolve_service("api")


def test_missing_key_names_the_key_and_file(env):
    project = Project.load("moodle", statefile.load()["projects"]["moodle"])
    with pytest.raises(AwsError) as exc:
        project.require("NOPE")
    assert "NOPE" in str(exc.value) and "ids.txt" in exc.value.hint


def test_state_remove_clears_current(env):
    st = statefile.load()
    statefile.remove_project(st, "moodle")
    assert st["current_project"] is None and "moodle" not in st["projects"]


def test_type_recipes_merge_with_ids_recipes(env):
    project = Project.load("moodle", statefile.load()["projects"]["moodle"])
    recipes = types.recipes_for(project)
    assert {"purge", "upgrade", "cron", "reset-password", "hello"} <= set(recipes)
    assert recipes["purge"]["user"] == "www-data"
    assert recipes["hello"] == {**recipes["hello"], "cmd": "echo hi=there", "service": "cron", "user": None}


def test_fill_shell_quotes_answers():
    recipe = types.get("moodle")["recipes"]["reset-password"]
    cmd = types.fill(recipe, {"username": "admin", "password": "a b'c"})
    assert "--password='a b'\"'\"'c'" in cmd


def test_pick_order(monkeypatch):
    items = ["moodle-web", "moodle-cron"]
    assert pick.choose(items, "service", given="moodle-cron") == "moodle-cron"
    assert pick.choose(items, "service", default="moodle-web") == "moodle-web"
    assert pick.choose(["only"], "service") == "only"
    monkeypatch.setattr(pick, "interactive", lambda: False)
    with pytest.raises(AwsError):
        pick.choose(items, "service")


# -- help -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["account"],
        ["account", "ls"],
        ["account", "add"],
        ["account", "login"],
        ["account", "logout"],
        ["account", "rm"],
        ["project"],
        ["project", "add"],
        ["project", "enter"],
        ["project", "leave"],
        ["project", "types"],
        ["status"],
        ["park"],
        ["unpark"],
        ["logs"],
        ["exec"],
        ["run"],
        ["push"],
        ["files"],
    ],
)
def test_help_runs(path):
    result = CliRunner().invoke(cli, ["aws", *path, "--help"])
    assert result.exit_code == 0, result.output


# -- project ----------------------------------------------------------------------------------------


def test_project_add_enter_leave_rm(env):
    other = env["tmp"] / "shop"
    (other / "aws").mkdir(parents=True)
    (other / "aws" / "ids.txt").write_text(f"ACCOUNT_ID={ACCOUNT}\nCLUSTER=shop\nSERVICES=shop-web\n")
    assert invoke("project", "add", "shop", "--path", str(other), "--type", "ecs", "--account", "lab").exit_code == 0
    result = invoke("project", "enter", "shop")
    assert result.exit_code == 0, result.output
    assert statefile.load()["current_project"] == "shop"
    assert "shop" in invoke("project", "ls").output
    invoke("project", "leave")
    assert statefile.load()["current_project"] is None
    invoke("project", "rm", "shop")
    assert "shop" not in statefile.load()["projects"]


def test_project_add_needs_ids_txt(env):
    empty = env["tmp"] / "empty"
    empty.mkdir()
    result = invoke("project", "add", "x", "--path", str(empty), "--account", "lab")
    assert result.exit_code == 1 and "ids.txt" in result.output


def test_actions_need_a_project(env):
    st = statefile.load()
    st["current_project"] = None
    statefile.save(st)
    result = invoke("status")
    assert result.exit_code == 1 and "project enter" in result.output


# -- account ----------------------------------------------------------------------------------------


def test_acc_is_an_alias_for_account(env):
    result = invoke("acc", "ls")
    assert result.exit_code == 0, result.output
    assert "lab" in result.output
    assert "acc " not in CliRunner().invoke(cli, ["aws", "--help"]).output


def test_account_add_sets_region_then_logs_in_remotely(env, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    result = invoke("account", "add", "work", "--region", "ap-southeast-1")
    assert result.exit_code == 0, result.output
    fake = env["fake"]
    assert ["aws", "configure", "set", "region", "ap-southeast-1", "--profile", "work"] in fake.ran("configure", "set")
    assert ["aws", "login", "--profile", "work", "--remote"] in fake.ran("login")
    assert statefile.load()["current_account"] == "work"


def test_account_login_skips_login_when_signed_in(env):
    result = invoke("account", "login", "lab")
    assert result.exit_code == 0, result.output
    assert not [c for c in env["fake"].ran("login") if c[1] == "login"]


def test_account_login_signs_in_when_expired(env):
    env["fake"].on("sts", "get-caller-identity", rc=255, err="Your session has expired. Please reauthenticate")
    result = invoke("account", "login", "lab", "--remote")
    assert result.exit_code == 0, result.output
    assert ["aws", "login", "--profile", "lab", "--remote"] in env["fake"].ran("login")


def test_account_rm_refuses_while_a_project_uses_it(env):
    result = invoke("account", "rm", "lab")
    assert result.exit_code == 1 and "moodle" in result.output


def test_account_rm_removes_the_profile_section(env, monkeypatch):
    cfg = env["tmp"] / "awsconfig"
    cfg.write_text("[default]\nregion = x\n[profile lab]\nregion = y\n[profile work]\nregion = z\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    result = invoke("account", "rm", "lab", "--force")
    assert result.exit_code == 0, result.output
    assert cfg.read_text() == "[default]\nregion = x\n[profile work]\nregion = z\n"


# -- status / park / unpark -----------------------------------------------------------------------


SERVICES_JSON = json.dumps(
    {
        "services": [
            {"serviceName": "moodle-web", "taskDefinition": "arn:...:task-definition/moodle-web:2",
             "runningCount": 1, "desiredCount": 1, "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}]},
            {"serviceName": "moodle-cron", "taskDefinition": "arn:...:task-definition/moodle-cron:2",
             "runningCount": 1, "desiredCount": 1, "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}]},
        ]
    }
)  # fmt: skip


def test_status_shows_services_db_targets_and_site(env):
    fake = env["fake"]
    fake.on("ecs", "describe-services", out=SERVICES_JSON)
    fake.on("rds", "describe-db-instances", out="available\n")
    fake.on("elbv2", "describe-target-health", out=json.dumps([["172.31.1.2", "healthy"]]))
    result = invoke("status")
    assert result.exit_code == 0, result.output
    for text in ("moodle-web", "COMPLETED", "available", "172.31.1.2", "303"):
        assert text in result.output
    assert all(c[-2:] == ["--profile", "lab"] or "--profile" in c for c in fake.ran("aws"))


def test_every_aws_call_carries_the_project_profile(env):
    env["fake"].on("ecs", "describe-services", out=SERVICES_JSON)
    invoke("status")
    aws_calls = env["fake"].ran("aws")
    assert aws_calls and all(c[c.index("--profile") + 1] == "lab" for c in aws_calls)


def test_park_scales_to_zero_then_stops_db(env):
    fake = env["fake"]
    fake.on("rds", "describe-db-instances", out="available\n")
    result = invoke("park")
    assert result.exit_code == 0, result.output
    zero = fake.ran("update-service", "--desired-count", "0")
    assert [c[c.index("--service") + 1] for c in zero] == ["moodle-web", "moodle-cron"]
    assert fake.ran("stop-db-instance")
    assert "7 days" in result.output


def test_park_leaves_a_stopped_db_alone(env):
    env["fake"].on("rds", "describe-db-instances", out="stopped\n")
    invoke("park")
    assert not env["fake"].ran("stop-db-instance")


def test_dry_run_changes_nothing(env):
    env["fake"].on("rds", "describe-db-instances", out="available\n")
    result = invoke("-n", "park")
    assert result.exit_code == 0, result.output
    assert not env["fake"].ran("update-service")
    assert not env["fake"].ran("stop-db-instance")
    assert "would run" in result.output


def test_unpark_waits_while_stopping_then_starts(env):
    fake = env["fake"]
    fake.on(
        "rds", "describe-db-instances", seq=[Result(0, "stopping\n"), Result(0, "stopping\n"), Result(0, "stopped\n")]
    )
    result = invoke("unpark")
    assert result.exit_code == 0, result.output
    assert result.output.count("still stopping") == 2
    assert fake.ran("start-db-instance")
    assert fake.ran("db-instance-available")
    assert len(fake.ran("update-service", "--desired-count", "1")) == 2


def test_unpark_refuses_services_when_db_never_comes_up(env):
    fake = env["fake"]
    fake.on("rds", "describe-db-instances", out="starting\n")
    fake.on("db-instance-available", rc=255, err="Waiter DBInstanceAvailable failed: Max attempts exceeded")
    result = invoke("unpark")
    assert result.exit_code == 1
    assert "NOT starting services" in result.output
    assert not fake.ran("update-service")


def test_wrong_account_is_refused(env):
    env["fake"].on("sts", "get-caller-identity", out=json.dumps({"Account": "999999999999", "Arn": "x"}))
    result = invoke("park")
    assert result.exit_code == 1 and "999999999999" in result.output
    assert not env["fake"].ran("update-service")


# -- logs / exec / run ----------------------------------------------------------------------------


def test_logs_uses_the_short_name_as_stream_prefix(env):
    result = invoke("logs", "cron", "--since", "5m", "-f", "-g", "objectfs")
    assert result.exit_code == 0, result.output
    (call,) = env["fake"].ran("logs", "tail")
    assert call[:4] == ["aws", "logs", "tail", "/ecs/moodle"]
    assert ["--since", "5m"] == call[4:6]
    assert "cron" in call and "--follow" in call and "objectfs" in call


def test_logs_defaults_to_default_service(env):
    invoke("logs")
    (call,) = env["fake"].ran("logs", "tail")
    assert call[call.index("--log-stream-name-prefix") + 1] == "web"


def test_exec_uses_type_container_and_wraps_user(env):
    env["fake"].on("list-tasks", out=json.dumps(["arn:aws:ecs:x:task/moodle/abc123"]))
    result = invoke("exec", "web", "-u", "www-data", "--", "php", "admin/cli/cron.php")
    assert result.exit_code == 0, result.output
    (call,) = env["fake"].ran("execute-command")
    assert call[call.index("--container") + 1] == "php"
    assert call[call.index("--task") + 1].endswith("abc123")
    assert call[call.index("--command") + 1] == "su -s /bin/sh www-data -c 'php admin/cli/cron.php'"


def test_exec_with_no_running_task_says_so(env):
    env["fake"].on("list-tasks", out="[]")
    result = invoke("exec", "web")
    assert result.exit_code == 1 and "no running task" in result.output


def test_run_lists_recipes(env):
    result = invoke("run")
    assert result.exit_code == 0
    for name in ("purge", "upgrade", "hello"):
        assert name in result.output


def test_run_purge_as_www_data_in_web(env):
    env["fake"].on("list-tasks", out=json.dumps(["arn:aws:ecs:x:task/moodle/abc123"]))
    result = invoke("run", "purge")
    assert result.exit_code == 0, result.output
    (call,) = env["fake"].ran("execute-command")
    assert call[call.index("--command") + 1] == (
        "su -s /bin/sh www-data -c 'OBJECTFS_BUCKET= php admin/cli/purge_caches.php'"
    )
    list_call = env["fake"].ran("list-tasks")[0]
    assert list_call[list_call.index("--service-name") + 1] == "moodle-web"


def test_run_custom_recipe_goes_to_its_service_unwrapped(env):
    env["fake"].on("list-tasks", out=json.dumps(["arn:aws:ecs:x:task/moodle/def456"]))
    invoke("run", "hello")
    list_call = env["fake"].ran("list-tasks")[0]
    assert list_call[list_call.index("--service-name") + 1] == "moodle-cron"
    (call,) = env["fake"].ran("execute-command")
    assert call[call.index("--command") + 1] == "echo hi=there"


def test_run_reset_password_prompts(env):
    env["fake"].on("list-tasks", out=json.dumps(["arn:aws:ecs:x:task/moodle/abc123"]))
    result = invoke("run", "reset-password", input="admin\nS3cret pass\n")
    assert result.exit_code == 0, result.output
    (call,) = env["fake"].ran("execute-command")
    assert "--username=admin" in call[call.index("--command") + 1]
    assert "S3cret pass" not in result.output.split("password:")[-1].splitlines()[0]


# -- push / files ----------------------------------------------------------------------------------


def test_push_refuses_a_dirty_tree(env):
    env["fake"].on("git", "status", out=" M docker/Dockerfile\n")
    result = invoke("push")
    assert result.exit_code == 1 and "Uncommitted" in result.output
    assert not env["fake"].ran("docker", "push")


def test_push_tags_with_the_commit_and_pipes_the_login(env):
    fake = env["fake"]
    fake.on("git", "rev-parse", out="e0a7b25\n")
    result = invoke("push")
    assert result.exit_code == 0, result.output
    login = [c for c in fake.calls if c["stdin_from"]]
    assert login and login[0]["stdin_from"][:3] == ["aws", "ecr", "get-login-password"]
    assert login[0]["argv"][:2] == ["docker", "login"]
    registry = f"{ACCOUNT}.dkr.ecr.ap-southeast-2.amazonaws.com"
    assert ["docker", "tag", "moodle-php:local", f"{registry}/moodle-php:e0a7b25"] in fake.ran("docker", "tag")
    assert ["docker", "push", f"{registry}/moodle-nginx:e0a7b25"] in fake.ran("docker", "push")


def test_files_lists_the_bucket_prefix(env):
    invoke("files", "ecs/")
    (call,) = env["fake"].ran("s3", "ls")
    assert f"s3://moodle-files-{ACCOUNT}/ecs/" in call


# -- errors ----------------------------------------------------------------------------------------


def test_expired_session_points_at_account_login():
    message, hint = runner.explain("Your session has expired. Please reauthenticate using 'aws login'.")
    assert "expired" in message and "account login" in hint


def test_env_credentials_are_called_out(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    _, hint = runner.explain(
        "An error occurred (AccessDenied) ... assumed-role/moodle-local-objectfs/x is not authorized"
    )
    assert "AWS_ACCESS_KEY_ID" in hint


def test_missing_tool_is_reported(env, monkeypatch):
    monkeypatch.setattr(runner, "_which", lambda tool: None)
    result = invoke("status")
    assert result.exit_code == 1 and "not installed" in result.output
