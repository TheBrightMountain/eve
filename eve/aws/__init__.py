from typing import ClassVar

import rich_click as click

from eve.aws.account import account_group
from eve.aws.actions import ACTIONS
from eve.aws.project import project_group

HELP = """Run the AWS chores of an ECS project without retyping the commands.

Pick an account and a project once, then act on it:

- `account` (or `acc`) - the AWS accounts you sign in to (`ls`, `add`, `login`, `logout`, `rm`)
- `project` - the projects you work on (`ls`, `add`, `enter`, `leave`, `rm`, `types`)
- `status` · `park` · `unpark` · `logs` · `exec` · `run` · `push` · `files` - act on the current project

Every step prints the real `aws` / `docker` command it runs, so you keep
learning the CLI. `-q` hides them; `-n` shows what would change without changing it.
"""


class AliasedGroup(click.RichGroup):
    """A group that also answers to short aliases (`acc` for `account`) without listing them twice."""

    ALIASES: ClassVar[dict] = {"acc": "account"}

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self.ALIASES.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        name, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd else name), cmd, rest


@click.group("aws", cls=AliasedGroup, help=HELP)
@click.option("-q", "--quiet", is_flag=True, help="Hide the underlying aws/docker commands.")
@click.option(
    "-n", "--dry-run", is_flag=True, help="Show commands that would change something; run only read-only ones."
)
@click.pass_context
def aws_group(ctx, quiet, dry_run):
    ctx.ensure_object(dict)
    ctx.obj.update(quiet=quiet, dry_run=dry_run)


aws_group.add_command(account_group)
aws_group.add_command(project_group)
for action in ACTIONS:
    aws_group.add_command(action)
