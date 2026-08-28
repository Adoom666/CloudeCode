#!/bin/bash
# scripts/upgrade_lib/upgrade_rollback_common.sh
#
# Shared functions for scripts/upgrade.sh and scripts/rollback.sh. Sourced,
# never executed directly. Keeps both scripts DRY and keeps the
# three-outcome contract (pass / fail / could-not-evaluate) implemented in
# exactly one place instead of drifting between two copies.
#
# Not named scripts/lib/ - this repo's .gitignore has a generic Python
# "lib/" entry (build-artifact convention) that would silently swallow
# anything placed there. Named upgrade_lib/ instead so it is actually
# tracked; see the README's "why upgrade_lib, not lib" note if this
# surprises you.
#
# Every function documents Description / Inputs / Output per this repo's
# code-standards.md. None of them use a bare `except`-equivalent: every
# external command's exit code is checked explicitly.

# What "user state" means for a from-source install (Path B in the README).
# Split by WHERE it actually lives, verified against the app's own code
# rather than guessed:
#
#   - .env / config.json live in the install dir (AUTH_CONFIG_FILE default
#     ./config.json, .env read from cwd by both setup_auth.py and the app).
#   - config.json.bak / .update-check.json ALSO live in the install dir
#     (config_migration.py writes the former beside config.json;
#     update_check.py's default_cache_path writes the latter beside
#     repo_root() which for a from-source install IS the install dir).
#   - session_metadata.json, pinned_themes.json, unread_state.json, and
#     refresh_tokens.db do NOT live in the install dir. They live under
#     the app's STATE DIRECTORY (feat/state-directory - src/config.py's
#     Settings.get_state_dir() / get_session_metadata_path() /
#     get_pinned_themes_path() / analogous unread-state path, and
#     src/main.py's RefreshStore placement). The default, when
#     CLOUDE_STATE_DIR is unset, is
#     ``~/Library/Application Support/CloudeCode`` - a backup that skips
#     this directory entirely because it went looking in the install dir
#     would silently protect nothing. This split exists specifically so
#     that mistake is structurally impossible: the two arrays are keyed
#     by directory, not merged into one guess.
#
#     PRE feat/state-directory installs still have these files under the
#     OLD LOG_DIRECTORY-based location. resolve_state_dir() resolves only
#     the CURRENT (CLOUDE_STATE_DIR / default) DIRECTORY, matching
#     Settings.get_state_dir() exactly - that is a directory question and
#     it has one answer. Locating an individual FILE is a different
#     question with a fallback, and resolve_state_file() below is the one
#     that answers it, mirroring Settings._resolve_state_file() precedence
#     for precedence: new location wins when the file is in both, the old
#     LOG_DIRECTORY location is used when the file is only there, and the
#     new path is returned when it is in neither.
#
#     WITHOUT that fallback take_backup() aborts on a real, existing
#     install: the user's refresh_tokens.db is under the old
#     LOG_DIRECTORY, it is in STATE_DIR_REQUIRED_FILES, and the
#     directory-only resolver reports it MISSING - a hard die() on an
#     install whose data is perfectly fine and merely one directory over.
#
# A single flat list previously named every one of these as if they all
# lived in the install dir. Four of six did not: take_backup found .env
# and config.json, silently found nothing for the rest (wrong directory,
# not "absent"), and reported success anyway - a false green. See
# resolve_state_dir() and take_backup() below for the fix.
INSTALL_REQUIRED_PAIR=(".env" "config.json")          # either both exist or neither does; a half-set is a broken install
INSTALL_OPTIONAL_FILES=("config.json.bak" ".update-check.json")  # app-created lazily; absence is normal
STATE_DIR_REQUIRED_FILES=("refresh_tokens.db")          # RefreshStore.init() runs on EVERY server startup - if the server was running, this exists
STATE_DIR_OPTIONAL_FILES=("session_metadata.json" "pinned_themes.json" "unread_state.json" "cloude.db" "migration_trail.jsonl" "hook_tokens.json")  # created lazily on first session / theme pin / unread event; cloude.db + migration_trail.jsonl are created on the first startup that runs feat/datastore-and-trail, so they are legitimately absent on an install that has not been upgraded to it yet; hook_tokens.json holds the per-session HMAC hook tokens and is created on the first session mint - restoring it with the rest is what lets a rolled-back install keep talking to agents that survived the rollback, exactly as it does across a restart. Absent on any install predating it, and harmless to omit (tokens re-mint on the next create), but omitting it silently costs every SURVIVING agent its hooks

# DEFAULT_PORT and resolve_port() live in scripts/resolve-port.sh - the
# single shared shell-side port resolver every port-aware script in this
# repo sources, instead of each one carrying its own copy of the "8000"
# literal. See that file's docstring for the three-outcome contract
# (resolve_port returns 1 with a stderr reason on an unparseable PORT= -
# it never silently substitutes DEFAULT_PORT for a value it could not
# parse).
_URC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "${_URC_DIR}/../resolve-port.sh"

# --- logging: three outcomes, named, never collapsed ------------------------

