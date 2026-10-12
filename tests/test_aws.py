"""eve aws: config, state, types, picking, and every command's real argv.

A fake replaces subprocess, so nothing here talks to AWS or Docker.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from eve.aws import actions, pick, runner, types
from eve.aws import state as statefile
from eve.aws.config import Project, from_ids_values, parse_ids, set_value, short_name, unset_value
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
    proj.mkdir()

    fake = FakeExec()
    fake.on(
        "sts", "get-caller-identity", out=json.dumps({"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/nms"})
    )
    fake.on("configure", "list-profiles", out="default\nlab\n")
    monkeypatch.setattr(runner, "_execute", fake)
    monkeypatch.setattr(runner, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(actions, "sleep", lambda s: None)
    monkeypatch.setattr(actions, "http_status", lambda url: 303)

    statefile.save_project(from_ids_values("moodle", "moodle", "lab", proj, parse_ids(IDS)))
    st = statefile.empty()
    st["current_project"] = "moodle"
    st["current_account"] = "lab"
    statefile.save(st)
    return {"fake": fake, "proj": proj, "tmp": tmp_path}


def invoke(*args, input=None):
    return CliRunner().invoke(cli, ["aws", *args], input=input)


def load(name="moodle"):
    return Project(statefile.load_project(name))


# -- config / state / types / pick ---------------------------------------------------------------


def test_parse_ids_skips_comments_and_keeps_equals_in_values():
    values = parse_ids("# c\n\nA=1\nB = two words \nC=x=y\nnot a pair\n")
    assert values == {"A": "1", "B": "two words", "C": "x=y"}


def test_ids_txt_converts_to_structured_json(env):
    data = statefile.load_project("moodle")
    assert data["services"] == ["moodle-web", "moodle-cron"]
    assert data["repos"] == ["moodle-php", "moodle-nginx"]
    assert data["account_id"] == ACCOUNT and data["cluster"] == "moodle"
    assert data["recipes"]["hello"] == {"cmd": "echo hi=there", "service": "cron"}
    assert data["path"] == str(env["proj"])


def test_service_short_names_and_resolution(env):
    project = load()
    assert short_name("moodle-web") == "web"
    assert project.resolve_service("cron") == "moodle-cron"
    assert project.resolve_service("moodle-web") == "moodle-web"
    with pytest.raises(AwsError):
        project.resolve_service("api")


def test_missing_key_points_at_project_set(env):
    with pytest.raises(AwsError) as exc:
        load().require("files_bucket_nope")
    assert "files_bucket_nope" in str(exc.value) and "project set moodle" in exc.value.hint


def test_set_and_unset_values():
    data = {"name": "x", "type": "ecs", "account": "lab"}
    set_value(data, "services", ["a-web", "a-cron"])
    set_value(data, "containers.web", ["php"])
    set_value(data, "recipes.hi.cmd", ["echo", "hi"])
    set_value(data, "cluster", ["c"])
    assert data["services"] == ["a-web", "a-cron"] and data["containers"] == {"web": "php"}
    assert data["recipes"] == {"hi": {"cmd": "echo hi"}} and data["cluster"] == "c"
    unset_value(data, "containers.web")
    assert data["containers"] == {}
    for bad in (("account", ["x"]), ("nope", ["x"]), ("containers", ["x"])):
        with pytest.raises(AwsError):
            set_value(data, *bad)


def test_legacy_projects_in_aws_json_are_migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    proj = tmp_path / "old"
    (proj / "aws").mkdir(parents=True)
    (proj / "aws" / "ids.txt").write_text(f"ACCOUNT_ID={ACCOUNT}\nCLUSTER=old\nSERVICES=old-web\nVPC=vpc-1\n")
    statefile.state_path().parent.mkdir(parents=True)
    statefile.state_path().write_text(json.dumps({
        "current_account": "lab", "current_project": "old",
        "projects": {"old": {"path": str(proj), "type": "ecs", "account": "lab"}},
    }))  # fmt: skip
    st = statefile.load()
    assert st == {"current_account": "lab", "current_project": "old"}
    data = statefile.load_project("old")
    assert data["cluster"] == "old" and data["services"] == ["old-web"] and data["resources"] == {"vpc": "vpc-1"}
    assert "projects" not in json.loads(statefile.state_path().read_text())


def test_migration_also_happens_on_project_ls(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    proj = tmp_path / "old"
    (proj / "aws").mkdir(parents=True)
    (proj / "aws" / "ids.txt").write_text("CLUSTER=old\n")
    statefile.state_path().parent.mkdir(parents=True)
    statefile.state_path().write_text(
        json.dumps({"projects": {"old": {"path": str(proj), "type": "ecs", "account": "lab"}}})
    )
    monkeypatch.setattr(runner, "_which", lambda tool: f"/usr/bin/{tool}")
    result = invoke("project", "ls")
    assert result.exit_code == 0 and "old" in result.output


def test_type_recipes_merge_with_project_recipes(env):
    recipes = types.recipes_for(load())
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
        ["account", "edit"],
        ["project"],
        ["project", "add"],
        ["project", "edit"],
        ["project", "show"],
        ["project", "set"],
        ["project", "unset"],
        ["project", "keys"],
        ["project", "export"],
        ["project", "import"],
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


def test_project_add_set_enter_leave_rm(env):
    assert invoke("project", "add", "shop", "--type", "ecs", "--account", "lab").exit_code == 0
    assert invoke("project", "set", "shop", "account_id", ACCOUNT).exit_code == 0
    assert invoke("project", "set", "shop", "cluster", "shop").exit_code == 0
    assert invoke("project", "set", "shop", "services", "shop-web", "shop-worker").exit_code == 0
    assert statefile.load_project("shop")["services"] == ["shop-web", "shop-worker"]
    result = invoke("project", "enter", "shop")
    assert result.exit_code == 0, result.output
    assert statefile.load()["current_project"] == "shop"
    assert "shop" in invoke("project", "ls").output
    assert '"cluster": "shop"' in invoke("project", "show").output
    invoke("project", "leave")
    assert statefile.load()["current_project"] is None
    invoke("project", "rm", "shop")
    assert "shop" not in statefile.project_names()


def test_project_add_imports_an_old_ids_txt_from_path(env):
    old = env["tmp"] / "old"
    old.mkdir()
    (old / "ids.txt").write_text("CLUSTER=x\nSERVICES=x-web\n")
    result = invoke("project", "add", "old", "--path", str(old), "--account", "lab")
    assert result.exit_code == 0, result.output
    assert statefile.load_project("old")["cluster"] == "x"


def test_project_export_import_round_trip(env):
    out = env["tmp"] / "shared.json"
    assert invoke("project", "export", "moodle", "-o", str(out)).exit_code == 0
    shared = json.loads(out.read_text())
    assert "path" not in shared and shared["cluster"] == "moodle"
    result = invoke("project", "import", str(out), "--name", "copy", "--account", "work")
    assert result.exit_code == 0, result.output
    copy = statefile.load_project("copy")
    assert copy["account"] == "work" and copy["services"] == ["moodle-web", "moodle-cron"]
    assert "already exists" in invoke("project", "import", str(out)).output


def test_project_edit_renames_and_keeps_it_current(env):
    result = invoke("project", "edit", "moodle", "--name", "lms", "--type", "ecs", "--account", "work")
    assert result.exit_code == 0, result.output
    assert "moodle" not in statefile.project_names()
    lms = statefile.load_project("lms")
    assert lms["type"] == "ecs" and lms["account"] == "work" and lms["cluster"] == "moodle"
    st = statefile.load()
    assert st["current_project"] == "lms" and st["current_account"] == "work"


def test_project_edit_without_options_opens_the_editor(env, monkeypatch):
    def fake_edit(filename):
        path = Path(filename)
        data = json.loads(path.read_text())
        data["cluster"] = "edited"
        path.write_text(json.dumps(data))

    monkeypatch.setattr("eve.aws.project.click.edit", fake_edit)
    result = invoke("project", "edit", "moodle")
    assert result.exit_code == 0, result.output
    assert statefile.load_project("moodle")["cluster"] == "edited"


def test_project_edit_undoes_broken_json(env, monkeypatch):
    monkeypatch.setattr("eve.aws.project.click.edit", lambda filename: Path(filename).write_text("{ nope"))
    result = invoke("project", "edit", "moodle")
    assert result.exit_code == 1 and "undone" in result.output
    assert statefile.load_project("moodle")["cluster"] == "moodle"


def test_project_edit_refuses_taken_name(env):
    invoke("project", "add", "shop", "--account", "lab")
    assert "already exists" in invoke("project", "edit", "moodle", "--name", "shop").output


def test_enter_signs_in_when_the_session_expired(env):
    env["fake"].on("sts", "get-caller-identity", rc=255, err="Your session has expired. Please reauthenticate")
    result = invoke("project", "enter", "moodle", "--remote")
    assert result.exit_code == 0, result.output
    assert ["aws", "login", "--profile", "lab", "--remote"] in env["fake"].ran("login")


def test_enter_no_login_skips_signing_in(env):
    env["fake"].on("sts", "get-caller-identity", rc=255, err="Your session has expired. Please reauthenticate")
    result = invoke("project", "enter", "moodle", "--no-login")
    assert result.exit_code == 0
    assert not [c for c in env["fake"].ran("login") if c[1] == "login"]
    assert "not signed in" in result.output


def test_enter_does_not_sign_in_when_already_signed_in(env):
    invoke("project", "enter", "moodle")
    assert not [c for c in env["fake"].ran("login") if c[1] == "login"]


def test_enter_refuses_a_missing_account(env):
    invoke("project", "edit", "moodle", "--account", "ghost")
    result = invoke("project", "enter", "moodle")
    assert result.exit_code == 1 and "ghost" in result.output


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


def test_account_add_resumes_a_saved_but_signed_out_profile(env):
    env["fake"].on("configure", "list-profiles", out="default\nlab\nhalf\n")
    env["fake"].on("sts", "get-caller-identity", rc=255, err="Unable to locate credentials")
    result = invoke("account", "add", "half", "--region", "ap-southeast-2", "--remote")
    assert result.exit_code == 0, result.output
    assert "signing in" in result.output
    assert ["aws", "login", "--profile", "half", "--remote"] in env["fake"].ran("login")


def test_account_add_refuses_a_signed_in_profile(env):
    result = invoke("account", "add", "lab", "--region", "ap-southeast-2")
    assert result.exit_code == 1 and "already exists and is signed in" in result.output


def test_failed_sign_in_says_how_to_retry(env):
    env["fake"].on("login", "--profile", rc=255, err="State parameter does not match")
    result = invoke("account", "add", "work", "--region", "ap-southeast-1", "--remote")
    assert result.exit_code == 1
    assert "Signing in to work failed" in result.output and "eve aws account login work" in result.output


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


def _awsconfig(env, monkeypatch, text):
    cfg = env["tmp"] / "awsconfig"
    cfg.write_text(text)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    return cfg


def test_account_edit_renames_profile_refs_and_projects(env, monkeypatch):
    cfg = _awsconfig(
        env,
        monkeypatch,
        "[default]\nregion = x\n[profile lab]\nregion = y\nlogin_session = arn\n"
        "[profile helper]\nrole_arn = r\nsource_profile = lab\n",
    )
    result = invoke("account", "edit", "lab", "--name", "example")
    assert result.exit_code == 0, result.output
    assert cfg.read_text() == (
        "[default]\nregion = x\n[profile example]\nregion = y\nlogin_session = arn\n"
        "[profile helper]\nrole_arn = r\nsource_profile = example\n"
    )
    st = statefile.load()
    assert statefile.load_project("moodle")["account"] == "example"
    assert st["current_account"] == "example"


def test_account_edit_region(env):
    result = invoke("account", "edit", "lab", "--region", "us-east-1")
    assert result.exit_code == 0, result.output
    assert ["aws", "configure", "set", "region", "us-east-1", "--profile", "lab"] in env["fake"].ran("configure", "set")


def test_account_edit_default_asks_first(env, monkeypatch):
    cfg = _awsconfig(env, monkeypatch, "[default]\nregion = x\n")
    result = invoke("account", "edit", "default", "--name", "main", input="n\n")
    assert result.exit_code == 0
    assert "plain `aws ...`" in result.output
    assert cfg.read_text() == "[default]\nregion = x\n"
    result = invoke("account", "edit", "default", "--name", "main", "--yes")
    assert result.exit_code == 0, result.output
    assert cfg.read_text() == "[profile main]\nregion = x\n"


def test_account_edit_refuses_an_existing_name(env, monkeypatch):
    _awsconfig(env, monkeypatch, "[profile lab]\n[profile work]\n")
    result = invoke("account", "edit", "lab", "--name", "work")
    assert result.exit_code == 1 and "already exists" in result.output


def test_account_edit_needs_something_to_change(env):
    result = invoke("account", "edit", "lab")
    assert result.exit_code == 1 and "Nothing to change" in result.output


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
