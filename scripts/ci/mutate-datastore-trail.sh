#!/bin/bash
# Mutation check for the cloude.db datastore and its migration trail.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific false green this subsystem exists to
# prevent. Every one must turn the suite red.
#
# The first block is the one the brief calls out by name: EVERY
# backup_verified gate, flipped to unconditionally true. If a migration
# can proceed on an unverified backup and no test notices, the safety net
# is decorative.
#
# The second block covers the other three-outcome collapses: an unreadable
# trail read as a fresh install, a truncated tail read as corruption, a
# missing database re-created by the probe that was supposed to report it,
# a rollback that commits, and VACUUM INTO downgraded to a byte copy.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_db_migration.py tests/test_db_degraded_states.py tests/test_migration_trail.py \
tests/test_db_backup_wal.py tests/test_datastore_version_route.py"

FILES=(
  "src/core/db_backup.py"
  "src/core/db_migration.py"
  "src/core/db_health.py"
  "src/core/db.py"
  "src/core/trail_reader.py"
  "src/core/migration_trail.py"
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

echo "--- backup_verified gates: every one flipped to unconditionally true ---"

mutate "verify_backup always returns verified" "src/core/db_backup.py" \
  '    path = Path(path)
    if not path.exists():||=>||    return True, None
    path = Path(path)
    if not path.exists():'

mutate "verify_backup skips integrity_check" "src/core/db_backup.py" \
  '            if verdict != "ok":||=>||            if False:'

mutate "verify_backup skips the from_version match" "src/core/db_backup.py" \
  '    if found != expect_version:||=>||    if False:'

mutate "take_backup returns verified even when verification failed" "src/core/db_backup.py" \
  '    verified, reason = verify_backup(target, from_version)
    if not verified:||=>||    verified, reason = verify_backup(target, from_version)
    if False:'

mutate "trail records backup_verified=1 unconditionally" "src/core/db_migration.py" \
  '            backup_verified = 1 if result.verified else 0||=>||            backup_verified = 1'

mutate "migration proceeds on an unverified backup" "src/core/db_migration.py" \
  '            if not result.verified:
                # backup_verified||=>||            if False:
                # backup_verified'

echo "--- the other three-outcome collapses ---"

mutate "an unreadable trail is read as a fresh install" "src/core/trail_reader.py" \
  '            status=TRAIL_READ_UNREADABLE,
            entries=entries,
            corrupt_line_no=line_no,
            error=f"{path.name} is corrupt at line {line_no}",||=>||            status=TRAIL_READ_ABSENT,
            entries=[],
            corrupt_line_no=None,
            error=None,'

mutate "a truncated tail is collapsed into unreadable" "src/core/trail_reader.py" \
  '                    status=TRAIL_READ_TRUNCATED_TAIL,||=>||                    status=TRAIL_READ_UNREADABLE,'

mutate "the health probe creates the database it cannot find" "src/core/db_health.py" \
  '        with closing(connect(db_path, create=False)) as conn:||=>||        with closing(connect(db_path, create=True)) as conn:'

mutate "a failed migration commits instead of rolling back" "src/core/db.py" \
  '            conn.execute("ROLLBACK")||=>||            conn.execute("COMMIT")'

mutate "an interrupted step is closed as completed" "src/core/migration_trail.py" \
  '            TRAIL_STATUS_INTERRUPTED,||=>||            "completed",'

mutate "a schema version ahead of this code is ignored" "src/core/db_migration.py" \
  '        if current > CURRENT_SCHEMA_VERSION:||=>||        if current > 9999:'

mutate "VACUUM INTO downgraded to a plain byte copy" "src/core/db_backup.py" \
  '            conn.execute("VACUUM INTO ?", (str(target),))||=>||            import shutil as _s; _s.copyfile(str(db_path), str(target))'

mutate "the mirror table is trusted over the authoritative file" "src/core/db_migration.py" \
  '    for entry in read.entries:
        latest[entry.entry_uuid] = entry||=>||    for entry in []:
        latest[entry.entry_uuid] = entry'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
