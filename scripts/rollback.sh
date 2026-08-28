#!/bin/bash
# scripts/rollback.sh — return a from-source CloudeCode install to a
# previous release tag, restoring the matching backup instead of just
# checking out older code.
#
# WHY IT RESTORES A BACKUP RATHER THAN JUST GIT-CHECKOUT: config
# migrations (src/core/config_migration.py) are versioned and FORWARD-ONLY
# — additive, never destructive going up, but nothing downgrades
# config_version going back down. Older code checked out against a
# newer-shaped config.json is running against a file it never wrote and
# was never tested against. The only config.json known to be correct for
# an older tag is the one that was actually running under that tag, which
# is exactly what scripts/upgrade.sh's backup step captured right before
# it moved away. This script finds and restores THAT backup.
#
# Usage:
#   ./scripts/rollback.sh TARGET_TAG [--yes] [--install-dir DIR] [--backup-dir DIR]
#
#   TARGET_TAG       the version to roll back to, e.g. 0.8.1 or v0.8.1.
#                     Required — there is no "latest previous" guess.
#   --yes / -y        skip the confirmation prompt.
#   --install-dir DIR the CloudeCode checkout to roll back. Defaults to the
#                     repository this script lives in.
#   --code-only       roll back CODE ONLY. Skips the data restore and
#                     prints the resulting code/schema mismatch loudly.
#                     The opt-out, never the default.
#   --backup-dir DIR   use this exact backup directory instead of searching
#                     .upgrade-backups for one matching TARGET_TAG. Mainly
#                     for rehearsal and for restoring a backup that was
#                     copied in from elsewhere.
#
# REFUSES OUTRIGHT if no matching backup can be found — a rollback with no
# backup would just be a slower, worse git-checkout, and would silently
# leave a newer-shaped config.json under older code. See the README's
# "when rollback refuses" section before reaching for `git checkout`
# directly.
#
# What this script does, in order:
#   1. Resolve the current version (best-effort — a broken install after a
#      failed upgrade is exactly when this script gets used, so an
#      unresolvable current version is logged but does not block).
#   2. Find the backup taken right before this install last left
#      TARGET_TAG (directory name pattern *_from-<TARGET_TAG>_to-*), or
#      use --backup-dir. Refuse if none exists.
#   3. State the plan and confirm (unless --yes).
#   4. Stop the server.
#   5. `git fetch --tags` then `git checkout tags/<TARGET_TAG>` (detached).
#   6. `pip install -r requirements.txt` for that tag's dependencies.
#   7. Restore every file in the backup over the current one. Does NOT run
#      config migration — the restored config already matches this code.
#   8. Restart the server.
#   9. Verify: server answers on /health AND reports TARGET_TAG. A failure
#      here is a FAILURE, not a warning.
#
# Exit codes: 0 success. 1 on any failure or refusal.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PROBE="${SCRIPT_DIR}/upgrade_lib/version_probe.py"

# shellcheck source=upgrade_lib/upgrade_rollback_common.sh
source "${SCRIPT_DIR}/upgrade_lib/upgrade_rollback_common.sh"
# shellcheck source=upgrade_lib/trail_code_entry.sh
source "${SCRIPT_DIR}/upgrade_lib/trail_code_entry.sh"
# shellcheck source=upgrade_lib/rollback_data.sh
source "${SCRIPT_DIR}/upgrade_lib/rollback_data.sh"
# Overridable only so tests can substitute a stub that returns an exit
# code the real selector never produces, proving the unknown-code branch
# refuses rather than falling through to success.
TRAIL_SELECT="${TRAIL_SELECT:-${SCRIPT_DIR}/upgrade_lib/trail_select.py}"

TARGET_TAG=""
ASSUME_YES="${ASSUME_YES:-0}"
INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
BACKUP_DIR_OVERRIDE=""
CODE_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=1; shift ;;
        --code-only) CODE_ONLY=1; shift ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR_OVERRIDE="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown flag: $1 (see --help)" ;;
        *)
            if [ -n "${TARGET_TAG}" ]; then
                die "unexpected extra argument: $1 (target tag already given as ${TARGET_TAG})"
            fi
            TARGET_TAG="$1"
            shift
            ;;
    esac
done

if [ -z "${TARGET_TAG}" ]; then
    die "TARGET_TAG is required — which version do you want to roll back to? (see --help)"
fi

log_step "install directory: ${INSTALL_DIR}"
require_git_root "${INSTALL_DIR}"

# --- 1. current version, best-effort (does not block a rollback) -----------

CURRENT="$(current_version "${INSTALL_DIR}" "${PROBE}")"
if [ -z "${CURRENT}" ]; then
    log_unknown "could not determine the current installed version — proceeding anyway, since a broken current install is exactly when rollback is used. Target is explicit: ${TARGET_TAG}"
    CURRENT="(unknown)"