# Description: print a PASS-tier informational line. Writes to STDERR, not
#   stdout - load-bearing: several functions (take_backup, current_version,
#   resolve_remote, ...) are called via command substitution where stdout
#   IS the return value, and a log line on stdout would silently corrupt
#   it. Every log_* function in this file writes to stderr for exactly
#   this reason; stdout is reserved for values a caller captures.
# Inputs: $* - message.
# Output: none (writes to stderr).
log_ok() { printf '[OK]   %s\n' "$*" >&2; }

# Description: print a plain step/info line, no verdict implied. See
#   log_ok's docstring for why this is stderr, not stdout.
# Inputs: $* - message.
# Output: none (writes to stderr).
log_step() { printf '[STEP] %s\n' "$*" >&2; }

# Description: print a FAIL-tier line. Does not exit by itself.
# Inputs: $* - message.
# Output: none (writes to stderr).
log_fail() { printf '[FAIL] %s\n' "$*" >&2; }

# Description: print the explicit COULD-NOT-EVALUATE state. Never call
#   log_ok or silently proceed when this is the honest answer - see this
#   project's three-outcome rule in CLAUDE.md.
# Inputs: $* - message describing what could not be checked and why.
# Output: none (writes to stderr).
log_unknown() { printf '[UNKNOWN] %s\n' "$*" >&2; }

# Description: print a FAIL line and exit the script non-zero.
# Inputs: $* - message.
# Output: never returns; exits 1.
die() {
    log_fail "$*"
    exit 1
}

# --- environment resolution --------------------------------------------------

# Description: pick the python interpreter to run install-scoped checks
#   with - the install's own venv when it exists (so we run against the
#   dependency versions that install actually has), falling back to a bare
#   python3 on PATH for a fresh checkout that has no venv yet.
# Inputs: $1 - install_dir.
# Output: prints an absolute path to a python3 executable on stdout. Exits
#   with a die() if neither exists (COULD-NOT-EVALUATE: nothing to run).
resolve_python() {
    local install_dir="$1"
    if [ -x "${install_dir}/venv/bin/python3" ]; then
        printf '%s\n' "${install_dir}/venv/bin/python3"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    die "no venv at ${install_dir}/venv and no python3 on PATH - cannot run any check"
}

# resolve_port() is sourced from scripts/resolve-port.sh (see the source
# line near the top of this file) rather than defined here.

# Description: resolve the app's state directory the SAME WAY the app
#   does - src/config.py's Settings.get_state_dir() precedence, mirrored
#   here byte-for-byte (see tests/test_state_dir_drift.py, which asserts
#   this function and get_state_dir() agree on every path for all three
#   env cases):
#     1. CLOUDE_STATE_DIR in the CURRENT PROCESS ENVIRONMENT, if set to a
#        non-empty value - takes priority, matching pydantic-settings'
#        own env-var-beats-env-file precedence for the identical field.
#     2. CLOUDE_STATE_DIR= read out of install_dir/.env, if the process
#        environment did not have it - an install-local override.
#     3. ~/Library/Application Support/CloudeCode - the macOS-native
#        default, computed via Python so "~" expands identically to the
#        app's own Path.expanduser() call (not a bash tilde trick).
#   Unlike the old resolve_log_dir() this replaces, there is ALWAYS a
#   resolvable value (the default never fails to compute) - the function
#   never returns 1. This is pure path computation, mirroring
#   get_state_dir()'s path-resolution half only; it does NOT create the
#   directory - callers that need it to exist still call mkdir -p
#   themselves, same as before.
# Inputs: $1 - install_dir (its .env is consulted only as step 2 above;
#   its absence is not an error).
# Output: prints the expanded absolute state directory path and returns 0,
#   always.
resolve_state_dir() {
    local install_dir="$1" python_bin
    local env_file="${install_dir}/.env"
    local raw="${CLOUDE_STATE_DIR:-}"
    if [ -z "${raw}" ] && [ -f "${env_file}" ]; then
        raw="$(grep -E '^CLOUDE_STATE_DIR=' "${env_file}" | tail -1 | cut -d= -f2-)"
    fi
    python_bin="$(resolve_python "${install_dir}")"
    if [ -n "${raw}" ]; then
        "${python_bin}" -c "import os, sys; print(os.path.expanduser(sys.argv[1]))" "${raw}"
    else
        "${python_bin}" -c "import os; print(os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'CloudeCode'))"
    fi
}

# Description: resolve the OLD, pre-feat/state-directory state location -
#   the .env LOG_DIRECTORY value, expanded. This is NOT a fallback for the
#   state DIRECTORY itself (get_state_dir() has no such fallback and
#   neither does resolve_state_dir); it exists only so resolve_state_file()
#   can look for an individual leftover file where an older install put it.
# Inputs: $1 - install_dir (its .env is the only source; the process
#   environment is deliberately NOT consulted, matching how the Python side
#   reads log_directory off the loaded Settings rather than off os.environ
#   at the point of the fallback).
# Output: prints the expanded path and returns 0 when LOG_DIRECTORY is set
#   to a non-empty value; prints nothing and returns 1 when it is not set.
#   An empty stdout therefore means "there is no old location", never "the
#   old location is the current directory".
resolve_legacy_state_dir() {
    local install_dir="$1" python_bin raw
    local env_file="${install_dir}/.env"
    [ -f "${env_file}" ] || return 1
    raw="$(grep -E '^LOG_DIRECTORY=' "${env_file}" | tail -1 | cut -d= -f2-)"
    [ -n "${raw}" ] || return 1
    python_bin="$(resolve_python "${install_dir}")"
    "${python_bin}" -c "import os, sys; print(os.path.expanduser(sys.argv[1]))" "${raw}"
}

