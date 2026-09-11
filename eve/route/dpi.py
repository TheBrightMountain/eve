"""Shared zapret plumbing and the platform backend selector.

Both backends run the same tool from the same release: zapret's `nfqws` on
Linux, its Windows port `winws.exe` on Windows. They share the desync flag
vocabulary, so the strategy ladder is written once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ZAPRET_VERSION = "v72.13"
TARBALL_URL = f"https://github.com/bol-van/zapret/releases/download/{ZAPRET_VERSION}/zapret-{ZAPRET_VERSION}.tar.gz"

# Verified against the sha256sum.txt published with the release, 2026-09-11.
# The check is on each extracted member rather than the tarball, so it lands on
# the bytes that actually execute.
ARTIFACTS = {
    "linux": {
        "binaries/linux-x86_64/nfqws": "f34615964d7321650197cd69d3f7cbfdaabe8118b5a0d57c6e41dccdff658999",
    },
    "windows": {
        "binaries/windows-x86_64/winws.exe": "a14bff1df6234ea555d2e0c61b589f0707c0b12d6c9b7eeccda76012154996e8",
        "binaries/windows-x86_64/cygwin1.dll": "103104a52e5293ce418944725df19e2bf81ad9269b9a120d71d39028e821499b",
        "binaries/windows-x86_64/WinDivert.dll": "c1e060ee19444a259b2162f8af0f3fe8c4428a1c6f694dce20de194ac8d7d9a2",
        "binaries/windows-x86_64/WinDivert64.sys": "8da085332782708d8767bcace5327a6ec7283c17cfb85e40b03cd2323a90ddc2",
    },
}


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def download_tarball(destination=None, opener=urllib.request.urlopen):
    destination = Path(destination or (Path(tempfile.gettempdir()) / f"zapret-{ZAPRET_VERSION}.tar.gz"))
    with opener(TARBALL_URL) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)
    return destination


def extract_verified(archive, members, dest):
    """Pull just `members` out of the tarball, checking each one's sha256.

    A member whose digest does not match is refused and removed - a tampered
    download must never end up on disk, let alone get executed.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    with tarfile.open(archive) as tf:
        root = f"zapret-{ZAPRET_VERSION}/"
        for member, expected in members.items():
            try:
                handle = tf.extractfile(root + member)
            except KeyError:
                handle = None
            if handle is None:
                raise RuntimeError(f"{member} is missing from the zapret archive")
            payload = handle.read()
            actual = sha256_bytes(payload)
            if actual != expected:
                for path in written:
                    path.unlink(missing_ok=True)
                raise RuntimeError(f"refusing a tampered download - sha256 mismatch on {member}: got {actual}")
            target = dest / Path(member).name
            target.write_bytes(payload)
            written.append(target)

    for path in written:
        if path.suffix in ("", ".exe"):
            path.chmod(0o755)
    return written


def backend(name=None):
    name = name or ("windows" if os.name == "nt" else "linux")
    if name == "windows":
        from eve.route import dpi_windows

        return dpi_windows.WindowsBackend()
    from eve.route import dpi_linux

    return dpi_linux.LinuxBackend()
