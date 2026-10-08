"""Route lists to import in one go: the presets that ship with eve, or your own file.

One entry per line - `HOST [PROBE]`, `#` for comments. A probe makes the entry
cover the whole domain, exactly like `eve route add HOST --probe PROBE`.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

SUFFIX = ".txt"


def _directory():
    return resources.files("eve.route") / "presets"


def available():
    """(name, summary) for every shipped preset; the summary is its first comment."""
    found = []
    for item in sorted(_directory().iterdir(), key=lambda p: p.name):
        if not item.name.endswith(SUFFIX):
            continue
        summary = next(
            (
                line.lstrip("#").strip()
                for line in item.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            ),
            "",
        )
        found.append((item.name[: -len(SUFFIX)], summary))
    return found


def parse(text, origin="<text>"):
    """[(host, probe-or-None)] from the preset format. Raises ValueError on a bad line."""
    entries = []
    for number, raw in enumerate(text.splitlines(), start=1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) > 2:
            raise ValueError(f"{origin}:{number}: expected `HOST [PROBE]`, got {raw.strip()!r}")
        host = fields[0].lower()
        probe = fields[1].lower() if len(fields) == 2 else None
        if probe and probe != host and not probe.endswith(f".{host}"):
            raise ValueError(f"{origin}:{number}: probe {probe} is not under {host}")
        entries.append((host, probe))
    if not entries:
        raise ValueError(f"{origin} has no entries")
    return entries


def load(source):
    """Entries from a file path if `source` is one, otherwise from the shipped preset of that name."""
    path = Path(source)
    if path.is_file():
        return parse(path.read_text(encoding="utf-8"), origin=str(path))

    preset = _directory() / f"{source}{SUFFIX}"
    if not preset.is_file():
        names = ", ".join(name for name, _ in available()) or "none"
        raise ValueError(f"{source} is neither a file nor a preset. Presets: {names}")
    return parse(preset.read_text(encoding="utf-8"), origin=source)
