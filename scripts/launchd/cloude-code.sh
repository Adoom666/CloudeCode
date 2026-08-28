#!/bin/bash
# cloude-code.sh - launchd wrapper for the CloudeCode server (mac-mini-m4)
#
# THIS FILE IS A REPO MIRROR, NOT THE LIVE WRAPPER. The launchd job
# com.imc.cloude-code runs the copy at
# ~/Development/ai-setup/scripts/launchd/cloude-code.sh on the mini, which
# lives OUTSIDE this repo (outside ai-setup's own git tracking too, as far
# as this repo can see) and is NOT touched by a `git pull` of CloudeCode.
# Editing this file changes nothing on the mini until someone deliberately
# copies the change over - that is the same untracked-copy hazard class
# recorded for the QNAP backup scripts in Infrastructure/CLAUDE.md hazards
# 7/15. Keep this mirror byte-identical to the live file (diffs are the
# signal something drifted) except for the one guard block added below,
# which still needs to be applied to the live file by hand - see the
# "CLOUDE_DEV_RELOAD guard" comment block.
#
# Runs as a USER LaunchAgent (gui/501), NOT a LaunchDaemon. CloudeCode shells
# out to the `claude` CLI, which reads credentials from the macOS login
# Keychain, and the Keychain is unreadable outside a GUI session. See
# com.imc.claude-remote for the same constraint.
#
# launchd handles restarts via KeepAlive; do NOT loop in this script.

set -euo pipefail

export PATH=/opt/homebrew/bin:$PATH

APP_DIR="/Users/jsugamele/Development/CloudeCode"
LOG_DIR="$HOME/Library/Logs/cloude-code"
LOG_FILE="$LOG_DIR/server.log"

mkdir -p "$LOG_DIR"
builtin cd "$APP_DIR"

# SAFETY GUARD: upstream's setup_auth.py rewrites .env and has been known to
# reset HOST=0.0.0.0, which silently un-does the deliberate tailnet-only bind
# (100.126.160.23) and exposes a remote-shell UI on every interface. Refuse
# to start rather than fail open. .env is loaded RELATIVE to cwd by the app,
# which is why the cd above matters.
ENV_HOST=$(grep -E '^HOST=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)

# 2026-08-16: the user explicitly asked for an all-interfaces bind so a phone
# on plain wifi can reach this without joining the tailnet. 0.0.0.0 is now
# PERMITTED, but only with the explicit opt-in CLOUDE_ALLOW_ALL_INTERFACES=1
# in .env. Exposure that implies: the CloudeCode remote-shell UI becomes
# reachable from every device on the LAN, protected only by the app's own
# auth layer. An EMPTY or UNSET HOST is still fatal, since that was the
# accidental reversion this guard was originally written to catch.
ALLOW_ALL=$(grep -E '^CLOUDE_ALLOW_ALL_INTERFACES=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)

if [ -z "$ENV_HOST" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: refusing to start, HOST is empty or unset in .env. Set HOST before restarting." >>"$LOG_FILE"
    exit 1
fi

if [ "$ENV_HOST" = "0.0.0.0" ] && [ "$ALLOW_ALL" != "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: refusing to start, HOST=0.0.0.0 (all interfaces) without CLOUDE_ALLOW_ALL_INTERFACES=1 in .env. Set the opt-in deliberately or fix HOST." >>"$LOG_FILE"
    exit 1
fi

# CLOUDE_DEV_RELOAD guard (added fix/no-reload-in-production, 2026-08-18):
# src/main.py now reads settings.dev_reload (env CLOUDE_DEV_RELOAD) instead
# of hardcoding uvicorn's reload=True. That fixes the default. This is a
# second, independent line of defense at the launchd layer, mirroring the
# HOST guard above: a deployed .env must never carry
# CLOUDE_DEV_RELOAD=1 - a file-watching reloader has no business running
# under launchd, since it re-execs the whole server on every write under
# the watch root, including the writes a `git pull` makes. Refuse to start
# rather than silently run degraded.
DEV_RELOAD=$(grep -E '^CLOUDE_DEV_RELOAD=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)

if [ "$DEV_RELOAD" = "1" ] || [ "$DEV_RELOAD" = "true" ] || [ "$DEV_RELOAD" = "True" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: refusing to start, CLOUDE_DEV_RELOAD is set in .env under launchd. This flag enables uvicorn's file-watching auto-reload, which restarts the whole server on any file change (including a git pull) - it must never run under the production launchd job. Unset it in .env before restarting." >>"$LOG_FILE"
    exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') starting CloudeCode server, HOST=$ENV_HOST" >>"$LOG_FILE"

exec /Users/jsugamele/Development/CloudeCode/venv/bin/python3 -m src.main
