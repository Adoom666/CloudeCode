#!/bin/bash
# scripts/upgrade_lib/trail_code_entry.sh
#
# The shell side of the unified migration trail: appending kind='code'
# entries from scripts/upgrade.sh and scripts/rollback.sh, two-phase,
# exactly as design section 9.3 specifies.
#
# WHY THIS EXISTS AT ALL. The trail is unified only if CODE moves and DATA
# moves land in the same file, interleaved by real time. The app writes
# the schema and config entries; nothing writes the code entries. The
# reader (src/core/trail_reader.py) and the rollback selector
# (trail_select.py) both already expect them - trail_select.py cannot
# answer "which data version was in force at release X" without a code
# entry to anchor the question to. Until this file existed, a rollback
# could only ever answer that question with a guess.
#
# TWO-PHASE, AND THE ORDER IS THE MECHANISM. `trail_code_open` appends a
# `started` line and fsyncs it BEFORE anything is mutated;
# `trail_code_close` appends `completed` or `failed` AFTER. A process
# killed in between leaves an unclosed `started` line, which is exactly
# what the app's startup detects and closes as `interrupted` - detected
# from a line on disk, never inferred from a missing artifact. The
# converse ordering would allow a `completed` line describing a change
# that did not happen, which is the one shape of lie the trail must never
# carry.
#
# TIMESTAMP RESOLUTION IS ONE SECOND, DELIBERATELY. The app writes
# microseconds; BSD `date` has no %N and this script must run on macOS
# (CLAUDE.md: "macOS/BSD lacks GNU flags"). One-second resolution is
# sufficient because ordering only has to separate a code move from a
# data move, and those are separated by a `git checkout` and a
# `pip install`. src/core/trail_reader.py's header records the matching
# decision on the reading side: it deliberately does NOT treat equal or
# out-of-order timestamps as corruption, precisely so a one-second bash
# writer cannot pause a healthy install's migrations.
#
# THE FSYNC IS DONE THROUGH PYTHON, THE FORMAT IS OWNED BY BASH. Section
# 9.9 requires an explicit fsync per line; bash has no fsync. The JSON
# line is built here, in shell, with no jq; the interpreter that
# upgrade_rollback_common.sh already resolves for `resolve_state_dir` is
# used only to perform the append-and-fsync syscall pair. No new
# dependency, and in particular no SQLite client, which is the constraint
# section 9.2 actually cared about.
#
# Sourced, never executed. Requires upgrade_rollback_common.sh to have
# been sourced first (uses resolve_python, resolve_state_file, log_*).

# Description: locate migration_trail.jsonl for an install, using the same
#   resolution the app itself uses, including the pre-state-directory
#   fallback. Delegates to resolve_state_file so this file cannot drift
#   from the resolution the backup code performs.
# Inputs: $1 - install_dir.
# Output: prints the absolute path the app would read the trail from, and
#   returns 0. The path is where a first writer WOULD create the file; the
#   caller must still test -f. Locating is not asserting existence.
trail_file() {
    resolve_state_file "$1" "migration_trail.jsonl"
}

# Description: mint a fresh entry_uuid. Never reused; it is the join key
#   between the JSONL file and the mirror table inside cloude.db.
# Inputs: $1 - install_dir (only used to resolve a Python interpreter for
#   the fallback path).
# Output: prints a UUID string and returns 0; returns 1 with nothing on
#   stdout when neither uuidgen nor Python can produce one, so a caller
#   cannot mistake an empty string for an identifier.
trail_uuid() {
    local install_dir="$1" value python_bin
    if command -v uuidgen >/dev/null 2>&1; then
        value="$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z')"
        if [ -n "${value}" ]; then
            printf '%s\n' "${value}"
            return 0
        fi
    fi
    python_bin="$(resolve_python "${install_dir}")"
    value="$("${python_bin}" -c 'import uuid; print(uuid.uuid4())' 2>/dev/null)"
    [ -n "${value}" ] || return 1
    printf '%s\n' "${value}"
}

# Description: current UTC time in the trail's timestamp format. One-second
#   resolution; see this file's header for why that is deliberate and safe.
# Inputs: none.
# Output: prints e.g. 2026-08-18T22:44:40Z and returns 0.
trail_now() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Description: escape one string for embedding in a JSON string literal.
#   Handles backslash, double quote, and the control characters a detail
#   or error message realistically carries (newline, carriage return, tab).
#   Written in shell rather than delegated so the trail's format stays
#   owned by this file.
# Inputs: $1 - the raw string.
# Output: prints the escaped body WITHOUT surrounding quotes; returns 0.
# Example: trail_json_escape 'a"b' -> a\"b
trail_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "${s}"
}

# Description: render either a JSON string literal or the bare literal
#   null. The trail distinguishes "no error" (null) from "an error whose
#   text happened to be empty" (""), and collapsing the two would make an
#   empty-message failure read as a success.
# Inputs: $1 - the value; an empty string yields null.
# Output: prints `null` or `"escaped"`; returns 0.
trail_json_value() {
    if [ -z "${1:-}" ]; then
        printf 'null'
    else
        printf '"%s"' "$(trail_json_escape "$1")"
    fi
}

