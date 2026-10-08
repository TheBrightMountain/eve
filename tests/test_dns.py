"""`eve dns`: set and reset the machine's DNS servers, through a fake backend."""

import json

import pytest
from click.testing import CliRunner

from eve.cli import cli
from eve.dns import commands, state, system
from eve.route import paths


class FakeBackend:
    def __init__(self, interfaces):
        self.ifaces = {i["name"]: dict(i) for i in interfaces}
        self.automatic = []
        self.flushed = False

    def interfaces(self):
        return [dict(i) for i in self.ifaces.values()]

    def set_servers(self, iface, servers):
        self.ifaces[iface["name"]]["servers"] = list(servers)

    def set_automatic(self, iface):
        self.automatic.append(iface["name"])
        self.ifaces[iface["name"]]["servers"] = ["192.168.1.1"]

    def flush(self):
        self.flushed = True


@pytest.fixture
def machine(tmp_path, monkeypatch):
    backend = FakeBackend(
        [
            {"name": "Ethernet", "id": "20", "servers": ["1.1.1.1", "1.0.0.1"], "manual": ["1.1.1.1", "1.0.0.1"]},
            {"name": "Wi-Fi", "id": "7", "servers": ["192.168.1.1"], "manual": []},
        ]
    )
    monkeypatch.setattr(system, "backend", lambda name=None: backend)
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "is_admin", lambda: True)
    monkeypatch.setattr(commands, "_answers", lambda server, timeout=2.5: True)
    return backend


def run(*args):
    return CliRunner().invoke(cli, ["dns", *args])


def test_set_changes_every_connected_interface_and_flushes(machine):
    result = run("set", "9.9.9.9", "8.8.8.8")
    assert result.exit_code == 0, result.output
    assert all(i["servers"] == ["9.9.9.9", "8.8.8.8"] for i in machine.interfaces())
    assert machine.flushed


def test_set_keeps_the_originals_for_reset(machine, tmp_path):
    run("set", "9.9.9.9")
    saved = json.loads((tmp_path / "dns.json").read_text())
    assert saved["Ethernet"]["manual"] == ["1.1.1.1", "1.0.0.1"]
    assert saved["Wi-Fi"]["manual"] == []


def test_a_second_set_does_not_overwrite_the_originals(machine):
    run("set", "9.9.9.9")
    run("set", "8.8.8.8")
    assert state.load()["Ethernet"]["manual"] == ["1.1.1.1", "1.0.0.1"]


def test_reset_puts_back_hand_set_servers_and_dhcp(machine):
    run("set", "9.9.9.9")
    result = run("reset")
    assert result.exit_code == 0, result.output
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]
    assert machine.automatic == ["Wi-Fi"]
    assert state.load() == {}


def test_interface_narrows_the_change(machine):
    result = run("set", "9.9.9.9", "-i", "Wi-Fi")
    assert result.exit_code == 0, result.output
    assert machine.ifaces["Wi-Fi"]["servers"] == ["9.9.9.9"]
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]
    assert list(state.load()) == ["Wi-Fi"]


def test_an_unknown_interface_is_refused(machine):
    result = run("set", "9.9.9.9", "-i", "Nope")
    assert result.exit_code == 1
    assert "Ethernet" in result.output


def test_a_name_instead_of_an_address_is_refused(machine):
    result = run("set", "dns.google")
    assert result.exit_code == 1
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]


def test_nothing_changes_when_no_server_answers(machine, monkeypatch):
    monkeypatch.setattr(commands, "_answers", lambda server, timeout=2.5: False)
    result = run("set", "10.9.9.9")
    assert result.exit_code == 1
    assert "--force" in result.output
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]


def test_force_sets_them_anyway(machine, monkeypatch):
    monkeypatch.setattr(commands, "_answers", lambda server, timeout=2.5: False)
    result = run("set", "10.9.9.9", "--force")
    assert result.exit_code == 0, result.output
    assert machine.ifaces["Ethernet"]["servers"] == ["10.9.9.9"]


def test_one_dead_server_among_good_ones_only_warns(machine, monkeypatch):
    monkeypatch.setattr(commands, "_answers", lambda server, timeout=2.5: server != "10.9.9.9")
    result = run("set", "1.1.1.1", "10.9.9.9")
    assert result.exit_code == 0, result.output
    assert "did not answer" in result.output


