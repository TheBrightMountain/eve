"""`eve device`, `eve wake` and `eve ssh`, against a temp config dir and no real network."""

import json

import pytest
from click.testing import CliRunner

from eve.cli import cli
from eve.device import commands, net, store


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "eve" / "devices.json"


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(net, "send_magic_packet", lambda mac, targets: calls.append((mac, targets)))
    return calls


def run(*args, input=None):
    return CliRunner().invoke(cli, list(args), input=input)


def saved(config):
    return json.loads(config.read_text())


def add_bs(*extra):
    result = run(
        "device", "add", "battlestation", "-a", "bs", "--mac", "9c-6b-00-36-2b-bc", "--ip", "10.0.0.73",
        "--host", "battlestation.ts.net", "--user", "nms", *extra,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return result


def test_add_normalizes_and_saves(config):
    add_bs()
    d = saved(config)["battlestation"]
    assert d == {
        "aliases": ["bs"],
        "mac": "9C:6B:00:36:2B:BC",
        "ip": "10.0.0.73",
        "host": "battlestation.ts.net",
        "user": "nms",
    }


@pytest.mark.parametrize(
    "args",
    [
        ("--mac", "nope"),
        ("--mac", "9c6b0036"),
        ("--ip", "10.0.0.300"),
        ("--port", "70000"),
    ],
)
def test_add_rejects_bad_values(config, args):
    result = run("device", "add", "pc", *args)
    assert result.exit_code == 1
    assert not config.exists()


def test_names_and_aliases_cannot_collide(config):
    add_bs()
    assert run("device", "add", "BS").exit_code == 1
    assert run("device", "add", "other", "-a", "battlestation").exit_code == 1
    assert run("device", "add", "other", "-a", "bs").exit_code == 1


def test_edit_changes_only_what_is_given(config):
    add_bs()
    result = run("device", "edit", "bs", "--port", "2222", "--unset", "host", "-a", "rig", "--drop-alias", "bs")
    assert result.exit_code == 0, result.output
    d = saved(config)["battlestation"]
    assert d["port"] == 2222
    assert "host" not in d
    assert d["aliases"] == ["rig"]
    assert d["mac"] == "9C:6B:00:36:2B:BC"


def test_edit_rename(config):
    add_bs()
    assert run("device", "edit", "battlestation", "--name", "rig").exit_code == 0
    assert list(saved(config)) == ["rig"]


def test_edit_with_nothing_fails(config):
    add_bs()
    assert run("device", "edit", "bs").exit_code == 1


def test_remove_asks_then_forgets(config):
    add_bs()
    assert run("device", "rm", "bs", input="n\n").exit_code == 0
    assert "battlestation" in saved(config)
    assert run("device", "remove", "bs", "-y").exit_code == 0
    assert saved(config) == {}


def test_unknown_device(config):
    result = run("wake", "ghost")
    assert result.exit_code == 1
    assert "No device called" in result.output


def test_ls(config):
    assert "No devices yet" in run("device", "ls").output
    add_bs()
    out = run("device", "ls").output
    assert "battlestation" in out and "nms@battlestation.ts.net" in out


def test_wake_sends_to_subnet_and_global_broadcast(config, sent):
    add_bs()
    result = run("wake", "battlestation")
    assert result.exit_code == 0, result.output
    assert sent == [("9C:6B:00:36:2B:BC", ["10.0.0.255", "255.255.255.255"])]


def test_wake_prefers_configured_broadcast(config, sent):
    add_bs("--broadcast", "10.0.3.255")
    run("wake", "bs")
    assert sent[0][1] == ["10.0.3.255", "10.0.0.255", "255.255.255.255"]


def test_wake_needs_a_mac(config, sent):
    run("device", "add", "pc", "--ip", "10.0.0.5")
    result = run("wake", "pc")
    assert result.exit_code == 1
    assert not sent


def test_wake_wait_until_up(config, sent, monkeypatch):
    add_bs()
    answers = iter([False, False, True])
    monkeypatch.setattr(net, "ping", lambda host, timeout=1: next(answers))
    monkeypatch.setattr(commands.time, "sleep", lambda s: None)
    result = run("wake", "bs", "--wait")
    assert result.exit_code == 0, result.output
    assert "is up" in result.output


def test_magic_packet_layout():
    packet = net.magic_packet("01:02:03:04:05:06")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes([1, 2, 3, 4, 5, 6])


def test_ssh_builds_command_and_passes_args(config, monkeypatch):
    add_bs("--port", "2222")
    calls = []
    monkeypatch.setattr(commands.os, "execvp", lambda prog, cmd: calls.append(cmd))
    monkeypatch.setattr(commands.shutil, "which", lambda prog: "/usr/bin/ssh")
    result = run("ssh", "bs", "-L", "8080:localhost:80", "uptime")
    assert result.exit_code == 0, result.output
    assert calls == [["ssh", "-p", "2222", "nms@battlestation.ts.net", "-L", "8080:localhost:80", "uptime"]]


def test_ssh_falls_back_to_ip(config, monkeypatch):
    run("device", "add", "pi", "--ip", "10.0.0.9")
    result = run("ssh", "-n", "pi")
    assert result.exit_code == 0
    assert "$ ssh 10.0.0.9" in result.output


def test_store_resolve_prefers_names_over_aliases():
    devices = {"a": {"aliases": ["b"]}, "b": {}}
    assert store.resolve(devices, "b")[0] == "b"
