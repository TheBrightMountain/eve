"""Both DPI backends, verified by what they render rather than by running."""

import io
import tarfile

import pytest

from eve.route import dpi, dpi_linux, dpi_windows, strategy


def test_every_pinned_artifact_has_a_full_sha256():
    for platform, members in dpi.ARTIFACTS.items():
        assert members, platform
        for member, digest in members.items():
            assert len(digest) == 64, f"{platform}/{member}"
            assert member.startswith("binaries/"), member


def _tarball(tmp_path, name, payload):
    path = tmp_path / "z.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(f"zapret-{dpi.ZAPRET_VERSION}/{name}")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return path


def test_extract_accepts_a_member_matching_its_checksum(tmp_path):
    payload = b"pretend this is nfqws"
    tar = _tarball(tmp_path, "binaries/linux-x86_64/nfqws", payload)
    dest = tmp_path / "out"
    written = dpi.extract_verified(tar, {"binaries/linux-x86_64/nfqws": dpi.sha256_bytes(payload)}, dest)
    assert (dest / "nfqws").read_bytes() == payload
    assert written == [dest / "nfqws"]


def test_extract_refuses_a_tampered_member_and_leaves_nothing_behind(tmp_path):
    tar = _tarball(tmp_path, "binaries/linux-x86_64/nfqws", b"tampered")
    dest = tmp_path / "out"
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        dpi.extract_verified(tar, {"binaries/linux-x86_64/nfqws": "0" * 64}, dest)
    assert not (dest / "nfqws").exists()


def test_linux_ruleset_lets_traffic_through_if_the_daemon_dies():
    """`bypass` is the safety valve - without it a dead nfqws hangs the network."""
    text = dpi_linux.nft_ruleset(qnum=200)
    assert "queue num 200 bypass" in text
    assert "table inet eve_route" in text


def test_linux_ruleset_queues_only_the_handshake():
    assert "ct original packets 1-8" in dpi_linux.nft_ruleset(qnum=200)


def test_linux_unit_passes_the_hostlist_and_the_chosen_strategy():
    text = dpi_linux.unit_text("--dpi-desync=fake --dpi-desync-ttl=4", qnum=200)
    assert "--qnum=200" in text
    assert "--hostlist=" in text
    assert "--dpi-desync=fake --dpi-desync-ttl=4" in text
    assert "Restart=on-failure" in text


def test_windows_service_keeps_binpath_as_its_own_argument():
    """sc.exe wants `binPath=` and its value as two argv entries.

    Glue them together and sc silently prints its usage instead of creating
    anything - a bug already learned once in evo-cli.
    """
    argv = dpi_windows.create_argv("--dpi-desync=fake")
    assert "binPath=" in argv
    assert "start=" in argv
    assert argv[argv.index("start=") + 1] == "auto"
    value = argv[argv.index("binPath=") + 1]
    assert "winws.exe" in value and "--dpi-desync=fake" in value


def test_windows_steers_traffic_without_any_firewall_rule():
    """WinDivert filters in-process, so there is no rule to add or clean up."""
    value = dpi_windows.create_argv("--dpi-desync=fake")[dpi_windows.create_argv("x").index("binPath=") + 1]
    assert "--wf-tcp=80,443" in value


def test_the_ladder_only_uses_modes_the_binary_accepts():
    """Validated against `nfqws --help` for v72.13 on 2026-09-11."""
    valid_modes = {
        "synack",
        "syndata",
        "fake",
        "fakeknown",
        "rst",
        "rstack",
        "hopbyhop",
        "destopt",
        "ipfrag1",
        "multisplit",
        "multidisorder",
        "fakedsplit",
        "fakeddisorder",
        "hostfakesplit",
        "ipfrag2",
        "udplen",
        "tamper",
    }
    valid_fooling = {"none", "md5sig", "badseq", "badsum", "datanoack", "ts", "hopbyhop", "hopbyhop2"}
    assert strategy.LADDER
    for candidate in strategy.LADDER:
        for token in candidate.split():
            key, _, value = token.partition("=")
            if key == "--dpi-desync":
                assert set(value.split(",")) <= valid_modes, candidate
            elif key == "--dpi-desync-fooling":
                assert set(value.split(",")) <= valid_fooling, candidate
            else:
                assert key in {"--dpi-desync-ttl", "--dpi-desync-split-pos"}, candidate


