# eve route — design

Date: 2026-09-11
Status: approved design, not yet implemented

## 1. Purpose

`eve route` finds a way through to a host the network is keeping from you, and
keeps that way open.

Two blocks look identical from a browser and need opposite fixes:

- **DNS poisoning** — the resolver answers with a lie (`medium.com → 127.0.0.1`).
  The real servers are reachable; you were handed a dead address. Fixed by
  pinning the true address in the hosts file.
- **DPI / SNI filtering** — DNS is honest and the address is right, but a
  middlebox reads the hostname out of the plaintext TLS ClientHello and kills
  the connection. Every address for that name dies. No hosts or DNS edit can
  help; the block is keyed on the *name*. Fixed at the packet level.

Telling these two apart is the hard part, and it is what the diagnosis engine
does.

## 2. Origin and scope decisions

The diagnosis half is ported from `evo-cli`'s `evo_cli/commands/route/`
(~955 lines, 9 files). It is self-contained: its only in-repo dependency is
`evo_cli.console`, everything else is stdlib plus `rich` / `rich-click`.

Decisions taken during design, with reasons:

| Decision | Choice | Reason |
|---|---|---|
| eve's shape | Python CLI, flat layout | No nested `commands/` tree; eve is small |
| Linux DPI bypass | zapret `nfqws` | GoodbyeDPI is Windows-only; zapret is the Linux equivalent |
| Windows DPI bypass | zapret `winws.exe` | Same project, same flags — keeps one strategy model |
| Strategy selection | try a ladder, keep the winner | No strategy beats every DPI; verify empirically |
| Command surface | ledger + reconcile | Four backends cannot be poked imperatively without rot |
| Windows support | full parity, both backends | Diagnosis and pinning were already portable; zapret closes the last gap |

Deliberately **not** carried over from evo-cli:

- GoodbyeDPI (`_dpi.py`). Superseded by zapret's `winws.exe`, which shares the
  desync flag vocabulary with the Linux `nfqws` and so shares the ladder.
- `console.download_file` / `console.resolve_executable`. Both existed to serve
  the GoodbyeDPI installer; the zapret fetcher owns its own download path.

## 3. Command surface

```
eve route check <host>...     diagnose only, changes nothing, needs no privilege
    --addresses               per-IP ranking table (absorbs evo-cli's `probe`)
    --json / --no-vn / -t

eve route add <host>          diagnose → apply the fix that fits → verify → record
eve route rm <host> | --all   undo + forget
eve route ls                  ledger with live health per entry
eve route sync                re-verify every entry; re-pin stale addresses,
                              restart the DPI service if the hostlist drifted

--dry-run on add / rm / sync  print exactly what would change, change nothing
```

Changes from evo-cli's `check / probe / open / close / status`:

- `probe` folded into `check --addresses`; they were ~80% the same operation.
- `--method hosts|dpi` dropped. Which mechanism a block needs is a fact the
  diagnosis establishes, not a thing the user should have to know. A
  `--method` escape hatch may return if auto-detection proves wrong in practice.
- `open`/`close` → `add`/`rm`, because they now mutate a record rather than
  poke the system directly.
- `sync` is new. It is the answer to a flaw evo-cli's `status` could only
  describe: *"a pin that outlives its reason looks exactly like a broken site"*.

## 4. Repo layout

```
eve/
  pyproject.toml            [project.scripts] eve = "eve.__main__:main"
  eve/
    __init__.py  __main__.py  cli.py
    console.py              theme + info / success / warning / error / step
    route/
      __init__.py           the click group
      dns.py                ported ~unchanged
      probe.py              ported ~unchanged
      hosts.py              ported; markers renamed to `# >>> eve route >>>`
      paths.py              NEW  platform-aware state locations
      ledger.py             NEW  the record
      reconcile.py          NEW  ledger → system
      dpi.py                NEW  backend protocol + platform selector
      dpi_linux.py          NEW  nfqws + nft + systemd
      dpi_windows.py        NEW  winws + WinDivert + sc.exe
      strategy.py           NEW  the ladder
      commands.py           the five commands
      render.py             rich tables, kept out of commands.py
  tests/test_route.py
