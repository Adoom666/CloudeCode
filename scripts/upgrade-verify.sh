#!/bin/bash
# scripts/upgrade-verify.sh - compare the live install against the newest
# baseline and report each check separately, with three outcomes each.
#
# Read-only. Never repairs anything: its whole job is to tell you the truth
# about what the upgrade did, including the parts it cannot determine.
#
# Usage:
#   ./scripts/upgrade-verify.sh [--install-dir DIR] [--state-dir DIR]
#                               [--baseline FILE] [--url URL]
#
# Exit codes:
#   0  every check passed
#   1  at least one check FAILED
#   2  at least one check COULD NOT BE EVALUATED and none failed
#
# 2 is deliberately not 0. "I could not look" is not "nothing is wrong".
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR=""
BASELINE=""
URL="http://127.0.0.1:8000"

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --state-dir)   STATE_DIR="$2";   shift 2 ;;
        --baseline)    BASELINE="$2";    shift 2 ;;
        --url)         URL="$2";         shift 2 ;;
        -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

PY="$INSTALL_DIR/venv/bin/python3"
[ -x "$PY" ] || PY="python3"

CLOUDE_BASELINE_STATE_DIR="$STATE_DIR" \
CLOUDE_BASELINE_INSTALL_DIR="$INSTALL_DIR" \
CLOUDE_BASELINE_OUT_DIR="$INSTALL_DIR/.upgrade-baselines" \
CLOUDE_BASELINE_FILE="$BASELINE" \
CLOUDE_BASELINE_URL="$URL" \
"$PY" "$INSTALL_DIR/scripts/upgrade_snapshot.py" verify