class FakeBackend:
    def __init__(self, winner=None):
        self.winner = winner
        self.applied = []
        self.torn_down = False
        self.hostlist = None

    def write_hostlist(self, hosts):
        self.hostlist = list(hosts)

    def apply(self, strategy):
        self.applied.append(strategy)

    def teardown(self):
        self.torn_down = True


def test_the_ladder_stops_at_the_first_strategy_that_works():
    backend = FakeBackend()
    target = strategy.LADDER[1]
    found = strategy.find("x.com", backend, ["x.com"], verify=lambda h: backend.applied[-1] == target, settle=0)
    assert found == target
    assert backend.applied == strategy.LADDER[:2]
    assert not backend.torn_down


def test_a_ladder_that_never_works_tears_the_service_back_down():
    backend = FakeBackend()
    assert strategy.find("x.com", backend, ["x.com"], verify=lambda h: False, settle=0) is None
    assert backend.applied == list(strategy.LADDER)
    assert backend.torn_down


def test_a_known_good_strategy_is_tried_before_the_ladder():
    backend = FakeBackend()
    known = strategy.LADDER[-1]
    found = strategy.find("y.com", backend, ["y.com"], verify=lambda h: True, settle=0, preferred=known)
    assert found == known
    assert backend.applied == [known]


# --- QUIC ------------------------------------------------------------------
#
# A browser that falls back to HTTP/3 on UDP 443 would route straight around a
# TCP-only bypass. eve corrupts the QUIC Initial for hostlisted hosts so the
# handshake cannot authenticate and the browser drops back to TCP, where the
# bypass already works and is already verified.


def test_linux_queues_quic_on_udp_443():
    assert "udp dport 443" in dpi_linux.nft_ruleset(qnum=200)


def test_quic_gets_six_packets_for_initial_retransmissions():
    """zapret's own example uses 6 - TCP's 8 does not cover QUIC retransmits."""
    ruleset = dpi_linux.nft_ruleset(qnum=200)
    udp_line = next(line for line in ruleset.splitlines() if "udp dport" in line)
    tcp_line = next(line for line in ruleset.splitlines() if "tcp dport" in line)
    assert "ct original packets 1-6" in udp_line
    assert "ct original packets 1-8" in tcp_line


def test_tcp_and_udp_feed_the_same_queue():
    ruleset = dpi_linux.nft_ruleset(qnum=317)
    assert ruleset.count("queue num 317 bypass") == 2


def test_daemon_args_split_into_two_profiles():
    args = dpi.daemon_args("--dpi-desync=fake", "/etc/eve/route-dpi.txt")
    assert args.count("--new") == 1


def test_the_tcp_profile_is_pinned_to_tcp_so_it_cannot_swallow_udp():
    """nfqws: "setting tcp and not setting udp filter denies udp"."""
    args = dpi.daemon_args("--dpi-desync=fake", "/list")
    tcp_profile = args[: args.index("--new")]
    assert "--filter-tcp=80,443" in tcp_profile
    assert "--dpi-desync=fake" in tcp_profile
    assert not [a for a in tcp_profile if a.startswith("--filter-udp")]


def test_the_quic_profile_targets_quic_and_tampers():
    args = dpi.daemon_args("--dpi-desync=fake", "/list")
    quic_profile = args[args.index("--new") + 1 :]
    assert "--filter-udp=443" in quic_profile
    assert "--filter-l7=quic" in quic_profile
    assert dpi.QUIC_DESYNC in quic_profile


def test_both_profiles_carry_the_hostlist_so_quic_stays_per_host():
    """Other sites must keep HTTP/3 - only listed hosts get knocked back."""
    args = dpi.daemon_args("--dpi-desync=fake", "/etc/eve/route-dpi.txt")
    assert args.count("--hostlist=/etc/eve/route-dpi.txt") == 2


def test_the_quic_mode_is_one_udp_can_actually_use():
    """Only these eight modes apply to UDP - readme.en.md, "UDP support"."""
    udp_applicable = {"fake", "fakeknown", "hopbyhop", "destopt", "ipfrag1", "ipfrag2", "udplen", "tamper"}
    mode = dpi.QUIC_DESYNC.split("=", 1)[1]
    assert set(mode.split(",")) <= udp_applicable


