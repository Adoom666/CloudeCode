#!/bin/bash
# Mutation check for the upgrade-aware config merge (src/core/config_merge.py).
#
# The defect class: an upgrade that either destroys the user's customisations
# or silently withholds every new default. Both look like success from the
# outside, which is why the mutations below are about COLLAPSING the three
# cases into each other rather than about crashing anything.
#
# The sharpest ones reintroduce a silent conflict resolution (adopting the
# upstream value behind his back) and the guess a missing base makes possible
# (classifying a field as customised when there is no evidence either way).
#
# Oracle: tests/test_config_merge.py under the project venv, which is the only
# interpreter here that can import the package.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

PY="$ROOT/venv/bin/python3"
SUITE="$ROOT/tests/test_config_merge.py"
OUT=/tmp/mutate-config-merge-out.log
FILES=("src/core/config_merge.py")
mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0; cannot_determine=0; killed=0

if [ ! -x "$PY" ]; then
  echo "CANNOT DETERMINE: no venv interpreter at $PY. Refusing to run."
  exit 2
fi

run_check() { mutate_run "$PY" -m pytest "$SUITE" -q >"$OUT" 2>&1; return $?; }

echo "--- baseline ---"
run_check
if [ $? -ne 0 ]; then echo "BASELINE IS RED. Refusing to run."; tail -20 "$OUT"; exit 2; fi
echo "baseline green"

mutate() {
  local name="$1" file="$2" expr="$3"
  mutate_restore_files
  python3 - "${ROOT}/${file}" "$expr" <<'PYEOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (anchor stale)"; cannot_determine=$((cannot_determine+1)); return
  fi
  run_check
  local status=$?
  if [ "$status" -eq 1 ]; then killed=$((killed+1)); echo "killed   $name"; return; fi
  if [ "$status" -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (exit $status)"; cannot_determine=$((cannot_determine+1)); return
  fi
  echo "SURVIVED $name"; survived=$((survived+1))
}

echo "--- collapsing the three cases ---"

mutate "a conflict silently adopts the upstream value" \
  "src/core/config_merge.py" \
  "        outcome=CONFLICT,
        mine=mine,
        theirs=theirs,
        base=base,
        chosen=mine,||=>||        outcome=CONFLICT,
        mine=mine,
        theirs=theirs,
        base=base,
        chosen=theirs,"

mutate "a conflict is downgraded to keeping his value with no report" \
  "src/core/config_merge.py" \
  "NEEDS_ATTENTION = frozenset({CONFLICT, REMOVED_UPSTREAM, CANNOT_DETERMINE})||=>||NEEDS_ATTENTION = frozenset({REMOVED_UPSTREAM, CANNOT_DETERMINE})"

mutate "a customised field is overwritten by the shipped default" \
  "src/core/config_merge.py" \
  "            outcome=KEPT_CUSTOM,
            mine=mine,
            theirs=theirs,
            base=base,
            chosen=mine,||=>||            outcome=KEPT_CUSTOM,
            mine=mine,
            theirs=theirs,
            base=base,
            chosen=theirs,"

mutate "an untouched field never receives the new default" \
  "src/core/config_merge.py" \
  "            outcome=UPDATED_DEFAULT,
            mine=mine,
            theirs=theirs,
            base=base,
            chosen=theirs,||=>||            outcome=UPDATED_DEFAULT,
            mine=mine,
            theirs=theirs,
            base=base,
            chosen=mine,"

mutate "user-changed and default-changed are no longer distinguished" \
  "src/core/config_merge.py" \
  "    default_changed = theirs != base||=>||    default_changed = False"

echo "--- guessing without a base ---"

mutate "a missing base is treated as if a base existed" \
  "src/core/config_merge.py" \
  "    if not had_base or base is MISSING:||=>||    if False:"

mutate "cannot-determine is not reported to the user" \
  "src/core/config_merge.py" \
  "            outcome=CANNOT_DETERMINE,
            mine=mine,
            theirs=theirs,
            base=None,
            chosen=mine,||=>||            outcome=UNCHANGED,
            mine=mine,
            theirs=theirs,
            base=None,
            chosen=mine,"

mutate "an ambiguous field adopts the upstream value" \
  "src/core/config_merge.py" \
  "            outcome=CANNOT_DETERMINE,
            mine=mine,
            theirs=theirs,
            base=None,
            chosen=mine,||=>||            outcome=CANNOT_DETERMINE,
            mine=mine,
            theirs=theirs,
            base=None,
            chosen=theirs,"

echo "--- additions, removals and lists ---"

mutate "a setting dropped upstream is deleted from his config" \
  "src/core/config_merge.py" \
  "            outcome=REMOVED_UPSTREAM,
            mine=mine,
            theirs=None,
            base=None if base is MISSING else base,
            chosen=mine,||=>||            outcome=REMOVED_UPSTREAM,
            mine=mine,
            theirs=None,
            base=None if base is MISSING else base,
            chosen=None,"

mutate "a brand new upstream setting never arrives" \
  "src/core/config_merge.py" \
  "            outcome=ADDED,
            mine=None,
            theirs=theirs,
            base=None if base is MISSING else base,
            chosen=theirs,||=>||            outcome=ADDED,
            mine=None,
            theirs=theirs,
            base=None if base is MISSING else base,
            chosen=None,"

mutate "new upstream list items are applied without being asked" \
  "src/core/config_merge.py" \
  "        return decision.chosen||=>||        if isinstance(mine, list) and isinstance(theirs, list):
            return mine + [i for i in theirs if i not in mine]
        return decision.chosen"

mutate "importing rewrites his list order instead of appending" \
  "src/core/config_merge.py" \
  "    for item in items:
        if item not in target:
            target.append(item)||=>||    cursor[parts[-1]] = list(items) + [i for i in target if i not in items]"

mutate "importing duplicates items already present" \
  "src/core/config_merge.py" \
  "        if item not in target:
            target.append(item)||=>||        target.append(item)"

mutate_restore_files
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"; exit 1
fi
echo "MUTATION CHECK PASSED"
