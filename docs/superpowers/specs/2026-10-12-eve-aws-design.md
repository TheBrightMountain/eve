# eve aws — design

Date: 2026-10-12
Status: implemented (branch eve-aws)

## 1. Purpose

`eve aws` wraps the AWS CLI commands that kept coming back while running small
ECS Fargate projects (first user: the Moodle lab in `~/moodle-aws`): switching
between AWS accounts, seeing whether a project's stack is up, parking it to save
money, tailing logs, opening a shell in a container, pushing images.

It is a **thin, honest wrapper**: every step prints the real `aws` / `docker`
command before running it, so using eve keeps teaching the underlying CLI rather
than hiding it.

## 2. Scope decisions

| Decision | Choice | Reason |
|---|---|---|
| Shape | Two managed nouns (`account`, `project`), one *current* selection of each, then actions on the current project | Mirrors how the user thinks: pick account + project once, then act |
| Accounts | Stored as standard AWS CLI profiles in `~/.aws/config`; signed in with `aws login --remote --profile <name>` | No credentials ever stored by eve; plain `aws --profile x` keeps working |
| Projects | Registered by name with a path, a **type** and an account; details read from the project's `aws/ids.txt` | The repo stays the source of truth; works for non-Moodle projects |
| Project types | `ecs` (generic) and `moodle` (ecs + built-in recipes/defaults) | Moodle knowledge lives in a type, not in the core commands |
| Selecting a project | `eve aws project enter <name>` / `leave` | User's choice of noun |
| How AWS is called | Shell out to `aws` and `docker` | Reuses the login session and credential precedence exactly; no `boto3` |
| Show commands | On by default, `-q` hides them | Learning tool first |
| Ambiguity (service/task/container) | flag → configured default → the only one → numbered picker in a TTY (error listing choices otherwise) | Rarely asks, never guesses silently |
| Safety | Mutating actions verify the signed-in account ID matches the project's | Can't park the wrong account's services |
| Shell credential helper | **Not in v1** | Keep using `aws configure export-credentials ... \| source` by hand |
| park/unpark scripts | Ported into eve; the `.fish` scripts stay until eve's version is proven | No flag day |

Out of scope for v1: deploying new task definition revisions (stage 7: Terraform +
CI), creating/deleting infrastructure, Windows PowerShell specifics.

## 3. Command surface

```
eve aws [-q] [-n] <group|action> ...

-q/--quiet     hide the underlying commands
-n/--dry-run   print the commands that would change something; run only read-only ones

── accounts ────────────────────────────────────────────────────────────────
eve aws account ls                         name, account ID, region, signed in?, * = current
eve aws account add <name> --region R      create the profile and sign in
eve aws account rm <name>                  remove the profile (refuses if a project uses it, unless --force)
eve aws account login <name>               make it current; signs in only if the session is missing/expired
eve aws account edit <name> [--name NEW] [--region R]   rename (updates source_profile refs + projects) / change region
eve aws account logout [<name>]            sign out (default: current)

── projects ────────────────────────────────────────────────────────────────
eve aws project ls                       name, type, account, path, * = current
eve aws project add <name> --path P --type ecs|moodle --account A
eve aws project rm <name>                forget it (touches nothing in AWS or on disk)
eve aws project enter <name>             make it current (and switch to its account)
eve aws project leave                    clear the current project
eve aws project types                    list types and what each adds

── actions (on the current project, in its account) ───────────────────────
eve aws status                           services (rev, desired/running, rollout), RDS, target health, site HTTP code
eve aws park                             services → 0, wait stable, stop RDS
eve aws unpark                           wait while RDS stopping → start → wait available → services → 1 → site check
eve aws logs <svc> [--since 10m] [-f] [-g PATTERN]
eve aws exec <svc> [-t TASK] [-c CONTAINER] [-u USER] [-- CMD]     default CMD: bash
eve aws run [<recipe>] [-s SVC] [-t TASK]                            no name = list recipes
eve aws push [--tag TAG] [--allow-dirty]
eve aws files [<prefix>]
```

Every action also accepts `-P/--project NAME` to target a project without
entering it. `EVE_AWS_PROJECT` in the environment overrides the current project
for one shell (the current selection is otherwise shared by all terminals).

## 4. State and config

### eve's own state: `~/.config/eve/aws.json` (no secrets)

```json
{
  "current_account": "lab",
  "current_project": "moodle",
  "projects": {
    "moodle": { "path": "/home/nms/moodle-aws", "type": "moodle", "account": "lab" }
  }
}
```

Accounts are not duplicated here: `account ls` reads profiles from `~/.aws/config`
and checks each with `aws sts get-caller-identity --profile <name>`.

### Account selection order

1. `-P` project's account (or the current project's account)
2. `current_account`
3. AWS CLI default

eve passes `--profile <name>` to every `aws` call it makes.

### Project details: `<path>/aws/ids.txt`

