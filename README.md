# eve

A personal toolbox of commands worth keeping.

```bash
uv venv && uv pip install -e ".[test]"
eve -h
```

## `eve route`

Find a way through to somewhere the network is keeping from you.

Two blocks look identical from a browser and need opposite fixes:

- **DNS poisoning** — the resolver answers with a lie (`medium.com → 127.0.0.1`).
  The real servers are reachable; you were handed a dead address.
- **DPI / SNI filtering** — DNS is honest and the address is right, but a
  middlebox reads the hostname out of the plaintext TLS ClientHello and kills
  the connection. Every address for that name dies, so no hosts or DNS edit can
  help.

Telling them apart is the hard part. `check` opens a real TLS connection to each
address, and when nothing completes it runs one more handshake to the *same
address* announcing a harmless server name. If that one succeeds, the route is
open and the block is keyed on the name — which is what separates
`sni-blocked` from `ip-blocked`.

```
eve route check <host>...   diagnose only, changes nothing, needs no privilege
    -a / --addresses        show every address and how each one answered
eve route add <host>        diagnose → apply the fix that fits → verify → record
eve route rm <host> | --all undo and forget
eve route ls                every route eve is holding open
eve route sync              re-check them all; re-pin whatever went stale

-n / --dry-run              on add, rm and sync: say what would change
```

### How the fixes work

A **ledger** at `/etc/eve/route.json` (`%ProgramData%\eve\route.json` on
Windows) is the single source of truth. Commands mutate it; eve then reconciles
the machine to match. Everything eve touches is wholly its own and removable:

| | |
|---|---|
| hosts block | between `# >>> eve route >>>` markers, backed up before each write |
| DPI hostlist | `/etc/eve/route-dpi.txt` |
| service | `eve-route-dpi` (systemd, or a Windows service) |
| firewall | nft table `inet eve_route` — Linux only |

`sync` exists because a pinned address rots when a CDN moves, and a pin that
outlives its reason looks exactly like a broken site.

### The DPI bypass

Uses [zapret](https://github.com/bol-van/zapret) v72.13 — `nfqws` on Linux,
`winws.exe` over WinDivert on Windows. eve downloads the release, extracts only
the binaries it needs, and verifies each one against the sha256 published with
the release before it ever runs.

No single desync strategy beats every DPI, so eve tries a ladder of them and
keeps the first that gets a real TLS handshake through — then reuses it.

Linux queues packets to nfqws with `queue num 200 bypass`; the `bypass` matters,
because without it a dead daemon would hang the network instead of letting
traffic flow. Windows needs no firewall rule at all — WinDivert filters
in-process via `--wf-tcp` / `--wf-udp`.

Both need administrator rights. `check` and `ls` never do.

### QUIC

A browser that falls back to HTTP/3 on UDP 443 would route straight around a
TCP-only bypass, so the daemon runs two profiles:

```
--filter-tcp=80,443 --hostlist=<list> <strategy>     # bypass over TCP
--new
--filter-udp=443 --filter-l7=quic --hostlist=<list> --dpi-desync=tamper
```

The second corrupts the QUIC Initial for listed hosts. Its AEAD tag then fails
to authenticate, the server cannot decrypt it, and the browser drops back to
TCP — where the bypass already works and can actually be verified.

zapret decrypts QUIC Initials to read the SNI, so `--hostlist` applies over
QUIC too: **only the hosts eve manages lose HTTP/3.** Everything else is
untouched.

Why knock QUIC back rather than bypass it: zapret puts the success rate of a
real QUIC bypass at [50–75%](https://github.com/bol-van/zapret), and eve has no
way to verify one — its probe speaks TCP. A fix eve cannot verify is a fix it
should not claim.
