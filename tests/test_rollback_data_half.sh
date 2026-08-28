#!/bin/bash
# tests/test_rollback_data_half.sh - the shell half of design section 9.8.
#
# Covers scripts/upgrade_lib/trail_code_entry.sh (the two-phase kind='code'
# writer that upgrade.sh and rollback.sh now use) and
# scripts/upgrade_lib/rollback_data.sh (the restore executor and the
# --code-only warning), plus one end-to-end assertion that rollback.sh
# refuses BEFORE it stops anything when the trail cannot be read.
#
# Plain bash, same shape as tests/test_upgrade_backup.sh: there is no
# pytest bridge for these functions because they are bash, not Python.
# Run directly: ./tests/test_rollback_data_half.sh
#
# THE ONE THAT MATTERS is case 8. An unreadable trail must make
# rollback.sh die with the server still running, the code still on its
# current commit, and nothing copied - and it must NOT reach for the
# newest backup. That is asserted by inspecting the install after the
# refusal, not by reading the script's exit message.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${TEST_DIR}/.." && pwd -P)"
# shellcheck source=../scripts/upgrade_lib/upgrade_rollback_common.sh
source "${REPO_ROOT}/scripts/upgrade_lib/upgrade_rollback_common.sh"
# shellcheck source=../scripts/upgrade_lib/trail_code_entry.sh
source "${REPO_ROOT}/scripts/upgrade_lib/trail_code_entry.sh"
# shellcheck source=../scripts/upgrade_lib/rollback_data.sh
source "${REPO_ROOT}/scripts/upgrade_lib/rollback_data.sh"

SELECTOR="${REPO_ROOT}/scripts/upgrade_lib/trail_select.py"
FAILURES=0

# Description: record one assertion result. Tracks failures rather than
#   exiting, so one run reports every case instead of stopping at the first.
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

# Description: build a throwaway install dir whose state directory is
#   inside it, so nothing in these tests can reach the real
#   ~/Library/Application Support/CloudeCode.
# Inputs: none.
# Output: prints the install dir path.
make_install() {
    local dir
    dir="$(mktemp -d)"
    mkdir -p "${dir}/state"
    printf 'CLOUDE_STATE_DIR=%s\n' "${dir}/state" > "${dir}/.env"
    printf '{}\n' > "${dir}/config.json"
    printf '%s\n' "${dir}"
}

# Description: count the lines in a trail file, 0 when it does not exist.
# Inputs: $1 - trail path.
# Output: prints the count.
trail_lines() {
    [ -f "$1" ] || { printf '0\n'; return 0; }
    wc -l < "$1" | tr -d ' '
}

echo "=== 1. trail_code_open writes a started line and fsyncs it ==="
INSTALL="$(make_install)"
TRAIL="$(trail_file "${INSTALL}")"
assert "$([ "$(trail_lines "${TRAIL}")" = "0" ] && echo 0 || echo 1)" \
    "no trail before the first open"
HANDLE="$(trail_code_open "${INSTALL}" "0.8.1" "0.8.2" "0.8.1" "test open")"
OPEN_RC=$?
UUID="${HANDLE%% *}"
STARTED="${HANDLE#* }"
assert "${OPEN_RC}" "trail_code_open returns 0"
assert "$([ -n "${UUID}" ] && echo 0 || echo 1)" "it returns an entry_uuid"
assert "$([ "$(trail_lines "${TRAIL}")" = "1" ] && echo 0 || echo 1)" \
    "exactly one line on disk after phase one"
STATUS1="$("${REPO_ROOT}/venv/bin/python3" -c "
import json, sys
line = open(sys.argv[1]).read().splitlines()[0]
d = json.loads(line)
print(d['status'], d['kind'], d['from_version'], d['to_version'], d['backup_path'])
" "${TRAIL}")"
assert "$([ "${STATUS1}" = "started code 0.8.1 0.8.2 None" ] && echo 0 || echo 1)" \
    "phase one is status=started, kind=code, versions recorded, backup null (got: ${STATUS1})"

echo ""
echo "=== 2. trail_code_close appends, reusing the uuid and started_at ==="
trail_code_close "${INSTALL}" "${UUID}" "${STARTED}" "0.8.1" "0.8.2" \
    "completed" "0.8.2" ""
