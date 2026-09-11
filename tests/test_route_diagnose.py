"""DNS resolution and TLS probing - the diagnosis half, ported from evo-cli."""

import socket
import struct

from eve.route import dns, probe


def test_is_bogus():
    assert dns.is_bogus("127.0.0.1")
    assert dns.is_bogus("0.0.0.0")
    assert not dns.is_bogus("162.159.152.4")
    assert not dns.is_bogus("118.68.82.144")


def test_encode_qname_round_trip():
    encoded = dns.encode_qname("store.steampowered.com")
    assert encoded.startswith(b"\x05store")
    assert encoded.endswith(b"\x00")
    assert dns.skip_name(encoded, 0) == len(encoded)


def test_query_a_parses_an_answer(monkeypatch):
    host = "example.com"
    qname = dns.encode_qname(host)
    header = struct.pack(">HHHHHH", 0x4242, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 1, 1)
    answer = qname + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("93.184.216.34")
    packet = header + question + answer

    class FakeSocket:
        def __init__(self, *a, **kw):
            pass

        def settimeout(self, _):
            pass

        def sendto(self, *_):
            pass

        def recvfrom(self, _):
            return packet, ("8.8.8.8", 53)

        def close(self):
            pass

    monkeypatch.setattr(dns.socket, "socket", FakeSocket)
    assert dns.query_a(host, "8.8.8.8") == ["93.184.216.34"]


def test_trusted_union_drops_bogus_and_untrusted():
    answers = {
        "google": ["1.2.3.4"],
        "doh": ["1.2.3.4", "5.6.7.8"],
        "vnpt": ["127.0.0.1", "9.9.9.9"],
        "cloudflare": ["127.0.0.1"],
    }
    assert dns.trusted_union(answers) == ["1.2.3.4", "5.6.7.8"]


def _row(ip, tcp=True, tls=False):
    return {"ip": ip, "sni": "x", "tcp": tcp, "tls": tls, "ms": 1.0 if tls else None, "error": None, "reset": not tls}


def test_verdict_open_when_a_probe_completes():
    rows = [_row("1.2.3.4", tls=True)]
    assert probe.verdict(["1.2.3.4"], rows, rows, None, False) == probe.OPEN


def test_verdict_dns_poisoned_when_route_works_but_answer_lied():
    rows = [_row("1.2.3.4", tls=True)]
    assert probe.verdict(["1.2.3.4"], rows, rows, None, True) == probe.DNS_POISONED


def test_verdict_sni_blocked_when_control_name_gets_through():
    assert probe.verdict(["1.2.3.4"], [_row("1.2.3.4")], [], _row("1.2.3.4", tls=True), False) == probe.SNI_BLOCKED


def test_verdict_ip_blocked_when_control_name_fails_too():
    assert probe.verdict(["1.2.3.4"], [_row("1.2.3.4")], [], _row("1.2.3.4"), False) == probe.IP_BLOCKED


def test_verdict_unreachable_without_candidates():
    assert probe.verdict([], [], [], None, False) == probe.UNREACHABLE


def test_verdict_unreachable_when_tcp_never_opens():
    rows = [_row("1.2.3.4", tcp=False)]
    assert probe.verdict(["1.2.3.4"], rows, [], _row("1.2.3.4", tcp=False), False) == probe.UNREACHABLE
