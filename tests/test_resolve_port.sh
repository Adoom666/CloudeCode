#!/bin/bash
# tests/test_resolve_port.sh - tests for resolve_port in
# scripts/resolve-port.sh, the single shared shell-side port resolver
# sourced by nuke.sh, stop.sh, reset.sh, and
# scripts/upgrade_lib/upgrade_rollback_common.sh (used by upgrade.sh /
# rollback.sh).
#
# Plain bash test (no pytest bridge exists for these functions - they are
# bash, not Python). Run directly: ./tests/test_resolve_port.sh
# Exits 0 if every case passes, 1 on the first failure, printing which.
#
# Covers the three-outcome contract documented at the top of
# scripts/resolve-port.sh:
#   - .env absent -> DEFAULT_PORT (8000), exit 0.
#   - .env present with no PORT= line -> DEFAULT_PORT, exit 0.
#   - .env present with a valid PORT= override -> that value, exit 0.
#   - .env present with a non-numeric PORT= -> nothing on stdout, a
#     "could not determine port" message on stderr, exit 1. This is the
#     case that must NEVER be silently coerced to DEFAULT_PORT - see
#     CLAUDE.md's THE THREE-OUTCOME RULE.
#   - .env present with an out-of-range PORT= (e.g. 99999) -> same
#     could-not-determine failure, not a truncated/wrapped port number.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${TEST_DIR}/.." && pwd -P)"
# shellcheck source=../scripts/resolve-port.sh
source "${REPO_ROOT}/scripts/resolve-port.sh"

FAILURES=0

# Description: minimal fail-fast assertion. Prints PASS/FAIL and tracks
#   failures in $FAILURES rather than exiting immediately, so one run
#   reports every case instead of stopping at the first.
# Inputs: $1 - condition already evaluated to 0/1. $2 - description.
# Output: none; increments FAILURES on a non-zero condition.
assert() {
    local rc="$1" desc="$2"
    if [ "${rc}" -eq 0 ]; then
        printf 'PASS: %s\n' "${desc}"
    else
        printf 'FAIL: %s\n' "${desc}"
        FAILURES=$((FAILURES + 1))
    fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# ---------------------------------------------------------------------- #
# Case 1: no .env at all - a fresh checkout that has never been set up.
# Must resolve to DEFAULT_PORT and succeed, matching what
# src/config.py's Settings.port field default would resolve to.
# ---------------------------------------------------------------------- #
INSTALL1="${WORK}/install1"
mkdir -p "${INSTALL1}"

OUT="$(resolve_port "${INSTALL1}")"
RC=$?
assert "${RC}" "case 1: resolve_port exits 0 when .env is absent"
assert "$([ "${OUT}" = "8000" ] && echo 0 || echo 1)" \
    "case 1: resolve_port prints DEFAULT_PORT (8000) when .env is absent"

# ---------------------------------------------------------------------- #
# Case 2: .env present but with no PORT= line at all (e.g. HOST= only).
# Same expected outcome as case 1 - no override configured is a real,
# determined answer, not a failure.
# ---------------------------------------------------------------------- #
INSTALL2="${WORK}/install2"
mkdir -p "${INSTALL2}"
printf 'HOST=0.0.0.0\n' > "${INSTALL2}/.env"

OUT="$(resolve_port "${INSTALL2}")"
RC=$?
assert "${RC}" "case 2: resolve_port exits 0 when .env has no PORT= line"
assert "$([ "${OUT}" = "8000" ] && echo 0 || echo 1)" \
    "case 2: resolve_port prints DEFAULT_PORT when .env has no PORT= line"

# ---------------------------------------------------------------------- #
# Case 3: .env sets a valid, non-default PORT=. Must be honored exactly -
# this is the real defect the whole change fixes: nuke.sh / stop.sh /
# reset.sh / the macOS menu-bar app previously ignored this entirely and
# always acted against the literal 8000.
# ---------------------------------------------------------------------- #
INSTALL3="${WORK}/install3"
mkdir -p "${INSTALL3}"
printf 'PORT=9123\n' > "${INSTALL3}/.env"

OUT="$(resolve_port "${INSTALL3}")"
RC=$?
assert "${RC}" "case 3: resolve_port exits 0 for a valid PORT= override"
assert "$([ "${OUT}" = "9123" ] && echo 0 || echo 1)" \
    "case 3: resolve_port honors a valid non-default PORT= override"

# ---------------------------------------------------------------------- #
# Case 4: .env sets a non-numeric PORT=. Must fail loudly - exit 1, no
# stdout, a "could not determine port" reason on stderr - and must NEVER
# silently print DEFAULT_PORT. This is the three-outcome case.
# ---------------------------------------------------------------------- #
INSTALL4="${WORK}/install4"
mkdir -p "${INSTALL4}"
printf 'PORT=notaport\n' > "${INSTALL4}/.env"

OUT="$(resolve_port "${INSTALL4}" 2>"${WORK}/case4.stderr")"
RC=$?
assert "$([ "${RC}" -ne 0 ] && echo 0 || echo 1)" \
    "case 4: resolve_port exits non-zero for a non-numeric PORT="
assert "$([ -z "${OUT}" ] && echo 0 || echo 1)" \
    "case 4: resolve_port prints nothing on stdout for a non-numeric PORT= (never DEFAULT_PORT)"
assert "$(grep -q 'could not determine port' "${WORK}/case4.stderr" && echo 0 || echo 1)" \
    "case 4: resolve_port names the failure on stderr as could-not-determine, not a silent guess"

# ---------------------------------------------------------------------- #
# Case 5: .env sets an out-of-range PORT= (99999, above the 65535 max).
# Must fail the same way as case 4 - a malformed value is a malformed
# value regardless of whether it merely looks numeric.
# ---------------------------------------------------------------------- #
INSTALL5="${WORK}/install5"
mkdir -p "${INSTALL5}"
printf 'PORT=99999\n' > "${INSTALL5}/.env"

OUT="$(resolve_port "${INSTALL5}" 2>"${WORK}/case5.stderr")"
RC=$?
assert "$([ "${RC}" -ne 0 ] && echo 0 || echo 1)" \
    "case 5: resolve_port exits non-zero for an out-of-range PORT="
assert "$([ -z "${OUT}" ] && echo 0 || echo 1)" \
    "case 5: resolve_port prints nothing on stdout for an out-of-range PORT="

# ---------------------------------------------------------------------- #
# Case 6: .env sets PORT= to an empty value (trailing "PORT=" with
# nothing after it). Treated the same as "no override" - DEFAULT_PORT,
# not a failure - matching pydantic-settings' own treatment of an empty
# env var as unset.
# ---------------------------------------------------------------------- #
INSTALL6="${WORK}/install6"
mkdir -p "${INSTALL6}"
printf 'PORT=\n' > "${INSTALL6}/.env"

OUT="$(resolve_port "${INSTALL6}")"
RC=$?
assert "${RC}" "case 6: resolve_port exits 0 for an empty PORT= value"
assert "$([ "${OUT}" = "8000" ] && echo 0 || echo 1)" \
    "case 6: resolve_port treats an empty PORT= as no override (DEFAULT_PORT)"

echo ""
if [ "${FAILURES}" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
else
    echo "${FAILURES} FAILURE(S)"
    exit 1
fi