assert "$?" "trail_code_close returns 0"
assert "$([ "$(trail_lines "${TRAIL}")" = "2" ] && echo 0 || echo 1)" \
    "two lines after phase two, never one rewritten line"
SAME="$("${REPO_ROOT}/venv/bin/python3" -c "
import json, sys
a, b = [json.loads(x) for x in open(sys.argv[1]).read().splitlines()]
print(a['entry_uuid'] == b['entry_uuid'],
      a['started_at'] == b['started_at'],
      b['status'], b['completed_at'] is not None)
" "${TRAIL}")"
assert "$([ "${SAME}" = "True True completed True" ] && echo 0 || echo 1)" \
    "the pair shares entry_uuid and started_at, closes completed with a completed_at (got: ${SAME})"

# The pair above is only evidence if the two timestamps could have
# DIFFERED. trail_now has one-second resolution, so an open and a close in
# the same second make "reused the started_at" and "minted a fresh one"
# indistinguishable. Close a second entry with an explicitly old
# started_at, where the two cannot collide.
INSTALL_TS="$(make_install)"
trail_code_open "${INSTALL_TS}" "0.8.1" "0.8.2" "0.8.1" "ts" >/dev/null
trail_code_close "${INSTALL_TS}" "fixed-uuid" "2020-01-01T00:00:00Z" \
    "0.8.1" "0.8.2" "completed" "0.8.2" ""
KEPT="$("${REPO_ROOT}/venv/bin/python3" -c "
import json, sys
d = json.loads(open(sys.argv[1]).read().splitlines()[1])
print(d['started_at'], d['completed_at'] != d['started_at'])
" "$(trail_file "${INSTALL_TS}")")"
assert "$([ "${KEPT}" = "2020-01-01T00:00:00Z True" ] && echo 0 || echo 1)" \
    "the close reuses the started_at it was GIVEN, never a fresh one (got: ${KEPT})"

echo ""
echo "=== 3. a failed close records the error text, not a silent completed ==="
INSTALL2="$(make_install)"
H2="$(trail_code_open "${INSTALL2}" "0.8.2" "0.8.1" "0.8.2" "d")"
trail_code_close "${INSTALL2}" "${H2%% *}" "${H2#* }" "0.8.2" "0.8.1" \
    "failed" "0.8.1" 'boom: "quoted" and	tabbed'
ERRTEXT="$("${REPO_ROOT}/venv/bin/python3" -c "
import json, sys
d = json.loads(open(sys.argv[1]).read().splitlines()[1])
print(repr(d['status']), repr(d['error']))
" "$(trail_file "${INSTALL2}")")"
assert "$([ "${ERRTEXT}" = "'failed' 'boom: \"quoted\" and\\ttabbed'" ] && echo 0 || echo 1)" \
    "status=failed and the error survives quotes and tabs intact (got: ${ERRTEXT})"

echo ""
echo "=== 4. an unclosed open is exactly what the app detects as interrupted ==="
INSTALL3="$(make_install)"
trail_code_open "${INSTALL3}" "0.8.1" "0.8.2" "0.8.1" "killed here" >/dev/null
UNCLOSED="$("${REPO_ROOT}/venv/bin/python3" -c "
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from src.core.trail_reader import read_trail, find_unclosed
r = read_trail(Path(sys.argv[1]))
print(r.status, len(find_unclosed(r.entries)))
" "$(trail_file "${INSTALL3}")" "${REPO_ROOT}")"
assert "$([ "${UNCLOSED}" = "ok 1" ] && echo 0 || echo 1)" \
    "the app's own reader parses the bash-written line and finds 1 unclosed step (got: ${UNCLOSED})"

echo ""
echo "=== 5. plan_data_rollback reports the trail's three outcomes by code ==="
INSTALL4="$(make_install)"
plan_data_rollback "${INSTALL4}" "0.8.1" "${SELECTOR}" >/dev/null 2>&1
assert "$([ "$?" = "${TRAIL_SELECT_ABSENT}" ] && echo 0 || echo 1)" \
    "no trail at all exits ${TRAIL_SELECT_ABSENT} (absent), not 0 and not 2"

