#!/bin/bash
# Mutation check for the projects table's four-state presence model.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific false green this subsystem exists to
# prevent - starting with the one the brief calls out by name: collapsing
# 'unreachable' into 'missing', which is the sleeping-external-drive bug
# stated as the crux of this whole build step. Every mutation below must
# turn the suite red.
#
# All mutated files are restored on exit, including on failure. Modelled
# on scripts/ci/mutate-datastore-trail.sh - same harness shape, same
# restore-on-exit discipline.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_project_presence.py tests/test_project_store.py tests/test_projects_presence_route.py"

FILES=(
  "src/core/project_presence.py"
  "src/core/project_store.py"
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

echo "--- THE mutation: unreachable collapsed into missing ---"

mutate "every unreachable errno reported as missing instead" "src/core/project_presence.py" \
  '        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE, f"{name}: {message}", checked_at
        )||=>||        return PresenceResult(
            PROJECT_PRESENCE_MISSING, f"{name}: {message}", checked_at
        )'

mutate "a stat timeout reported as missing instead of unreachable" "src/core/project_presence.py" \
  '        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE, f"TIMEOUT: {exc}", checked_at
        )||=>||        return PresenceResult(
            PROJECT_PRESENCE_MISSING, f"TIMEOUT: {exc}", checked_at
        )'

mutate "a non-directory root reported as missing instead of unreachable" "src/core/project_presence.py" \
  '        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE,
            "ENOTDIR: root exists but is not a directory",
            checked_at,
        )||=>||        return PresenceResult(
            PROJECT_PRESENCE_MISSING,
            "ENOTDIR: root exists but is not a directory",
            checked_at,
        )'

mutate "ENOENT widened to also swallow every other errno as missing" "src/core/project_presence.py" \
  '        if code == errno.ENOENT:||=>||        if True:'

echo "--- other collapses in the same subsystem ---"

mutate "an unclassified exception reported as unreachable is silently ok" "src/core/project_presence.py" \
  '        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE,
            f"{type(exc).__name__}: {exc}",
            checked_at,
        )||=>||        return PresenceResult(
            PROJECT_PRESENCE_PRESENT,
            None,
            checked_at,
        )'

mutate "normalize_root calls resolve(), rewriting a symlinked root" "src/core/project_store.py" \
  '    return str(Path(raw_path).expanduser())||=>||    return str(Path(raw_path).expanduser().resolve())'

mutate "duplicate roots are inserted instead of recorded and dropped" "src/core/project_store.py" \
  '        root = normalize_root(cfg.path)
        if root in existing_roots:||=>||        root = normalize_root(cfg.path)
        if False:'

mutate "dropped duplicates are computed but never persisted to meta" "src/core/project_store.py" \
  '    if dropped:
        _record_import_result(
            conn, {"projects_duplicate_roots_dropped": dropped}
        )||=>||    if False:
        _record_import_result(
            conn, {"projects_duplicate_roots_dropped": dropped}
        )'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