# Description: resolve ONE state file by name, with the same old-location
#   fallback the app itself applies. Mirrors
#   Settings._resolve_state_file() in src/config.py precedence for
#   precedence; tests/test_state_dir_drift.py asserts the two agree for
#   every one of the four cases, so this cannot quietly drift.
#
#   Four outcomes, in order:
#     1. present in BOTH the new state dir and the old LOG_DIRECTORY
#        location - ambiguous. The NEW path wins and the old file is left
#        alone. (The Python side logs a warning here; a backup script that
#        printed one would corrupt the command substitutions this file is
#        full of, so the resolution is identical and silent.)
#     2. present only in the new state dir - use it.
#     3. present only in the old LOG_DIRECTORY location - use it. This is
#        the case that keeps an existing install upgradable.
#     4. present in neither - return the NEW path, which is where a
#        first-time writer would create it. The CALLER still has to test
#        -f on the result; this function locates a file, it does not
#        assert that one exists.
# Inputs: $1 - install_dir. $2 - bare filename, e.g. "refresh_tokens.db".
# Output: prints the absolute path the app would read that file from, and
#   returns 0 always (path computation cannot fail).
resolve_state_file() {
    local install_dir="$1" filename="$2" state_dir legacy_dir
    state_dir="$(resolve_state_dir "${install_dir}")"
    local new_path="${state_dir}/${filename}"
    legacy_dir="$(resolve_legacy_state_dir "${install_dir}")" || {
        printf '%s\n' "${new_path}"
        return 0
    }
    local old_path="${legacy_dir}/${filename}"
    if [ -f "${new_path}" ]; then
        printf '%s\n' "${new_path}"
    elif [ -f "${old_path}" ]; then
        printf '%s\n' "${old_path}"
    else
        printf '%s\n' "${new_path}"
    fi
}

# Description: confirm install_dir is itself a git work tree root (not an
#   ancestor's), the same guard src/core/version.py's is_git_root enforces
#   for the identical reason - `git -C` walks upward and would otherwise
#   silently answer with an unrelated enclosing repo.
# Inputs: $1 - install_dir.
# Output: exit 0 if install_dir is a git work tree root; exit 1 with a
#   die() otherwise.
require_git_root() {
    local install_dir="$1"
    if [ ! -d "${install_dir}/.git" ]; then
        die "${install_dir} is not a git checkout (no .git). This script only manages a from-source install (README Path B). A packaged .app install upgrades by downloading a new DMG - see the README."
    fi
    local toplevel
    toplevel="$(cd "${install_dir}" && git rev-parse --show-toplevel 2>/dev/null)" || die "git rev-parse failed in ${install_dir} - is git installed?"
    if [ "$(cd "${toplevel}" && pwd -P)" != "$(cd "${install_dir}" && pwd -P)" ]; then
        die "${install_dir} sits inside another repository's work tree (${toplevel}). Refusing - this is exactly the footgun src/core/version.py's is_git_root guards against."
    fi
}

# Description: refuse to touch an install whose tracked files have local
#   modifications. A dirty tree means `git checkout <tag>` can silently
#   discard edits or refuse partway through, and either is worse than
#   stopping up front with a clear message.
# Inputs: $1 - install_dir.
# Output: exit 0 if clean; exit 1 with a die() and the dirty file list
#   otherwise.
require_clean_tree() {
    local install_dir="$1"
    local dirty
    dirty="$(cd "${install_dir}" && git status --porcelain --untracked-files=no)"
    if [ -n "${dirty}" ]; then
        log_fail "tracked files in ${install_dir} have local modifications:"
        printf '%s\n' "${dirty}" >&2
        die "commit, stash, or discard these before upgrading/rolling back - a dirty tree makes 'git checkout <tag>' unsafe to run unattended"
    fi
}

# --- version + tag resolution (delegates to version_probe.py) ---------------

# Description: resolve the currently-installed version via the project's
#   single version resolver (src/core/version.py), never a parallel
#   implementation.
# Inputs: $1 - install_dir. $2 - path to scripts/upgrade_lib/version_probe.py.
# Output: prints the version on stdout and returns 0 on success. Returns 3
#   (COULD-NOT-EVALUATE, not 1) and prints nothing on stdout when the
#   version did not resolve - callers must check the exit code, not assume
#   a non-empty string.
current_version() {
    local install_dir="$1" probe="$2" python_bin
    python_bin="$(resolve_python "${install_dir}")"
    "${python_bin}" "${probe}" current --install-dir "${install_dir}"
}

# Description: resolve which git remote release tags should be read from.
# Inputs: $1 - install_dir. $2 - probe script path.
# Output: prints the remote URL on stdout. Always succeeds (falls back to
#   the public upstream) - see version_probe.py's resolve_remote.
resolve_remote() {
    local install_dir="$1" probe="$2" python_bin config_arg=()
    python_bin="$(resolve_python "${install_dir}")"
    if [ -f "${install_dir}/config.json" ]; then
        config_arg=(--config "${install_dir}/config.json")
    fi
    "${python_bin}" "${probe}" remote --install-dir "${install_dir}" "${config_arg[@]}"
}