else
    log_ok "current version: ${CURRENT}"
fi

# --- 2. find the matching backup --------------------------------------------

BACKUP_ROOT="${INSTALL_DIR}/.upgrade-backups"

if [ -n "${BACKUP_DIR_OVERRIDE}" ]; then
    BACKUP_DIR="${BACKUP_DIR_OVERRIDE}"
    if [ ! -d "${BACKUP_DIR}" ]; then
        die "--backup-dir ${BACKUP_DIR} does not exist"
    fi
else
    if [ ! -d "${BACKUP_ROOT}" ]; then
        die "no backup directory ${BACKUP_ROOT} at all — cannot roll back to ${TARGET_TAG} blind. If a backup exists somewhere else, pass --backup-dir."
    fi
    # Directory names: <timestamp>_from-<from>_to-<to>. A rollback TO
    # <TARGET_TAG> needs the backup taken while <TARGET_TAG> was running,
    # i.e. the one recorded as "_from-<TARGET_TAG>_".
    MATCHES=()
    while IFS= read -r -d '' d; do
        MATCHES+=("${d}")
    done < <(find "${BACKUP_ROOT}" -maxdepth 1 -type d -name "*_from-${TARGET_TAG}_to-*" -print0 2>/dev/null | sort -z)

    if [ "${#MATCHES[@]}" -eq 0 ]; then
        log_fail "no backup found for version ${TARGET_TAG} in ${BACKUP_ROOT}"
        # basename in a loop, not `find -printf` - macOS ships BSD find,
        # which does not have -printf (GNU-only). Verified the hard way:
        # this exact line produced "find: -printf: unknown primary or
        # operator" on real BSD find while a Homebrew-shadowed `find`
        # earlier in PATH silently made it look fine in one shell. See
        # CLAUDE.md hazard: "QNAP/busybox and macOS lack GNU flags."
        AVAILABLE_LIST=()
        while IFS= read -r -d '' d; do
            AVAILABLE_LIST+=("$(basename "${d}")")
        done < <(find "${BACKUP_ROOT}" -maxdepth 1 -type d -name '*_from-*_to-*' -print0 2>/dev/null)
        if [ "${#AVAILABLE_LIST[@]}" -gt 0 ]; then
            log_fail "available backups:"
            printf '%s\n' "${AVAILABLE_LIST[@]}" >&2
        else
            log_fail "no backups exist at all in ${BACKUP_ROOT}"
        fi
        die "refusing to roll back to ${TARGET_TAG} without its matching backup — restoring config from thin air is worse than not rolling back. Pass --backup-dir if you have one from elsewhere."
    fi

    # Most recent match (directory names sort lexically by timestamp).
    BACKUP_DIR="${MATCHES[${#MATCHES[@]}-1]}"
    if [ "${#MATCHES[@]}" -gt 1 ]; then
        log_unknown "more than one backup matches ${TARGET_TAG} — using the most recent: $(basename "${BACKUP_DIR}"). Others found: $(printf '%s ' "${MATCHES[@]}" | tr '\n' ' ')"
    fi
fi

log_ok "using backup: ${BACKUP_DIR}"

# --- 2b. the DATA half: which backups belong to TARGET_TAG -------------------
#
# Computed BEFORE anything is stopped or changed, so every refusal below
# leaves the server running and the install untouched. This is the step
# that was deferred while CURRENT_SCHEMA_VERSION was 1: with one schema
# version there was no data version to roll back to. There is now.

STATE_DIR="$(resolve_state_dir "${INSTALL_DIR}")"
DATA_PLAN=""
DATA_PLAN_RC=0
if [ "${CODE_ONLY}" = "1" ]; then
    DATA_PLAN="$(plan_data_rollback "${INSTALL_DIR}" "${TARGET_TAG}" "${TRAIL_SELECT}" 2>/dev/null)"
    DATA_PLAN_RC=$?
    warn_code_only "${INSTALL_DIR}" "${DATA_PLAN}" "${TARGET_TAG}"
