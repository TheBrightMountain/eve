"""eve's own memory for `eve aws`. Never credentials - those stay with the AWS CLI.

~/.config/eve/aws.json                  which account and project are current
~/.config/eve/aws/projects/<name>.json  one file per project: its account, type
                                        and every ID eve needs (shareable with
                                        `eve aws project export` / `import`)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from eve.aws.runner import AwsError


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "eve"


def state_path():
    return config_dir() / "aws.json"


def projects_dir():
    return config_dir() / "aws" / "projects"


def project_path(name):
    return projects_dir() / f"{name}.json"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# -- current selections ----------------------------------------------------------------------------


def empty():
    return {"current_account": None, "current_project": None}


def _read_state():
    path = state_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def migrate_if_needed():
    """Run once before anything reads projects."""
    data = _read_state()
    if data.get("projects"):
        _migrate_legacy(data)


def load():
    migrate_if_needed()
    data = _read_state()
    return {**empty(), **{k: v for k, v in data.items() if k in ("current_account", "current_project")}}


def save(state):
    _write_json(state_path(), {k: state.get(k) for k in ("current_account", "current_project")})


def _migrate_legacy(data):
    """Projects used to live inside aws.json with their IDs in <path>/ids.txt.
    Turn each into its own JSON file, then drop the old list."""
    from eve.aws.config import from_ids_file  # local import: config imports this module

    for name, entry in data["projects"].items():
        if project_path(name).exists():
            continue
        save_project(from_ids_file(name, entry))
    data.pop("projects")
    _write_json(state_path(), data)


# -- projects --------------------------------------------------------------------------------------


def project_names():
    migrate_if_needed()
    folder = projects_dir()
    return sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []


def load_project(name):
    migrate_if_needed()
    path = project_path(name)
    if not path.exists():
        raise AwsError(f"Unknown project {name!r}.", "eve aws project ls")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AwsError(f"{path} is not valid JSON ({exc}).", f"eve aws project edit {name}") from exc
    data["name"] = name
    return data


def save_project(data):
    _write_json(project_path(data["name"]), data)


def delete_project(name):
    project_path(name).unlink(missing_ok=True)


def projects_using(account):
    return [n for n in project_names() if load_project(n).get("account") == account]
