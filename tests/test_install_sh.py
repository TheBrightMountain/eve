"""install.sh must not strand eve's state.

Uninstalling while routes are still held open leaves the hosts block, /etc/eve
and the eve-route-dpi service behind with nothing left that knows how to clean
them up. The uninstaller refuses rather than trusting the README.
"""

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"


def run_uninstall(tmp_path, ledger=None, force=False):
    """Run `install.sh uninstall` with every destructive step neutered.

    UV=true turns the tool removal into a successful no-op, and SYSTEM_BIN
    points at an empty dir so the symlink removal is skipped entirely.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "UV": "true",
        "SYSTEM_BIN": str(tmp_path / "bin"),
        "LEDGER": str(ledger) if ledger else str(tmp_path / "absent.json"),
    }
    (tmp_path / "bin").mkdir(exist_ok=True)
    argv = ["bash", str(SCRIPT), "uninstall"] + (["--force"] if force else [])
    return subprocess.run(argv, capture_output=True, text=True, env=env, check=False)


def write_ledger(tmp_path, entries):
    path = tmp_path / "route.json"
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return path


@pytest.fixture
def held_open(tmp_path):
    return write_ledger(tmp_path, {"medium.com": {"method": "pin", "address": "1.2.3.4"}})


def test_refuses_while_routes_are_still_held_open(tmp_path, held_open):
    result = run_uninstall(tmp_path, held_open)
    assert result.returncode != 0, "uninstalled anyway, stranding the state"


def test_the_refusal_names_the_command_that_fixes_it(tmp_path, held_open):
    output = run_uninstall(tmp_path, held_open)
    combined = output.stdout + output.stderr
    assert "route rm --all" in combined
    assert "--force" in combined


def test_the_refusal_says_which_routes_are_in_the_way(tmp_path):
    ledger = write_ledger(tmp_path, {"medium.com": {"method": "pin"}, "x.com": {"method": "dpi"}})
    result = run_uninstall(tmp_path, ledger)
    combined = result.stdout + result.stderr
    assert "medium.com" in combined
    assert "x.com" in combined


def test_force_uninstalls_anyway(tmp_path, held_open):
    result = run_uninstall(tmp_path, held_open, force=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_empty_ledger_is_no_obstacle(tmp_path):
    result = run_uninstall(tmp_path, write_ledger(tmp_path, {}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_ledger_at_all_is_no_obstacle(tmp_path):
    result = run_uninstall(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_refusal_counts_the_routes_correctly(tmp_path):
    """`wc -l` on a string with no trailing newline undercounts by one."""
    ledger = write_ledger(tmp_path, {"a.com": {"method": "pin"}, "b.com": {"method": "pin"}})
    result = run_uninstall(tmp_path, ledger)
    assert "2 route" in result.stdout + result.stderr


def test_a_single_route_counts_as_one(tmp_path, held_open):
    assert "1 route" in run_uninstall(tmp_path, held_open).stderr
