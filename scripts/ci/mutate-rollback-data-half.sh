#!/bin/bash
# Mutation check for the DATA half of rollback (design section 9.8) and
# for the version surface's trail_status (9.7).
#
# A test that passes is only evidence if it can also FAIL. Every mutation
# below reintroduces one specific defect this branch exists to prevent,
# and each must turn the suite red.
#
# The mutations are grouped by the lie they tell:
#
#   1. THE SELECTION IS WRONG BUT LOOKS PRINCIPLED. Taking design 9.8's
#      sentence literally selects the entry that names the version rather
#      than the entry that holds a backup OF that version - one version
#      too far back, with a perfectly sensible-looking justification.
#   2. THE SCRIPT GUESSES. Falling back to the newest backup, accepting an
#      unverified one, or reading an unreadable trail as an absent one.
#      This is the whole failure the section closes.
#   3. THE FILE FORMAT IS ASSUMED RATHER THAN READ. Dropping the
#      coalescing of a step's two lines, or ordering by line position
#      instead of started_at.
#   4. THE CONFIRMATION STOPS BEING EVIDENCE. A static string cannot be
#      wrong about which backup is about to overwrite live data, which is
#      exactly why it must not be one.
#   5. THE THREE-OUTCOME COLLAPSES on the version surface.
#
# BASELINE-GREEN GATE: the suite is run unmutated first. A red baseline
# makes every "killed" below meaningless - the mutation would be taking
# credit for a failure it did not cause - so this aborts instead.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
PYTESTS="tests/test_rollback_trail_select.py tests/test_version_data_trail_paused.py"
SHTEST="tests/test_rollback_data_half.sh"

FILES=(
  "scripts/upgrade_lib/trail_select.py"
  "scripts/upgrade_lib/trail_records.py"
  "scripts/upgrade_lib/rollback_data.sh"
  "scripts/upgrade_lib/trail_code_entry.sh"
  "scripts/rollback.sh"
  "src/core/db_state.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

restore_all() {
    mutate_restore_files
}

# Description: run the whole suite for this branch, quietly.
# Inputs: none.
# Output: returns 0 when every test passes, non-zero otherwise.
run_suite() {
  mutate_run "$PY" -m pytest $PYTESTS -q -p no:randomly >/dev/null 2>&1 || return 1
  mutate_run bash "$SHTEST" >/dev/null 2>&1 || return 1
  return 0
}

echo "--- baseline gate: the suite must be GREEN before any mutation ---"
restore_all
if run_suite; then
  echo "baseline green"
else
  echo "BASELINE IS RED. Aborting: a mutation cannot be credited with a"
  echo "failure that was already there. Fix the suite first."
  exit 1
fi

survived=0
cannot_determine=0
killed=0

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
  if run_suite; then
    echo "SURVIVED $name"
    survived=$((survived + 1))
  else
    echo "killed   $name"
    killed=$((killed + 1))
  fi
}

echo ""
echo "--- 1. the selection is wrong but looks principled ---"

mutate "restores the entry that NAMES the version instead of one holding its backup" \
  "scripts/upgrade_lib/trail_select.py" \
  '    source = moved_away[0]||=>||    source = before[-1]'

mutate "takes the LAST move away from the version instead of the first" \
  "scripts/upgrade_lib/trail_select.py" \
  '    source = moved_away[0]||=>||    source = moved_away[-1]'

mutate "reads the version off the entry AFTER the target instead of before" \
  "scripts/upgrade_lib/trail_select.py" \
  '    version_at_target = before[-1].to_version||=>||    version_at_target = before[0].to_version'

mutate "bootstrap no longer establishes a schema version" \
  "scripts/upgrade_lib/trail_select.py" \
  'VERSION_ESTABLISHING = {KIND_SCHEMA: (KIND_SCHEMA, KIND_BOOTSTRAP),||=>||VERSION_ESTABLISHING = {KIND_SCHEMA: (KIND_SCHEMA,),'

echo ""
echo "--- 2. the script guesses ---"

mutate "falls back to the newest backup when the version cannot be matched" \
  "scripts/upgrade_lib/trail_select.py" \
  '    moved_away = [s for s in after if s.from_version == version_at_target]||=>||    moved_away = [s for s in after if s.from_version == version_at_target] or [s for s in of_kind if s.has_verified_backup][-1:]'

mutate "an unverified backup is used anyway" \
  "scripts/upgrade_lib/trail_records.py" \
  '        return bool(self.backup_path) and self.backup_verified in (1, True)||=>||        return bool(self.backup_path)'

mutate "an unreadable trail is read as absent" \
  "scripts/upgrade_lib/trail_records.py" \
  '''            return (
                READ_UNREADABLE,||=>||            return (
                READ_ABSENT,'''

mutate "the CLI reports unreadable with the absent exit code" \
  "scripts/upgrade_lib/trail_select.py" \
  '        return EXIT_UNREADABLE||=>||        return EXIT_ABSENT'

mutate "one unresolvable kind no longer refuses the whole rollback" \
  "scripts/upgrade_lib/trail_select.py" \
  '    if undecided:||=>||    if False:'

