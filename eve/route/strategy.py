"""Find a desync strategy that actually gets through, by trying them.

No single strategy beats every DPI - zapret ships `blockcheck.sh` precisely
because the answer depends on the ISP. eve does the same thing in Python,
verifying each candidate with the real TLS probe it already has.
"""

from __future__ import annotations

import time

# Validated against `nfqws --help` for zapret v72.13 on 2026-09-11: every mode
# and fooling value below appears in the binary's own accepted list.
LADDER = [
    "--dpi-desync=fake --dpi-desync-ttl=4",
    "--dpi-desync=fake --dpi-desync-fooling=badseq",
    "--dpi-desync=multisplit --dpi-desync-split-pos=1",
    "--dpi-desync=fake,multisplit --dpi-desync-split-pos=1 --dpi-desync-fooling=badseq",
    "--dpi-desync=fakeddisorder --dpi-desync-split-pos=1 --dpi-desync-fooling=badseq",
]

SETTLE_SECONDS = 1.5


def candidates(preferred=None):
    """The ladder, with a known-good strategy pulled to the front."""
    if not preferred:
        return list(LADDER)
    return [preferred] + [c for c in LADDER if c != preferred]


def find(host, backend, hosts, verify, preferred=None, settle=SETTLE_SECONDS, on_try=None):
    """Apply each candidate until `verify(host)` says the handshake got through.

    Returns the winning strategy, or None. A failed search does not tear the
    service down: other hosts may already depend on it. The caller restores
    the previous state from the ledger instead.
    """
    backend.write_hostlist(hosts)
    for candidate in candidates(preferred):
        if on_try:
            on_try(candidate)
        backend.apply(candidate)
        if settle:
            time.sleep(settle)
        if verify(host):
            return candidate
    return None
