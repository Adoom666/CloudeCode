#!/bin/bash
# scripts/upgrade_lib/rollback_data.sh
#
# The DATA half of scripts/rollback.sh, design section 9.8.
#
# THE GAP THIS CLOSES. rollback.sh reverted CODE only. The condition that
# made that safe was explicit and it has expired: with one schema version
# there is no data version to roll back to. The live database is at schema
# 4. Roll code back now and the app meets a newer database, refuses to
# write, and drops to degraded read-only (src/core/db_state.py's
# degraded_schema_ahead). That is the safe failure rather than data loss -
# but a rollback script that produces it silently is half a rollback that
# says nothing about the half it skipped.
#
# RESTORE IS THE DEFAULT AND THE ONLY PATH THIS SCRIPT TAKES. Design 9.5
# gives two ways back: RESTORE (copy the backup taken at that version) and
# REVERSE (apply the step's own recorded reversal). REVERSE keeps rows
# RESTORE would discard, and its correctness depends on a human having
# written a complete reversal for the step. A script running unattended
# must never choose the path whose correctness rests on a hand-maintained
# claim, so REVERSE is not offered here at all - not as a flag, not
# behind a prompt. `--code-only` is the opt-out, and it is loud.
#
# THE REFUSALS ARE THE POINT. An unreadable trail makes this refuse and
# touch nothing - it never falls back to "the newest backup", because a
# rollback tool guessing which backup to overwrite live data with is the
# entire failure section 9.8 exists to close. A resolvable-but-
# unbackable version refuses the same way. Both leave the server running
# and the install untouched, because the plan is computed BEFORE
# rollback.sh stops anything.
#
# Sourced, never executed. Requires upgrade_rollback_common.sh first.

# Exit codes from trail_select.py, restated so the shell reads by name.
TRAIL_SELECT_OK=0
TRAIL_SELECT_REFUSED=2
TRAIL_SELECT_UNREADABLE=3
TRAIL_SELECT_ABSENT=4

# Description: ask trail_select.py which backups a rollback to TARGET must
#   restore. Pure read; nothing is mutated by this call.
# Inputs: $1 - install_dir. $2 - target tag. $3 - path to trail_select.py.
# Output: prints the selector's JSON plan on stdout and returns its exit
#   code: 0 a usable plan, 2 refused with a named reason, 3 the trail is
#   unreadable, 4 there is no trail. Three outcomes plus "no trail at
#   all", never two.
plan_data_rollback() {
    local install_dir="$1" target="$2" selector="$3" python_bin trail
    python_bin="$(resolve_python "${install_dir}")"
    trail="$(trail_file "${install_dir}")"
    "${python_bin}" "${selector}" select --trail "${trail}" --target-code "${target}"
}

# Description: read one top-level or nested field out of the plan JSON.
#   Used instead of grep/sed so a value containing a brace or a quote
#   cannot silently produce a wrong path for an irreversible copy.
# Inputs: $1 - install_dir (resolves the interpreter). $2 - the plan JSON.
#   $3 - a dotted path, e.g. "error" or "items.0.backup_path".
# Output: prints the value ("" when absent or null) and returns 0.
plan_field() {
    local install_dir="$1" plan="$2" path="$3" python_bin
    python_bin="$(resolve_python "${install_dir}")"
    "${python_bin}" - "${plan}" "${path}" <<'PYEOF'
import json
import sys

plan, path = sys.argv[1], sys.argv[2]
try:
    node = json.loads(plan)
except ValueError:
    sys.exit(0)
for part in path.split("."):
    if isinstance(node, list):
        try:
            node = node[int(part)]
        except (ValueError, IndexError):
            sys.exit(0)
    elif isinstance(node, dict):
        if part not in node:
            sys.exit(0)
        node = node[part]
    else:
        sys.exit(0)
if node is None:
    sys.exit(0)
print(node)
PYEOF
}

# Description: list the restores in a plan, one per line, as
#   "<kind>\t<artifact>\t<backup_path>". Only items whose outcome is
#   `restore` appear; an `already_current` item is a real outcome and
#   correctly contributes no copy.
# Inputs: $1 - install_dir. $2 - the plan JSON.
# Output: prints zero or more tab-separated lines; returns 0.
plan_restores() {
    local install_dir="$1" plan="$2" python_bin
    python_bin="$(resolve_python "${install_dir}")"
    "${python_bin}" - "${plan}" <<'PYEOF'
import json
import sys

try:
    plan = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
for item in plan.get("items", []):
    if item.get("outcome") == "restore":
        print("\t".join([item["kind"], item["artifact"], item["backup_path"]]))
PYEOF
}

# Description: report the code-below-schema mismatch a `--code-only`
#   rollback deliberately creates, naming BOTH numbers. This is the
#   printed counterpart of src/core/db_state.py's degraded_schema_ahead
#   refusal: the app will state it at startup, and a rollback that caused
#   it must state it at the moment it chose to.
# Inputs: $1 - install_dir. $2 - the plan JSON (may be empty or a refusal).
#   $3 - target tag.
# Output: prints the warning block to stderr; returns 0 always. Never
#   silent: the whole point of the flag's loudness is that a user who
#   asked for half a rollback is told which half is missing.
warn_code_only() {
    local install_dir="$1" plan="$2" target="$3" schema_at cfg_at
    schema_at="$(plan_field "${install_dir}" "${plan}" "items.0.version_at_target")"
    cfg_at="$(plan_field "${install_dir}" "${plan}" "items.1.version_at_target")"
    echo "" >&2
    echo "==================================================================" >&2
    log_fail "--code-only: the DATA half of this rollback was SKIPPED"
    log_fail "code is being moved to ${target}; cloude.db and config.json are NOT being restored"
    if [ -n "${schema_at}" ]; then
        log_fail "the trail says schema was at v${schema_at} at ${target}; the database on disk is NOT being returned to it"
    else
        log_fail "the trail could not even say which schema version belonged to ${target}, so the size of this mismatch is unknown"
    fi
    if [ -n "${cfg_at}" ]; then
        log_fail "the trail says config was at v${cfg_at} at ${target}; config.json is NOT being returned to it"
    fi
    log_fail "expect the app to refuse writes and run DEGRADED READ-ONLY if the data is at a schema this code does not know."
    log_fail "GET /api/v1/version -> data.status will read degraded_schema_ahead and name both numbers."
    log_fail "to complete the rollback, re-run this script without --code-only."
    echo "==================================================================" >&2
    echo "" >&2
}