```

Modules lose evo-cli's `_` prefix; they are ordinary modules in a package.

## 5. State locations (`paths.py`)

Follows the shape `hosts.hosts_path()` already uses.

| | Linux | Windows |
|---|---|---|
| ledger | `/etc/eve/route.json` | `%ProgramData%\eve\route.json` |
| DPI hostlist | `/etc/eve/route-dpi.txt` | `%ProgramData%\eve\route-dpi.txt` |
| zapret binaries | `/opt/eve/zapret/` | `%ProgramFiles%\eve\zapret\` |
| hosts file | `/etc/hosts` | `%SystemRoot%\System32\drivers\etc\hosts` |

The ledger is root/Administrator-owned but world-readable, so `check` and `ls`
work without privilege.

## 6. The ledger

```json
{"version": 1,
 "entries": {
   "medium.com": {"method": "pin", "address": "162.159.152.4",
                  "verdict": "dns-poisoned", "verified": "2026-09-11T04:12:35Z"},
   "x.com":      {"method": "dpi",
                  "strategy": "fake,multisplit --dpi-desync-split-pos=1",
                  "verdict": "sni-blocked", "verified": "2026-09-11T04:13:02Z"}},
 "zapret": {"version": "v72.13", "qnum": 200}}
```

`version` is present so the schema can migrate. Unknown top-level keys are
preserved on write, so a newer eve's ledger is not silently truncated by an
older one.

## 7. Reconcile — the core model

`reconcile.py` derives desired system state from the ledger and makes reality
match. `add`, `rm` and `sync` mutate the ledger and then reconcile; **none of
them touch a backend directly.**

| Backend | Desired state |
|---|---|
| hosts block | every `pin` entry's address; untouched if already correct |
| DPI hostlist | every `dpi` entry's hostname, one per line |
| DPI service | exists and running **iff** ≥ 1 dpi entry |
| Linux nft table | exists **iff** ≥ 1 dpi entry (Linux only) |

Two properties this buys:

1. **Every backend is wholly owned by eve** and independently removable, under
   a distinct name — the hosts block markers, `/etc/eve/`, `eve-route-dpi`,
   the `inet eve_route` table. Nothing else on the system is touched. This
   generalises the marked-block discipline `hosts.py` already has.
2. **The derivation is a pure function** — ledger in, action list out — so the
   whole model is testable without root, without network, on either platform.

## 8. DPI backends

`dpi.py` defines the protocol; `dpi.backend()` selects by `os.name`. Reconcile
never names nft, systemd or `sc.exe`.

```python
class DpiBackend(Protocol):
    def available(self) -> tuple[bool, str | None]: ...
    def install(self) -> None: ...          # fetch + verify + place binaries
    def write_hostlist(self, hosts) -> None: ...
    def apply(self, strategy: str) -> None: ...   # (re)start with this strategy
    def state(self) -> dict: ...
    def teardown(self) -> None: ...
    def plan(self, strategy, hosts) -> list[str]:  # dry-run text, never executes
```

### Binary provenance

Both backends extract from the same release tarball and verify **each extracted
file** against the checksums published in the release's `sha256sum.txt`
(verified against the real artefacts on 2026-09-11). Verifying the member
rather than the tarball means the check is on the thing that actually executes.
Mismatch ⇒ delete and refuse, as evo-cli's `verify_download` did.

```
release  https://github.com/bol-van/zapret/releases/download/v72.13/zapret-v72.13.tar.gz
         7844856 bytes  sha256 25c74e6c5f48963fa244c2955e76694a07c39447245a0457e2efdc74b3317e68

