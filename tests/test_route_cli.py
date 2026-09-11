"""The command surface, driven through click's runner."""

import json

import pytest
from click.testing import CliRunner

from eve.cli import cli
from eve.route import ledger, paths, probe

SUBCOMMANDS = ("check", "add", "rm", "ls", "sync")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect every path eve owns into a temp dir, and never need root."""
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ledger_path", lambda: tmp_path / "route.json")
    monkeypatch.setattr(paths, "hosts_path", lambda: hosts_file)
    monkeypatch.setattr(paths, "dpi_hostlist_path", lambda: tmp_path / "route-dpi.txt")
    monkeypatch.setattr(paths, "zapret_dir", lambda: tmp_path / "zapret")
    monkeypatch.setattr(paths, "is_admin", lambda: True)
    return tmp_path


class StubBackend:
    """A DPI backend that records instead of installing or shelling out."""

    def available(self):
        return True, None

    def present(self):
        return False

    def install(self):
        return None

    def write_hostlist(self, hosts):
        self.hosts = list(hosts)

    def apply(self, strategy):
        self.strategy = strategy

    def state(self):
        return {"installed": False, "running": False}

    def teardown(self):
        pass

    def plan(self, strategy, hosts):
        return [f"would run {strategy} for {', '.join(hosts)}"]


@pytest.fixture
def stub_dpi(monkeypatch):
    backend = StubBackend()
    monkeypatch.setattr("eve.route.dpi.backend", lambda name=None: backend)
    return backend


def _report(host, verdict, best=None):
    return {
        "host": host,
        "verdict": verdict,
        "poisoned": verdict == probe.DNS_POISONED,
        "system_ips": ["127.0.0.1"],
        "answers": {"google": ["1.2.3.4"]},
        "candidates": ["1.2.3.4"],
        "probes": [
            {
                "ip": "1.2.3.4",
                "sni": host,
                "tcp": True,
                "tls": best is not None,
                "ms": 12.0 if best else None,
                "error": None,
                "reset": False,
            }
        ],
        "working": [{"ip": best, "sni": host, "tcp": True, "tls": True, "ms": 12.0, "error": None, "reset": False}]
        if best
        else [],
        "control": None,
        "best": best,
    }


def test_route_is_registered_with_all_its_subcommands():
    assert "route" in cli.commands
    for name in SUBCOMMANDS:
        assert name in cli.commands["route"].commands


def test_every_subcommand_help_runs():
    runner = CliRunner()
    for name in SUBCOMMANDS:
        result = runner.invoke(cli, ["route", name, "--help"])
        assert result.exit_code == 0, f"{name}: {result.output}"


def test_check_reports_the_verdict(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    result = CliRunner().invoke(cli, ["route", "check", "medium.com"])
    assert result.exit_code == 0
    assert "dns-poisoned" in result.output


def test_check_changes_nothing(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    CliRunner().invoke(cli, ["route", "check", "medium.com"])
    assert not (sandbox / "route.json").exists()
    assert "eve route" not in (sandbox / "hosts").read_text(encoding="utf-8")


def test_check_json_is_machine_readable(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.OPEN, best="1.2.3.4"))
    result = CliRunner().invoke(cli, ["route", "check", "a.com", "--json"])
    assert json.loads(result.output)[0]["verdict"] == "open"


def test_add_pins_a_poisoned_host_and_records_it(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    result = CliRunner().invoke(cli, ["route", "add", "medium.com"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"]["medium.com"]["address"] == "1.2.3.4"
    assert "1.2.3.4  medium.com" in (sandbox / "hosts").read_text(encoding="utf-8")


def test_add_dry_run_writes_nothing(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    result = CliRunner().invoke(cli, ["route", "add", "medium.com", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (sandbox / "route.json").exists()
    assert "medium.com" not in (sandbox / "hosts").read_text(encoding="utf-8")
    assert "1.2.3.4" in result.output


def test_add_refuses_a_block_it_cannot_fix(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.IP_BLOCKED))
    result = CliRunner().invoke(cli, ["route", "add", "a.com"])
    assert result.exit_code == 1
    assert "tunnel" in result.output.lower()


def test_rm_unpins_and_forgets(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    CliRunner().invoke(cli, ["route", "add", "medium.com"])
    result = CliRunner().invoke(cli, ["route", "rm", "medium.com"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"] == {}
    assert "medium.com" not in (sandbox / "hosts").read_text(encoding="utf-8")


def test_rm_needs_something_to_remove(sandbox):
    result = CliRunner().invoke(cli, ["route", "rm"])
    assert result.exit_code == 1


def test_ls_on_an_empty_ledger_says_so(sandbox):
    result = CliRunner().invoke(cli, ["route", "ls"])
    assert result.exit_code == 0
    assert "nothing" in result.output.lower()


def test_ls_lists_what_was_added(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    CliRunner().invoke(cli, ["route", "add", "medium.com"])
    result = CliRunner().invoke(cli, ["route", "ls", "--json"])
    assert json.loads(result.output)["entries"]["medium.com"]["method"] == "pin"


def test_sync_repins_an_address_that_went_stale(monkeypatch, sandbox):
    """A CDN moves and the pin rots - sync is the whole reason it exists."""
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    CliRunner().invoke(cli, ["route", "add", "medium.com"])

    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="9.9.9.9"))
    result = CliRunner().invoke(cli, ["route", "sync"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"]["medium.com"]["address"] == "9.9.9.9"
    assert "9.9.9.9  medium.com" in (sandbox / "hosts").read_text(encoding="utf-8")


def test_writing_commands_refuse_without_privilege(monkeypatch, sandbox):
    monkeypatch.setattr(paths, "is_admin", lambda: False)
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.DNS_POISONED, best="1.2.3.4"))
    result = CliRunner().invoke(cli, ["route", "add", "medium.com"])
    assert result.exit_code == 1
    assert "sudo" in result.output.lower() or "administrator" in result.output.lower()


# --- overriding the diagnosis -----------------------------------------------
#
# The checker is not infallible: an intermittent block, a lucky handshake or a
# resolver that behaves differently for one query can all make a blocked host
# look reachable. `add` must never be the thing standing between you and a fix.


def test_add_pins_a_host_the_checker_calls_reachable(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.OPEN, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    result = CliRunner().invoke(cli, ["route", "add", "a.com"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"]["a.com"]["address"] == "1.2.3.4"


def test_add_says_it_is_overriding_a_reachable_verdict(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.OPEN, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    output = CliRunner().invoke(cli, ["route", "add", "a.com"]).output
    assert "open" in output
    assert "anyway" in output.lower() or "continuing" in output.lower()


def test_method_pin_is_honoured_without_consulting_the_verdict(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    result = CliRunner().invoke(cli, ["route", "add", "a.com", "--method", "pin"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"]["a.com"]["method"] == "pin"


def test_method_dpi_overrides_a_reachable_verdict(monkeypatch, sandbox, stub_dpi):
    """The case pinning cannot solve: 'open' is wrong and the truth is DPI."""
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.OPEN, best="1.2.3.4"))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "add", "a.com", "--method", "dpi"])
    assert result.exit_code == 0, result.output
    entry = ledger.load(sandbox / "route.json")["entries"]["a.com"]
    assert entry["method"] == "dpi"
    assert entry["strategy"] == "--dpi-desync=fake"


def test_ip_blocked_still_refuses_by_default(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.IP_BLOCKED))
    result = CliRunner().invoke(cli, ["route", "add", "a.com"])
    assert result.exit_code == 1
    assert "tunnel" in result.output.lower()


def test_but_an_explicit_method_pushes_through_ip_blocked(monkeypatch, sandbox, stub_dpi):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.IP_BLOCKED))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: False)
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "add", "a.com", "--method", "dpi"])
    assert result.exit_code == 0, result.output
    assert ledger.load(sandbox / "route.json")["entries"]["a.com"]["method"] == "dpi"


def test_pinning_still_needs_an_address_to_pin(monkeypatch, sandbox):
    """--method pin cannot invent one when nothing resolved."""
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.UNREACHABLE))
    result = CliRunner().invoke(cli, ["route", "add", "a.com", "--method", "pin"])
    assert result.exit_code == 1
    assert "address" in result.output.lower()


def test_dry_run_still_writes_nothing_when_forced(monkeypatch, sandbox):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.OPEN, best="1.2.3.4"))
    result = CliRunner().invoke(cli, ["route", "add", "a.com", "--method", "pin", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (sandbox / "route.json").exists()


# --- when a backend fails ----------------------------------------------------
#
# Observed on Windows: sc create failed with 1072 and the RuntimeError escaped
# as a traceback, after the ladder had already created the service but before
# the ledger was saved - leaving a live bypass that `ls` could not see and `rm`
# could not undo.


class ExplodingBackend(StubBackend):
    def apply(self, strategy):
        raise RuntimeError("[SC] CreateService FAILED 1072: marked for deletion")


@pytest.fixture
def exploding_dpi(monkeypatch):
    backend = ExplodingBackend()
    monkeypatch.setattr("eve.route.dpi.backend", lambda name=None: backend)
    return backend


def test_a_backend_failure_is_reported_not_raised(monkeypatch, sandbox, exploding_dpi):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "add", "x.com"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "1072" in result.output


def test_the_ledger_survives_a_failed_apply(monkeypatch, sandbox, exploding_dpi):
    """Otherwise the fix is live on the machine with no record eve can act on."""
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    CliRunner().invoke(cli, ["route", "add", "x.com"])
    assert "x.com" in ledger.load(sandbox / "route.json")["entries"]


def test_the_failure_says_how_to_recover(monkeypatch, sandbox, exploding_dpi):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    output = CliRunner().invoke(cli, ["route", "add", "x.com"]).output
    assert "route rm" in output or "route sync" in output


def test_a_failure_while_installing_is_reported_too(monkeypatch, sandbox, stub_dpi):
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))

    def boom():
        raise RuntimeError("refusing a tampered download - sha256 mismatch on nfqws")

    monkeypatch.setattr(stub_dpi, "install", boom)
    result = CliRunner().invoke(cli, ["route", "add", "x.com"])
    assert result.exit_code == 1
    assert "sha256 mismatch" in result.output
