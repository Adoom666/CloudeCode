#!/bin/bash
# scripts/upgrade.sh — move a from-source CloudeCode install to a release tag.
#
# Usage:
#   ./scripts/upgrade.sh [TAG] [--yes] [--remote URL] [--install-dir DIR]
#
#   TAG            release tag to upgrade to, e.g. 0.9.0 or v0.9.0. Defaults
#                  to the newest published release tag on the remote.
#   --yes / -y     skip the confirmation prompt (for the menu bar app or CI).
#                  Without it, a non-interactive shell refuses outright
#                  rather than silently assuming yes.
#   --remote URL   override which git remote to read release tags from.
#                  Defaults to config.json's `updates.remote`, else the
#                  checkout's own `origin`, else the public upstream — the
#                  exact precedence src/core/update_check.py already uses.
#   --install-dir DIR   the CloudeCode checkout to upgrade. Defaults to the
#                  repository this script lives in. Exists so this script
#                  can be rehearsed against a throwaway copy without editing
#                  it — see README "Verifying this script" section.
#
# THIS SCRIPT ONLY MANAGES A FROM-SOURCE INSTALL (README "Path B"): a git
# checkout with its own venv/, .env, config.json, running via ./start.sh /
# ./stop.sh / ./reset.sh on port 8000. The packaged .app has no in-place
# upgrader — see the README's "Upgrading the packaged .app" section — but a
# packaged install's server payload IS this same source tree underneath
# (macOS/bootstrap.js rsyncs it from the bundle on every launch), so the code
# and config-migration story below is identical either way.
#
# What this script does, in order:
#   1. Resolve the CURRENT version. Refuses to continue if it cannot be
#      determined — see "could not evaluate" in the README.
#   2. Resolve the TARGET tag (given or latest) and confirm it is a real
#      published release tag.
#   3. If current == target: report and exit 0. Idempotent — no stop, no
#      restart, no backup on a no-op run.
#   4. Compute where the backup will land (deterministic; no copying yet).
#   5. State the plan and require confirmation (unless --yes).
#   6. Stop the server.
#   6a. Take the backup of user state (.env, config.json, session/auth
#      state, including refresh_tokens.db) into the directory from step 4,
#      now that the server is stopped, and print that path prominently.
#      SQLite files are copied via VACUUM INTO + integrity_check, not a
#      raw file copy - see upgrade_rollback_common.sh's SQLite-safe-copying
#      note. Still runs before anything destructive to the CODE.
#   7. `git fetch --tags` then `git checkout tags/<target>` (detached HEAD).
#   8. `pip install -r requirements.txt` for the new tag's dependencies.
#   9. Run the config migration (src/core/config_migration.py) — additive,
#      idempotent, takes its OWN config.json.bak on top of the backup from
#      step 6a.
#  10. Restart the server.
#  11. Verify: the server answers on /health AND reports the target version.
#      A failure here is a FAILURE, not a warning — see verify_upgrade in
#      scripts/upgrade_lib/upgrade_rollback_common.sh.
#
# Exit codes: 0 success (including the idempotent no-op case). 1 on any
# failure or refusal, with a [FAIL] or [UNKNOWN] line explaining which.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PROBE="${SCRIPT_DIR}/upgrade_lib/version_probe.py"

# shellcheck source=upgrade_lib/upgrade_rollback_common.sh
source "${SCRIPT_DIR}/upgrade_lib/upgrade_rollback_common.sh"
# shellcheck source=upgrade_lib/trail_code_entry.sh
source "${SCRIPT_DIR}/upgrade_lib/trail_code_entry.sh"

TAG=""
ASSUME_YES="${ASSUME_YES:-0}"
REMOTE_OVERRIDE=""
INSTALL_DIR="${DEFAULT_INSTALL_DIR}"

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=1; shift ;;
        --remote) REMOTE_OVERRIDE="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown flag: $1 (see --help)" ;;
        *)
            if [ -n "${TAG}" ]; then
                die "unexpected extra argument: $1 (tag already given as ${TAG})"
            fi
            TAG="$1"
            shift
            ;;
    esac
done

log_step "install directory: ${INSTALL_DIR}"
require_git_root "${INSTALL_DIR}"

# --- 1. current version, refuse if unknown ----------------------------------

CURRENT="$(current_version "${INSTALL_DIR}" "${PROBE}")"
CURRENT_RC=$?
if [ "${CURRENT_RC}" -ne 0 ] || [ -z "${CURRENT}" ]; then
    log_unknown "could not determine the CURRENT installed version of ${INSTALL_DIR}"
    log_unknown "refusing to upgrade blind — fix version resolution first (see src/core/version.py's resolution order in the README), or if this is a fresh unconfigured checkout, there is nothing installed to upgrade"
    exit 1
fi
log_ok "current version: ${CURRENT}"

# --- 2. resolve remote + target tag -----------------------------------------

