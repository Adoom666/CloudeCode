#!/bin/bash
# scripts/upgrade-baseline.sh - snapshot what the install looks like BEFORE
# an upgrade, so the upgrade can be VERIFIED afterwards rather than assumed.
#
# You cannot check that a migration preserved your data without a record of
# what the data was. This writes that record. It is read-only: it opens
# cloude.db query-only and never writes to it.
#
# Usage:
#   ./scripts/upgrade-baseline.sh [--install-dir DIR] [--state-dir DIR]
#
# Output:
#   .upgrade-baselines/<UTC timestamp>.json, plus the same content on stdout.
#   Exit 0 when a usable baseline was written; exit 2 when it COULD NOT BE
#   EVALUATED (no database, unreadable database). Exit 2 is not "nothing to
#   do" - it means do not start an upgrade you will not be able to verify.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --state-dir)   STATE_DIR="$2";   shift 2 ;;
        -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

PY="$INSTALL_DIR/venv/bin/python3"
[ -x "$PY" ] || PY="python3"

OUT_DIR="$INSTALL_DIR/.upgrade-baselines"
mkdir -p "$OUT_DIR"

CLOUDE_BASELINE_STATE_DIR="$STATE_DIR" \
CLOUDE_BASELINE_INSTALL_DIR="$INSTALL_DIR" \
CLOUDE_BASELINE_OUT_DIR="$OUT_DIR" \
"$PY" "$INSTALL_DIR/scripts/upgrade_snapshot.py" baseline