T4="$(trail_file "${INSTALL4}")"
{
  printf '{"entry_uuid": "b", "kind": "bootstrap", "from_version": "0", "to_version": "1", "status": "completed", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:01Z", "backup_path": null, "backup_verified": null, "app_version": null, "error": null, "detail": null}\n'
  printf 'this line is not json at all\n'
  printf '{"entry_uuid": "c", "kind": "code", "from_version": "0.8.0", "to_version": "0.8.1", "status": "completed", "started_at": "2026-01-02T00:00:00Z", "completed_at": null, "backup_path": null, "backup_verified": null, "app_version": null, "error": null, "detail": null}\n'
} > "${T4}"
BEFORE_MD5="$(md5 -q "${T4}")"
OUT="$(plan_data_rollback "${INSTALL4}" "0.8.1" "${SELECTOR}" 2>/dev/null)"
RC=$?
assert "$([ "${RC}" = "${TRAIL_SELECT_UNREADABLE}" ] && echo 0 || echo 1)" \
    "a bad line in the middle exits ${TRAIL_SELECT_UNREADABLE} (unreadable), got ${RC}"
assert "$([ "$(md5 -q "${T4}")" = "${BEFORE_MD5}" ] && echo 0 || echo 1)" \
    "reading an unreadable trail did not modify it"
REASON="$(plan_field "${INSTALL4}" "${OUT}" error)"
case "${REASON}" in
    *"corrupt at line 2"*) assert 0 "the reason names the line: ${REASON}" ;;
    *) assert 1 "the reason names the corrupt line (got: ${REASON})" ;;
esac

echo ""
echo "=== 6. restore_one_artifact snapshots, copies, and clears stale wal/shm ==="
INSTALL5="$(make_install)"
STATE5="$(resolve_state_dir "${INSTALL5}")"
printf 'NEW LIVE DATA\n' > "${STATE5}/cloude.db"
printf 'stale wal\n' > "${STATE5}/cloude.db-wal"
printf 'stale shm\n' > "${STATE5}/cloude.db-shm"
printf 'OLD BACKUP DATA\n' > "${STATE5}/cloude.db.bak-v1-20260101T000000Z"
restore_one_artifact "${INSTALL5}" "cloude.db" \
    "${STATE5}/cloude.db.bak-v1-20260101T000000Z" >/dev/null 2>&1
assert "$?" "restore_one_artifact returns 0"
assert "$([ "$(cat "${STATE5}/cloude.db")" = "OLD BACKUP DATA" ] && echo 0 || echo 1)" \
    "the live artifact now holds the backup's bytes"
assert "$([ ! -f "${STATE5}/cloude.db-wal" ] && [ ! -f "${STATE5}/cloude.db-shm" ] && echo 0 || echo 1)" \
    "the stale -wal and -shm of the OVERWRITTEN database are gone"
SNAP_COUNT="$(find "${STATE5}" -name 'cloude.db.prerestore-*' | wc -l | tr -d ' ')"
assert "$([ "${SNAP_COUNT}" = "1" ] && echo 0 || echo 1)" \
    "the destroyed data was snapshotted first (found ${SNAP_COUNT})"
SNAP="$(find "${STATE5}" -name 'cloude.db.prerestore-*' | head -1)"
assert "$([ "$(cat "${SNAP}")" = "NEW LIVE DATA" ] && echo 0 || echo 1)" \
    "the snapshot holds what was about to be destroyed"

echo ""
echo "=== 7. a backup the trail names but that is not on disk is a refusal ==="
SNAPS_BEFORE="$(find "${STATE5}" -name 'cloude.db.prerestore-*' | wc -l | tr -d ' ')"
MISSING_OUT="$(restore_one_artifact "${INSTALL5}" "cloude.db" "${STATE5}/does-not-exist" 2>&1)"
assert "$([ "$?" -ne 0 ] && echo 0 || echo 1)" \
    "restoring from a missing backup returns non-zero"
case "${MISSING_OUT}" in
    *"MISSING BACKUP"*"does-not-exist"*)
        assert 0 "it names the backup the trail asked for and that is absent" ;;
    *) assert 1 "the refusal must name the missing backup (got: ${MISSING_OUT})" ;;
