"""Importing a list of routes: the preset format, the shipped presets, the command."""

import pytest
from click.testing import CliRunner

from eve.cli import cli
from eve.route import ledger, preset, probe
from tests.test_route_cli import StubBackend, _report, sandbox  # noqa: F401 - sandbox is a fixture


def test_parse_reads_hosts_probes_and_comments():
    text = """
    # a comment
    cdn.com   cache1.cdn.com   # trailing comment
    Exact.Host.net
    """
    assert preset.parse(text) == [("cdn.com", "cache1.cdn.com"), ("exact.host.net", None)]


def test_parse_refuses_a_probe_outside_its_domain():
    with pytest.raises(ValueError, match="not under"):
        preset.parse("cdn.com elsewhere.org", origin="f.txt")


def test_parse_names_the_line_it_could_not_read():
    with pytest.raises(ValueError, match="f.txt:2"):
        preset.parse("a.com\na.com b.a.com extra", origin="f.txt")


def test_parse_refuses_an_empty_list():
    with pytest.raises(ValueError, match="no entries"):
        preset.parse("# only comments\n")


def test_steam_ships_and_covers_the_download_cdn():
    assert "steam" in dict(preset.available())
    entries = dict(preset.load("steam"))
    assert entries["steamcontent.com"].endswith(".steamcontent.com")
    assert "steampowered.com" in entries


def test_load_prefers_a_file_on_disk(tmp_path):
    mine = tmp_path / "mine.txt"
    mine.write_text("a.com\n", encoding="utf-8")
    assert preset.load(str(mine)) == [("a.com", None)]


def test_load_lists_the_presets_when_the_name_is_unknown():
    with pytest.raises(ValueError, match="steam"):
        preset.load("no-such-preset")


@pytest.fixture
def stub_dpi(monkeypatch):
    backend = StubBackend()
    monkeypatch.setattr("eve.route.dpi.backend", lambda name=None: backend)
    return backend


def test_import_adds_every_entry_and_applies_once(monkeypatch, sandbox, stub_dpi, tmp_path):  # noqa: F811
    lst = tmp_path / "list.txt"
    lst.write_text("cdn.com a.cdn.com\nexact.net\n", encoding="utf-8")
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "import", str(lst)])
    assert result.exit_code == 0, result.output
    entries = ledger.load(sandbox / "route.json")["entries"]
    assert entries["cdn.com"]["probe"] == "a.cdn.com"
    assert entries["exact.net"]["method"] == "dpi"
    assert stub_dpi.hosts == ["cdn.com", "exact.net"]


def test_import_skips_hosts_already_held(monkeypatch, sandbox, stub_dpi, tmp_path):  # noqa: F811
    book = ledger.blank()
    ledger.add_entry(book, "held.com", method="pin", verdict="open", address="1.2.3.4")
    ledger.save(book, sandbox / "route.json")
    lst = tmp_path / "list.txt"
    lst.write_text("held.com\nnew.com\n", encoding="utf-8")
    diagnosed = []
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: diagnosed.append(h) or _report(h, probe.SNI_BLOCKED))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "import", str(lst)])
    assert result.exit_code == 0, result.output
    assert diagnosed == ["new.com"]
    assert ledger.load(sandbox / "route.json")["entries"]["held.com"]["method"] == "pin"


def test_one_bad_entry_does_not_stop_the_rest(monkeypatch, sandbox, stub_dpi, tmp_path):  # noqa: F811
    lst = tmp_path / "list.txt"
    lst.write_text("dead.com\ngood.com\n", encoding="utf-8")
    verdicts = {"dead.com": probe.UNREACHABLE, "good.com": probe.SNI_BLOCKED}
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, verdicts[h]))
    monkeypatch.setattr(probe, "reachable", lambda h, **kw: True)
    monkeypatch.setattr("eve.route.strategy.find", lambda *a, **kw: "--dpi-desync=fake")
    result = CliRunner().invoke(cli, ["route", "import", str(lst)])
    assert result.exit_code == 1
    assert "1 added" in result.output and "1 failed" in result.output
    assert list(ledger.load(sandbox / "route.json")["entries"]) == ["good.com"]


def test_import_dry_run_writes_nothing(monkeypatch, sandbox, stub_dpi):  # noqa: F811
    monkeypatch.setattr(probe, "diagnose", lambda h, **kw: _report(h, probe.SNI_BLOCKED))
    result = CliRunner().invoke(cli, ["route", "import", "steam", "-n"])
    assert result.exit_code == 0, result.output
    assert not (sandbox / "route.json").exists()


def test_import_list_shows_the_presets(sandbox):  # noqa: F811
    result = CliRunner().invoke(cli, ["route", "import", "--list"])
    assert result.exit_code == 0
    assert "steam" in result.output


def test_import_needs_admin_unless_dry_run(monkeypatch, sandbox):  # noqa: F811
    monkeypatch.setattr("eve.route.paths.is_admin", lambda: False)
    result = CliRunner().invoke(cli, ["route", "import", "steam"])
    assert result.exit_code == 1
    assert "administrator" in result.output


def test_every_steam_entry_uses_the_bypass_rather_than_a_pin():
    # Pinned CDN addresses go stale; a probe forces a name-keyed DPI entry.
    assert all(probe_name for _, probe_name in preset.load("steam"))