linux-x86_64/nfqws          125760  f34615964d7321650197cd69d3f7cbfdaabe8118b5a0d57c6e41dccdff658999
windows-x86_64/winws.exe    223232  a14bff1df6234ea555d2e0c61b589f0707c0b12d6c9b7eeccda76012154996e8
windows-x86_64/cygwin1.dll 2954293  103104a52e5293ce418944725df19e2bf81ad9269b9a120d71d39028e821499b
windows-x86_64/WinDivert.dll 47616  c1e060ee19444a259b2162f8af0f3fe8c4428a1c6f694dce20de194ac8d7d9a2
windows-x86_64/WinDivert64.sys 94144 8da085332782708d8767bcace5327a6ec7283c17cfb85e40b03cd2323a90ddc2
```

`nfqws` is a fully static executable (`ldd`: *not a dynamic executable*), so
Linux needs that one 123 KB file and nothing else. `winws.exe` imports
`cygwin1.dll`, `WinDivert.dll` and `wlanapi/ole32/OLEAUT32/ADVAPI32/KERNEL32`
— the last five are system DLLs, so Windows needs all four shipped files,
~3.3 MB.

### `dpi_linux.py`

```
# nft — `bypass` is the safety valve: if nfqws dies, traffic flows normally
#        instead of the network hanging.
# `ct original packets` queues only the handshake, not every packet. QUIC gets
# 6 rather than 8, matching zapret's example, to cover Initial retransmissions.
table inet eve_route {
  chain postrouting {
    type filter hook postrouting priority mangle;
    tcp dport { 80, 443 } ct original packets 1-8 queue num 200 bypass
    udp dport 443         ct original packets 1-6 queue num 200 bypass
  }
}

# systemd unit eve-route-dpi.service, root, CAP_NET_ADMIN + CAP_NET_RAW
ExecStart=/opt/eve/zapret/nfqws --qnum=200 \
          --hostlist=/etc/eve/route-dpi.txt <strategy>
Restart=on-failure
```

### `dpi_windows.py`

Simpler: WinDivert filters traffic itself, so there are **no firewall rules**.
Two things to manage, not three.

```
sc.exe create eve-route-dpi binPath= "…\winws.exe --wf-tcp=80,443
       --hostlist=…\route-dpi.txt <strategy>" start= auto
