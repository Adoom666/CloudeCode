#!/bin/sh
# Pre-commit hook: refuse a commit that stages credential material.
#
# WHAT IT SCANS. Only the lines the commit ADDS, via git diff --cached.
# A credential already sitting in a file you happened to touch does not
# block you, because blocking on someone else's old line is how a hook
# gets uninstalled.
#
# TWO GATES, BOTH RUN HERE. scripts/scan_secrets.py first, then gitleaks
# with the repo's own .gitleaks.toml - the same scanner and the same config
# CI runs. They catch different things (see the block above the gitleaks
# call for the measured difference), and running only one of them locally is
# what let a commit pass this hook and then fail CI.
#
# WHAT IT NEVER DOES. It never prints, logs or writes a matched value.
# The report is path, line, detector and a sha256 prefix, plus a masked
# excerpt. See scripts/scan_secrets.py.
#
# EXIT CODES. 0 clean, 1 findings (commit refused), 2 could not scan
# (commit refused, and it SAYS it could not scan). 2 is not 0. A hook
# that waves a commit through because it failed to run is worse than no
# hook, because it produces a record of having checked.
#
# BYPASS. git commit --no-verify. Documented deliberately: a hook you
# cannot get past in an emergency is a hook that gets deleted instead.
#
# Install:   ./scripts/install-secret-hook.sh
# Uninstall: ./scripts/uninstall-secret-hook.sh
# Docs:      docs/secret-scanning.md

set -u

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "pre-commit secret scan: not in a git work tree, refusing" >&2
    exit 2
}

SCANNER="$REPO_ROOT/scripts/scan_secrets.py"
if [ ! -f "$SCANNER" ]; then
    echo "pre-commit secret scan: $SCANNER is missing, refusing" >&2
    echo "  (uninstall with ./scripts/uninstall-secret-hook.sh)" >&2
    exit 2
fi

# The scanner is stdlib-only on purpose, so the hook still works when the
# virtualenv is absent, half-built or mid-upgrade. Prefer it when present
# only so the hook runs the same interpreter the tests do.
if [ -x "$REPO_ROOT/venv/bin/python3" ]; then
    PY="$REPO_ROOT/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "pre-commit secret scan: no python3 found, refusing" >&2
    exit 2
fi

"$PY" "$SCANNER" --staged --repo "$REPO_ROOT"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

# ---------------------------------------------------------------------------
# SECOND GATE: gitleaks, the same scanner CI runs, with the same config.
#
# WHY BOTH. They are genuinely complementary, measured 2026-08-31, not
# assumed:
#   - the python scanner catches a 1Password `ops_` SERVICE ACCOUNT TOKEN.
#     gitleaks 8.30.1 with the default ruleset does NOT - it has no rule for
#     that shape at all. That is the credential class this fleet has actually
#     leaked, so dropping the python scanner would remove the one gate that
#     sees it.
#   - gitleaks names ~170 vendor formats (stripe, sendgrid, twilio, npm, ...)
#     that the python scanner only ever sees through its generic entropy
#     heuristic, if at all.
# So neither is a superset. Running one locally and the other in CI is what
# produced the failure mode this block exists to remove: a commit that passes
# the local hook and is then rejected by CI. A local gate that green-lights
# what CI refuses is worse than no local gate, because it produces a record
# of having checked.
#
# WHY A MISSING GITLEAKS DOES NOT REFUSE THE COMMIT. A hook a contributor
# cannot get past is a hook that gets uninstalled, and then BOTH gates are
# gone. Absence is reported as what it is - a gate that did not run - rather
# than being silently folded into a pass. That is the three-outcome rule:
# clean, findings, and could-not-check are three different answers.
# ---------------------------------------------------------------------------
CONFIG="$REPO_ROOT/.gitleaks.toml"

if ! command -v gitleaks >/dev/null 2>&1; then
    echo "pre-commit secret scan: NOTE - gitleaks is not installed, so the" >&2
    echo "  second gate DID NOT RUN. The python scanner passed; that is not" >&2
    echo "  the same as clean. CI runs gitleaks and can still reject this." >&2
    echo "  Install: brew install gitleaks   (see docs/secret-scanning.md)" >&2
    exit 0
fi

# A missing config is reported, not fatal. The hook is installed per-clone
# and can legitimately end up in a work tree that has no .gitleaks.toml;
# refusing there would block every commit in that tree and get the hook
# uninstalled, losing the python gate too. Say what did not run instead.
if [ ! -f "$CONFIG" ]; then
    echo "pre-commit secret scan: NOTE - $CONFIG is missing, so the gitleaks" >&2
    echo "  gate DID NOT RUN. The python scanner passed; that is not the same" >&2
    echo "  as clean. CI runs gitleaks and can still reject this." >&2
    exit 0
fi

# Version parity with CI. The pin lives in the workflow, which is the single
# source of truth; this only reads it. A parse failure is reported, never
# guessed at - an unverified version is not a matching version.
WORKFLOW="$REPO_ROOT/.github/workflows/secret-scan.yml"
PINNED=$(sed -n 's/^[[:space:]]*GITLEAKS_VERSION:[[:space:]]*\([0-9][0-9.]*\).*/\1/p' "$WORKFLOW" 2>/dev/null | head -1)
LOCAL_V=$(gitleaks version 2>/dev/null | tr -d '\r' | head -1)
if [ -z "$PINNED" ]; then
    echo "pre-commit secret scan: NOTE - could not read the pinned gitleaks" >&2
    echo "  version from $WORKFLOW, so local/CI parity is UNVERIFIED." >&2
elif [ "$PINNED" != "$LOCAL_V" ]; then
    echo "pre-commit secret scan: NOTE - local gitleaks $LOCAL_V does not match" >&2
    echo "  the version CI pins ($PINNED). Rules differ between versions, so a" >&2
    echo "  local pass here does not guarantee CI agrees." >&2
fi

gitleaks git --staged "$REPO_ROOT" --config "$CONFIG" --redact --no-banner
grc=$?
if [ "$grc" -ne 0 ]; then
    echo "pre-commit secret scan: gitleaks refused this commit (exit $grc)." >&2
    echo "  A non-zero exit is EITHER findings OR a config that failed to load;" >&2
    echo "  the output above says which. Both refuse, on purpose." >&2
    echo "  Bypass (recorded, and CI will still run): git commit --no-verify" >&2
    exit 1
fi

exit 0