esac
assert "$([ "$(cat "${STATE5}/cloude.db")" = "OLD BACKUP DATA" ] && echo 0 || echo 1)" \
    "and it changed nothing"
SNAPS_AFTER="$(find "${STATE5}" -name 'cloude.db.prerestore-*' | wc -l | tr -d ' ')"
assert "$([ "${SNAPS_BEFORE}" = "${SNAPS_AFTER}" ] && echo 0 || echo 1)" \
    "it checked the backup BEFORE snapshotting, so no stray snapshot was left"

echo ""
echo "=== 8. rollback.sh refuses on an unreadable trail, touching nothing ==="
INSTALL6="$(mktemp -d)"
mkdir -p "${INSTALL6}/state" "${INSTALL6}/.upgrade-backups/20260101T000000Z_from-0.8.1_to-0.8.2"
printf 'CLOUDE_STATE_DIR=%s\n' "${INSTALL6}/state" > "${INSTALL6}/.env"
printf '{}\n' > "${INSTALL6}/config.json"
printf 'not json\n' > "${INSTALL6}/state/migration_trail.jsonl"
printf 'DO NOT TOUCH\n' > "${INSTALL6}/state/cloude.db"
printf 'a newer backup that must NOT be chosen\n' \
    > "${INSTALL6}/state/cloude.db.bak-v3-20260818T224440Z"
(cd "${INSTALL6}" && git init -q . && git config user.email t@t && \
 git config user.name t && git add -A >/dev/null 2>&1 && \
 git commit -qm init >/dev/null 2>&1)
DB_MD5="$(md5 -q "${INSTALL6}/state/cloude.db")"
HEAD_BEFORE="$(cd "${INSTALL6}" && git rev-parse HEAD)"
ROLL_OUT="$(ASSUME_YES=1 "${REPO_ROOT}/scripts/rollback.sh" 0.8.1 \
    --install-dir "${INSTALL6}" 2>&1)"
ROLL_RC=$?
assert "$([ "${ROLL_RC}" -ne 0 ] && echo 0 || echo 1)" \
    "rollback.sh exits non-zero on an unreadable trail"
assert "$([ "$(md5 -q "${INSTALL6}/state/cloude.db")" = "${DB_MD5}" ] && echo 0 || echo 1)" \
    "cloude.db was not touched"
assert "$([ "$(cd "${INSTALL6}" && git rev-parse HEAD)" = "${HEAD_BEFORE}" ] && echo 0 || echo 1)" \
    "the code was not checked out"
case "${ROLL_OUT}" in
    *"will not pick a backup out of an unreadable history"*)
        assert 0 "it says why, in the operator's terms" ;;
    *) assert 1 "it names the refusal (got: ${ROLL_OUT})" ;;
esac
case "${ROLL_OUT}" in
    *"cloude.db.bak-v3-20260818T224440Z"*)
        assert 1 "it must NEVER name the newest backup as a candidate" ;;
    *) assert 0 "it never reaches for the newest backup" ;;
esac
case "${ROLL_OUT}" in
    *"stopping the server"*)
        assert 1 "it must refuse BEFORE stopping the server" ;;
    *) assert 0 "it refused before stopping the server" ;;
esac

echo ""
echo "=== 9. --code-only prints the mismatch loudly ==="
CODEONLY_OUT="$(warn_code_only "${INSTALL4}" '{"items":[{"kind":"schema","version_at_target":"3"},{"kind":"config","version_at_target":"2"}]}' "0.8.1" 2>&1)"
for needle in "DATA half of this rollback was SKIPPED" \
              "schema was at v3" \
              "config was at v2" \
              "DEGRADED READ-ONLY" \
              "degraded_schema_ahead" \
              "re-run this script without --code-only"; do
    case "${CODEONLY_OUT}" in
        *"${needle}"*) assert 0 "the warning says: ${needle}" ;;
        *) assert 1 "the warning is missing: ${needle}" ;;
    esac
done
UNKNOWN_OUT="$(warn_code_only "${INSTALL4}" '{"error":"nope"}' "0.8.1" 2>&1)"
case "${UNKNOWN_OUT}" in
    *"the size of this mismatch is unknown"*)
        assert 0 "an unresolvable plan still warns, and says the size is unknown" ;;
    *) assert 1 "an unresolvable plan must still warn (got: ${UNKNOWN_OUT})" ;;
