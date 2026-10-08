import rich_click as click

from eve.dns.commands import reset, set_, show

HELP = """Choose which DNS servers the machine asks.

- `show` - what each connected interface uses now, and who set it
- `set <server>...` - switch to these servers, after checking they answer
- `reset` - put back exactly what was there before eve changed it

`show` needs no privileges; `set` and `reset` want administrator rights.

Changing DNS fixes a **poisoned** answer, not a DPI block - for that, see
`eve route`.
"""


@click.group("dns", help=HELP)
def dns_group():
    pass


for command in (show, set_, reset):
    dns_group.add_command(command)