REMOTE="${REMOTE_OVERRIDE:-$(resolve_remote "${INSTALL_DIR}" "${PROBE}")}"
log_step "release remote: ${REMOTE}"

PYTHON_BIN="$(resolve_python "${INSTALL_DIR}")"

if [ -z "${TAG}" ]; then
    TAG="$("${PYTHON_BIN}" "${PROBE}" latest --install-dir "${INSTALL_DIR}" --remote "${REMOTE}")"
    LATEST_RC=$?
    case "${LATEST_RC}" in
        0) log_ok "no tag given — latest release tag is ${TAG}" ;;
        2) log_fail "${REMOTE} published no release tags — nothing to upgrade to"; exit 1 ;;
        *) log_unknown "could not reach ${REMOTE} to find the latest release tag — check network/git, or pass a tag explicitly"; exit 1 ;;
    esac
else
    "${PYTHON_BIN}" "${PROBE}" check-tag --install-dir "${INSTALL_DIR}" --remote "${REMOTE}" "${TAG}"
    CHECK_RC=$?
    case "${CHECK_RC}" in
        0) log_ok "${TAG} is a published release tag on ${REMOTE}" ;;
        2) log_fail "${TAG} is not a published release tag on ${REMOTE} — see the list above"; exit 1 ;;
        *) log_unknown "could not reach ${REMOTE} to verify ${TAG} exists — check network/git before retrying"; exit 1 ;;
    esac
fi

# --- 3. idempotent no-op ------------------------------------------------------

if [ "${CURRENT}" = "${TAG}" ]; then
    log_ok "already at ${TAG} — nothing to do (idempotent no-op, no backup, no restart)"
    exit 0
fi

# --- 4. compute the backup destination (deterministic, no copying yet) ------
#
# BACKUP_DIR's path is fully determined right here from TIMESTAMP/CURRENT/
# TAG - it does not depend on take_backup having run. That means it can be
# shown to the operator in the plan below BEFORE the backup is actually
# taken, so moving the actual copy to step 6a (after the server is
# stopped, see the note there) costs nothing in the confirmation UX: the
# path printed at confirm time is the exact path the backup lands at.

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${INSTALL_DIR}/.upgrade-backups"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}_from-${CURRENT}_to-${TAG}"
mkdir -p "${BACKUP_ROOT}"

# --- 5. state the plan, confirm ----------------------------------------------

echo "About to upgrade CloudeCode in:"
echo "  ${INSTALL_DIR}"
echo "From version:  ${CURRENT}"
echo "To version:    ${TAG}"
echo "Backup will be written to (after the server is stopped): ${BACKUP_DIR}"
echo "Steps: stop server -> back up user state -> git checkout tags/${TAG} -> pip install -> config migration -> restart -> verify"
echo ""
confirm_or_die "Proceed with this upgrade?"

require_clean_tree "${INSTALL_DIR}"

# --- 5b. open the code trail entry, two-phase --------------------------------
#
# design 9.3, and the reason the trail is UNIFIED rather than two systems
# in one directory: the app writes the schema and config entries, and
# until now nothing wrote the code entries, so reading the trail showed
# data moves with no code moves to interleave them against.
# scripts/upgrade_lib/trail_select.py cannot answer "which data version
# was in force at release X" without this line existing.
#
# Appended and fsynced BEFORE anything is mutated. A process killed after
# this point leaves an unclosed `started` line, which the app detects at
# startup and closes as `interrupted`.

TRAIL_OPENED=0
TRAIL_UUID=""
TRAIL_STARTED=""
TRAIL_HANDLE="$(trail_code_open "${INSTALL_DIR}" "${CURRENT}" "${TAG}" \
    "${CURRENT}" "upgrade.sh: backup at ${BACKUP_DIR}")"
if [ -n "${TRAIL_HANDLE}" ]; then
    TRAIL_UUID="${TRAIL_HANDLE%% *}"
    TRAIL_STARTED="${TRAIL_HANDLE#* }"
    TRAIL_OPENED=1
    log_ok "migration trail: opened code entry ${TRAIL_UUID}"
else
    log_unknown "could not open a code entry in the migration trail. Proceeding with the upgrade; the trail will not record this code move, which means a later rollback to ${CURRENT} cannot anchor to it and will refuse rather than guess."
fi

# Description: close the code trail entry, if one was opened.
# Inputs: $1 - status (completed|failed). $2 - error text, empty when
#   completed.
# Output: returns 0 always; a trail write failure is logged by
#   trail_code_close and never masks the upgrade's own outcome.
close_trail() {
    [ "${TRAIL_OPENED}" = "1" ] || return 0
    trail_code_close "${INSTALL_DIR}" "${TRAIL_UUID}" "${TRAIL_STARTED}" \
        "${CURRENT}" "${TAG}" "$1" "${TAG}" "${2:-}"
    return 0
}

# --- 6. stop --------------------------------------------------------------

