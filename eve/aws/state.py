"""eve's own memory for `eve aws`: which account and project are current, and
where each project lives. Never credentials - those stay with the AWS CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path


def state_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "eve" / "aws.json"


def empty():
    return {"current_account": None, "current_project": None, "projects": {}}


def load():
    path = state_path()
    if not path.exists():
        return empty()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {**empty(), **data}


def save(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_project(state, name, path, kind, account):
    state["projects"][name] = {"path": str(path), "type": kind, "account": account}
    return state


def remove_project(state, name):
    state["projects"].pop(name, None)
    if state.get("current_project") == name:
        state["current_project"] = None
    return state


def projects_using(state, account):
    return sorted(n for n, p in state["projects"].items() if p.get("account") == account)