else
    DATA_PLAN="$(plan_data_rollback "${INSTALL_DIR}" "${TARGET_TAG}" "${TRAIL_SELECT}")"
    DATA_PLAN_RC=$?
    case "${DATA_PLAN_RC}" in
        "${TRAIL_SELECT_UNREADABLE}")
            log_fail "the upgrade trail could not be read: $(plan_field "${INSTALL_DIR}" "${DATA_PLAN}" error)"
            die "REFUSING to roll back. Nothing has been stopped, checked out or copied. This script will not pick a backup out of an unreadable history, and it will NOT fall back to the newest one: a rollback that guesses which backup to write over your live database is worse than no rollback. Repair or move $(trail_file "${INSTALL_DIR}") and re-run, or pass --code-only to move code alone and accept the mismatch."
            ;;
        "${TRAIL_SELECT_ABSENT}")
            DB_PATH="$(resolve_state_file "${INSTALL_DIR}" "cloude.db")"
            if [ -f "${DB_PATH}" ]; then
                die "there is a database at ${DB_PATH} but no migration trail beside it, so nothing records which schema version belongs to ${TARGET_TAG}. REFUSING to roll back data on a guess. Nothing has been changed. Pass --code-only to move code alone and accept the mismatch."
            fi
            log_unknown "no migration trail and no cloude.db: this install predates the datastore, so there is no data half to roll back. Continuing with the code half only."
            ;;
        "${TRAIL_SELECT_REFUSED}")
            log_fail "$(plan_field "${INSTALL_DIR}" "${DATA_PLAN}" error)"
            die "REFUSING to roll back. Nothing has been stopped, checked out or copied. Pass --code-only to move code alone and accept the mismatch, after reading the reason above."
            ;;
        "${TRAIL_SELECT_OK}")
            log_ok "data rollback plan resolved from $(trail_file "${INSTALL_DIR}")"
            ;;
        *)
            die "the trail selector exited ${DATA_PLAN_RC}, which is not an outcome this script knows how to act on. REFUSING rather than treating an unrecognised code as success. Nothing has been changed."
            ;;
    esac
fi

# --- 3. state the plan, confirm ----------------------------------------------

echo "About to roll back CloudeCode in:"
echo "  ${INSTALL_DIR}"
echo "From version:  ${CURRENT}"
echo "To version:    ${TARGET_TAG}"
echo "Restoring:     ${BACKUP_DIR}"
# The step list must match what actually happens. A plan that says
# "restore backup" while the script is also about to overwrite cloude.db
# is a confirmation nobody really gave.
if [ "${CODE_ONLY}" = "1" ]; then
    echo "Data:          NOT restored (--code-only)"
    echo "Steps: stop server -> git checkout tags/${TARGET_TAG} -> pip install -> restore install files -> restart -> verify"
elif [ "${DATA_PLAN_RC}" = "${TRAIL_SELECT_OK}" ]; then
    echo "Steps: stop server -> git checkout tags/${TARGET_TAG} -> pip install -> restore install files -> RESTORE DATA -> restart -> verify"
else
    echo "Data:          nothing to restore (see above)"
    echo "Steps: stop server -> git checkout tags/${TARGET_TAG} -> pip install -> restore install files -> restart -> verify"
fi
echo ""

# The DATA confirmation is generated FROM the trail entry, never from a
# static string, so the filename and the timestamp shown are always the
# real ones for this specific jump. Two different steps produce two
# different prompts; that difference is what proves the text was read
# rather than written.
if [ "${DATA_PLAN_RC}" = "${TRAIL_SELECT_OK}" ] && [ "${CODE_ONLY}" != "1" ]; then
    plan_field "${INSTALL_DIR}" "${DATA_PLAN}" confirmation
    echo ""
fi
confirm_or_die "Proceed with this rollback?"

require_clean_tree "${INSTALL_DIR}"

# --- 3b. open the code trail entry, two-phase --------------------------------
#
# Appended and fsynced BEFORE the first mutation. A process killed from
# here on leaves an unclosed `started` line, which the app detects at
# startup and closes as `interrupted`. The converse ordering would allow a
# `completed` line describing a rollback that did not happen.

TRAIL_OPENED=0
TRAIL_UUID=""
TRAIL_STARTED=""
TRAIL_HANDLE="$(trail_code_open "${INSTALL_DIR}" "${CURRENT}" "${TARGET_TAG}" \
    "${CURRENT}" "rollback.sh: restoring ${BACKUP_DIR}")"
if [ -n "${TRAIL_HANDLE}" ]; then
    TRAIL_UUID="${TRAIL_HANDLE%% *}"
    TRAIL_STARTED="${TRAIL_HANDLE#* }"
    TRAIL_OPENED=1
    log_ok "migration trail: opened code entry ${TRAIL_UUID}"
else
    log_unknown "could not open a code entry in the migration trail. Proceeding with the rollback; the trail will not record this code move, which means a LATER rollback cannot anchor to it. Fix the state directory before the next upgrade."
fi

# Description: close the code trail entry, if one was opened.
# Inputs: $1 - status (completed|failed). $2 - error text, empty when
#   completed.
# Output: returns 0 always; a trail write failure is logged by
#   trail_code_close and never masks the rollback's own outcome.
close_trail() {
    [ "${TRAIL_OPENED}" = "1" ] || return 0
    trail_code_close "${INSTALL_DIR}" "${TRAIL_UUID}" "${TRAIL_STARTED}" \
        "${CURRENT}" "${TARGET_TAG}" "$1" "${TARGET_TAG}" "${2:-}"
    return 0
}

