"""Where eve keeps its state, and the hosts-file block it owns."""

import pytest

from eve.route import hosts, paths

HOSTS_SAMPLE = "127.0.0.1 localhost\n192.168.1.9 nas.local\n"


def test_state_locations_on_linux(monkeypatch):
    monkeypatch.setattr(paths, "_is_windows", lambda: False)
    assert paths.ledger_path() == paths.Path("/etc/eve/route.json")
    assert paths.dpi_hostlist_path() == paths.Path("/etc/eve/route-dpi.txt")
    assert paths.zapret_dir() == paths.Path("/opt/eve/zapret")
    assert paths.hosts_path() == paths.Path("/etc/hosts")


def test_state_locations_on_windows(monkeypatch):
    # Patch the seam, not os.name: pathlib reads os.name to choose its flavour,
    # so a global patch makes Path() try to build an unusable WindowsPath here.
    monkeypatch.setattr(paths, "_is_windows", lambda: True)
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert paths.ledger_path().as_posix().endswith("ProgramData/eve/route.json")
    assert paths.dpi_hostlist_path().as_posix().endswith("ProgramData/eve/route-dpi.txt")
    assert paths.zapret_dir().as_posix().endswith("Program Files/eve/zapret")
    assert paths.hosts_path().as_posix().endswith("System32/drivers/etc/hosts")


@pytest.fixture
def hosts_file(tmp_path):
    path = tmp_path / "hosts"
    path.write_text(HOSTS_SAMPLE, encoding="utf-8")
    return path


def test_pin_and_unpin_leave_the_rest_of_hosts_alone(hosts_file):
    hosts.pin("medium.com", "1.2.3.4", path=hosts_file, make_backup=False)
    body = hosts_file.read_text(encoding="utf-8")
    assert "127.0.0.1 localhost" in body
    assert "192.168.1.9 nas.local" in body
    assert hosts.read_block(hosts_file) == {"medium.com": "1.2.3.4"}

    hosts.pin("steamcommunity.com", "5.6.7.8", path=hosts_file, make_backup=False)
    assert hosts.read_block(hosts_file) == {"medium.com": "1.2.3.4", "steamcommunity.com": "5.6.7.8"}

    _, removed = hosts.unpin("medium.com", path=hosts_file, make_backup=False)
    assert removed
    assert hosts.read_block(hosts_file) == {"steamcommunity.com": "5.6.7.8"}


def test_pinning_twice_does_not_stack_blocks(hosts_file):
    hosts.pin("a.com", "1.1.1.1", path=hosts_file, make_backup=False)
    hosts.pin("a.com", "2.2.2.2", path=hosts_file, make_backup=False)
    assert hosts_file.read_text(encoding="utf-8").count(hosts.MARK_START) == 1
    assert hosts.read_block(hosts_file) == {"a.com": "2.2.2.2"}


def test_clear_removes_the_block_entirely(hosts_file):
    hosts.pin("a.com", "1.1.1.1", path=hosts_file, make_backup=False)
    hosts.clear(path=hosts_file, make_backup=False)
    body = hosts_file.read_text(encoding="utf-8")
    assert hosts.MARK_START not in body
    assert body.strip() == HOSTS_SAMPLE.strip()
    assert hosts.read_block(hosts_file) == {}


def test_unpin_a_host_we_never_pinned_is_a_no_op(hosts_file):
    _, removed = hosts.unpin("nothing.com", path=hosts_file, make_backup=False)
    assert not removed


def test_the_marked_block_says_eve_not_evo(hosts_file):
    hosts.pin("a.com", "1.1.1.1", path=hosts_file, make_backup=False)
    assert "# >>> eve route >>>" in hosts_file.read_text(encoding="utf-8")