```

`sc.exe` requires `binPath=` and its value as two separate argv entries — pass
an explicit list, never a shell string, or `sc` silently prints usage instead
of creating anything. (This bug was already learned once in evo-cli's `_dpi.py`;
the comment there records it.)

## 9. Strategy ladder (`strategy.py`)

No single desync strategy beats every DPI — zapret ships `blockcheck.sh`
precisely because the answer depends on the ISP. eve does the same thing in
Python, reusing the TLS probe it already has.

For each candidate in an ordered list: write the hostlist → `backend.apply()` →
brief settle → `probe.tls_probe(host)` → first strategy that completes a real
handshake wins. The winner is recorded in the ledger and tried first for later
hosts. If none work, tear down and say so plainly rather than leaving a
half-configured service behind.

Candidate list (starting point, **must be validated** — see §12):

```
--dpi-desync=fake --dpi-desync-ttl=4
--dpi-desync=fake --dpi-desync-fooling=badseq
--dpi-desync=multisplit --dpi-desync-split-pos=1
--dpi-desync=fake,multisplit --dpi-desync-split-pos=1 --dpi-desync-fooling=badseq
--dpi-desync=fakeddisorder --dpi-desync-split-pos=1 --dpi-desync-fooling=badseq
```

A note inherited from evo-cli worth preserving in spirit: GoodbyeDPI's bundled
`-5` preset was avoided there because its `--max-payload` 1200-byte ceiling
skips the very packet that matters — a modern Chrome ClientHello carries a
post-quantum key share and runs past 1200 bytes, so it sails through untouched
while curl's small ClientHello looks fine. **Verify each candidate against a
browser, not only curl.**

## 10. Platform matrix

| Capability | Linux | Windows |
|---|---|---|
| `check`, `ls` | yes, unprivileged | yes, unprivileged |
| DNS resolvers, TLS probing, verdicts | yes | yes (pure stdlib sockets) |
| hosts pinning (`add` on dns-poisoned) | yes, root | yes, Administrator |
| DPI bypass (`add` on sni-blocked) | nfqws + nft + systemd | winws + WinDivert + sc.exe |

## 11. Testing

TDD throughout. Port all 19 existing evo-cli route tests — the DNS wire-format
round-trip, the verdict matrix, and the hosts-block isolation tests are good and
carry over with only import changes.

New tests:

- ledger round-trip, schema migration, unknown-key preservation
- reconcile's action derivation from synthetic ledgers (pure, no root)
- ladder picks the first winner, with a mocked probe and mocked backend
- checksum rejection deletes the file and refuses
- **both** backends' rendered output — nft ruleset, systemd unit, `sc.exe` argv
  — asserted as text, never executed

Everything root-, network- or platform-touching is mocked. **The suite runs
unprivileged on Linux and covers the Windows backend by rendering.**

## 12. Risks and open questions

1. ~~**Strategy flags unvalidated.**~~ **Closed 2026-09-11.** `nfqws --help`
   for v72.13 confirms every mode in the §9 ladder (`fake`, `multisplit`,
   `fakeddisorder`), the fooling value `badseq`, and the flags
   `--dpi-desync-ttl` / `--dpi-desync-split-pos` / `--hostlist` / `--qnum`.
   `tests/test_route_dpi.py` now asserts the ladder against that mode list, so
   a future version bump that drops a mode fails the suite.

1b. ~~**The nft ruleset syntax is unvalidated.**~~ **Closed 2026-09-11.**
   `sudo nft -c -f` accepts the ruleset with no output, including the QUIC UDP
   clause added in §12a. The systemd unit likewise passes `systemd-analyze
   verify` (only the expected "binary not installed yet" note), and the nfqws
   argument syntax was confirmed against the binary itself. Every generated
   artefact is now validated by the tool that will consume it.
2. **The Windows backend cannot be executed from this machine.** It is verified
   by rendered-output tests and `--dry-run` only. Bugs will surface on first
   real use on Windows; this is accepted, not solved.
3. **`WinDivert64.sys` is a kernel driver.** It must load with a valid
   signature; Secure Boot or aggressive AV can block or flag it. Inherent to
   packet-level work on Windows — GoodbyeDPI had the identical property — not
   something eve introduces.
4. **Pinned addresses go stale** when a CDN moves. `sync` is the mitigation;
   it is not automatic, and nothing runs it on a timer.
5. **Pinned addresses defeat CDN geo-routing.** Pinning one address for a name
   means losing whatever locality the resolver was providing. Acceptable for a
   host that is otherwise unreachable, but it is a real cost.
6. **Version bumps are manual.** Moving off v72.13 means refreshing five
   checksums by hand.

## 12a. QUIC (added 2026-09-11)

A browser falling back to HTTP/3 on UDP 443 would bypass a TCP-only fix, so the
daemon runs two profiles, shared by both backends via `dpi.daemon_args`:

```
--filter-tcp=80,443 --hostlist=<list> <tcp strategy>
--new
--filter-udp=443 --filter-l7=quic --hostlist=<list> --dpi-desync=tamper
```

`--filter-tcp` on the first profile is load-bearing: nfqws denies UDP to a
profile that sets a TCP filter and no UDP filter, which keeps the two from
contending for the same packets. Verified against the real binary, which
reports *"we have 2 user defined desync profile(s)"* and fails only at the
privilege drop.

**Knock-back, not bypass.** Tampering the QUIC Initial makes its AEAD tag fail
to authenticate, so the handshake cannot complete and the browser falls back to
TCP. Chosen over a real QUIC bypass for two reasons: zapret puts a real bypass
at 50-75% success (readme.en.md, "IP fragmentation"), and eve's probe speaks
TCP only, so it could not verify one. Fallback is verified by the probe eve
already has, because the browser is on TCP by then.

Per-host, not global: zapret decrypts QUIC Initials to extract the SNI, so
`--hostlist` applies over QUIC (readme.en.md line 664). Only managed hosts lose
HTTP/3.

Unverified: that browsers actually fall back within a useful time. The AEAD
reasoning is sound, but the behaviour was not exercised against a real block.

## 13. Out of scope

- Tunnels and VPNs. When the verdict is `ip-blocked` or `unreachable`, eve says
  so and stops; it will not set up a tunnel.
- IPv6. `dns.py` queries A records only, as it does in evo-cli today.
- Automatic `sync` on a schedule.