# Description: append one already-built JSON line to the trail and fsync
#   it, creating the file and its directory if needed. This is the only
#   function in this file that writes.
# Inputs: $1 - install_dir (resolves the interpreter). $2 - trail path.
#   $3 - the complete JSON object, one line, no trailing newline.
# Output: returns 0 on a durable append; returns 1 with a log_fail line
#   when the write or the fsync did not succeed. A failed append is
#   reported, never swallowed: an unwritten trail line is the same defect
#   as a missing backup - invisible exactly when it matters.
trail_append_line() {
    local install_dir="$1" path="$2" line="$3" python_bin
    python_bin="$(resolve_python "${install_dir}")"
    "${python_bin}" - "${path}" "${line}" <<'PYEOF'
import os
import sys

path, line = sys.argv[1], sys.argv[2]
directory = os.path.dirname(path)
if directory:
    os.makedirs(directory, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try:
    os.write(fd, (line + "\n").encode("utf-8"))
    os.fsync(fd)
finally:
    os.close(fd)
PYEOF
    local rc=$?
    if [ "${rc}" -ne 0 ]; then
        log_fail "could not append to the migration trail at ${path} (rc=${rc})"
        return 1
    fi
    return 0
}

# Description: build one kind='code' trail line. Field order matches
#   src/core/trail_entry.py's FIELD_ORDER, entry_uuid and kind first, so a
#   line cut off mid-write is still recoverable by the app's reader.
# Inputs: $1 uuid, $2 status, $3 from_version, $4 to_version,
#   $5 started_at, $6 completed_at (empty for null), $7 app_version,
#   $8 error (empty for null), $9 detail (empty for null).
# Output: prints the JSON object on one line; returns 0.
trail_code_line() {
    printf '{"entry_uuid": "%s", "kind": "code", "from_version": %s, "to_version": %s, "status": "%s", "started_at": "%s", "completed_at": %s, "backup_path": null, "backup_verified": null, "app_version": %s, "error": %s, "detail": %s}' \
        "$(trail_json_escape "$1")" \
        "$(trail_json_value "$3")" \
        "$(trail_json_value "$4")" \
        "$(trail_json_escape "$2")" \
        "$(trail_json_escape "$5")" \
        "$(trail_json_value "$6")" \
        "$(trail_json_value "$7")" \
        "$(trail_json_value "$8")" \
        "$(trail_json_value "$9")"
}

# Description: phase one of the two-phase write. Appends and fsyncs a
#   `started` code entry BEFORE the caller mutates anything.
#
#   backup_path is null on a code entry by design. A code move's backup is
#   the .upgrade-backups directory the shell already takes, which is a
#   directory of install files, not the single-artifact backup the DATA
#   entries name; putting a directory path in a field whose consumers
#   treat it as a restorable artifact would be worse than leaving it null.
# Inputs: $1 install_dir, $2 from_version, $3 to_version, $4 app_version
#   (may be empty), $5 detail (may be empty).
# Output: prints "<entry_uuid> <started_at>" on one line, for the caller to
#   pass back to trail_code_close; returns 0. Returns 1 and prints nothing
#   when the uuid could not be minted or the append failed - the caller
#   must treat that as "the trail did not record this", not as success.
trail_code_open() {
    local install_dir="$1" from_v="$2" to_v="$3" app_v="$4" detail="$5"
    local path uuid started line
    path="$(trail_file "${install_dir}")"
    uuid="$(trail_uuid "${install_dir}")" || {
        log_fail "could not mint an entry_uuid for the migration trail"
        return 1
    }
    started="$(trail_now)"
    line="$(trail_code_line "${uuid}" "started" "${from_v}" "${to_v}" \
        "${started}" "" "${app_v}" "" "${detail}")"
    trail_append_line "${install_dir}" "${path}" "${line}" || return 1
    printf '%s %s\n' "${uuid}" "${started}"
}

# Description: phase two. Appends and fsyncs the closing line for an entry
#   opened by trail_code_open, reusing its entry_uuid and its ORIGINAL
#   started_at so the two lines coalesce into one step.
# Inputs: $1 install_dir, $2 entry_uuid, $3 started_at, $4 from_version,
#   $5 to_version, $6 status (completed|failed), $7 app_version (may be
#   empty), $8 error text (empty for a completed entry).
# Output: returns 0 on a durable append, 1 otherwise (already logged).
trail_code_close() {
    local install_dir="$1" uuid="$2" started="$3" from_v="$4" to_v="$5"
    local status="$6" app_v="$7" error="$8"
    local path line completed
    path="$(trail_file "${install_dir}")"
    completed="$(trail_now)"
    line="$(trail_code_line "${uuid}" "${status}" "${from_v}" "${to_v}" \
        "${started}" "${completed}" "${app_v}" "${error}" "")"
    trail_append_line "${install_dir}" "${path}" "${line}"
}