# --- confirmation -------------------------------------------------------------

# Description: interactive yes/no gate. Skipped entirely when ASSUME_YES=1.
# Inputs: $1 - prompt text. Reads $ASSUME_YES from the environment.
# Output: exit 0 (proceed) or exit 1 via die() (user declined / non-tty
#   without --yes).
confirm_or_die() {
    local prompt="$1"
    if [ "${ASSUME_YES:-0}" = "1" ]; then
        log_step "non-interactive (--yes): proceeding without a prompt"
        return 0
    fi
    if [ ! -t 0 ]; then
        die "no terminal attached and --yes was not given - refusing to guess. Re-run with --yes for non-interactive use (e.g. from the menu bar app)."
    fi
    local reply
    read -r -p "${prompt} [type yes to continue] " reply
    if [ "${reply}" != "yes" ]; then
        die "not confirmed - no changes made"
    fi
}

# --- backup / restore ---------------------------------------------------------
#
# Every file this backup touches gets exactly one of THREE recorded
# outcomes, written to backup_dir/.manifest as "<OUTCOME>\t<category>\t<name>":
#   BACKED_UP     - copied, bytes verified present in the backup
#   NOT_PRESENT   - legitimately doesn't exist yet (lazily-created file on
#                   a feature never used, or a never-configured install)
#   MISSING       - should exist and does not. This one is FATAL: take_backup
#                   dies rather than returning, because a backup that is
#                   silently missing declared state is worse than no backup -
#                   the caller would proceed to upgrade believing it has a
#                   safety net it does not have.
# Never collapse MISSING into NOT_PRESENT to make a run "succeed" - that is
# the exact false-green this file exists to prevent.

# Description: append one manifest line. Internal helper, not called
#   directly from upgrade.sh/rollback.sh.
#
#   The fourth field is the ORIGIN DIRECTORY - the directory the file was
#   actually copied FROM. It exists because restore_backup() used to
#   ignore where take_backup() found a file and place every state file at
#   resolve_state_dir(), so rolling back an install whose
#   session_metadata.json still lives at the old LOG_DIRECTORY MOVED that
#   file into the state directory. The older code being restored
#   alongside it reads only LOG_DIRECTORY, so the one tool a user runs to
#   go back was itself the step that made going back fail. A restore has
#   to put a file where it came from, and that is a fact take_backup
#   measured, not one restore_backup can re-derive: by restore time the
#   original file has usually been deleted, which is exactly the state
#   that makes any re-derivation answer the wrong location.
#
#   Field is APPENDED, so a manifest written by an older take_backup
#   parses unchanged and simply carries an empty origin - restore_backup
#   treats that as COULD NOT DETERMINE and says so, rather than silently
#   guessing.
# Inputs: $1 - backup_dir. $2 - outcome (BACKED_UP|NOT_PRESENT|MISSING).
#   $3 - category (install|state). $4 - filename. $5 - origin directory
#   (optional; meaningful only for BACKED_UP).
# Output: none (appends to backup_dir/.manifest).
_manifest_record() {
    if [ -n "${5:-}" ]; then
        printf '%s\t%s\t%s\t%s\n' "$2" "$3" "$4" "$5" >> "$1/.manifest"
    else
        # Three fields, not four-with-an-empty-one. A NOT_PRESENT/MISSING
        # row has no origin by definition, and a trailing tab would make
        # every existing anchored matcher of this format silently stop
        # matching.
        printf '%s\t%s\t%s\n' "$2" "$3" "$4" >> "$1/.manifest"
    fi
}

# --- SQLite-safe copying ------------------------------------------------------
#
# A plain `cp -p` of a live SQLite database is not a safe read. WAL-mode
# SQLite (what this project uses - see src/main.py's RefreshStore) keeps
# committed-but-not-yet-checkpointed pages in a separate `-wal` file and
# reconciles them with the main `.db` file only through SQLite's own
# engine. `cp` has no idea any of that exists: it can copy the main file
# mid-checkpoint, or copy it without its `-wal` sibling at all, and the
# result is a file that passes a plain `[ -f ... ]` existence check while
# being torn or missing committed transactions. This was already a live
# risk for refresh_tokens.db (RefreshStore holds it open with an active
# purge loop) and becomes a correctness bug for the cloude.db datastore
# once that exists.
#
# The fix used throughout this file: `sqlite3 <src> "VACUUM INTO '<dst>'"`.
# VACUUM INTO reads through SQLite's own transactional/WAL-aware engine,
# so it always produces a copy of the last COMMITTED state - never a
# torn mix of pages - with no separate `-wal`/`-shm` file to lose track
# of. The copy is then verified with PRAGMA integrity_check before it is
# ever recorded BACKED_UP; see _copy_sqlite's docstring for why a bare
# `sqlite3` exit 0 is not itself proof of a usable backup.

