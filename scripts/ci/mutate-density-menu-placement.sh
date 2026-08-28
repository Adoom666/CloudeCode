#!/bin/bash
# Mutation check for fix/density-menu-placement: the density menu's
# containing-block fix (session-sidebar.css .session-sidebar-header,
# session-sidebar-density.css .session-sidebar-density-wrap and
# .session-sidebar-density-menu).
#
# This bug shipped past 282+ green node-suite assertions because none of
# them opened the menu in a real browser and measured its box - the
# defect was invisible to anything that reads state or DOM text rather
# than painted pixels. scripts/ci/mutate-sidebar-sessions.sh already
# covers this feature's JS with the node suites; it cannot kill THIS
# class of mutant because a CSS containing-block change alters no JS
# text a node test reads. So the oracle here is
# scripts/verify_sidebar_sessions.py itself, run in a REAL Chromium via
# Playwright - the same check that caught the original bug (see its
# ITEM 47b, measure_density_menu_placement).
#
# Interpreter: the brew python3 with playwright installed
# (/opt/homebrew/bin/python3), NOT the project's ./venv - that venv has
# no playwright, and scripts/verify_sidebar_sessions.py's own contract
# is that a missing playwright is exit 2, CANNOT DETERMINE, never a
# pass. A mutation run against exit-2 baseline would have every mutant
# read as killed for free, which is exactly what the baseline gate below
# refuses.
#
# Modelled on scripts/ci/mutate-sidebar-sessions.sh - same restore-on-exit
# discipline via lib/mutate-trap.sh, same baseline gate, same
# mutate()/run_suites() shape.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

PY="/opt/homebrew/bin/python3"
VERIFY="$ROOT/scripts/verify_sidebar_sessions.py"

FILES=(
  "client/css/session-sidebar.css"
  "client/css/session-sidebar-density.css"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# Runs the real Playwright geometry check. Exit 0 = every assertion held
# (survived). Exit 1 = something measured wrong (killed - what we want
# for every mutation below). Exit 2 = could not evaluate at all
# (playwright/chromium missing) - never counted as a pass or a kill.
run_check() {
  mutate_run "$PY" "$VERIFY" >/tmp/mutate-density-menu-out.log 2>&1
  return $?
}

echo "--- baseline: the real check must be GREEN before anything is mutated ---"
run_check
baseline_status=$?
if [ "$baseline_status" -eq 2 ]; then
  echo "CANNOT DETERMINE: playwright/chromium unavailable to $PY. Refusing to run."
  tail -20 /tmp/mutate-density-menu-out.log
  exit 2
fi
if [ "$baseline_status" -ne 0 ]; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  tail -20 /tmp/mutate-density-menu-out.log
  exit 2
fi
echo "baseline green"

restore_all() {
  mutate_restore_files
}

# Apply one textual mutation, run the real check, expect exit 1 (killed).
#   mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" expr="$3"
  restore_all
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
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  run_check
  local status=$?
  if [ "$status" -eq 1 ]; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  if [ "$status" -eq 2 ]; then
    echo "CANNOT_DETERMINE $name (check could not evaluate the mutant)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- the containing-block fix ---"

# NOTE ON AN EXCLUDED MUTATION: "remove position:relative from
# .session-sidebar-header alone" is not included here. It is an
# EQUIVALENT mutant - .session-sidebar-panel (session-sidebar.css) is
# ALSO position:fixed and occupies the exact same box as the header (the
# header has no margin and stretches to the panel's full width as a
# flex-column child), so the menu's containing block resolves to the
# panel instead and nothing observable changes. Forcing an equivalent
# mutant to "die" would mean weakening the test to detect a difference
# that produces no actual bug, which is the wrong direction to fix a
# survivor in. The comment on that `position: relative` declaration
# names this explicitly so the next person does not read its survival
# as a gap.

# Apply two edits (one per file) as ONE mutation, then run the real
# check once. The generic single-file `mutate()` cannot express this:
# it restores every armed file before each call, so two sequential
# mutate() calls can never be observed together. This is needed here
# because reproducing the ORIGINAL bug precisely requires both halves at
# once - the wrap regaining position:relative (re-narrowing the
# containing block to the 28px button) AND the menu regaining its old
# `right: 0` (no left, no max-width). Doing only the density.css half on
# top of the still-fixed header is not the original bug: with the header
# still spanning the panel, `right: 0` alone is just "anchored to the
# other edge of the panel", which is still fully on-panel - a validly
# different placement, not a regression. Tried first and found to
# SURVIVE for exactly this reason before being replaced with this
# two-file version.
mutate2() {
  local name="$1" f1="$2" e1="$3" f2="$4" e2="$5"
  restore_all
  python3 - "${ROOT}/${f1}" "$e1" "${ROOT}/${f2}" "$e2" <<'PYEOF'
import sys
p1, e1, p2, e2 = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
for path, expr in ((p1, e1), (p2, e2)):
    text = open(path, encoding='utf-8').read()
    old, new = expr.split('||=>||')
    if old not in text:
        sys.exit('mutation target not found in ' + path + ': ' + old[:70])
    open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  run_check
  local status=$?
  if [ "$status" -eq 1 ]; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  if [ "$status" -eq 2 ]; then
    echo "CANNOT_DETERMINE $name (check could not evaluate the mutant)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

mutate2 "full revert: wrap regains position:relative AND menu regains the original wrap-relative right:0 - the exact pre-fix bug" \
  "client/css/session-sidebar-density.css" \
  "    margin-left: auto;
    display: inline-flex;
}

.session-sidebar-density {||=>||    position: relative;
    margin-left: auto;
    display: inline-flex;
}

.session-sidebar-density {" \
  "client/css/session-sidebar-density.css" \
  "    left: 16px;
    right: 16px;
    max-width: 210px;||=>||    right: 0;"

mutate "the wrap keeps position:relative alone - containing block re-narrows to the 28px button and the menu collapses to a sliver" \
  "client/css/session-sidebar-density.css" \
  "    margin-left: auto;
    display: inline-flex;
}

.session-sidebar-density {||=>||    position: relative;
    margin-left: auto;
    display: inline-flex;
}

.session-sidebar-density {"

mutate "left inset pushed past the panel's own left edge" \
  "client/css/session-sidebar-density.css" \
  "    left: 16px;||=>||    left: -50px;"

mutate "right inset pushed past the panel's own right edge - only observable once the panel is narrower than the menu's design width" \
  "client/css/session-sidebar-density.css" \
  "    right: 16px;||=>||    right: -100px;"

mutate "max-width removed - a fixed wide menu overflows the panel on the 85vw-narrowed viewport" \
  "client/css/session-sidebar-density.css" \
  "    max-width: 210px;||=>||    width: 500px;"

restore_all
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
