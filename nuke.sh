#!/bin/bash

# Nuke script for Cloude Code.
# Completely removes local configuration, venv, and state.
#
# Plan v3.2: the Cloudflare tunnel system was demolished. This script no
# longer touches Cloudflare resources (tunnels, DNS records) or the
# `cloudflared` binary state - there's nothing to tear down on Cloudflare.
#
# ---------------------------------------------------------------------------
# TESTABILITY CONTRACT
#
# Every destructive target in this script is redirectable through an
# environment variable whose DEFAULT is the real production value. That is
# not decoration: an uninstaller nobody can rehearse is an uninstaller
# nobody can regression-test, and this script shipped for months silently
# missing the state directory (it targeted "Cloude Code" with a space; the
# app has always used "CloudeCode") because there was no way to prove
# otherwise. See tests/test_nuke_sandbox.py, which runs THIS FILE,
# unmodified, with every one of these pointed into a temp sandbox.
#
#   CLOUDE_STATE_DIR          state directory. Read by the APP too
#                             (src/config.py Settings.get_state_dir), so
#                             this is not a nuke-only knob - it is the one
#                             the application itself honours.
#   CLOUDE_NUKE_HOME          root for $HOME-derived paths (LaunchAgents,
#                             Application Support, ~/cloude-projects).
#                             Default: $HOME.
#   CLOUDE_NUKE_TMP_DIR       root for the /tmp artifacts. Default: /tmp.
#   CLOUDE_NUKE_LAUNCHCTL     launchctl executable. Default: launchctl.
#   CLOUDE_NUKE_PGREP_PATTERN process-name pattern for the menubar kill.
#                             Default: "Cloude Code". Set EMPTY to skip
#                             the process kill entirely (the default is a
#                             machine-wide, HOME-independent string match
#                             and is the one target a sandbox cannot
#                             contain by redirection alone).
#   CLOUDE_NUKE_TMUX_BIN      tmux executable. Default: tmux.
#   CLOUDE_NUKE_TMUX_SOCKET   tmux socket name. Default: session.
#                             tmux_socket_name from config.json, else
#                             "cloude".
#   CLOUDE_NUKE_KILL_TMUX     "true" to kill that tmux server. Default
#                             FALSE, deliberately - see the tmux section.
#   CLOUDE_NUKE_DRY_RUN       "true" (or --dry-run) to print every action
#                             without performing it.
# ---------------------------------------------------------------------------

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR"
source "${SCRIPT_DIR}/scripts/resolve-port.sh"
# resolve_state_dir() mirrors src/config.py's Settings.get_state_dir()
# precedence and is held to it by tests/test_state_dir_drift.py. Sourcing
# the shared resolver is the whole point: a second hardcoded literal in
# shell is what produced the defect this section fixes.
source "${SCRIPT_DIR}/scripts/upgrade_lib/upgrade_rollback_common.sh"

echo "Cloude Code - Nuke it from Orbit!"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse flags. --skip-confirm is for macOS app usage; --dry-run performs no
# destructive action at all.
SKIP_CONFIRM=false
DRY_RUN="${CLOUDE_NUKE_DRY_RUN:-false}"
for arg in "$@"; do
    case "$arg" in
        --skip-confirm) SKIP_CONFIRM=true ;;
        --dry-run)      DRY_RUN=true ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: nuke.sh [--skip-confirm] [--dry-run]" >&2
            exit 2
            ;;
    esac
done
[ "$SKIP_CONFIRM" = "true" ] && echo "Running in non-interactive mode (confirmation already provided)"
[ "$DRY_RUN" = "true" ] && echo -e "${YELLOW}DRY RUN - nothing will be deleted.${NC}"

# --- redirectable roots ------------------------------------------------------

NUKE_HOME="${CLOUDE_NUKE_HOME:-$HOME}"
NUKE_TMP="${CLOUDE_NUKE_TMP_DIR:-/tmp}"
LAUNCHCTL_BIN="${CLOUDE_NUKE_LAUNCHCTL:-launchctl}"
PGREP_PATTERN="${CLOUDE_NUKE_PGREP_PATTERN-Cloude Code}"
TMUX_BIN="${CLOUDE_NUKE_TMUX_BIN:-tmux}"
KILL_TMUX="${CLOUDE_NUKE_KILL_TMUX:-false}"

cd "$PROJECT_ROOT"

# Source .env so subsequent steps can resolve LOG_DIRECTORY /
# DEFAULT_WORKING_DIR. Done from PROJECT_ROOT, not from whatever directory
# the caller happened to be in.
if [ -f "${PROJECT_ROOT}/.env" ]; then
    # shellcheck disable=SC1091
    source "${PROJECT_ROOT}/.env"
fi