# Description: detect whether a file is a SQLite database by its file
#   header magic ("SQLite format 3", the fixed ASCII prefix every valid
#   SQLite database file starts with) rather than by filename. Chosen
#   over an explicit filename list specifically so a future database
#   file - the cloude.db datastore named in this fix's motivating defect,
#   or anything added after it - is routed through the safe copy path
#   automatically, with nothing to remember to update here. An explicit
#   list is exactly the enumeration trap this repo's CLAUDE.md hazards
#   30/32/34 describe: a check that only knows about the names it was
#   told about silently stops covering anything renamed or added later.
# Inputs: $1 - path to a file (existence is checked here, callers do not
#   need to pre-check).
# Output: return 0 if the file exists, is non-empty, and its first 15
#   bytes are exactly "SQLite format 3". Return 1 otherwise (absent,
#   empty, or not a SQLite file) - all "not sqlite" for this check's
#   purpose; a plain cp -p is correct for those.
_is_sqlite_file() {
    local f="$1"
    [ -f "${f}" ] && [ -s "${f}" ] || return 1
    local header
    header="$(head -c 15 "${f}" 2>/dev/null)"
    [ "${header}" = "SQLite format 3" ]
}

# Description: copy a SQLite database file the way that is actually safe
#   under a concurrent writer - `sqlite3 <src> "VACUUM INTO '<dst>'"`,
#   never `cp`. Immediately verifies the result with PRAGMA
#   integrity_check before returning success, because an sqlite3 exit
#   code of 0 is evidence that VACUUM INTO ran, not evidence that the
#   resulting file opens cleanly.
#   THREE-OUTCOME, enforced by construction: every failure path calls
#   die() itself, naming exactly which of the three things could not be
#   done (no sqlite3 binary / VACUUM INTO failed / integrity_check
#   failed), and none of them fall through to a plain `cp`. A silent
#   fallback would convert a genuine COULD-NOT-EVALUATE into a false
#   BACKED_UP - the exact defect class this project's three-outcome rule
#   (CLAUDE.md) exists to prevent, and worse here than most instances of
#   it because the thing being backed up is the one place a restore
#   would be reached for.
# Inputs: $1 - source db path (must exist and be a real SQLite file).
#   $2 - destination path (must not already exist).
# Output: exit 0 with the destination written and verified (PRAGMA
#   integrity_check returned exactly "ok"). Calls die() and never returns
#   otherwise: `sqlite3` missing from PATH, VACUUM INTO exiting non-zero,
#   or integrity_check reporting anything other than "ok" (the partial
#   destination file is deleted first in that last case, so it can never
#   be mistaken for a usable backup by a later restore).
_copy_sqlite() {
    local src="$1" dst="$2"
    if ! command -v sqlite3 >/dev/null 2>&1; then
        die "sqlite3 is not on PATH - cannot take a verified backup of ${src}. Refusing to fall back to a raw file copy of a live SQLite database: see this file's SQLite-safe-copying note for why a plain cp of a WAL-mode database can capture a torn snapshot. Install sqlite3 (it ships with macOS and every Linux distro this fleet runs) and re-run."
    fi

    # Escape single quotes for the SQL string literal ('' inside a
    # single-quoted SQLite string literal is a literal single quote).
    local dst_escaped="${dst//\'/\'\'}"
    local vacuum_err vacuum_rc
    vacuum_err="$(sqlite3 "${src}" "VACUUM INTO '${dst_escaped}';" 2>&1 1>/dev/null)"
    vacuum_rc=$?
    if [ "${vacuum_rc}" -ne 0 ]; then
        die "VACUUM INTO failed backing up ${src} to ${dst} (sqlite3 exit ${vacuum_rc}): ${vacuum_err}"
    fi

    local integrity integrity_rc
    integrity="$(sqlite3 "${dst}" "PRAGMA integrity_check;" 2>&1)"
    integrity_rc=$?
    if [ "${integrity_rc}" -ne 0 ] || [ "${integrity}" != "ok" ]; then
        rm -f "${dst}"
        die "backup of ${src} failed PRAGMA integrity_check (sqlite3 exit ${integrity_rc}, output: '${integrity}') - deleted the unverified copy at ${dst} rather than leaving it behind to be mistaken for a real backup. A VACUUM INTO that completes without erroring is not by itself proof the result is usable."
    fi
}

# Description: copy one backed-up file using the method its content
#   actually needs - _copy_sqlite (VACUUM INTO + integrity_check) for
#   anything that is a SQLite database by header magic (_is_sqlite_file),
#   plain cp -p for everything else. The non-SQLite files this project
#   backs up (.env, config.json, config.json.bak, .update-check.json,
#   session_metadata.json, pinned_themes.json, unread_state.json) are
#   whole-file text/JSON writes with no WAL, no page cache, and no
#   partial-write hazard analogous to SQLite's, so cp -p remains correct
#   for them. Single dispatch point so take_backup's call sites make one
#   decision instead of repeating an `if _is_sqlite_file` check six times.
# Inputs: $1 - src path (must exist). $2 - dst path (must not exist).
# Output: return 0 on a successful copy. For the plain-cp path, returns
#   cp's own exit status so the caller's existing
#   `|| die "failed to back up ${f}"` pattern still fires. For the SQLite
#   path this never returns non-zero - _copy_sqlite calls die() itself
#   with a specific reason before that could happen; see its docstring.
_copy_backup_file() {
    local src="$1" dst="$2"
    if _is_sqlite_file "${src}"; then
        _copy_sqlite "${src}" "${dst}"
        return 0
    fi
    cp -p "${src}" "${dst}"
}