# --- 4. stop ----------------------------------------------------------------

log_step "stopping the server"
stop_service "${INSTALL_DIR}"

# --- 5. checkout the target tag ----------------------------------------------

REMOTE="$(resolve_remote "${INSTALL_DIR}" "${PROBE}")"
log_step "git fetch --tags ${REMOTE}"
(cd "${INSTALL_DIR}" && git fetch --tags "${REMOTE}") || die "git fetch --tags ${REMOTE} failed — if ${TARGET_TAG} was already fetched previously this may still work, but proceed with caution"

log_step "git checkout tags/${TARGET_TAG}"
(cd "${INSTALL_DIR}" && git checkout --detach "tags/${TARGET_TAG}") || die "git checkout tags/${TARGET_TAG} failed — server is stopped, code left on its previous commit. Nothing else was touched yet."

# --- 6. dependencies ------------------------------------------------------------

if [ -x "${INSTALL_DIR}/venv/bin/pip" ]; then
    log_step "installing dependencies for ${TARGET_TAG}"
    (cd "${INSTALL_DIR}" && ./venv/bin/pip install -q -r requirements.txt) || die "pip install -r requirements.txt failed after checking out ${TARGET_TAG} — code is on the old tag, config not yet restored, server stopped. Fix pip and re-run this script (it is idempotent)."
else
    log_unknown "no venv at ${INSTALL_DIR}/venv/bin/pip — skipping dependency install"
fi

# --- 7. restore the backup ----------------------------------------------------
#
# No ordering fix needed here (checked as part of the SQLite backup-
# ordering fix in upgrade_rollback_common.sh): stop_service already ran
# at step 4, above, before this. restore_backup copies from a static
# backup directory into an install_dir whose server is stopped, so
# neither side of that copy has a live writer - see restore_backup's
# docstring for why it correctly still uses plain cp -p rather than the
# VACUUM INTO path take_backup uses.

log_step "restoring user state from ${BACKUP_DIR}"
RESTORED="$(restore_backup "${INSTALL_DIR}" "${BACKUP_DIR}")" || exit 1
log_ok "restored ${RESTORED} file(s) from backup — config migration NOT re-run, this config already matches ${TARGET_TAG}"

# --- 7b. restore the DATA half -----------------------------------------------
#
# After the install files and before the restart, so the server never
# starts against a half-restored state directory. Safe to copy here for
# the same reason step 7 is: stop_service ran at step 4.

if [ "${CODE_ONLY}" = "1" ]; then
    log_unknown "--code-only: skipping the data restore (see the warning above)"
elif [ "${DATA_PLAN_RC}" = "${TRAIL_SELECT_OK}" ]; then
    log_step "restoring the data half from the migration trail"
    DATA_RESTORED="$(restore_data_half "${INSTALL_DIR}" "${DATA_PLAN}" "${STATE_DIR}")"
    if [ $? -ne 0 ]; then
        close_trail failed "data restore failed"
        die "the data restore FAILED after the code was already moved to ${TARGET_TAG}. The server is stopped. Read the pre-restore snapshot paths above before doing anything else."
    fi
    log_ok "restored ${DATA_RESTORED} data artifact(s) to the versions the trail records for ${TARGET_TAG}"
else
    log_unknown "no data half to restore (see step 2b)"
fi

# --- 8. restart --------------------------------------------------------------------

log_step "restarting the server"
start_service "${INSTALL_DIR}"

# --- 9. verify ---------------------------------------------------------------------

PORT="$(resolve_port "${INSTALL_DIR}")" || die "could not determine port for ${INSTALL_DIR} - see reason above. Fix PORT= in ${INSTALL_DIR}/.env and re-run verification, or verify manually."
log_step "verifying the server on port ${PORT} reports ${TARGET_TAG}"
verify_upgrade "${INSTALL_DIR}" "${PORT}" "${TARGET_TAG}" "${PROBE}"
VERIFY_RC=$?
case "${VERIFY_RC}" in
    0)
        close_trail completed ""
        log_ok "rollback to ${TARGET_TAG} complete and verified"
        exit 0
        ;;
    2)
        close_trail failed "server did not confirm ${TARGET_TAG} after the rollback"
        log_fail "rollback to ${TARGET_TAG} FAILED verification — code and config were restored, server did not confirm the version"
        log_fail "this install may now be in a broken state. Check ${INSTALL_DIR}/logs, and consider re-running this script (it is idempotent) or restoring ${BACKUP_DIR} by hand."
        exit 1
        ;;
    *)
        close_trail failed "rollback completed but could not be verified"
        log_unknown "rollback to ${TARGET_TAG} completed but could NOT be verified (see reason above) — do not assume success. Check manually: curl http://127.0.0.1:${PORT}/health"
        exit 1
        ;;
esac