log_step "stopping the server"
stop_service "${INSTALL_DIR}"

# --- 6a. backup, now that the server is stopped ------------------------------
#
# Moved here from before confirm/stop (see prior revision) because
# take_backup copies refresh_tokens.db, a live WAL-mode SQLite database
# that RefreshStore holds open with an active purge loop while the server
# runs - a plain file copy against that is not a safe read, and the same
# will be true of the cloude.db datastore once it exists (see
# upgrade_rollback_common.sh's SQLite-safe-copying note; take_backup now
# routes every SQLite file through VACUUM INTO instead of cp regardless of
# when it is called, but a quiesced writer is still simpler and faster to
# reason about, so we call it here rather than relying on that alone).
# Still runs BEFORE git checkout / pip install / config migration - the
# ordering promise that matters ("nothing destructive to the CODE happens
# before a backup exists") is unchanged, only the ordering relative to
# stop_service moved.
BACKUP_PATH="$(take_backup "${INSTALL_DIR}" "${BACKUP_DIR}")" || exit 1
log_ok "backup written to: ${BACKUP_PATH}"
echo ""
echo "=================================================================="
echo " BACKUP LOCATION (needed for rollback):"
echo "   ${BACKUP_PATH}"
echo "=================================================================="
echo ""

# --- 7. checkout the tag ------------------------------------------------------

log_step "git fetch --tags"
(cd "${INSTALL_DIR}" && git fetch --tags "${REMOTE}") || die "git fetch --tags ${REMOTE} failed"

log_step "git checkout tags/${TAG}"
(cd "${INSTALL_DIR}" && git checkout --detach "tags/${TAG}") || die "git checkout tags/${TAG} failed — install left on its previous commit, server is stopped. Restore with: git checkout ${CURRENT} (or the branch you were on) and re-run ./reset.sh"

# --- 8. dependencies -----------------------------------------------------------

if [ -x "${INSTALL_DIR}/venv/bin/pip" ]; then
    log_step "installing dependencies for ${TAG}"
    (cd "${INSTALL_DIR}" && ./venv/bin/pip install -q -r requirements.txt) || die "pip install -r requirements.txt failed after checking out ${TAG} — server is stopped and code is on the new tag but deps are not installed. Fix the pip failure and re-run: ./venv/bin/pip install -r requirements.txt, then ./reset.sh"
else
    log_unknown "no venv at ${INSTALL_DIR}/venv/bin/pip — skipping dependency install. If this install normally has a venv, something is wrong; if this is a fresh checkout that has never been set up, that is expected."
fi

# --- 9. config migration --------------------------------------------------------

if [ -f "${INSTALL_DIR}/config.json" ]; then
    log_step "running config migration"
    MIGRATE_OUT="$("${PYTHON_BIN}" -c "
import sys, json
sys.path.insert(0, '${INSTALL_DIR}')
from pathlib import Path
from src.core.config_migration import migrate_config_file
try:
    result = migrate_config_file(Path('${INSTALL_DIR}/config.json'))
except Exception as e:
    print(json.dumps({'error': str(e)}))
    sys.exit(1)
print(json.dumps(result))
")"
    MIGRATE_RC=$?
    if [ "${MIGRATE_RC}" -ne 0 ]; then
        die "config migration failed: ${MIGRATE_OUT} — restore from ${BACKUP_PATH} and investigate before retrying"
    fi
    log_ok "config migration: ${MIGRATE_OUT}"
else
    log_unknown "no config.json in ${INSTALL_DIR} — skipping migration (fresh/never-set-up install)"
fi

# --- 10. restart -----------------------------------------------------------------

log_step "restarting the server"
start_service "${INSTALL_DIR}"

# --- 11. verify --------------------------------------------------------------------

PORT="$(resolve_port "${INSTALL_DIR}")" || die "could not determine port for ${INSTALL_DIR} - see reason above. Fix PORT= in ${INSTALL_DIR}/.env and re-run verification, or verify manually."
log_step "verifying the server on port ${PORT} reports ${TAG}"
verify_upgrade "${INSTALL_DIR}" "${PORT}" "${TAG}" "${PROBE}"
VERIFY_RC=$?
case "${VERIFY_RC}" in
    0)
        close_trail completed ""
        log_ok "upgrade to ${TAG} complete and verified"
        exit 0
        ;;
    2)
        close_trail failed "server did not confirm ${TAG} after the upgrade"
        log_fail "upgrade to ${TAG} FAILED verification — code and config were changed, server did not confirm the new version"
        log_fail "roll back with: ./scripts/rollback.sh ${CURRENT} --install-dir ${INSTALL_DIR}"
        exit 1
        ;;
    *)
        close_trail failed "upgrade completed but could not be verified"
        log_unknown "upgrade to ${TAG} completed but could NOT be verified (see reason above) — do not assume success. Check manually: curl http://127.0.0.1:${PORT}/health"
        exit 1
        ;;
esac
