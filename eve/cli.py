import rich_click as click

from eve import __version__
from eve.dns import dns_group
from eve.route import route_group

click.rich_click.TEXT_MARKUP = "markdown"
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.STYLE_OPTIONS_TABLE_BOX = "SIMPLE"
click.rich_click.STYLE_COMMANDS_TABLE_BOX = "SIMPLE"
click.rich_click.STYLE_OPTION = "bold cyan"
click.rich_click.STYLE_COMMAND = "bold cyan"
click.rich_click.STYLE_SWITCH = "bold green"

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-v", "--version", prog_name="eve")
def cli():
    """**eve** - a personal toolbox of commands worth keeping.

    Run any command with `-h` for details.
    """


cli.add_command(route_group)
cli.add_command(dns_group)


def main():
    cli()