# --- state directory: resolved, never restated -------------------------------
#
# THE defect this section exists to prevent. Do not replace this with a
# literal. A nuke that cannot determine a target must FAIL, not continue
# and report success having silently skipped it.

if ! STATE_DIR="$(resolve_state_dir "$PROJECT_ROOT" 2>/dev/null)" || [ -z "$STATE_DIR" ]; then
    echo -e "${RED}FATAL${NC}: could not resolve the Cloude Code state directory." >&2
    echo "  resolve_state_dir() failed. It needs either ${PROJECT_ROOT}/venv/bin/python3" >&2
    echo "  or a python3 on PATH. Refusing to continue: the state directory holds" >&2
    echo "  cloude.db and refresh_tokens.db, and a reset that skips them is not a" >&2
    echo "  reset. Fix the interpreter, or set CLOUDE_STATE_DIR explicitly, and" >&2
    echo "  re-run. NOTHING has been deleted." >&2
    exit 1
fi
case "$STATE_DIR" in
    /*) ;;
    *)
        echo -e "${RED}FATAL${NC}: resolved state directory is not an absolute path: '${STATE_DIR}'. Refusing to continue. NOTHING has been deleted." >&2
        exit 1
        ;;
esac
# Guard against a resolution that would take out the world. Any of these
# means the resolver produced something nonsensical; deleting it would be
# far worse than not deleting anything.
for forbidden in "/" "$HOME" "$NUKE_HOME" "/Users" "/Library" "$HOME/Library" "$HOME/Library/Application Support"; do
    if [ "${STATE_DIR%/}" = "${forbidden%/}" ]; then
        echo -e "${RED}FATAL${NC}: resolved state directory '${STATE_DIR}' is a protected path. Refusing to continue. NOTHING has been deleted." >&2
        exit 1
    fi
done
echo -e "${BLUE}i${NC} State directory resolved to: ${STATE_DIR}"

echo ""
if [ "$SKIP_CONFIRM" = "false" ] && [ "$DRY_RUN" = "false" ]; then
    echo -e "${RED}WARNING${NC}"
    echo ""
    echo "This will completely remove ALL Cloude Code configuration and setup:"
    echo ""
    echo "  x All local configuration files (.env, config.json, etc.)"
    echo "  x Python virtual environment"
    echo "  x The state directory and everything in it, INCLUDING the"
    echo "    session database and your stored refresh tokens:"
    echo "      ${STATE_DIR}"
    echo "  x All logs and temporary files"
    echo "  x macOS app settings and LaunchAgent"
    echo ""
    echo -e "${YELLOW}You will need to run setup.sh again to use Cloude Code.${NC}"
    echo ""
    read -p "Are you ABSOLUTELY SURE you want to continue? (type 'NUKE' to confirm): " CONFIRM

    if [ "$CONFIRM" != "NUKE" ]; then
        echo ""
        echo "Aborted. No changes made."
        exit 0
    fi
fi

echo ""
echo "========================================"
echo "Starting cleanup process..."
echo "========================================"
echo ""

# Track what we've cleaned up
CLEANUP_LOG=()

# Description: record and print a completed cleanup action.
# Inputs: $1 - message.
# Output: none.
log_cleanup() {
    local message=$1
    CLEANUP_LOG+=("$message")
    echo -e "${GREEN}v${NC} $message"
}

# Description: print a target that was not present. PASS-tier: absence of a
#   target is a legitimate outcome, distinct from "could not evaluate".
# Inputs: $1 - message.
# Output: none.
log_skip() {
    local message=$1
    echo -e "${BLUE}o${NC} $message (not found, skipping)"
}

# Description: print the explicit COULD-NOT-EVALUATE state - a target this
#   script deliberately did not act on, and why. Never collapse this into
#   log_skip: "absent" and "present but not safely actionable" are
#   different facts and the summary must not conflate them.
# Inputs: $1 - message describing what was not done and why.
# Output: none.
log_unknown_target() {
    echo -e "${YELLOW}?${NC} CANNOT DETERMINE / NOT ACTIONED: $1"
}

# Description: delete a path, honouring --dry-run, and log the outcome.
# Inputs: $1 - path; $2 - human label for the summary line.
# Output: none. Returns 0 always (an absent target is not a failure).
nuke_path() {
    local target="$1" label="$2"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            log_cleanup "[dry-run] would remove: ${label}"
        else
            rm -rf "$target"
            log_cleanup "Removed: ${label}"
        fi
    else
        log_skip "$label"
    fi
}

# 1. Stop running processes
echo "Stopping running processes..."
echo "--------------------------------------"

# Stop server (find by configured port, or fall through to the menubar
# process kill below). A malformed PORT= in .env must not silently be
# treated as "8000" here - that would kill the wrong process (or none)
# while claiming this step succeeded. It also must not abort the rest of
# nuke.sh (venv, log dirs, LaunchAgent, etc. are unaffected by not being
# able to resolve a port), so this reports and continues rather than
# dying under `set -e`.
if PORT="$(resolve_port "${PROJECT_ROOT}")"; then
    SERVER_PID=$(lsof -ti:"${PORT}" 2>/dev/null || echo "")
    if [ -n "$SERVER_PID" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            log_cleanup "[dry-run] would stop server on port ${PORT} (PID: $SERVER_PID)"
        else
            kill -9 $SERVER_PID 2>/dev/null || true
            log_cleanup "Stopped server process on port ${PORT} (PID: $SERVER_PID)"
        fi
    else
        log_skip "Server process not running on port ${PORT}"
    fi
else
    log_unknown_target "port could not be determined (see reason above); the port-based server kill was skipped. If the server is still running, stop it manually."
fi

# Stop macOS menubar app.
#
# This is a machine-wide, HOME-independent process-name match: it is the one
# target in this script that redirection cannot confine to a sandbox, because
# there is no per-sandbox process namespace on macOS. Setting
# CLOUDE_NUKE_PGREP_PATTERN to the empty string disables it outright, which is
# what the sandbox test does - and the test says so rather than pretending it
# exercised this branch.
if [ -z "$PGREP_PATTERN" ]; then
    log_unknown_target "process kill disabled (CLOUDE_NUKE_PGREP_PATTERN is empty). Any running menubar app was NOT stopped."
else
    MENUBAR_PIDS=$(pgrep -f "$PGREP_PATTERN" || echo "")
    if [ -n "$MENUBAR_PIDS" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            log_cleanup "[dry-run] would kill processes matching '${PGREP_PATTERN}': $(echo $MENUBAR_PIDS | tr '\n' ' ')"
        else
            echo "$MENUBAR_PIDS" | xargs kill -9 2>/dev/null || true
            log_cleanup "Stopped macOS menubar app"
        fi
    else
        log_skip "macOS menubar app not running"
    fi
fi

echo ""

# 2. tmux sessions
echo "tmux sessions..."
echo "--------------------------------------"

# The app runs its sessions on `tmux -L <session.tmux_socket_name>`, default
# "cloude". That socket is keyed on (user, socket name) ONLY - it carries no
# record of which checkout created a session. Several checkouts of this repo on
# one machine therefore SHARE it, and this script has no way to tell a session
# this install started from a session another install started, or from one the
# user started by hand.
#
# Killing the server would destroy all of them. Per the three-outcome rule that
# is a CANNOT DETERMINE, not a pass, and the honest action is to name the
# socket and not touch it. Opt in with CLOUDE_NUKE_KILL_TMUX=true when you know
# the socket is yours alone.
TMUX_SOCKET="${CLOUDE_NUKE_TMUX_SOCKET:-}"
if [ -z "$TMUX_SOCKET" ] && [ -f "${PROJECT_ROOT}/config.json" ]; then
    TMUX_SOCKET="$("$(resolve_python "$PROJECT_ROOT")" -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("session", {}).get("tmux_socket_name", "") or "")
except Exception:
    print("")
' "${PROJECT_ROOT}/config.json" 2>/dev/null || echo "")"
fi
TMUX_SOCKET="${TMUX_SOCKET:-cloude}"

if [ "$KILL_TMUX" != "true" ]; then
    log_unknown_target "tmux socket '${TMUX_SOCKET}' was NOT killed. A tmux socket is shared by every checkout run as this user, so sessions on it cannot be attributed to this install. Kill it yourself with: ${TMUX_BIN} -L ${TMUX_SOCKET} kill-server  (or re-run with CLOUDE_NUKE_KILL_TMUX=true)."
elif ! command -v "$TMUX_BIN" >/dev/null 2>&1 && [ ! -x "$TMUX_BIN" ]; then
    log_unknown_target "CLOUDE_NUKE_KILL_TMUX=true but tmux ('${TMUX_BIN}') was not found. Socket '${TMUX_SOCKET}' left alone."
elif [ "$DRY_RUN" = "true" ]; then
    log_cleanup "[dry-run] would run: ${TMUX_BIN} -L ${TMUX_SOCKET} kill-server"
elif "$TMUX_BIN" -L "$TMUX_SOCKET" kill-server 2>/dev/null; then
    log_cleanup "Killed tmux server on socket '${TMUX_SOCKET}'"
else
    log_skip "tmux socket '${TMUX_SOCKET}' (no server running)"
fi

echo ""

# 3. Remove local files
echo "Removing local files..."
echo "--------------------------------------"

FILES_TO_DELETE=(
    ".env"
    "config.json"
    "totp-qr.png"
    "session_metadata.json"
    ".env.tmp"
)

for file in "${FILES_TO_DELETE[@]}"; do
    nuke_path "${PROJECT_ROOT}/${file}" "File: ${file}"
done

nuke_path "${PROJECT_ROOT}/venv" "Directory: venv/"

echo ""

# 4. Remove directories
echo "Removing directories..."
echo "--------------------------------------"

# Get directory paths from .env if it existed (we already sourced it above).
LOG_DIR="${LOG_DIRECTORY:-${NUKE_TMP}/cloude-code-logs}"
PROJECTS_DIR="${DEFAULT_WORKING_DIR:-${NUKE_HOME}/cloude-projects}"

# Expand ~ and variables
LOG_DIR=$(eval echo "$LOG_DIR")
PROJECTS_DIR=$(eval echo "$PROJECTS_DIR")

nuke_path "$LOG_DIR" "Directory: $LOG_DIR"
nuke_path "$PROJECTS_DIR" "Directory: $PROJECTS_DIR"

# Remove /tmp logs. NOTE the nuke log itself is handled separately, at the
# very end, because the macOS app holds a write stream open on it for the
# duration of this run.
TMP_LOGS=(
    "${NUKE_TMP}/cloudecode-server.log"
    "${NUKE_TMP}/cloudecode-menubar.log"
    "${NUKE_TMP}/cloudecode-menubar-error.log"
    "${NUKE_TMP}/electron-test.log"
)

for log_file in "${TMP_LOGS[@]}"; do
    nuke_path "$log_file" "File: $log_file"
done

nuke_path "${NUKE_TMP}/cloude-app-extract" "Directory: ${NUKE_TMP}/cloude-app-extract"

echo ""

# 5. Remove macOS LaunchAgent
echo "Removing macOS LaunchAgent..."
echo "--------------------------------------"

LAUNCH_AGENT="${NUKE_HOME}/Library/LaunchAgents/com.cloudecode.menubar.plist"
if [ -f "$LAUNCH_AGENT" ]; then
    if [ "$DRY_RUN" = "true" ]; then
        log_cleanup "[dry-run] would unload and remove: $LAUNCH_AGENT"
    else
        "$LAUNCHCTL_BIN" unload "$LAUNCH_AGENT" 2>/dev/null || true
        rm -f "$LAUNCH_AGENT"
        log_cleanup "Removed: $LAUNCH_AGENT"
    fi
else
    log_skip "LaunchAgent: com.cloudecode.menubar.plist"
fi

echo ""

# 6. Remove application state and App Support files
echo "Removing application state..."
echo "--------------------------------------"

# STATE_DIR is the resolved, app-authoritative location - cloude.db,
# refresh_tokens.db, migration_trail.jsonl. Leaving refresh tokens behind
# after an uninstall is a security defect, not untidiness.
nuke_path "$STATE_DIR" "State directory: $STATE_DIR"

APP_SUPPORT_DIRS=(
    # Electron's own store.
    "${NUKE_HOME}/Library/Application Support/cloude-code-menubar"
    # LEGACY: a spaced name some early build may have used. The app has
    # never written here; kept only so an old install is not left behind.
    # It is NOT a substitute for STATE_DIR above - believing it was is the
    # bug this file was rewritten to fix.
    "${NUKE_HOME}/Library/Application Support/Cloude Code"
)

for APP_SUPPORT in "${APP_SUPPORT_DIRS[@]}"; do
    nuke_path "$APP_SUPPORT" "Directory: $APP_SUPPORT"
done

echo ""

# 7. This script's own log, last.
NUKE_LOG="${NUKE_TMP}/cloudecode-nuke.log"
if [ "$SKIP_CONFIRM" = "true" ]; then
    # The macOS app (macOS/main.js) opens a write stream on this file before
    # exec'ing us and keeps writing to it after we exit. Deleting it here
    # would leave that stream writing into an unlinked inode and destroy the
    # only record of this run at the moment it is most wanted.
    log_unknown_target "left in place: ${NUKE_LOG} (the macOS app is still writing to it; it is overwritten on the next run)."
else
    nuke_path "$NUKE_LOG" "File: $NUKE_LOG"
fi

echo ""
echo "========================================"
if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}Dry run complete. Nothing was deleted.${NC}"
else
    echo -e "${GREEN}Cleanup Complete!${NC}"
fi
echo "========================================"
echo ""
echo "Summary of removed items:"
echo ""

for item in "${CLEANUP_LOG[@]}"; do
    echo "  - $item"
done

echo ""
if [ "$DRY_RUN" = "false" ]; then
    echo -e "${YELLOW}Your system has been reset to a fresh state.${NC}"
    echo ""
    echo "To set up Cloude Code again, run:"
    echo "  ./setup.sh"
fi
echo ""
