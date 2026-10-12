"""A project's details, read from `<project>/aws/ids.txt`.

The file is the human record of a project's IDs, so eve only reads the keys it
needs and ignores the rest. One `KEY=VALUE` per line; `#` comments and blank
lines are skipped; the value is everything after the first `=`.
"""

from __future__ import annotations

from pathlib import Path

from eve.aws.runner import AwsError

IDS_RELPATH = Path("aws") / "ids.txt"


def ids_file(project_path):
    return Path(project_path).expanduser() / IDS_RELPATH


def parse(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class Project:
    def __init__(self, name, path, kind, account, values):
        self.name = name
        self.path = Path(path).expanduser()
        self.kind = kind
        self.account = account
        self.values = values

    @classmethod
    def load(cls, name, entry):
        path = ids_file(entry["path"])
        if not path.exists():
            raise AwsError(f"Project {name}: {path} not found.", "Check the path with `eve aws project ls`.")
        return cls(name, entry["path"], entry.get("type", "ecs"), entry.get("account"), parse(path.read_text("utf-8")))

    @property
    def ids_path(self):
        return ids_file(self.path)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, key):
        value = self.values.get(key)
        if not value:
            raise AwsError(f"{key} is not set for project {self.name}.", f"Add `{key}=...` to {self.ids_path}.")
        return value

    def words(self, key, required=True):
        value = self.require(key) if required else self.values.get(key, "")
        return value.split()

    # -- services -----------------------------------------------------------

    @property
    def services(self):
        return self.words("SERVICES")

    def resolve_service(self, given):
        """Accept a short name (`web`) or the full one (`moodle-web`)."""
        for full in self.services:
            if given in (full, short_name(full)):
                return full
        raise AwsError(f"No service {given!r} in project {self.name}.", f"Services: {', '.join(self.services)}")

    def recipes(self):
        """Custom recipes from ids.txt: RECIPE_<name> and RECIPE_<name>_SERVICE."""
        found = {}
        for key, value in self.values.items():
            if not key.startswith("RECIPE_") or key.endswith("_SERVICE"):
                continue
            name = key[len("RECIPE_") :]
            found[name] = {"cmd": value, "service": self.values.get(f"RECIPE_{name}_SERVICE"), "source": "ids.txt"}
        return found


def short_name(service):
    return service.rsplit("-", 1)[-1]
