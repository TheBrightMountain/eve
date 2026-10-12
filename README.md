# eve

A personal toolbox of commands worth keeping.

## Install

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/). Nothing else —
not even `make`.

**On a fresh machine:**

```bash
uv tool install git+https://github.com/TheBrightMountain/eve
```

**From a clone**, which also wires up `sudo` (see below):

```bash
git clone git@github.com:TheBrightMountain/eve.git && cd eve
./install.sh
```

Then `eve -h`.

### Why the extra step for sudo

uv installs tools into `~/.local/bin`, which is **not** on sudo's `secure_path`.
Since `route add`, `rm` and `sync` all need root, a plain `uv tool install`
leaves you with:

```
$ sudo eve route add medium.com
sudo: eve: command not found
```

`./install.sh` fixes that by linking the binary into `/usr/local/bin`, which is
on `secure_path`. It points at `~/.local/bin/eve` rather than the venv, so the
link survives upgrades. If you'd rather not have a system-wide link, skip the
script and use `sudo $(which eve)` instead.

On Windows there is no such split — `uv tool install` is enough; run the
privileged commands from an elevated PowerShell.

### Updating

```bash
uv tool upgrade eve
```

Re-fetches the latest commit on `main`. If you installed from a clone instead,
that would rebuild from your local directory — pull first:

```bash
cd /path/to/eve && git pull && ./install.sh
```

The `/usr/local/bin` link survives either, because it points at
`~/.local/bin/eve`, which uv recreates on every install.

`eve --version` tells you exactly what you're running: the version comes from
git tags via setuptools-scm, so it moves with the code.

```
0.1.0                     exactly the v0.1.0 tag
0.1.1.dev4+g18e2da7       4 commits past it, at 18e2da7
0.1.1.dev4+g18e2da7.d20260911   ...and the tree was dirty
0.0.0+unknown             built with no git history to read
```

Cutting a release is `git tag -a v0.2.0 -m ... && git push --follow-tags` —
nothing to bump by hand.

### Removing it

```bash
sudo eve route rm --all    # undo what eve is holding open, first
./install.sh uninstall
```

The order matters, so the uninstaller enforces it: if the ledger still lists
open routes it names them, refuses, and stops. Uninstalling first would strand
the hosts block, `/etc/eve` and the `eve-route-dpi` service with nothing left
that knows how to clean them up.

`./install.sh uninstall --force` overrides it, if you mean to deal with them by
hand.

### Working on it

```bash
./install.sh dev      # editable .venv
./install.sh test     # pytest
./install.sh lint     # ruff check + format --check
```

`make install` / `make test` also work if you have make — the Makefile just
delegates to `install.sh`.

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
    -n / --samples N        probe N times; exposes an intermittent block
eve route add <host>        diagnose → apply the fix that fits → verify → record
    --method pin|dpi        force a fix instead of following the diagnosis
    --probe <name>          cover a whole domain (dpi), testing this real subdomain
eve route import <preset|file>
                            add a whole list - one `HOST [PROBE]` per line
    -l / --list             the presets that ship with eve (e.g. `steam`)
eve route rm <host> | --all undo and forget
eve route ls                every route eve is holding open
eve route sync              re-check them all; re-pin whatever went stale

-n / --dry-run              on add, import, rm and sync: say what would change
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

`check` reads the ledger too, so a reachable verdict never gets mistaken for
"nothing was ever wrong here":

```
$ eve route check medium.com
medium.com  open - reachable - nothing in the way
  held open by eve: pinned 162.159.153.4
✓ medium.com is reachable because eve is holding it open (pinned 162.159.153.4).
  Undo with `eve route rm medium.com`.
```

A DPI entry covers subdomains, because zapret applies a hostlist to them
automatically — so `store.steampowered.com` reports as held by an entry for
`steampowered.com`. A pin does not: the hosts file has no notion of subdomains.

### When the diagnosis is wrong

The checker is not infallible. An intermittent block, one lucky handshake, or a
resolver that answers differently for a single query can all make a blocked host
look fine. On a hijacked network the candidate list can collapse to a *single*
address — every plain-UDP resolver poisoned, only DoH answering — and then one
lucky or unlucky IP flips the verdict outright.

`-n` measures that instead of hiding it:

```
$ eve route check store.steampowered.com -n 3
store.steampowered.com  sni-blocked - DPI blocks the TLS handshake by server name
  sampled 3x: open 2/3, sni-blocked 1/3
! Intermittent - the samples disagreed. The worst one is reported, because a
  host blocked some of the time is still blocked.
```

