#!/usr/bin/env bash
#
# eve - install, remove, and development tasks.
#
# Needs nothing but bash and uv. `make` is deliberately not required: a tool
# whose installer needs another tool installed first is not much of an
# installer.
#
#   ./install.sh              install eve on PATH, sudo included
#   ./install.sh uninstall    remove it again
#   ./install.sh dev          editable .venv for working on eve
#   ./install.sh test|lint    run the suite / the linters

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

UV="${UV:-uv}"
SYSTEM_BIN="${SYSTEM_BIN:-/usr/local/bin}"
LEDGER="${LEDGER:-/etc/eve/route.json}"
VENV=".venv"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need_uv() {
    command -v "$UV" >/dev/null \
        || die "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"
}

cmd_install() {
    need_uv
    bold "Installing eve"
    "$UV" tool install --force .

    local user_bin
    user_bin="$("$UV" tool dir --bin)"
    [ -n "$user_bin" ] || die "could not work out where uv puts executables"
    ok "installed $user_bin/eve"

    # uv installs under ~/.local/bin, which is not on sudo's secure_path. Since
    # `route add`, `rm` and `sync` all need root, a user-only install would
    # leave `sudo eve` failing with "command not found". One link fixes it.
    if [ "$(uname -s)" = "Linux" ] || [ "$(uname -s)" = "Darwin" ]; then
        bold "Linking into $SYSTEM_BIN so sudo can find it"
        if sudo ln -sfn "$user_bin/eve" "$SYSTEM_BIN/eve"; then
            ok "linked $SYSTEM_BIN/eve -> $user_bin/eve"
        else
            warn "could not link into $SYSTEM_BIN - use 'sudo $user_bin/eve' instead"
        fi
    fi

    echo
    bold "Check"
    "$user_bin/eve" --version
    if sudo -n eve --version >/dev/null 2>&1; then
        ok "sudo eve works"
    else
        warn "verify with: sudo eve --version"
    fi
    echo
    echo "Try:  eve route check example.com"
}

# Hosts eve is currently holding open, one per line.
held_routes() {
    [ -f "$LEDGER" ] || return 0
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json,sys
try:
    print("\n".join(json.load(open(sys.argv[1])).get("entries", {})))
except Exception:
    pass' "$LEDGER" 2>/dev/null
    else
        # No python3: fall back to scraping the keys next to a "method" field.
        grep -oE '"[^"]+": \{ *"method"' "$LEDGER" 2>/dev/null | cut -d'"' -f2
    fi
}

cmd_uninstall() {
    need_uv

    # Uninstalling while routes are open strands the hosts block, /etc/eve and
    # the eve-route-dpi service with nothing left that knows how to undo them.
    local held
    held="$(held_routes)"
    if [ -n "$held" ] && [ "${FORCE:-0}" != 1 ]; then
        printf '\033[31m✗\033[0m eve is still holding %s route(s) open:\n' "$(printf '%s\n' "$held" | wc -l | tr -d ' ')" >&2
        printf '%s\n' "$held" | sed 's/^/    /' >&2
        cat >&2 <<'HINT'

Undo them first, while eve is still installed to do it:

    sudo eve route rm --all
    ./install.sh uninstall

Uninstalling now would strand the hosts block, /etc/eve and the eve-route-dpi
service with nothing left that knows how to clean them up.

Pass --force if you intend to deal with them by hand.
HINT
        exit 1
    fi

    bold "Removing eve"
    if [ -L "$SYSTEM_BIN/eve" ] || [ -e "$SYSTEM_BIN/eve" ]; then
        sudo rm -f "$SYSTEM_BIN/eve" && ok "removed $SYSTEM_BIN/eve"
    fi
    "$UV" tool uninstall eve 2>/dev/null && ok "uninstalled the tool" || warn "eve was not installed as a tool"
    echo
    echo "eve leaves state behind on purpose - remove it with 'sudo eve route rm --all' BEFORE uninstalling,"
    echo "or by hand: /etc/eve, the '# >>> eve route >>>' block in /etc/hosts, and the eve-route-dpi service."
}

cmd_dev() {
    need_uv
    "$UV" venv "$VENV"
    "$UV" pip install -e ".[test]"
    ok "editable install ready - run $VENV/bin/eve"
}

cmd_test() { "$VENV/bin/python" -m pytest tests/ -q; }
cmd_lint() { "$VENV/bin/python" -m ruff check . && "$VENV/bin/python" -m ruff format --check .; }
cmd_fmt()  { "$VENV/bin/python" -m ruff format .; }

cmd_help() {
    # The header comment is the help text, up to the first non-comment line.
    awk 'NR>2 && /^#/ { sub(/^# ?/, ""); print; next } NR>2 { exit }' "${BASH_SOURCE[0]}"
}

FORCE=0
args=()
for arg in "$@"; do
    case "$arg" in
        -f|--force) FORCE=1 ;;
        *) args+=("$arg") ;;
    esac
done
set -- "${args[@]+"${args[@]}"}"

case "${1:-install}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    dev)       cmd_dev ;;
    test)      cmd_test ;;
    lint)      cmd_lint ;;
    fmt)       cmd_fmt ;;
    -h|--help|help) cmd_help ;;
    *) die "unknown command: $1  (try: install, uninstall, dev, test, lint, fmt)" ;;
esac
