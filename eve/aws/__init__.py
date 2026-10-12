import rich_click as click

from eve.aws.actions import ACTIONS
from eve.aws.creds import creds_group
from eve.aws.project import project_group

HELP = """Run the AWS chores of an ECS project without retyping the commands.

Pick an account and a project once, then act on it:

- `creds` - the AWS accounts you sign in to (`ls`, `add`, `login`, `logout`, `rm`)
- `project` - the projects you work on (`ls`, `add`, `enter`, `leave`, `rm`, `types`)
- `status` · `park` · `unpark` · `logs` · `exec` · `run` · `push` · `files` - act on the current project

Every step prints the real `aws` / `docker` command it runs, so you keep
learning the CLI. `-q` hides them; `-n` shows what would change without changing it.
"""


@click.group("aws", help=HELP)
@click.option("-q", "--quiet", is_flag=True, help="Hide the underlying aws/docker commands.")
@click.option(
    "-n", "--dry-run", is_flag=True, help="Show commands that would change something; run only read-only ones."
)
@click.pass_context
def aws_group(ctx, quiet, dry_run):
    ctx.ensure_object(dict)
    ctx.obj.update(quiet=quiet, dry_run=dry_run)


aws_group.add_command(creds_group)
aws_group.add_command(project_group)
for action in ACTIONS:
    aws_group.add_command(action)
