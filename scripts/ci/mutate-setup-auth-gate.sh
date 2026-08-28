#!/bin/bash
# Mutation check for the setup wizard's auth gate and the bind lockdown
# (src/core/setup_state.py, src/api/setup_routes.py).
#
# WHY THIS SCRIPT EXISTS AND NOT JUST THE TESTS
#
# The tests for this feature pass. So would tests written against a wizard
# with no gate at all, because the easy half of the property - "an
# unconfigured instance can load the wizard" - is satisfied by a gate that is
# broken wide open. A green suite is therefore not evidence that the gate
# works; it is evidence that nothing in the suite noticed.
#
# Each mutation below is a specific, plausible way somebody could break this
# in a future edit, and every one of them is a remote takeover if it ships.
# The suite must turn red on each. A SURVIVED line here means the tests are
# decorative for that mutation, whatever their pass count says.
#
# Both source files are restored on every exit path, signals included, via
# scripts/ci/lib/mutate-trap.sh - see CLAUDE.md's hazard about a killed
# mutation run leaving a mutant in the tree and manufacturing a false finding.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
STATE_SRC="$ROOT/src/core/setup_state.py"
ROUTES_SRC="$ROOT/src/api/setup_routes.py"
TESTS_EXPOSURE="$ROOT/tests/test_setup_wizard_exposure.py"
TESTS_HTTP="$ROOT/tests/test_setup_wizard_auth_http.py"
PYTEST="$ROOT/venv/bin/python3"
mutate_arm_trap "$ROOT" "$STATE_SRC" "$ROUTES_SRC"

survived=0
cannot_determine=0
killed=0
total=0

restore() {
  mutate_restore_files
}

# Baseline gate. A kill count measured against a red baseline says nothing.
restore
if ! mutate_run "$PYTEST" -m pytest "$TESTS_EXPOSURE" "$TESTS_HTTP" >/dev/null 2>&1; then
  echo "BASELINE FAILED: the setup wizard tests do not pass against unmutated source."
  echo "MUTATION CHECK ABORTED (baseline must be green first)"
  exit 1
fi
echo "baseline green"

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" py="$3"
  total=$((total + 1))
  restore
  python3 - "$file" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
n = text.count(old)
if n == 0:
    sys.exit('mutation target not found: ' + old[:80])
if n > 1:
    sys.exit('mutation target ambiguous (%d occurrences): %s' % (n, old[:80]))
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (target moved or ambiguous - mutant not evaluated)"
    cannot_determine=1
    return
  fi
  if mutate_run "$PYTEST" -m pytest "$TESTS_EXPOSURE" "$TESTS_HTTP" >/dev/null 2>&1; then
    echo "SURVIVED  $name"
    survived=1
  else
    echo "killed    $name"
    killed=$((killed + 1))
  fi
}

# --- the gate itself, broken open ----------------------------------------
mutate "wizard auth gate always allows (the takeover)" "$ROUTES_SRC" \
  '    if not exposure.wizard_requires_auth:||=>||    if True:'

mutate "gate delegates to nothing instead of require_auth" "$ROUTES_SRC" \
  '    return await require_auth(credentials)||=>||    return True'

mutate "completed setup no longer demands auth" "$STATE_SRC" \
  '            locked_down=False,
            wizard_requires_auth=True,||=>||            locked_down=False,
            wizard_requires_auth=False,'

# --- the state that drives the gate --------------------------------------
mutate "undetermined state counted as complete" "$STATE_SRC" \
  '        return self.status == SETUP_COMPLETE||=>||        return self.status != SETUP_INCOMPLETE'

mutate "pairing sentinel no longer required for completeness" "$STATE_SRC" \
  '        paired_ok = _sentinel_path(config_path).exists()||=>||        paired_ok = True'

# --- the bind lockdown ---------------------------------------------------
mutate "lockdown removed: incomplete setup binds the configured address" "$STATE_SRC" \
  '            bind_host=LOOPBACK_HOST,
            configured_bind_host=configured_bind_host,||=>||            bind_host=configured_bind_host,
            configured_bind_host=configured_bind_host,'

mutate "the invariant guard neutered" "$STATE_SRC" \
  '    if not exposure.wizard_requires_auth and exposure.bind_host != LOOPBACK_HOST:||=>||    if False:'

# --- honesty about what is actually bound --------------------------------
mutate "exposure reports the configured host as the effective one" "$ROUTES_SRC" \
  '            "effective_host": exposure.bind_host,||=>||            "effective_host": exposure.configured_bind_host,'

mutate "restart requirement silently suppressed" "$STATE_SRC" \
  '        return self.bind_host != self.configured_bind_host||=>||        return False'

# --- the three-outcome rule over the wire --------------------------------
mutate "could-not-evaluate flattened to a definite failure in the API" "$ROUTES_SRC" \
  '                    "passed": c.passed,||=>||                    "passed": bool(c.passed),'

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo ""
  echo "$killed/$total mutants killed"
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo ""
echo "$killed/$total mutants killed"
echo "MUTATION CHECK PASSED"
