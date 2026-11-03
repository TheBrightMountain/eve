"""`eve update`: which commands it picks for each kind of install, without running them."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from eve import update as upd
from eve.cli import cli


class FakeDist:
    def __init__(self, data):
        self.data = data

    def read_text(self, name):
        return json.dumps(self.data) if self.data is not None else None


def installed(monkeypatch, data):
    monkeypatch.setattr(upd, "distribution", lambda name: FakeDist(data))


def test_git_install(monkeypatch):
    installed(monkeypatch, {"url": "https://github.com/x/eve", "vcs_info": {"vcs": "git", "commit_id": "abc"}})
    source = upd.install_source()
    assert source == {"kind": "git", "url": "https://github.com/x/eve", "commit": "abc"}
    assert upd.plan(source) == [["uv", "tool", "upgrade", "--reinstall", "eve"]]


def test_clone_install_pulls_then_reinstalls(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    installed(monkeypatch, {"url": tmp_path.as_uri(), "dir_info": {}})
    source = upd.install_source()
    assert source["kind"] == "dir" and source["path"] == tmp_path
    assert upd.plan(source) == [
        ["git", "-C", str(tmp_path), "pull", "--ff-only"],
        ["uv", "tool", "install", "--force", "--reinstall", str(tmp_path)],
    ]
    assert upd.plan(source, pull=False) == [["uv", "tool", "install", "--force", "--reinstall", str(tmp_path)]]


def test_editable_clone_stays_editable(monkeypatch, tmp_path):
    installed(monkeypatch, {"url": tmp_path.as_uri(), "dir_info": {"editable": True}})
    assert upd.plan(upd.install_source()) == [
        ["uv", "tool", "install", "--force", "--reinstall", "--editable", str(tmp_path)]
    ]


GITHUB = [["uv", "tool", "install", "--force", "--reinstall", upd.REPO]]


def test_unknown_install_falls_back_to_github(monkeypatch):
    installed(monkeypatch, None)
    assert upd.plan(upd.install_source()) == GITHUB


def test_deleted_clone_falls_back_to_github(monkeypatch, tmp_path):
    gone = tmp_path / "docker-dev" / "eve"
    installed(monkeypatch, {"url": gone.as_uri(), "dir_info": {}})
    assert upd.plan(upd.install_source()) == GITHUB
    result = CliRunner().invoke(cli, ["update", "-n"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "is gone - reinstalling from GitHub" in out and upd.REPO in out


def test_dry_run_runs_nothing(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    installed(monkeypatch, {"url": tmp_path.as_uri(), "dir_info": {}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    result = CliRunner().invoke(cli, ["update", "-n"])
    assert result.exit_code == 0, result.output
    assert "pull --ff-only" in result.output


def test_failed_step_stops(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    installed(monkeypatch, {"url": tmp_path.as_uri(), "dir_info": {}})
    monkeypatch.setattr(upd.shutil, "which", lambda prog: f"/usr/bin/{prog}")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli, ["update"])
    assert result.exit_code == 1
    assert len(calls) == 1 and Path(calls[0][0]).name == "git"