One `KEY=VALUE` per line, `#` comments and blank lines ignored, value = everything
after the first `=`. Unknown keys ignored (the file doubles as the human record).

| Key | Example | Used by |
|---|---|---|
| `ACCOUNT_ID` ★ | `261732724428` | safety check |
| `CLUSTER` | `moodle` | status, park, unpark, exec, run |
| `SERVICES` ★ | `moodle-web moodle-cron` | status, park, unpark, logs, exec |
| `DEFAULT_SERVICE` ★ | `web` | exec, run, logs when no `<svc>` given |
| `CONTAINER_<svc>` ★ | `CONTAINER_web=php` | exec, run (type default for moodle: `php`) |
| `DB_ID` ★ | `moodle-db` | status, park, unpark (optional - projects without RDS skip it) |
| `TG_ARN` | `arn:aws:elasticloadbalancing:...` | status |
| `SITE_URL` ★ | `https://mdl.minhson.dev` | status, unpark |
| `LOG_GROUP` | `/ecs/moodle` | logs |
| `REGISTRY` ★ | `261732724428.dkr.ecr.ap-southeast-2.amazonaws.com` | push |
| `REPOS` ★ | `moodle-php moodle-nginx` | push |
| `LOCAL_IMAGE_TAG` ★ | `local` (default) | push |
| `FILES_BUCKET` | `moodle-files-261732724428` | files |
| `RECIPE_<name>` ★ | shell command | run (adds to / overrides type recipes) |
| `RECIPE_<name>_SERVICE` ★ | `web` | run (where the recipe runs) |

★ = new keys to add to the Moodle lab's file. Service short names (`web`, `cron`)
are the part of each `SERVICES` entry after the last `-`; either form is accepted,
and the short name is the CloudWatch stream prefix.

### Project types

| Type | Adds |
|---|---|
| `ecs` | nothing beyond the core actions |
| `moodle` | default container `php`; recipes run as `www-data`; built-in recipes `purge` (purge caches with `OBJECTFS_BUCKET=` cleared), `upgrade` (`admin/cli/upgrade.php --non-interactive`), `cron` (one `admin/cli/cron.php` run), `reset-password` (prompts for user + password, never echoed) |

## 5. Behaviour notes

- **status / every action header** shows `project · account · account ID` so it's
  always clear what is being touched.
- **park** skips `stop-db-instance` when RDS is `stopped`/`stopping`; prints the
  7-day auto-start reminder.
- **unpark** polls every 30 s while RDS is `stopping`, starts only when `stopped`,
  refuses to scale services if the DB never reaches `available`. Exit 1 on failure.
- **push** refuses a dirty git tree unless `--allow-dirty`; tag defaults to the
  project repo's short SHA. Login shown as
  `aws ecr get-login-password | docker login --username AWS --password-stdin <registry>`;
  the password is piped, never printed.
- **exec -u USER** wraps as `su -s /bin/sh USER -c '<cmd>'`.
- **Ambiguity picker** lists tasks as `id · revision · AZ · started`.
- **Safety check** before park, unpark, push, exec, run: `sts get-caller-identity`
  account must equal `ACCOUNT_ID`.

### Friendly errors

| Seen | eve says |
|---|---|
| `Your session has expired` / `ExpiredToken` | `eve aws account login <name>` |
| `AccessDenied` mentioning `assumed-role/` while `AWS_ACCESS_KEY_ID` is set | env-var credentials are overriding your profile |
| account ID mismatch | which account you're on vs the project's, and refuses |
| no current project | `eve aws project enter <name>` or `-P` |
| `aws` / `docker` / `session-manager-plugin` missing | which tool and how to install it |

## 6. Code layout

```
eve/aws/
  __init__.py      aws_group + help text; registers groups and actions
  state.py         ~/.config/eve/aws.json load/save
  config.py        ids.txt parsing, typed accessors, short-name resolution
  types.py         project types: defaults + built-in recipes
  runner.py        run()/run_json(): print "$ cmd", quiet/dry-run, --profile injection, error mapping
  pick.py          flag → default → single → picker resolution
  account.py       account group (alias `acc`)
  project.py       project group
  actions.py       status, park, unpark, logs, exec, run, push, files
tests/
  test_aws_state.py  test_aws_config.py  test_aws_account.py  test_aws_project.py  test_aws_actions.py
```

`runner` is the only module touching `subprocess`; tests swap in a fake that
records argv and returns canned JSON. No test calls real AWS.

## 7. Testing

- state: create/load/save, current selections, project add/rm/enter/leave, rm refusing a referenced account.
- config: parsing, `=` in values, missing-key errors, short names, type defaults merged with ids.txt overrides.
- account: profile creation via `aws configure set`, login only when expired, ls rendering.
- actions: exact argv per command, `--profile` always injected, dry-run skips mutating calls,
  unpark stopping→stopped→available and its refusal path, push dirty-tree refusal,
  safety-check refusal, picker resolution order, error mapping.