def test_linux_unit_carries_both_profiles():
    text = dpi_linux.unit_text("--dpi-desync=fake --dpi-desync-ttl=4", qnum=200)
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "--new" in exec_line
    assert "--filter-l7=quic" in exec_line
    assert "--qnum=200" in exec_line


def test_windows_filters_udp_as_well_as_tcp():
    line = dpi_windows.command_line("--dpi-desync=fake")
    assert "--wf-tcp=80,443" in line
    assert "--wf-udp=443" in line


def test_windows_command_line_carries_both_profiles():
    line = dpi_windows.command_line("--dpi-desync=fake")
    assert "--new" in line
    assert "--filter-l7=quic" in line


# --- Windows service lifecycle ----------------------------------------------


class FakeSc:
    """A stand-in for sc.exe that reproduces Windows' deletion semantics.

    A deleted service is not gone: it stays "marked for deletion", still
    visible to `sc qc`, until every handle to it closes. Creating one in that
    window fails with 1072 - which is exactly what a delete immediately
    followed by a create walks into. Each SCM call stands in for a handle
    getting a chance to close.
    """

    def __init__(self, settle_polls=2):
        self.services = {}
        self.deleting = {}
        self.settle_polls = settle_polls
        self.calls = []

    def __call__(self, argv, **kwargs):
        import subprocess as sp

        self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ""
        name = argv[2] if len(argv) > 2 else ""

        entry = self.deleting.get(name)
        if entry is not None:
            entry["polls"] -= 1
            if entry["polls"] <= 0:
                del self.deleting[name]

        def result(code, out=""):
            return sp.CompletedProcess(argv, code, stdout=out, stderr="")

        if verb == "create":
            if name in self.deleting:
                return result(
                    1,
                    "[SC] CreateService FAILED 1072:\n\nThe specified service has been marked for deletion.",
                )
            self.services[name] = {"binpath": argv[argv.index("binPath=") + 1], "running": False}
            return result(0)

        if verb == "delete":
            svc = self.services.pop(name, None)
            if svc is None:
                return result(1060)
            self.deleting[name] = {"polls": self.settle_polls, "binpath": svc["binpath"]}
            return result(0)

        if verb == "qc":
            svc = self.services.get(name) or self.deleting.get(name)
            if not svc:
                return result(1060, "The specified service does not exist")
            return result(0, f"SERVICE_NAME: {name}\n  BINARY_PATH_NAME   : {svc['binpath']}\n")

        if verb == "query":
            svc = self.services.get(name)
            if not svc:
                return result(1060, "The specified service does not exist")
            return result(0, f"STATE : 4  {'RUNNING' if svc['running'] else 'STOPPED'}")

        if verb == "start":
            if name not in self.services:
                return result(1060)
            self.services[name]["running"] = True
            return result(0)

        if verb == "stop":
            if name in self.services:
                self.services[name]["running"] = False
            return result(0)

        return result(0)

    def creates(self):
        return [c for c in self.calls if len(c) > 1 and c[1] == "create"]


@pytest.fixture
def fake_sc(monkeypatch, tmp_path):
    sc = FakeSc()
    monkeypatch.setattr(dpi_windows.subprocess, "run", sc)
    monkeypatch.setattr(dpi_windows.paths, "dpi_hostlist_path", lambda: tmp_path / "route-dpi.txt")
    monkeypatch.setattr(dpi_windows.paths, "zapret_dir", lambda: tmp_path / "zapret")
    monkeypatch.setattr(dpi_windows.time, "sleep", lambda _s: None)
    return sc


def test_applying_the_same_strategy_twice_does_not_blow_up(fake_sc):
    """The ladder applies a strategy, then reconcile applies the same one again."""
    backend = dpi_windows.WindowsBackend()
    backend.apply("--dpi-desync=fake --dpi-desync-ttl=4")
    backend.apply("--dpi-desync=fake --dpi-desync-ttl=4")
    assert fake_sc.services[dpi_windows.SERVICE]["running"]


def test_a_matching_service_is_left_alone_rather_than_rebuilt(fake_sc):
    backend = dpi_windows.WindowsBackend()
    backend.apply("--dpi-desync=fake")
    backend.apply("--dpi-desync=fake")
    assert len(fake_sc.creates()) == 1, "rebuilt a service that was already correct"