# Description: back up every piece of user state this install has, split
#   correctly between the install dir and the state directory (see the
#   array comments above resolve_state_dir). SQLite databases
#   (refresh_tokens.db today, cloude.db once it exists) are routed through
#   _copy_sqlite (VACUUM INTO + integrity_check), never a raw file copy -
#   see the "SQLite-safe copying" note above this function for why.
#   Everything else is copied with cp -p, which is a genuinely safe plain
#   read for whole-file text/JSON writers. Correct to call whether or not
#   the server is still running, but scripts/upgrade.sh and
#   scripts/rollback.sh both call it AFTER stop_service anyway: a copy
#   against a quiesced writer is simpler to reason about and faster
#   (VACUUM INTO against an idle database has nothing to reconcile),
#   even though it is not the thing that makes the copy correct.
# Inputs: $1 - install_dir. $2 - backup_dir (must not already exist).
# Output: prints the backup_dir on stdout for the caller to surface
#   prominently. Exits via die() if: backup_dir already exists; it cannot
#   be created; any BACKED_UP copy fails; INSTALL_REQUIRED_PAIR is
#   half-present (one of .env/config.json exists, the other does not - a
#   broken install, not a legitimate state); or any STATE_DIR_REQUIRED_FILES
#   entry is missing while the install is configured (refresh_tokens.db is
#   created by every server startup, so its absence on a configured,
#   presumably-just-stopped install means something is already wrong and a
#   backup here would be incomplete).
take_backup() {
    local install_dir="$1" backup_dir="$2"
    if [ -e "${backup_dir}" ]; then
        die "backup directory ${backup_dir} already exists - refusing to overwrite a prior backup"
    fi
    mkdir -p "${backup_dir}/install" "${backup_dir}/state" || die "could not create backup directory ${backup_dir}"
    : > "${backup_dir}/.manifest"

    local env_present=0 config_present=0
    [ -f "${install_dir}/.env" ] && env_present=1
    [ -f "${install_dir}/config.json" ] && config_present=1

    if [ "${env_present}" -ne "${config_present}" ]; then
        die "half-configured install in ${install_dir}: .env present=${env_present}, config.json present=${config_present} - a broken install, refusing to guess which half is authoritative"
    fi

    local configured=0
    if [ "${env_present}" -eq 1 ]; then
        configured=1
        local f
        for f in "${INSTALL_REQUIRED_PAIR[@]}"; do
            _copy_backup_file "${install_dir}/${f}" "${backup_dir}/install/${f}" || die "failed to back up ${f}"
            _manifest_record "${backup_dir}" BACKED_UP install "${f}" "${install_dir}"
        done
    else
        log_unknown "${install_dir} has neither .env nor config.json - nothing has been configured yet (fresh checkout); backing up nothing, which is correct, not a failure"
    fi

    local f
    for f in "${INSTALL_OPTIONAL_FILES[@]}"; do
        if [ -f "${install_dir}/${f}" ]; then
            _copy_backup_file "${install_dir}/${f}" "${backup_dir}/install/${f}" || die "failed to back up ${f}"
            _manifest_record "${backup_dir}" BACKED_UP install "${f}" "${install_dir}"
        else
            _manifest_record "${backup_dir}" NOT_PRESENT install "${f}"
        fi
    done

    if [ "${configured}" -eq 1 ]; then
        local log_dir
        log_dir="$(resolve_state_dir "${install_dir}")"
        if [ -z "${log_dir}" ]; then
            # Defensive only - resolve_state_dir() always resolves to a
            # value (its default never fails to compute). Kept in case a
            # future edit to resolve_state_dir breaks that contract.
            die "${install_dir} is configured (.env present) but the state directory could not be resolved - refusing to back up blind. session_metadata.json, pinned_themes.json, unread_state.json, and refresh_tokens.db all live there and would be silently skipped otherwise."
        fi
        log_step "state directory (CLOUDE_STATE_DIR): ${log_dir}"

        # Each state file is located INDIVIDUALLY via resolve_state_file,
        # not by assuming it sits in log_dir. An install that predates
        # feat/state-directory still has its files under the old
        # LOG_DIRECTORY, and that is a supported, upgradable install - not
        # a missing file. See resolve_state_file's four-outcome contract.
        local src
        for f in "${STATE_DIR_REQUIRED_FILES[@]}"; do
            src="$(resolve_state_file "${install_dir}" "${f}")"
            if [ -f "${src}" ]; then
                _copy_backup_file "${src}" "${backup_dir}/state/${f}" || die "failed to back up ${f} from ${src}"
                _manifest_record "${backup_dir}" BACKED_UP state "${f}" "$(dirname "${src}")"
                [ "${src}" = "${log_dir}/${f}" ] || log_step "${f} found at the pre-feat/state-directory location ${src} - backing up from there"
            else
                die "expected ${f} but it is in NEITHER the current state directory (${log_dir}) NOR the old LOG_DIRECTORY location. ${f} is created by every server startup, so either the server never started successfully on this install or state has already been lost. Refusing to take a partial backup and proceed with a destructive upgrade. If this install genuinely never ran, there is nothing to upgrade yet."
            fi
        done
        for f in "${STATE_DIR_OPTIONAL_FILES[@]}"; do
            src="$(resolve_state_file "${install_dir}" "${f}")"
            if [ -f "${src}" ]; then
                _copy_backup_file "${src}" "${backup_dir}/state/${f}" || die "failed to back up ${f} from ${src}"
                _manifest_record "${backup_dir}" BACKED_UP state "${f}" "$(dirname "${src}")"
            else
                _manifest_record "${backup_dir}" NOT_PRESENT state "${f}"
            fi
        done
    else
        log_unknown "skipping the state directory entirely - install was never configured, so its CLOUDE_STATE_DIR is unknown and nothing could exist there yet"
    fi

    local backed_up_count
    backed_up_count="$(grep -c '^BACKED_UP' "${backup_dir}/.manifest" || true)"
    printf '%s\n' "${backed_up_count:-0}" > "${backup_dir}/.file-count"
    log_ok "manifest: $(grep -c '^BACKED_UP' "${backup_dir}/.manifest" || echo 0) backed up, $(grep -c '^NOT_PRESENT' "${backup_dir}/.manifest" || echo 0) not present (see ${backup_dir}/.manifest)"
    printf '%s\n' "${backup_dir}"
}

