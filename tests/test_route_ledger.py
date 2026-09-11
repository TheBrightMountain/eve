"""The ledger is the source of truth; reconcile derives the system from it."""

import json

from eve.route import ledger, reconcile


def test_round_trips_through_disk(tmp_path):
    path = tmp_path / "route.json"
    book = ledger.blank()
    ledger.add_entry(book, "medium.com", method="pin", address="1.2.3.4", verdict="dns-poisoned")
    ledger.save(book, path)
    assert ledger.load(path)["entries"]["medium.com"]["address"] == "1.2.3.4"


def test_a_missing_ledger_loads_as_blank(tmp_path):
    assert ledger.load(tmp_path / "absent.json") == ledger.blank()


def test_unknown_top_level_keys_survive_a_write(tmp_path):
    """A newer eve's ledger must not be silently truncated by an older one."""
    path = tmp_path / "route.json"
    path.write_text(json.dumps({"version": 1, "entries": {}, "future_feature": {"keep": "me"}}), encoding="utf-8")
    book = ledger.load(path)
    ledger.add_entry(book, "x.com", method="dpi", strategy="fake", verdict="sni-blocked")
    ledger.save(book, path)
    assert json.loads(path.read_text(encoding="utf-8"))["future_feature"] == {"keep": "me"}


def test_remove_entry_reports_whether_it_was_there():
    book = ledger.blank()
    ledger.add_entry(book, "a.com", method="pin", address="1.1.1.1", verdict="dns-poisoned")
    assert ledger.remove_entry(book, "a.com")
    assert not ledger.remove_entry(book, "a.com")


def test_entries_are_split_by_method():
    book = ledger.blank()
    ledger.add_entry(book, "a.com", method="pin", address="1.1.1.1", verdict="dns-poisoned")
    ledger.add_entry(book, "b.com", method="dpi", strategy="fake", verdict="sni-blocked")
    assert list(ledger.by_method(book, "pin")) == ["a.com"]
    assert list(ledger.by_method(book, "dpi")) == ["b.com"]


def _book(*entries):
    book = ledger.blank()
    for host, kwargs in entries:
        ledger.add_entry(book, host, **kwargs)
    return book


def test_desired_state_pins_every_pin_entry():
    book = _book(
        ("a.com", {"method": "pin", "address": "1.1.1.1", "verdict": "dns-poisoned"}),
        ("b.com", {"method": "pin", "address": "2.2.2.2", "verdict": "dns-poisoned"}),
    )
    assert reconcile.desired(book)["pins"] == {"a.com": "1.1.1.1", "b.com": "2.2.2.2"}


def test_dpi_stays_off_when_no_entry_needs_it():
    book = _book(("a.com", {"method": "pin", "address": "1.1.1.1", "verdict": "dns-poisoned"}))
    state = reconcile.desired(book)
    assert state["dpi_active"] is False
    assert state["dpi_hosts"] == []


def test_dpi_turns_on_for_a_single_blocked_host():
    book = _book(("x.com", {"method": "dpi", "strategy": "fake", "verdict": "sni-blocked"}))
    state = reconcile.desired(book)
    assert state["dpi_active"] is True
    assert state["dpi_hosts"] == ["x.com"]
    assert state["strategy"] == "fake"


def test_the_newest_recorded_strategy_wins_for_the_whole_service():
    """One nfqws instance serves every DPI host, so it runs one strategy."""
    book = _book(
        ("x.com", {"method": "dpi", "strategy": "fake", "verdict": "sni-blocked"}),
        ("y.com", {"method": "dpi", "strategy": "multisplit", "verdict": "sni-blocked"}),
    )
    state = reconcile.desired(book)
    assert sorted(state["dpi_hosts"]) == ["x.com", "y.com"]
    assert state["strategy"] == "multisplit"


def test_removing_the_last_dpi_entry_turns_the_service_off():
    book = _book(("x.com", {"method": "dpi", "strategy": "fake", "verdict": "sni-blocked"}))
    ledger.remove_entry(book, "x.com")
    assert reconcile.desired(book)["dpi_active"] is False
