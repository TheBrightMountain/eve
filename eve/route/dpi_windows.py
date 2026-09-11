"""Windows DPI bypass: zapret's winws.exe over WinDivert, run as a service.

Simpler than Linux in one respect: WinDivert filters traffic in-process via
`--wf-tcp`, so there are no firewall rules to add or clean up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from eve.route import dpi, paths

SERVICE = "eve-route-dpi"
WF_FILTER = "--wf-tcp=80,443"


def binary():
    return paths.zapret_dir() / "winws.exe"


def command_line(strategy, hostlist=None, exe=None):
    hostlist = hostlist or paths.dpi_hostlist_path()
    exe = exe or binary()
    return f'"{exe}" {WF_FILTER} --hostlist={hostlist} {strategy}'


def create_argv(strategy, hostlist=None, exe=None):
    # sc.exe requires `binPath=` and its value as two separate argv entries.
    # Glue them into one string and sc silently prints its usage instead of
    # creating anything - a bug already learned once in evo-cli.
    return [
        "sc.exe",
        "create",
        SERVICE,
        "binPath=",
        command_line(strategy, hostlist, exe),
        "start=",
        "auto",
    ]


def _sc(*args):
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, errors="replace", check=False)


class WindowsBackend:
    name = "windows"

    def present(self):
        """Cheap, no-subprocess check for anything eve installed."""
        return binary().exists()

    def available(self):
        return True, None

    def install(self):
        if binary().exists():
            return binary()
        archive = dpi.download_tarball()
        dpi.extract_verified(archive, dpi.ARTIFACTS["windows"], paths.zapret_dir())
        Path(archive).unlink(missing_ok=True)
        return binary()

    def write_hostlist(self, hosts):
        path = paths.dpi_hostlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(hosts) + "\n" if hosts else "", encoding="utf-8")
        return path

    def apply(self, strategy):
        if self.state()["installed"]:
            _sc("stop", SERVICE)
            _sc("delete", SERVICE)
        created = subprocess.run(create_argv(strategy), capture_output=True, text=True, errors="replace", check=False)
        if created.returncode != 0:
            raise RuntimeError((created.stdout or created.stderr or "sc create failed").strip())
        _sc("description", SERVICE, "eve route DPI bypass (zapret winws)")
        _sc("start", SERVICE)
        return self.state()

    def state(self):
        described = _sc("qc", SERVICE)
        if described.returncode != 0:
            return {"backend": "windows", "installed": False, "running": False, "args": None}
        args = None
        for line in (described.stdout or "").splitlines():
            if "BINARY_PATH_NAME" in line:
                args = line.split(":", 1)[1].strip()
                break
        running = "RUNNING" in (_sc("query", SERVICE).stdout or "")
        return {"backend": "windows", "installed": True, "running": running, "args": args}

    def teardown(self):
        _sc("stop", SERVICE)
        _sc("delete", SERVICE)
        paths.dpi_hostlist_path().unlink(missing_ok=True)

    def plan(self, strategy, hosts):
        return [
            f"install {binary()} + WinDivert (zapret {dpi.ZAPRET_VERSION}, sha256-verified)",
            "create service:",
            f"    {' '.join(create_argv(strategy))}",
            "no firewall rules needed - WinDivert filters in-process",
        ]