# Description: restore every BACKED_UP entry from a backup's manifest back
#   to where the app actually reads it from - install_dir for the
#   "install" category, and for the "state" category the directory
#   take_backup RECORDED IT AS COMING FROM (manifest field 4), falling
#   back to the currently-configured state directory with an explicit
#   COULD-NOT-DETERMINE line when an older manifest carries no origin. Restores install files FIRST so CLOUDE_STATE_DIR can
#   be read from the just-restored .env before any state file is placed.
#   Does NOT run config migration afterward - the whole point of a
#   rollback is that the restored config matches the OLDER code being
#   checked out alongside it.
#   Uses plain cp -p, unlike take_backup's _copy_backup_file - and that is
#   correct here, not an oversight: scripts/rollback.sh (the only caller)
#   invokes this after stop_service, so the destination has no writer, and
#   the source is a static file inside an already-closed backup directory
#   that nothing is writing to either. Neither side of this copy has the
#   live-writer hazard _copy_sqlite exists to close, so there is nothing
#   for VACUUM INTO to buy here.
# Inputs: $1 - install_dir. $2 - backup_dir (must exist and have a
#   .manifest produced by take_backup).
# Output: prints the number of files restored on stdout. Exits via die()
#   if backup_dir or its manifest is missing, a BACKED_UP install file's
#   restore copy fails, or (once install files are back) the state
#   directory cannot be resolved while the manifest has BACKED_UP state
#   entries to place, or any of those copies fail. A failure partway
#   leaves install_dir in a MIXED state - reported loudly, never
#   swallowed.
restore_backup() {
    local install_dir="$1" backup_dir="$2"
    if [ ! -d "${backup_dir}" ]; then
        die "backup directory ${backup_dir} does not exist - cannot restore"
    fi
    if [ ! -f "${backup_dir}/.manifest" ]; then
        die "${backup_dir} has no .manifest - it was not produced by take_backup, refusing to trust it"
    fi

    local restored=0
    local outcome category name origin
    while IFS=$'\t' read -r outcome category name origin; do
        [ "${outcome}" = "BACKED_UP" ] || continue
        [ "${category}" = "install" ] || continue
        cp -p "${backup_dir}/install/${name}" "${install_dir}/${name}" || die "PARTIAL RESTORE: failed copying ${name} from ${backup_dir}/install - install_dir is now in a mixed state, ${restored} file(s) restored so far. Fix manually: diff ${backup_dir}/install ${install_dir}"
        restored=$((restored + 1))
    done < "${backup_dir}/.manifest"

    # NOT `grep -q '^BACKED_UP\tstate\t'`. In a POSIX basic regular
    # expression `\t` is undefined: BSD grep (macOS) happens to treat it as a
    # tab, GNU grep (every Linux install, and CI) reads it as a literal `t`
    # and never matches. Measured 2026-08-24: BSD grep 2.6.0-FreeBSD MATCH,
    # GNU grep 3.11 NOMATCH. The consequence was silent and total - on Linux
    # this branch was skipped, so restore_backup() restored the install files,
    # restored NONE of the state files (session_metadata.json,
    # pinned_themes.json, unread_state.json, refresh_tokens.db), and still
    # printed a success count. awk with a real tab field separator is
    # unambiguous on both.
    if awk -F'\t' '$1 == "BACKED_UP" && $2 == "state" { found = 1 }
                   END { exit found ? 0 : 1 }' "${backup_dir}/.manifest"; then
        local log_dir
        log_dir="$(resolve_state_dir "${install_dir}")"
        if [ -z "${log_dir}" ]; then
            # Defensive only - resolve_state_dir() always resolves to a
            # value. See the matching note in take_backup() above.
            die "PARTIAL RESTORE: install files restored (${restored}), but the state directory cannot be resolved from the just-restored .env, so session_metadata.json/pinned_themes.json/unread_state.json/refresh_tokens.db could NOT be restored. Fix .env's CLOUDE_STATE_DIR and re-run this script (idempotent) to finish."
        fi
        mkdir -p "${log_dir}" || die "could not create state directory ${log_dir} to restore state files into"
        local dest_dir
        while IFS=$'\t' read -r outcome category name origin; do
            [ "${outcome}" = "BACKED_UP" ] || continue
            [ "${category}" = "state" ] || continue

            # Put the file back WHERE IT CAME FROM. take_backup located
            # each state file individually (resolve_state_file) and
            # recorded the directory it actually read it out of, because
            # an install that predates feat/state-directory still keeps
            # session_metadata.json / pinned_themes.json /
            # unread_state.json / refresh_tokens.db under the old
            # LOG_DIRECTORY, and the OLDER CODE a rollback is checking
            # out reads only that location. Restoring everything to
            # resolve_state_dir() relocated those files, so scripts/
            # rollback.sh - the one tool a user runs specifically to go
            # back - was itself the step that lost the state it had just
            # backed up.
            #
            # An EMPTY origin means the manifest was written by an older
            # take_backup that did not record one. That is COULD NOT
            # DETERMINE, not "it was in the state dir": say so out loud
            # and use the current state directory, which is the behavior
            # that manifest was written under and therefore the only
            # answer consistent with it.
            if [ -n "${origin}" ]; then
                dest_dir="${origin}"
            else
                dest_dir="${log_dir}"
                log_unknown "${name}: this backup's manifest records no origin directory (written by an older take_backup), so where it was read FROM cannot be determined - restoring to the current state directory ${log_dir}. If this install predates feat/state-directory, check whether ${name} also needs to be at its LOG_DIRECTORY."
            fi
            mkdir -p "${dest_dir}" || die "PARTIAL RESTORE: could not create ${dest_dir} to restore ${name} into - ${restored} file(s) restored so far"
            cp -p "${backup_dir}/state/${name}" "${dest_dir}/${name}" || die "PARTIAL RESTORE: failed copying ${name} from ${backup_dir}/state to ${dest_dir} - ${restored} file(s) restored so far. Fix manually: diff ${backup_dir}/state ${dest_dir}"
            restored=$((restored + 1))
        done < "${backup_dir}/.manifest"
    fi

    printf '%s\n' "${restored}"
}

