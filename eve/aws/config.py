"""A project: its account, type and the AWS IDs eve needs, stored as JSON.

    {
      "name": "moodle-lab", "type": "moodle", "account": "default",
      "account_id": "261732724428", "path": "/home/nms/moodle-aws",
      "cluster": "moodle", "services": ["moodle-web", "moodle-cron"],
      "default_service": "web", "containers": {"web": "php"},
      "db_id": "moodle-db", "target_group_arn": "arn:...", "site_url": "https://...",
      "log_group": "/ecs/moodle", "registry": "...", "repos": ["moodle-php"],
      "files_bucket": "...", "recipes": {"hello": {"cmd": "echo hi", "service": "cron"}},
      "resources": {"vpc": "vpc-...", ...}
    }

`resources` is a free-form record of everything else (VPC, subnets, ARNs...).
"""

from __future__ import annotations

from pathlib import Path

from eve.aws.runner import AwsError

# Keys eve reads, and what they're for. `list` keys hold several values.
KEYS = {
    "account_id": ("str", "AWS account the project lives in - actions refuse to run anywhere else"),
    "path": ("str", "project folder (git repo) - used by `push` for the commit tag"),
    "cluster": ("str", "ECS cluster name"),
    "services": ("list", "ECS service names; the part after the last '-' is the short name (web, cron)"),
    "default_service": ("str", "service used when none is given (short or full name)"),
    "containers": ("dict", "containers.<short service>=<container> for exec/run"),
    "db_id": ("str", "RDS instance id (optional)"),
    "target_group_arn": ("str", "ALB target group (optional, for status)"),
    "site_url": ("str", "URL checked by status/unpark (optional)"),
    "log_group": ("str", "CloudWatch log group"),
    "registry": ("str", "ECR registry host"),
    "repos": ("list", "ECR repositories pushed from <repo>:<local_image_tag>"),
    "local_image_tag": ("str", "local tag pushed from (default: local)"),
    "files_bucket": ("str", "S3 bucket listed by `files`"),
    "recipes": ("dict", "recipes.<name>.cmd / recipes.<name>.service - your own `run` commands"),
    "resources": ("dict", "free-form record of other IDs (vpc, subnets, ARNs...)"),
}
LIST_KEYS = {k for k, (kind, _) in KEYS.items() if kind == "list"}
IDENTITY_KEYS = ("name", "type", "account")


def short_name(service):
    return service.rsplit("-", 1)[-1]


class Project:
    def __init__(self, data):
        self.data = data
        self.name = data["name"]
        self.kind = data.get("type", "ecs")
        self.account = data.get("account")

    @property
    def path(self):
        value = self.data.get("path")
        return Path(value).expanduser() if value else None

    def get(self, key, default=None):
        value = self.data.get(key)
        return default if value in (None, "", [], {}) else value

    def require(self, key):
        value = self.get(key)
        if value is None:
            hint = f"eve aws project set {self.name} {key} <value>"
            raise AwsError(f"{key} is not set for project {self.name}.", hint)
        return value

    def list_of(self, key, required=True):
        value = self.require(key) if required else self.get(key, [])
        return value.split() if isinstance(value, str) else list(value)

    @property
    def services(self):
        return self.list_of("services")

    def container_for(self, service):
        return (self.get("containers") or {}).get(short_name(service))

    def recipes(self):
        found = {}
        for name, recipe in (self.get("recipes") or {}).items():
            recipe = {"cmd": recipe} if isinstance(recipe, str) else recipe
            if recipe.get("cmd"):
                found[name] = {"cmd": recipe["cmd"], "service": recipe.get("service"), "source": "project"}
        return found

    def resolve_service(self, given):
        for full in self.services:
            if given in (full, short_name(full)):
                return full
        raise AwsError(f"No service {given!r} in project {self.name}.", f"Services: {', '.join(self.services)}")


# -- converting the old KEY=VALUE ids.txt ----------------------------------------------------------

LEGACY = {
    "ACCOUNT_ID": "account_id",
    "CLUSTER": "cluster",
    "SERVICES": "services",
    "DEFAULT_SERVICE": "default_service",
    "DB_ID": "db_id",
    "TG_ARN": "target_group_arn",
    "SITE_URL": "site_url",
    "LOG_GROUP": "log_group",
    "REGISTRY": "registry",
    "REPOS": "repos",
    "LOCAL_IMAGE_TAG": "local_image_tag",
    "FILES_BUCKET": "files_bucket",
}
LEGACY_IDS_CANDIDATES = (Path("ids.txt"), Path("aws") / "ids.txt")


def parse_ids(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def from_ids_values(name, kind, account, path, values):
    data = {"name": name, "type": kind, "account": account}
    if path:
        data["path"] = str(path)
    containers, recipes, resources = {}, {}, {}
    for key, value in values.items():
        if key in LEGACY:
            field = LEGACY[key]
            data[field] = value.split() if field in LIST_KEYS else value
        elif key.startswith("CONTAINER_"):
            containers[key[len("CONTAINER_") :]] = value
        elif key.startswith("RECIPE_") and key.endswith("_SERVICE"):
            recipes.setdefault(key[len("RECIPE_") : -len("_SERVICE")], {})["service"] = value
        elif key.startswith("RECIPE_"):
            recipes.setdefault(key[len("RECIPE_") :], {})["cmd"] = value
        else:
            resources[key.lower()] = value
    for field, found in (("containers", containers), ("recipes", recipes), ("resources", resources)):
        if found:
            data[field] = found
    return data


def find_ids_file(folder):
    root = Path(folder).expanduser()
    for rel in LEGACY_IDS_CANDIDATES:
        if (root / rel).exists():
            return root / rel
    return None


def from_ids_file(name, entry):
    """Build a project from an old-style aws.json entry and its ids.txt (if still there)."""
    found = find_ids_file(entry["path"])
    values = parse_ids(found.read_text(encoding="utf-8")) if found else {}
    return from_ids_values(name, entry.get("type", "ecs"), entry.get("account"), entry["path"], values)


# -- editing values by dotted key ------------------------------------------------------------------


def set_value(data, key, values):
    """`services a b`, `containers.web php`, `recipes.hello.cmd echo hi`."""
    parts = key.split(".")
    top = parts[0]
    if top in IDENTITY_KEYS:
        raise AwsError(f"Use `eve aws project edit` to change {top}.")
    if top not in KEYS:
        raise AwsError(f"Unknown key {top!r}.", "Known keys: " + ", ".join(KEYS))
    if len(parts) == 1 and KEYS[top][0] == "dict":
        raise AwsError(f"{top} holds several values - set {top}.<name> instead.")
    value = list(values) if (len(parts) == 1 and top in LIST_KEYS) else " ".join(values)
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise AwsError(f"{key}: {part} is not a group of values.")
    target[parts[-1]] = value
    return data


def unset_value(data, key):
    parts = key.split(".")
    if parts[0] in IDENTITY_KEYS:
        raise AwsError(f"{parts[0]} can't be removed.")
    target = data
    for part in parts[:-1]:
        target = target.get(part)
        if not isinstance(target, dict):
            raise AwsError(f"{key} is not set.")
    if parts[-1] not in target:
        raise AwsError(f"{key} is not set.")
    del target[parts[-1]]
    return data