And `add` never refuses on a clean verdict — it warns and pins anyway:

```
$ eve route add a.com
Diagnosis: open - reachable - nothing in the way
! a.com looks reachable, but the check is not infallible - continuing anyway.
✓ Pinned 1.2.3.4 for a.com
```

If you don't trust the diagnosis at all, `--method` overrides it outright —
including for `ip-blocked` and `unreachable`, which `auto` refuses because they
genuinely call for a tunnel:

```
$ eve route add a.com --method dpi
! Forcing dpi - ignoring the diagnosis (open).
```

The one thing it cannot do is invent an address: `--method pin` still fails when
nothing resolved.

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

## `eve dns`

Choose which DNS servers the machine asks.

```
eve dns show                  each connected interface's servers, and who set them
eve dns set <server>...       switch to these, in order of preference
    -i / --interface NAME     only this interface (repeatable)
    -f / --force              set them even if none answers a test lookup
eve dns reset                 put back exactly what was there before eve changed it

-n / --dry-run                on set and reset: say what would change
```

`set` asks each server to resolve a test name first. A dead one is warned
about; if none answers, nothing changes — a typo here takes every lookup on the
machine down with it.

The first `set` on an interface records what it had (`%ProgramData%\eve\dns.json`,
`/etc/eve/dns.json`), and later ones never overwrite that, so `reset` always
returns to the real original: servers typed in by hand come back exactly, and
automatic ones are handed back to whatever set them.

| | |
|---|---|
| Windows | DnsClient PowerShell module; persistent |
| Linux | `resolvectl` (systemd-resolved); lasts until the link or NetworkManager restarts |

New DNS fixes a poisoned answer, not a DPI block — that needs `eve route`.

## `eve aws`

Run the AWS chores of an ECS project without retyping the commands. Pick an
account and a project once, then act on it. Every step prints the real `aws` /
`docker` command it runs (`-q` hides them, `-n` shows what would change without
changing anything).

```
eve aws account ls                         accounts (AWS CLI profiles), which is current, signed in or not
eve aws account add <name> --region R      save a new account and sign in (aws login)
eve aws account login <name>               make it current; signs in only if the session expired
eve aws account logout [<name>]  |  rm <name>

eve aws project ls
eve aws project add <name> --path P --type ecs|moodle --account A
eve aws project enter <name>             make it current, switching to its account
eve aws project leave  |  rm <name>  |  types

eve aws status                           services, database, ALB targets, site HTTP code
eve aws park | unpark                    services → 0 + stop the DB  /  start the DB → services back
eve aws logs <svc> [--since 10m] [-f] [-g PATTERN]
eve aws exec <svc> [-t TASK] [-c CONTAINER] [-u USER] [-- CMD]
eve aws run [<recipe>] [-s SVC]          named commands from the project type and ids.txt
eve aws push [--tag T] [--allow-dirty]   tag local images with the git commit, push to ECR
eve aws files [<prefix>]                 list the project's files bucket
```

Any action takes `-P <project>` to target a project without entering it, and
`EVE_AWS_PROJECT` overrides the current project for one shell.

**Accounts** are ordinary AWS CLI profiles in `~/.aws/config`, signed in with
`aws login`; eve stores no credentials. **Projects** live in
`~/.config/eve/aws.json` (name, path, type, account) and read their details from
`<path>/aws/ids.txt`:

```
ACCOUNT_ID=261732724428                 # actions that change things refuse on another account
CLUSTER=moodle
SERVICES=moodle-web moodle-cron         # short names: web, cron
DEFAULT_SERVICE=web
DB_ID=moodle-db                         # optional
TG_ARN=arn:aws:elasticloadbalancing:... # optional, for status
SITE_URL=https://mdl.example.com        # optional, for status/unpark
LOG_GROUP=/ecs/moodle
REGISTRY=261732724428.dkr.ecr.ap-southeast-2.amazonaws.com
REPOS=moodle-php moodle-nginx           # pushed from <repo>:local
FILES_BUCKET=moodle-files-261732724428
CONTAINER_web=php                       # optional; the moodle type defaults to php
RECIPE_hello=echo hi                    # optional custom recipes
RECIPE_hello_SERVICE=cron
```

The `moodle` type adds recipes `purge`, `upgrade`, `cron` and `reset-password`
(run as `www-data`). When a service runs several tasks or a task has several
containers, eve uses the flag, then the configured default, then the only one -
and otherwise asks.
