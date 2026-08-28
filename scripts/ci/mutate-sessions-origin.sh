#!/bin/bash
# Mutation check for the sessions table, its identity index, and the
# one-way first-run import latch.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this subsystem can silently hand a
# user the wrong answer. Every one must turn the suite red.
#
# The first block is the one the brief calls out by name: the latch guard
# flipped so a FAILED tmux probe stamps imported_from_json_at anyway. That
# is the single most dangerous line in the datastore design - it imports
# zero sessions, marks the import permanently complete, and shows no error
# on any screen. If no test dies here, nothing is protecting the user's
# session history.
#
# The second block is identity: the epoch removed from the instance key,
# the same-second collision merged instead of refused, adoption made
# last-write-wins. Each one transfers one session's history, and its
# OWNERSHIP BADGE, to a different process.
#
# The third block is the three-outcome collapses: unknown attribution read
# as "no project", a pending import reported as completed, an archived row
# hiding a running session.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_session_store.py tests/test_session_import.py \
tests/test_session_ownership_origin.py tests/test_project_store.py \
tests/test_db_migration.py"

FILES=(
  "src/core/session_store.py"
  "src/core/session_identity.py"
  "src/core/session_reconcile.py"
  "src/core/session_import.py"
  "src/core/session_import_mapping.py"
  # S7: the attribution rule moved here out of session_import_mapping.py,
  # and a mutated file that is not in this list is never restored - the
  # mutation leaks onto disk and the NEXT run backs the mutant up as if
  # it were the source. Anything mutate() targets MUST be listed.
  "src/core/project_attribution.py"
  "src/core/db_models.py"
  "src/core/project_store.py"
  "src/core/tmux_backend.py"
  "src/core/tmux_listing_parse.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

restore_all() {
    mutate_restore_files
}

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" expr="$3"
  restore_all
  "$PY" - "${ROOT}/${file}" "$expr" <<'PYEOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  if mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=$((survived + 1))
  else
    echo "killed   $name"
    killed=$((killed + 1))
  fi
}

echo "--- THE LATCH: a failed probe must never stamp imported_from_json_at ---"

mutate "the failed-probe gate is removed, so the latch stamps unconditionally" \
  "src/core/session_import.py" \
  '    if not listing.ok:||=>||    if False:'

mutate "the failed-probe branch stamps the latch before returning" \
  "src/core/session_import.py" \
  '        logger.warning(
            "session_import_pending_listing_unavailable",||=>||        set_meta(conn, META_IMPORTED_FROM_JSON_AT, stamp)
        logger.warning(
            "session_import_pending_listing_unavailable",'

mutate "a pending import reports itself as completed" \
  "src/core/session_import.py" \
  '            outcome=IMPORT_PENDING_LISTING_UNAVAILABLE,
            sessions_imported=0,||=>||            outcome=IMPORT_COMPLETED,
            sessions_imported=0,'

mutate "project_store starts stamping the latch again" \
  "src/core/project_store.py" \
  '    return ImportResult(imported=imported, dropped=dropped)||=>||    set_meta(conn, "imported_from_json_at", stamp)
    return ImportResult(imported=imported, dropped=dropped)'

mutate "the once-only guard never fires, so the import runs every boot" \
  "src/core/session_import.py" \
  '    return bool(_load_result_blob(conn).get(RESULT_KEY_SESSIONS_STAGE))||=>||    return False'

echo "--- IDENTITY: the tmux instance, not the name ---"

mutate "the epoch is dropped from the unique index" \
  "src/core/db_models.py" \
  '    "ON sessions (tmux_socket, tmux_name, tmux_created_epoch) "||=>||    "ON sessions (tmux_socket, tmux_name) "'

mutate "instance lookup drops the epoch and matches on the name alone" \
  "src/core/session_store.py" \
  '        "SELECT * FROM sessions WHERE tmux_socket = ? AND tmux_name = ? "
        "AND tmux_created_epoch = ?",
        (socket, name, int(epoch)),||=>||        "SELECT * FROM sessions WHERE tmux_socket = ? AND tmux_name = ?",
        (socket, name),'

mutate "the same-second collision is merged instead of refused" \
  "src/core/session_reconcile.py" \
  '    if existing.get("lifecycle") == SESSION_LIFECYCLE_STOPPED:||=>||    if False:'

mutate "the refusal is silent (log event renamed, nothing else changes)" \
  "src/core/session_reconcile.py" \
  '            "session_instance_epoch_collision_refused",||=>||            "session_instance_merged",'

mutate "adopted_at becomes LAST-write-wins" \
  "src/core/session_identity.py" \
  '        "adopted_at = COALESCE(adopted_at, ?)",||=>||        "adopted_at = ?",'

# The instance tier moved out of tmux_backend and into
# tmux_listing_parse.resolve_ownership, so it could be unit-tested against
# a hostile tmux row without shelling out. The mutation follows it.
mutate "the ownership badge ignores the instance tier" \
  "src/core/tmux_listing_parse.py" \
  '    if owned_instances is not None:
        if (name, created_at_epoch) in owned_instances:||=>||    if False:
        if (name, created_at_epoch) in owned_instances:'

mutate "only 'created' badges as ours, so adoption never sticks" \
  "src/core/db_models.py" \
  'SESSION_OWNED_ORIGINS: Tuple[str, ...] = (
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_ADOPTED,
)||=>||SESSION_OWNED_ORIGINS: Tuple[str, ...] = (
    SESSION_ORIGIN_CREATED,
)'

mutate "the import invents an adoption it has no evidence for" \
  "src/core/session_store.py" \
  '        else SESSION_ORIGIN_OBSERVED
    )||=>||        else SESSION_ORIGIN_ADOPTED
    )'

echo "--- THREE-OUTCOME COLLAPSES ---"

# TARGET MOVED AT S7, MUTANT PRESERVED. The rule left
# session_import_mapping.py for src/core/project_attribution.py when the
# adopt path became a second caller. The mutant below is the SAME
# semantic collapse - could-not-read reported as belongs-to-nothing - at
# the line that now decides it. Re-pointing it rather than deleting it
# keeps the count honest: a mutant whose target moves is SURVIVED, not
# skipped, and the fix is to aim it at the code that took over the job.
mutate "an unprobeable working dir is reported as 'no project'" \
  "src/core/project_attribution.py" \
  '    normalized = normalize_path_for_match(working_dir)
    if normalized is None:
        return None, SESSION_ATTRIBUTION_UNKNOWN||=>||    normalized = normalize_path_for_match(working_dir)
    if normalized is None:
        return None, SESSION_ATTRIBUTION_NONE'

mutate "NEEDS ATTENTION requires BOTH failures, so single ones vanish" \
  "src/core/session_store.py" \
  '        "SELECT * FROM sessions WHERE lifecycle = ? OR project_attribution = ? "||=>||        "SELECT * FROM sessions WHERE lifecycle = ? AND project_attribution = ? "'

mutate "archiving hides a RUNNING session" \
  "src/core/session_store.py" \
  '    if not include_archived:
        clauses.append("archived_at IS NULL")||=>||    if True:
        clauses.append("archived_at IS NULL")'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
