"""Project types: what eve knows about a kind of app beyond plain ECS.

`ecs` is the generic type. A type can set a default container, a user that
built-in recipes run as, and built-in recipes. ids.txt can add recipes or
override built-in ones by name.
"""

from __future__ import annotations

import shlex

TYPES = {
    "ecs": {
        "description": "Generic ECS service(s) + optional RDS. Status, park/unpark, logs, exec, push.",
        "container": None,
        "user": None,
        "recipes": {},
    },
    "moodle": {
        "description": "Moodle on ECS: container `php`, recipes run as `www-data`.",
        "container": "php",
        "user": "www-data",
        "recipes": {
            "purge": {
                # objectfs switched off for this one command, so a purge still works when
                # the plugin list cache is stale and the file system class can't be found.
                "cmd": "OBJECTFS_BUCKET= php admin/cli/purge_caches.php",
                "help": "Purge all Moodle caches (run after every deploy)",
            },
            "upgrade": {
                "cmd": "php admin/cli/upgrade.php --non-interactive",
                "help": "Install new plugins / run DB upgrades",
            },
            "cron": {
                "cmd": "php admin/cli/cron.php",
                "help": "Run Moodle cron once",
            },
            "reset-password": {
                "cmd": "php admin/cli/reset_password.php --username={username} --password={password}",
                "help": "Reset a user's password (prompts; note: the command is recorded by AWS)",
                "prompts": [("username", False), ("password", True)],
            },
        },
    },
}


def names():
    return sorted(TYPES)


def get(kind):
    return TYPES.get(kind, TYPES["ecs"])


def recipes_for(project):
    """Built-in recipes for the project's type, overlaid with ids.txt recipes."""
    kind = get(project.kind)
    merged = {}
    for name, recipe in kind["recipes"].items():
        merged[name] = {**recipe, "source": project.kind, "user": kind["user"]}
    for name, recipe in project.recipes().items():
        merged[name] = {**merged.get(name, {}), **recipe, "user": None}
    return merged


def fill(recipe, answers):
    """Insert prompted values into a recipe command, shell-quoted."""
    return recipe["cmd"].format(**{k: shlex.quote(v) for k, v in answers.items()})