# Description: copy one backup over the live artifact, after snapshotting
#   what is about to be destroyed.
#
#   THE SNAPSHOT IS NOT AN UNDO. This script will never read it back; it
#   exists so a human can. The confirmation the operator agreed to says
#   the restore cannot be undone, and that stays true of this script.
#
#   Stale WAL/SHM siblings of the DESTINATION are removed after the copy.
#   A `-wal` left over from the newer database describes pages that no
#   longer exist in the restored file, and SQLite would apply it on the
#   next open. Deleting a live database's `-wal` is destructive; deleting
#   the one belonging to a file that has just been overwritten wholesale
#   is the only correct move, and it is safe here only because
#   rollback.sh stopped the server first.
# Inputs: $1 - install_dir. $2 - artifact filename (cloude.db /
#   config.json). $3 - absolute path of the backup to restore.
# Output: returns 0 on success; returns 1 with a log_fail on any failure.
restore_one_artifact() {
    local install_dir="$1" artifact="$2" backup="$3"
    local dest snapshot stamp
    dest="$(resolve_state_file "${install_dir}" "${artifact}")"
    if [ "${artifact}" = "config.json" ]; then
        dest="${install_dir}/config.json"
    fi
    if [ ! -f "${backup}" ]; then
        log_fail "MISSING BACKUP: the trail names ${backup} as the backup to restore ${artifact} from, and that file is not on disk. Nothing has been changed and no snapshot was taken."
        return 1
    fi
    stamp="$(trail_now)"
    snapshot="${dest}.prerestore-${stamp}"
    if [ -f "${dest}" ]; then
        _copy_backup_file "${dest}" "${snapshot}" || {
            log_fail "could not snapshot the current ${artifact} to ${snapshot} - refusing to overwrite it"
            return 1
        }
        log_ok "current ${artifact} snapshotted to ${snapshot} (this script will never read it back; it is there for you)"
    else
        log_unknown "no current ${artifact} at ${dest} to snapshot - restoring onto nothing"
    fi
    cp -p "${backup}" "${dest}" || {
        log_fail "cp ${backup} -> ${dest} failed; ${artifact} may be partially written. The pre-restore snapshot is at ${snapshot}."
        return 1
    }
    rm -f "${dest}-wal" "${dest}-shm"
    log_ok "restored ${artifact} from $(basename "${backup}")"
}

# Description: turn a trail backup_path into an absolute path on disk.
#
#   Design 9.6 says backup_path is a bare filename relative to the state
#   dir. That is true of a current install and NOT true of one whose data
#   is still under the pre-state-directory LOG_DIRECTORY location, where
#   the backups sit beside the database that produced them. Rather than
#   assume one directory and fail confusingly, this looks in the three
#   places the file can legitimately be, in precedence order, and returns
#   the first that exists.
#
#   When it exists in NONE of them, it returns the state-dir path anyway.
#   That is deliberate: locating a file is not asserting it exists, and
#   restore_one_artifact's own -f check is the single place that refusal
#   is worded. Two functions both refusing would produce two different
#   messages for one condition.
# Inputs: $1 - install_dir. $2 - state_dir. $3 - backup_path from the
#   trail (a bare filename, or an absolute path, which is returned as-is).
# Output: prints an absolute path; returns 0 always.
locate_backup() {
    local install_dir="$1" state_dir="$2" name="$3" legacy
    case "${name}" in
        /*) printf '%s\n' "${name}"; return 0 ;;
    esac
    if [ -f "${state_dir}/${name}" ]; then
        printf '%s\n' "${state_dir}/${name}"
        return 0
    fi
    legacy="$(resolve_legacy_state_dir "${install_dir}")" && \
        if [ -f "${legacy}/${name}" ]; then
            printf '%s\n' "${legacy}/${name}"
            return 0
        fi
    if [ -f "${install_dir}/${name}" ]; then
        printf '%s\n' "${install_dir}/${name}"
        return 0
    fi
    printf '%s\n' "${state_dir}/${name}"
}

# Description: carry out every restore in an approved plan.
# Inputs: $1 - install_dir. $2 - the plan JSON. $3 - the state dir that
#   backup_path values are relative to.
# Output: prints the number of artifacts restored to stdout; returns 0 on
#   success, 1 on the first failure (already logged).
restore_data_half() {
    local install_dir="$1" plan="$2" state_dir="$3" count=0
    local line kind artifact backup_name backup_path
    while IFS=$'\t' read -r kind artifact backup_name; do
        [ -n "${kind}" ] || continue
        backup_path="$(locate_backup "${install_dir}" "${state_dir}" "${backup_name}")"
        restore_one_artifact "${install_dir}" "${artifact}" "${backup_path}" || return 1
        count=$((count + 1))
    done < <(plan_restores "${install_dir}" "${plan}")
    printf '%s\n' "${count}"
}