mutate "a missing target code entry is tolerated" \
  "scripts/upgrade_lib/trail_select.py" \
  '    if code_step is None:||=>||    if False and code_step is None:'

mutate "rollback.sh warns instead of refusing on an unreadable trail" \
  "scripts/rollback.sh" \
  '            die "REFUSING to roll back. Nothing has been stopped, checked out or copied. This script will not pick a backup||=>||            log_unknown "continuing anyway. Nothing has been stopped, checked out or copied. This script will not pick a backup'

mutate "rollback.sh treats an unknown selector exit code as success" \
  "scripts/rollback.sh" \
  '            die "the trail selector exited ${DATA_PLAN_RC}||=>||            log_unknown "the trail selector exited ${DATA_PLAN_RC}'

mutate "restore_one_artifact proceeds when the named backup is not on disk" \
  "scripts/upgrade_lib/rollback_data.sh" \
  '    if [ ! -f "${backup}" ]; then||=>||    if false; then'

echo ""
echo "--- 3. the file format is assumed rather than read ---"

mutate "steps are ordered by line position, not started_at" \
  "scripts/upgrade_lib/trail_records.py" \
  '    ordered = sorted(steps.values(), key=lambda s: (s.started_at, s.entry_uuid))||=>||    ordered = list(steps.values())'

mutate "the closing line's backup_path is never merged onto the step" \
  "scripts/upgrade_lib/trail_records.py" \
  '''            "from_version", "to_version", "completed_at", "backup_path",
            "backup_verified", "app_version",||=>||            "from_version", "to_version", "completed_at",
            "app_version",'''

mutate "trail_code_close mints a new started_at, breaking coalescing" \
  "scripts/upgrade_lib/trail_code_entry.sh" \
  '        "${started}" "${completed}" "${app_v}" "${error}" "")"||=>||        "${completed}" "${completed}" "${app_v}" "${error}" "")"'

mutate "trail_code_close always records completed" \
  "scripts/upgrade_lib/trail_code_entry.sh" \
  '    local status="$6" app_v="$7" error="$8"||=>||    local status="completed" app_v="$7" error="$8"'

mutate "the trail line is written without escaping quotes" \
  "scripts/upgrade_lib/trail_code_entry.sh" \
  '    s="${s//\"/\\\"}"||=>||    s="${s}"'

mutate "phase one is skipped: only the closing line is ever written" \
  "scripts/upgrade_lib/trail_code_entry.sh" \
  '    trail_append_line "${install_dir}" "${path}" "${line}" || return 1||=>||    true || return 1'

echo ""
echo "--- 4. the confirmation stops being evidence ---"

mutate "the confirmation becomes a static string" \
  "scripts/upgrade_lib/trail_select.py" \
  '''    lines = [
        f"Rolling back code to {plan['"'"'target_code_version'"'"']} (which this "||=>||    return "Restore? This cannot be undone. is discarded OVERWRITE live data"
    lines = [
        f"Rolling back code to {plan['"'"'target_code_version'"'"']} (which this "'''

mutate "the confirmation stops naming the loss" \
  "scripts/upgrade_lib/trail_select.py" \
  '''            lines.append(
                f"  Everything written to {item['"'"'artifact'"'"']} since "
                f"{item['"'"'backup_taken_at'"'"']} is discarded."
            )||=>||            pass'''

mutate "--code-only stops printing the mismatch" \
  "scripts/upgrade_lib/rollback_data.sh" \
  '    log_fail "--code-only: the DATA half of this rollback was SKIPPED"||=>||    :'

mutate "--code-only stops naming the schema version it left behind" \
  "scripts/upgrade_lib/rollback_data.sh" \
  '        log_fail "the trail says schema was at v${schema_at}||=>||        log_step "schema note v${schema_at}'

echo ""
echo "--- 5. three-outcome collapses on the version surface ---"

mutate "an intact-but-frozen trail reports ok" \
  "src/core/db_state.py" \
  '''        if self.migrations_paused:
            return TRAIL_STATUS_PAUSED||=>||        if False:
            return TRAIL_STATUS_PAUSED'''

mutate "paused hides an unreadable trail" \
  "src/core/db_state.py" \
  '''        if self.trail_status == TRAIL_STATUS_UNREADABLE:
            return TRAIL_STATUS_UNREADABLE||=>||        if False:
            return TRAIL_STATUS_UNREADABLE'''

mutate "restore is offered while the trail cannot be read" \
  "src/core/db_state.py" \
  '        return self.trail_status != TRAIL_STATUS_UNREADABLE||=>||        return True'

mutate "schema-ahead stops being read-only" \
  "src/core/db_state.py" \
  "$(printf '    STATUS_DEGRADED_SCHEMA_AHEAD,\n    STATUS_DEGRADED_MIGRATION_FAILED,||=>||    STATUS_DEGRADED_MIGRATION_FAILED,')"

echo ""
echo "=================================================="
echo "killed:   ${killed}"
echo "survived: ${survived}"
echo "cannot_determine: ${cannot_determine}"
echo "=================================================="
[ "${survived}" -eq 0 ] && [ "${cannot_determine}" -eq 0 ] || exit 1
