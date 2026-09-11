"""Linux DPI bypass: zapret's nfqws, fed by an nftables queue, run by systemd."""

from __future__ import annotations

import subprocess
from pathlib import Path

from eve.route import dpi, paths

SERVICE = "eve-route-dpi"
UNIT_PATH = Path("/etc/systemd/system") / f"{SERVICE}.service"
TABLE = "eve_route"
DEFAULT_QNUM = 200


def binary():
    return paths.zapret_dir() / "nfqws"


def nft_ruleset(qnum=DEFAULT_QNUM):
    # `bypass` is the safety valve: if nfqws dies, packets flow normally
    # instead of the network hanging on an empty queue.
    # `ct original packets 1-8` queues only the start of the connection - the
    # ClientHello - rather than every packet of every download. QUIC gets 6,
    # which is what zapret's own example uses to cover Initial retransmissions.
    return f"""table inet {TABLE} {{
  chain postrouting {{
    type filter hook postrouting priority mangle; policy accept;
    tcp dport {{ 80, 443 }} ct original packets 1-8 queue num {qnum} bypass
    udp dport 443 ct original packets 1-6 queue num {qnum} bypass
  }}
}}
"""


def unit_text(strategy, qnum=DEFAULT_QNUM, hostlist=None, exe=None):
    hostlist = hostlist or paths.dpi_hostlist_path()
    exe = exe or binary()
    args = " ".join(dpi.daemon_args(strategy, hostlist))
    return f"""[Unit]
Description=eve route DPI bypass (zapret nfqws)
After=network.target

[Service]
Type=simple
ExecStart={exe} --qnum={qnum} {args}
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def _run(cmd, check=False, input_text=None):
    return subprocess.run(cmd, capture_output=True, text=True, input=input_text, check=check)


class LinuxBackend:
    name = "linux"

    def present(self):
        """Cheap, no-subprocess check for anything eve installed."""
        return UNIT_PATH.exists() or binary().exists()

    def available(self):
        if not Path("/usr/sbin/nft").exists() and not Path("/sbin/nft").exists():
            return False, "nft is not installed"
        return True, None

    def install(self, qnum=DEFAULT_QNUM):
        if binary().exists():
            return binary()
        archive = dpi.download_tarball()
        dpi.extract_verified(archive, dpi.ARTIFACTS["linux"], paths.zapret_dir())
        Path(archive).unlink(missing_ok=True)
        return binary()

    def write_hostlist(self, hosts):
        path = paths.dpi_hostlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(hosts) + "\n" if hosts else "", encoding="utf-8")
        return path

    def apply(self, strategy, qnum=DEFAULT_QNUM):
        """Bring the daemon and ruleset in line, touching only what has drifted.

        Reconcile runs after the strategy ladder has already applied the
        winning strategy, so this is routinely called with everything already
        in place. Restarting nfqws anyway would drop live connections for no
        reason - and on Windows the equivalent churn is what trips 1072.
        """
        wanted = unit_text(strategy, qnum=qnum)
        current = UNIT_PATH.read_text(encoding="utf-8") if UNIT_PATH.exists() else None
        state = self.state()

        if current != wanted:
            UNIT_PATH.write_text(wanted, encoding="utf-8")
            _run(["systemctl", "daemon-reload"])
            _run(["systemctl", "enable", "--now", SERVICE])
            _run(["systemctl", "restart", SERVICE])
        elif not state["running"]:
            _run(["systemctl", "enable", "--now", SERVICE])

        # The unit can be perfectly fine while a firewall flush has taken the
        # table with it, so this is checked independently of the daemon.
        if not state["rules"]:
            _run(["nft", "-f", "-"], input_text=nft_ruleset(qnum))
        return self.state()

    def state(self):
        active = _run(["systemctl", "is-active", SERVICE]).stdout.strip()
        listed = _run(["nft", "list", "table", "inet", TABLE])
        return {
            "backend": "linux",
            "installed": binary().exists(),
            "running": active == "active",
            "rules": listed.returncode == 0,
            "unit": str(UNIT_PATH),
        }

    def teardown(self):
        _run(["systemctl", "disable", "--now", SERVICE])
        UNIT_PATH.unlink(missing_ok=True)
        _run(["systemctl", "daemon-reload"])
        _run(["nft", "delete", "table", "inet", TABLE])
        paths.dpi_hostlist_path().unlink(missing_ok=True)

    def plan(self, strategy, hosts, qnum=DEFAULT_QNUM):
        return [
            f"install {binary()} (zapret {dpi.ZAPRET_VERSION}, sha256-verified)",
            f"write {UNIT_PATH}:",
            *[f"    {line}" for line in unit_text(strategy, qnum=qnum).strip().splitlines()],
            "load nft ruleset:",
            *[f"    {line}" for line in nft_ruleset(qnum).strip().splitlines()],
        ]