def test_dry_run_changes_nothing(machine, tmp_path):
    result = run("set", "9.9.9.9", "-n")
    assert result.exit_code == 0, result.output
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]
    assert not (tmp_path / "dns.json").exists()


def test_set_and_reset_need_admin(machine, monkeypatch):
    monkeypatch.setattr(paths, "is_admin", lambda: False)
    assert run("set", "9.9.9.9").exit_code == 1
    assert run("reset").exit_code == 0  # nothing saved, so nothing to do
    assert machine.ifaces["Ethernet"]["servers"] == ["1.1.1.1", "1.0.0.1"]


def test_reset_with_nothing_saved_says_so(machine):
    result = run("reset")
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


def test_show_says_who_set_what(machine):
    run("set", "9.9.9.9", "-i", "Wi-Fi")
    result = run("show")
    assert result.exit_code == 0, result.output
    assert "hand" in result.output
    assert "eve (was automatic" in result.output


def test_windows_single_interface_json_is_unwrapped(monkeypatch):
    one = {"name": "Ethernet 4", "id": "20", "servers": ["1.1.1.1"], "manual": ["1.1.1.1"]}
    backend = system.WindowsBackend()
    monkeypatch.setattr(backend, "_ps", lambda script: json.dumps(one))
    assert backend.interfaces() == [one]


def test_windows_commands_target_the_interface_index(monkeypatch):
    sent = []
    backend = system.WindowsBackend()
    monkeypatch.setattr(backend, "_ps", lambda script: sent.append(script) or "")
    backend.set_servers({"id": "20"}, ["1.1.1.1", "8.8.8.8"])
    backend.set_automatic({"id": "20"})
    assert sent == [
        "Set-DnsClientServerAddress -InterfaceIndex 20 -ServerAddresses @('1.1.1.1','8.8.8.8')",
        "Set-DnsClientServerAddress -InterfaceIndex 20 -ResetServerAddresses",
    ]


def _linux(monkeypatch, outputs, which=True):
    """A LinuxBackend whose `ip`/`resolvectl` calls return canned output."""
    calls = []

    def fake_run(argv):
        calls.append(argv)
        key = " ".join(argv)
        out = next((v for k, v in outputs.items() if key.startswith(k)), "")
        return system.subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(system, "_run", fake_run)
    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/resolvectl" if which else None)
    return system.LinuxBackend(), calls


def test_linux_lists_each_default_route_device_once(monkeypatch):
    backend, _ = _linux(
        monkeypatch,
        {
            "ip -j route show default": '[{"dst":"default","dev":"eth0"},{"dst":"default","dev":"eth0","metric":200}]',
            "resolvectl dns eth0": "Link 2 (eth0): 192.168.1.1 1.1.1.1\n",
        },
    )
    assert backend.interfaces() == [{"name": "eth0", "id": "eth0", "servers": ["192.168.1.1", "1.1.1.1"], "manual": []}]


def test_linux_servers_are_automatic_so_reset_reverts_rather_than_pins(monkeypatch):
    # resolved cannot tell DHCP from hand-set; recording them as manual would
    # make reset pin today's list instead of handing the link back.
    backend, _ = _linux(
        monkeypatch,
        {"ip -j route show default": '[{"dev":"eth0"}]', "resolvectl dns eth0": "Link 2 (eth0): 10.0.0.1\n"},
    )
    saved = {}
    state.remember(saved, backend.interfaces()[0])
    assert saved["eth0"]["manual"] == []


def test_linux_set_reset_and_flush_use_resolvectl(monkeypatch):
    backend, calls = _linux(monkeypatch, {})
    backend.set_servers({"id": "eth0"}, ["1.1.1.1", "8.8.8.8"])
    backend.set_automatic({"id": "eth0"})
    backend.flush()
    assert calls == [
        ["resolvectl", "dns", "eth0", "1.1.1.1", "8.8.8.8"],
        ["resolvectl", "revert", "eth0"],
        ["resolvectl", "flush-caches"],
    ]


def test_linux_without_resolved_says_what_is_missing(monkeypatch):
    backend, _ = _linux(monkeypatch, {"ip -j route show default": '[{"dev":"eth0"}]'}, which=False)
    with pytest.raises(RuntimeError, match="systemd-resolved"):
        backend.interfaces()