esac

echo ""
echo "=== 9b. a backup under the OLD LOG_DIRECTORY location is still found ==="
INSTALL_L="$(mktemp -d)"
mkdir -p "${INSTALL_L}/state" "${INSTALL_L}/oldlogs"
{ printf 'CLOUDE_STATE_DIR=%s\n' "${INSTALL_L}/state"
  printf 'LOG_DIRECTORY=%s\n' "${INSTALL_L}/oldlogs"; } > "${INSTALL_L}/.env"
printf 'LEGACY BACKUP\n' > "${INSTALL_L}/oldlogs/cloude.db.bak-v1-20260101T000000Z"
FOUND="$(locate_backup "${INSTALL_L}" "${INSTALL_L}/state" \
    "cloude.db.bak-v1-20260101T000000Z")"
assert "$([ "${FOUND}" = "${INSTALL_L}/oldlogs/cloude.db.bak-v1-20260101T000000Z" ] && echo 0 || echo 1)" \
    "a backup only in the pre-state-directory location is located there (got: ${FOUND})"
printf 'NEW BACKUP\n' > "${INSTALL_L}/state/cloude.db.bak-v1-20260101T000000Z"
FOUND2="$(locate_backup "${INSTALL_L}" "${INSTALL_L}/state" \
    "cloude.db.bak-v1-20260101T000000Z")"
assert "$([ "${FOUND2}" = "${INSTALL_L}/state/cloude.db.bak-v1-20260101T000000Z" ] && echo 0 || echo 1)" \
    "when it is in both, the current state dir wins (got: ${FOUND2})"
MISSING_LOC="$(locate_backup "${INSTALL_L}" "${INSTALL_L}/state" "nowhere.bak")"
assert "$([ "${MISSING_LOC}" = "${INSTALL_L}/state/nowhere.bak" ] && echo 0 || echo 1)" \
    "a backup in no location resolves to the state dir, for one refusal message"

echo ""
echo "=== 10. an unrecognised selector exit code is a refusal, not success ==="
INSTALL7="$(mktemp -d)"
mkdir -p "${INSTALL7}/state" "${INSTALL7}/.upgrade-backups/20260101T000000Z_from-0.8.1_to-0.8.2"
printf 'CLOUDE_STATE_DIR=%s\n' "${INSTALL7}/state" > "${INSTALL7}/.env"
printf '{}\n' > "${INSTALL7}/config.json"
printf 'DO NOT TOUCH\n' > "${INSTALL7}/state/cloude.db"
(cd "${INSTALL7}" && git init -q . && git config user.email t@t && \
 git config user.name t && git add -A >/dev/null 2>&1 && \
 git commit -qm init >/dev/null 2>&1)
STUB="${INSTALL7}/stub_selector.py"
printf 'import sys\nprint("{}")\nsys.exit(7)\n' > "${STUB}"
DB7_MD5="$(md5 -q "${INSTALL7}/state/cloude.db")"
STUB_OUT="$(ASSUME_YES=1 TRAIL_SELECT="${STUB}" \
    "${REPO_ROOT}/scripts/rollback.sh" 0.8.1 --install-dir "${INSTALL7}" 2>&1)"
assert "$([ "$?" -ne 0 ] && echo 0 || echo 1)" \
    "an exit code the script does not recognise makes it exit non-zero"
assert "$([ "$(md5 -q "${INSTALL7}/state/cloude.db")" = "${DB7_MD5}" ] && echo 0 || echo 1)" \
    "and nothing was touched"
case "${STUB_OUT}" in
    *"not an outcome this script knows how to act on"*)
        assert 0 "it says the code was unrecognised rather than assuming success" ;;
    *) assert 1 "it must name the unrecognised code (got: ${STUB_OUT})" ;;
esac
case "${STUB_OUT}" in
    *"stopping the server"*)
        assert 1 "it must refuse BEFORE stopping the server, not carry on and fail later" ;;
    *) assert 0 "it refused before stopping the server" ;;
esac

echo ""
if [ "${FAILURES}" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
fi
echo "${FAILURES} FAILURE(S)"
exit 1
