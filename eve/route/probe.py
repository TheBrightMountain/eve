"""Open real TLS connections and work out what, if anything, is in the way."""

from __future__ import annotations

import socket
import ssl
import time

from eve.route import dns

PORT = 443

# A name nobody blocks, used as the control in the SNI experiment below.
CONTROL_SNI = "example.com"

OPEN = "open"
DNS_POISONED = "dns-poisoned"
SNI_BLOCKED = "sni-blocked"
IP_BLOCKED = "ip-blocked"
UNREACHABLE = "unreachable"

VERDICT_TEXT = {
    OPEN: "reachable - nothing in the way",
    DNS_POISONED: "DNS is poisoned, the route itself is open",
    SNI_BLOCKED: "DPI blocks the TLS handshake by server name",
    IP_BLOCKED: "the address itself is blocked or dead",
    UNREACHABLE: "no address answers at all",
}

# Which verdicts mean "you can reach the host right now".
REACHABLE = (OPEN, DNS_POISONED)

# How stuck each verdict leaves you, worst last. One probe is a single
# measurement of something that is not stable - a host can read `open` and then
# `sni-blocked` a minute later - so repeated samples need an ordering to be
# summarised honestly.
SEVERITY = [OPEN, DNS_POISONED, SNI_BLOCKED, IP_BLOCKED, UNREACHABLE]


def severity(verdict):
    return SEVERITY.index(verdict)


def worst(verdicts):
    """The verdict that matters across samples.

    A host blocked one time in three is blocked: an intermittent failure is
    still the thing you need to fix, and calling it `open` because the majority
    of probes got lucky would hide exactly the problem worth reporting.
    """
    return max(verdicts, key=severity)


def _ms(start):
    return (time.perf_counter() - start) * 1000


def tls_probe(ip, sni, timeout=6.0):
    """Open one TLS connection to ``ip`` announcing ``sni``.

    Certificate checking is off on purpose: the question is whether the
    handshake is allowed to finish, not whether the certificate matches the
    name we announced.
    """
    result = {"ip": ip, "sni": sni, "tcp": False, "tls": False, "ms": None, "error": None, "reset": False}
    started = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, PORT))
        result["tcp"] = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(sock, server_hostname=sni):
            result["tls"] = True
            result["ms"] = _ms(started)
    except ConnectionResetError as exc:
        result["error"] = str(exc)
        result["reset"] = True
    except TimeoutError as exc:
        result["error"] = f"timeout: {exc}"
    except ssl.SSLError as exc:
        result["error"] = str(exc)
        result["reset"] = "reset" in str(exc).lower() or "ILLEGAL_MESSAGE" in str(exc)
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return result


def rank(host, candidates, timeout=6.0):
    rows = [tls_probe(ip, host, timeout=timeout) for ip in candidates]
    rows.sort(key=lambda r: (not r["tls"], r["ms"] if r["ms"] is not None else 1e9))
    return rows


def reachable(host, timeout=6.0):
    """Cheap yes/no used to verify a fix actually worked."""
    answers = dns.gather(host, include_vn=False)
    candidates = answers["trusted"] or [ip for ip in (answers["system"] or []) if not dns.is_bogus(ip)]
    return any(tls_probe(ip, host, timeout=timeout)["tls"] for ip in candidates)


def diagnose(host, timeout=6.0, include_vn=True):
    answers = dns.gather(host, include_vn=include_vn)
    system_ips = answers["system"] or []
    trusted = answers["trusted"]

    poisoned = bool(system_ips) and all(dns.is_bogus(ip) for ip in system_ips)
    if not poisoned and system_ips and trusted:
        poisoned = not set(system_ips) & set(trusted) and any(dns.is_bogus(ip) for ip in system_ips)

    candidates = trusted or [ip for ip in system_ips if not dns.is_bogus(ip)]
    probes = rank(host, candidates, timeout=timeout) if candidates else []
    working = [row for row in probes if row["tls"]]

    control = None
    if candidates and not working:
        control = tls_probe(candidates[0], CONTROL_SNI, timeout=timeout)

    return {
        "host": host,
        "verdict": verdict(candidates, probes, working, control, poisoned),
        "poisoned": poisoned,
        "system_ips": system_ips,
        "answers": answers["resolvers"],
        "candidates": candidates,
        "probes": probes,
        "working": working,
        "control": control,
        "best": working[0]["ip"] if working else None,
    }


def verdict(candidates, probes, working, control, poisoned):
    if not candidates:
        return UNREACHABLE
    if working:
        return DNS_POISONED if poisoned else OPEN
    if control and control["tls"]:
        return SNI_BLOCKED
    if any(row["tcp"] for row in probes):
        return IP_BLOCKED
    return UNREACHABLE
