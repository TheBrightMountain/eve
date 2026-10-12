"""Choosing one thing out of several: flag → default → the only one → ask."""

from __future__ import annotations

import sys

import rich_click as click

from eve.aws.runner import AwsError
from eve.console import console


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose(items, what, given=None, default=None, label=lambda x: x, match=lambda x, g: x == g):
    """Return one of `items`.

    `given` (a flag) wins, then `default`, then the only item; otherwise a
    numbered picker in a terminal, or an error listing the choices.
    """
    items = list(items)
    if not items:
        raise AwsError(f"No {what} found.")
    for wanted in (given, default):
        if wanted:
            for item in items:
                if match(item, wanted):
                    return item
            if wanted is given:
                raise AwsError(f"No {what} matches {given!r}.", "Choices: " + ", ".join(label(i) for i in items))
    if len(items) == 1:
        return items[0]
    if not interactive():
        raise AwsError(f"Several {what}s - pick one.", "Choices: " + ", ".join(label(i) for i in items))
    console.print(f"[warning]?[/warning] Several {what}s - pick one:")
    for n, item in enumerate(items, 1):
        console.print(f"  {n}) {label(item)}")
    index = click.prompt(">", type=click.IntRange(1, len(items)))
    return items[index - 1]
