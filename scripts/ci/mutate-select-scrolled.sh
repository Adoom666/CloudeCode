#!/bin/bash
# Mutation check for tests/test_terminal_select_scrolled.node.mjs.
#
# This suite exists because the ordinary kind of green was worth nothing
# here. The selection-while-scrolled fix shipped THREE times with a passing
# test suite and a completely broken feature, because the tests exercised a
# recorder instead of a browser and could not express the failure at all.
#
# So every mutation below reintroduces one of the three defects that were
# actually measured in a live fullscreen claude session on 2026-08-19, or
# breaks one of the behaviours that had to survive the fix. Each one must
# turn the suite red. A SURVIVED line means the suite is once again capable
# of passing over the real bug, which is the exact failure mode this whole
# exercise is about.
#
# The mutated file is restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
SEL="$ROOT/client/js/terminal-select-scrolled.js"
TERM_JS="$ROOT/client/js/terminal.js"
HTML="$ROOT/client/index.html"
SUITE="$ROOT/tests/test_terminal_select_scrolled.node.mjs"
mutate_arm_trap "$ROOT" "$SEL" "$TERM_JS" "$HTML"

survived=0
cannot_determine=0

restore_all() {
  mutate_restore_files
}

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" py="$3"
  restore_all
  python3 - "$file" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "CANNOT_DETERMINE $name (target moved)"; cannot_determine=1; return; fi
  if mutate_run node "$SUITE" >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=1
  else
    echo "killed    $name"
  fi
}

# --- DEFECT 1: re-entrancy. 44 synthetic events, none reaching xterm ----
mutate "the re-entrancy guard is removed entirely" "$SEL" \
  "        if (dispatching) return;
||=>||"

mutate "the guard is never raised, so it can never block re-entry" "$SEL" \
  "        dispatching = true;
||=>||"

mutate "the guard is raised and never lowered, swallowing every later click" "$SEL" \
  "            dispatching = false;
||=>||"

# --- DEFECT 2: the mouseup is reported and clears the selection ---------
mutate "the mouseup is no longer withheld from xterm" "$SEL" \
  "        ev.stopPropagation();
        document.dispatchEvent(new MouseEvent('mouseup', {||=>||        document.dispatchEvent(new MouseEvent('mouseup', {"

mutate "the mouseup is withheld but never replayed to SelectionService" "$SEL" \
  "        document.dispatchEvent(new MouseEvent('mouseup', {||=>||        if (false) document.dispatchEvent(new MouseEvent('mouseup', {"

mutate "the gesture is never marked in flight, so the up is not recognised" "$SEL" \
  "        forcedGesture = true;
||=>||"

mutate "the gesture flag is never cleared, so later ups are hijacked" "$SEL" \
  "        forcedGesture = false;
        if (!ev) return;||=>||        if (!ev) return;"

# --- DEFECT 3: post-release motion clears the finished selection --------
mutate "motion is never withheld, so the first pointer move kills it" "$SEL" \
  "        if (!has) return;
        if (!isScrolledUp(term)) return;
        ev.stopPropagation();||=>||        if (!has) return;
        if (!isScrolledUp(term)) return;"

mutate "motion is withheld unconditionally, breaking live hover and drags" "$SEL" \
  "    function handleMouseMove(term, ev) {
        if (forcedGesture) return;||=>||    function handleMouseMove(term, ev) {
        ev.stopPropagation();
        if (forcedGesture) return;"

mutate "the mid-drag exemption is dropped, so the drag cannot extend" "$SEL" \
  "        if (forcedGesture) return;
        if (!term || !ev) return;
        if (!areMouseEventsActive(term)) return;||=>||        if (!term || !ev) return;
        if (!areMouseEventsActive(term)) return;"

mutate "the has-selection gate is dropped, suppressing hover with nothing to protect" "$SEL" \
  "        if (!has) return;
        if (!isScrolledUp(term)) return;||=>||        if (!isScrolledUp(term)) return;"

mutate "the scrolled gate is dropped, so live vim and htop lose their motion" "$SEL" \
  "        if (!isScrolledUp(term)) return;
        ev.stopPropagation();||=>||        ev.stopPropagation();"

# --- the original gate must still hold ---------------------------------
mutate "the alternate-screen transcript case stops counting as scrolled" "$SEL" \
  "return as.detectState(term) === 'transcript';||=>||return false;"

mutate "everything counts as scrolled, so live apps lose their clicks" "$SEL" \
  "        if (!isScrolledUp(term)) return;         // live bottom: leave the app in control||=>||        if (false) return;"

mutate "the mac force-selection option is left permanently flipped on" "$SEL" \
  "            opts.macOptionClickForcesSelection = prevForce;||=>||"

# --- the wiring: the module is correct but nobody calls it -------------
mutate "the mouseup listener is never wired" "$SEL" \
  "        container.addEventListener('mouseup', function (ev) {
            handleMouseUp(ev);
        }, { capture: true });
||=>||"

mutate "the mousemove listener is never wired" "$SEL" \
  "        container.addEventListener('mousemove', function (ev) {
            handleMouseMove(termGetter(), ev);
        }, { capture: true });
||=>||"

mutate "the listeners are wired in the bubble phase instead of capture" "$SEL" \
  "        container.addEventListener('mousedown', function (ev) {
            handleMouseDown(termGetter(), ev);
        }, { capture: true });||=>||        container.addEventListener('mousedown', function (ev) {
            handleMouseDown(termGetter(), ev);
        }, { capture: false });"

mutate "terminal.js stops wiring the module at all" "$TERM_JS" \
  "if (window.TerminalSelectScrolled) window.TerminalSelectScrolled.init(||=>||if (false) window.TerminalSelectScrolled.init("

mutate "terminal-select-scrolled.js is not loaded" "$HTML" \
  '<script src="/static/js/terminal-select-scrolled.js"></script>||=>||'

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
