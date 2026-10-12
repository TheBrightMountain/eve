"""The one place `eve aws` touches subprocess.

Every command is printed before it runs - the point of `eve aws` is to keep
teaching the real CLI, not to hide it. `quiet` silences that, `dry_run` prints
the commands that would change something without running them.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

from eve.console import console

INSTALL_HINTS = {
    "aws": "Install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    "docker": "Install Docker: https://docs.docker.com/engine/install/",
    "session-manager-plugin": "Install the Session Manager plugin: "
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html",
    "git": "Install git.",
}


class AwsError(RuntimeError):
    """A command failed. `hint` is the next thing to try, if eve knows one."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _execute(argv, capture=True, stdin_from=None):
    """Run argv. Tests replace this function."""
    if stdin_from is not None:
        first = subprocess.Popen(stdin_from, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = subprocess.run(argv, stdin=first.stdout, capture_output=True, text=True, check=False)
        first.stdout.close()
        first_err = first.stderr.read().decode(errors="replace")
        first.wait()
        if first.returncode != 0:
            return Result(first.returncode, "", first_err)
        return Result(proc.returncode, proc.stdout, proc.stderr)
    if capture:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        return Result(proc.returncode, proc.stdout, proc.stderr)
    proc = subprocess.run(argv, check=False)
    return Result(proc.returncode)


def _which(tool):
    """Tests replace this too."""
    return shutil.which(tool)


def explain(stderr):
    """Map well-known aws errors to something actionable."""
    text = stderr.strip()
    if "session has expired" in text or "ExpiredToken" in text or "reauthenticate" in text:
        return "Your sign-in has expired.", "eve aws account login <name>"
    if "Unable to locate credentials" in text or "could not be found" in text and "profile" in text:
        return "No credentials for this account.", "eve aws account login <name>"
    if "AccessDenied" in text and "assumed-role/" in text and os.environ.get("AWS_ACCESS_KEY_ID"):
        return (
            text.splitlines()[-1] if text else "Access denied.",
            (
                "AWS_* credentials in your environment override the profile - unset AWS_ACCESS_KEY_ID, "
                "AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN, or use another terminal."
            ),
        )
    last = [line for line in text.splitlines() if line.strip()]
    return (last[-1] if last else "command failed"), None


class Runner:
    def __init__(self, quiet=False, dry_run=False, profile=None):
        self.quiet = quiet
        self.dry_run = dry_run
        self.profile = profile

    # -- printing -----------------------------------------------------------

    def show(self, argv, prefix="$"):
        if not self.quiet:
            console.print(f"  [dim]{prefix} {shlex.join(argv)}[/dim]", highlight=False, soft_wrap=True)

    # -- building -----------------------------------------------------------

    def aws_argv(self, args, profile=True):
        argv = ["aws", *args]
        if profile and self.profile:
            argv += ["--profile", self.profile]
        return argv

    def _need(self, tool):
        if not _which(tool):
            raise AwsError(f"`{tool}` is not installed or not on PATH.", INSTALL_HINTS.get(tool))

    # -- running ------------------------------------------------------------

    def run(self, argv, *, mutating=False, capture=True, check=True):
        """Run any command. Returns a Result, or None when skipped by dry-run."""
        self._need(argv[0])
        if mutating and self.dry_run:
            self.show(argv, prefix="would run:")
            return None
        self.show(argv)
        res = _execute(argv, capture=capture)
        if check and res.returncode != 0:
            message, hint = explain(res.stderr) if capture else (f"`{argv[0]}` exited with {res.returncode}", None)
            raise AwsError(message, hint)
        return res

    def aws(self, *args, mutating=False, capture=True, check=True, profile=True):
        return self.run(self.aws_argv(args, profile=profile), mutating=mutating, capture=capture, check=check)

    def aws_json(self, *args, profile=True):
        res = self.aws(*args, "--output", "json", profile=profile)
        return json.loads(res.stdout or "null")

    def aws_text(self, *args, profile=True, check=True):
        res = self.aws(*args, "--output", "text", profile=profile, check=check)
        return res.stdout.strip() if res and res.returncode == 0 else ""

    def interactive(self, argv, *, mutating=False):
        """Hand the terminal to a command (exec sessions, log follow, login)."""
        return self.run(argv, mutating=mutating, capture=False)

    def pipe(self, first, second, *, mutating=True, display=None):
        """first | second, e.g. ecr get-login-password | docker login. Output of first is never shown."""
        self._need(first[0])
        self._need(second[0])
        text = display or f"{shlex.join(first)} | {shlex.join(second)}"
        if mutating and self.dry_run:
            if not self.quiet:
                console.print(f"  [dim]would run: {text}[/dim]", highlight=False, soft_wrap=True)
            return None
        if not self.quiet:
            console.print(f"  [dim]$ {text}[/dim]", highlight=False, soft_wrap=True)
        res = _execute(second, stdin_from=first)
        if res.returncode != 0:
            message, hint = explain(res.stderr)
            raise AwsError(message, hint)
        return res
