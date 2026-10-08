import rich_click as click

from eve.route.commands import add, check, import_, ls, rm, sync

HELP = """Find a way through to somewhere the network is keeping from you.

Two blocks look identical from a browser and need opposite fixes. A **poisoned
DNS** answer hands you a dead address while the route itself stays open, so
pinning the real address fixes it. A **DPI filter** reading the server name out
of your TLS handshake blocks every address for that name, and no hosts or DNS
edit can touch it.

- `check <host>` - what stands in the way, and which of the two it is
- `add <host>` - apply the fix the diagnosis calls for, and remember it
- `import <preset|file>` - add a whole list at once (`import --list` for presets)
- `rm <host>` - undo it again
- `ls` - every route eve is holding open
- `sync` - re-check them all and repair whatever drifted

`check` and `ls` are read-only and need no privileges. `add`, `import`, `rm` and `sync`
edit hosts or manage a service, so they want administrator rights.
"""


@click.group("route", help=HELP)
def route_group():
    pass


for command in (check, add, import_, rm, ls, sync):
    route_group.add_command(command)