# --- service lifecycle ---------------------------------------------------------

# Description: stop the running server via the install's own stop.sh, so
#   behavior (graceful then SIGKILL, port-free verification) matches
#   exactly what a human running stop.sh by hand gets - no parallel kill
#   logic here.
# Inputs: $1 - install_dir.
# Output: exit 0 if stop.sh reports the port free (or was already free);
#   exit 1 via die() otherwise.
stop_service() {
    local install_dir="$1"
    if [ ! -x "${install_dir}/stop.sh" ]; then
        die "no executable stop.sh in ${install_dir}"
    fi
    (cd "${install_dir}" && ./stop.sh) || die "stop.sh exited non-zero - server may still be running, refusing to proceed with an upgrade against a live process"
}

# Description: start (or restart) the server via the install's own
#   reset.sh, so launchd-managed and unmanaged installs are both handled
#   correctly - same reasoning as stop_service.
# Inputs: $1 - install_dir.
# Output: exit 0 if reset.sh reports success; exit 1 via die() otherwise.
start_service() {
    local install_dir="$1"
    if [ ! -x "${install_dir}/reset.sh" ]; then
        die "no executable reset.sh in ${install_dir}"
    fi
    (cd "${install_dir}" && ./reset.sh) || die "reset.sh exited non-zero - the server did not come back up. Check ${install_dir}/logs and CLOUDE_LAUNCHD_LABEL if this is a launchd-managed install."
}

# --- post-restart verification: THREE outcomes, not two ---------------------

# Description: verify the server actually answers on its health endpoint
#   AND that this install's resolved version equals what was expected.
#   THREE-OUTCOME: distinguishes "confirmed matching" (0), "confirmed
#   wrong/unreachable" (2), and "could not even ask" (3, e.g. curl
#   missing) - never reports the third as if it were the first.
# Inputs: $1 - install_dir. $2 - port. $3 - expected_version. $4 - probe
#   script path. $5 - optional timeout in seconds (default 20).
# Output: prints a human status line. Returns 0 (verified), 2 (verified
#   failure - server answered but wrong version, or never came up within
#   the timeout), or 3 (could not evaluate - curl unavailable, python
#   unavailable to compute the expected-vs-actual comparison).
verify_upgrade() {
    local install_dir="$1" port="$2" expected_version="$3" probe="$4"
    local timeout_s="${5:-20}"

    if ! command -v curl >/dev/null 2>&1; then
        log_unknown "curl is not on PATH - cannot verify the server answers. Check manually: open http://127.0.0.1:${port}/health"
        return 3
    fi

    local waited=0
    local health_ok=0
    while [ "${waited}" -lt "${timeout_s}" ]; do
        if curl -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            health_ok=1
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if [ "${health_ok}" -ne 1 ]; then
        log_fail "server did not answer http://127.0.0.1:${port}/health within ${timeout_s}s after restart"
        return 2
    fi
    log_ok "server answers on port ${port} (waited ${waited}s)"

    local actual_version
    actual_version="$(current_version "${install_dir}" "${probe}")"
    local rc=$?
    if [ "${rc}" -ne 0 ] || [ -z "${actual_version}" ]; then
        log_unknown "server is up, but the post-restart version could not be resolved - cannot confirm it is running ${expected_version}"
        return 3
    fi
    if [ "${actual_version}" != "${expected_version}" ]; then
        log_fail "server is up but reports version '${actual_version}', expected '${expected_version}'"
        return 2
    fi
    log_ok "server confirms version ${actual_version}"
    return 0
}
