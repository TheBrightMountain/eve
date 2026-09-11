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