def test_changing_strategy_waits_for_the_deletion_to_settle(fake_sc):
    """Even a genuine rebuild hits 1072 if it does not wait."""
    backend = dpi_windows.WindowsBackend()
    backend.apply("--dpi-desync=fake")
    backend.apply("--dpi-desync=multisplit --dpi-desync-split-pos=1")
    assert len(fake_sc.creates()) == 2
    assert "multisplit" in fake_sc.services[dpi_windows.SERVICE]["binpath"]


def test_a_stopped_service_with_the_right_config_is_just_started(fake_sc):
    backend = dpi_windows.WindowsBackend()
    backend.apply("--dpi-desync=fake")
    fake_sc.services[dpi_windows.SERVICE]["running"] = False
    backend.apply("--dpi-desync=fake")
    assert fake_sc.services[dpi_windows.SERVICE]["running"]
    assert len(fake_sc.creates()) == 1


def test_a_deletion_that_never_settles_reports_clearly(fake_sc, monkeypatch):
    monkeypatch.setattr(dpi_windows, "DELETION_TIMEOUT", 0.05)
    fake_sc.settle_polls = 10**6
    backend = dpi_windows.WindowsBackend()
    backend.apply("--dpi-desync=fake")
    with pytest.raises(RuntimeError, match="marked for deletion"):
        backend.apply("--dpi-desync=multisplit")


# --- Linux service lifecycle -------------------------------------------------
#
# Same defect as the Windows 1072 crash, milder symptom: reconcile runs after
# the ladder has already applied the winning strategy, so an unconditional
# restart drops live connections for no reason.


class FakeRun:
    def __init__(self, active=False, table=False):
        self.calls = []
        self.active = active
        self.table = table

    def __call__(self, cmd, check=False, input_text=None):
        import subprocess as sp

        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "is-active" in joined:
            return sp.CompletedProcess(cmd, 0, stdout="active\n" if self.active else "inactive\n", stderr="")
        if "list" in joined and "table" in joined:
            return sp.CompletedProcess(cmd, 0 if self.table else 1, stdout="", stderr="")
        if cmd[:2] == ["systemctl", "enable"] or "restart" in joined:
            self.active = True
        if cmd[:2] == ["nft", "-f"]:
            self.table = True
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    def verbs(self, tool):
        return [" ".join(c) for c in self.calls if c and c[0] == tool]


@pytest.fixture
def fake_linux(monkeypatch, tmp_path):
    run = FakeRun()
    monkeypatch.setattr(dpi_linux, "_run", run)
    monkeypatch.setattr(dpi_linux, "UNIT_PATH", tmp_path / "eve-route-dpi.service")
    monkeypatch.setattr(dpi_linux.paths, "dpi_hostlist_path", lambda: tmp_path / "route-dpi.txt")
    monkeypatch.setattr(dpi_linux.paths, "zapret_dir", lambda: tmp_path / "zapret")
    return run


def test_linux_does_not_restart_a_service_already_running_the_strategy(fake_linux):
    backend = dpi_linux.LinuxBackend()
    backend.apply("--dpi-desync=fake")
    before = len([c for c in fake_linux.verbs("systemctl") if "restart" in c])
    backend.apply("--dpi-desync=fake")
    after = len([c for c in fake_linux.verbs("systemctl") if "restart" in c])
    assert after == before, "restarted a service that was already correct"


def test_linux_does_restart_when_the_strategy_changes(fake_linux):
    backend = dpi_linux.LinuxBackend()
    backend.apply("--dpi-desync=fake")
    backend.apply("--dpi-desync=multisplit --dpi-desync-split-pos=1")
    assert "multisplit" in (dpi_linux.UNIT_PATH).read_text(encoding="utf-8")
    assert any("restart" in c for c in fake_linux.verbs("systemctl"))


def test_linux_reinstates_the_ruleset_if_it_went_missing(fake_linux):
    """The unit can be fine while a firewall flush took the table with it."""
    backend = dpi_linux.LinuxBackend()
    backend.apply("--dpi-desync=fake")
    fake_linux.table = False
    backend.apply("--dpi-desync=fake")
    assert fake_linux.table
