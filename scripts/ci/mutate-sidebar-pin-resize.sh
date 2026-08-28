#!/bin/bash
# Mutation check for the sidebar-pin -> tmux resize path:
#   client/js/terminal-layout.js   (requestFitAfterTransition)
#   client/js/session-sidebar-pin.js (the docked-state geometry sync call)
#   client/js/terminal.js          (sendResize's named no-op)
# Target test: tests/test_terminal_layout.node.mjs
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one form of the bug this feature guards against: the
# refit never firing, firing on the wrong CSS property, reporting the
# wrong reason, racing to more than one send on a rapid double-toggle, or
# terminal.js's sendResize going back to a silent no-op instead of a named
# one. Every one must turn the suite red. All three mutated files are
# restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAYOUT_JS="$ROOT/client/js/terminal-layout.js"
PIN_JS="$ROOT/client/js/session-sidebar-pin.js"
TERM_JS="$ROOT/client/js/terminal.js"
TEST="$ROOT/tests/test_terminal_layout.node.mjs"

BAK_LAYOUT="$(mktemp)"; BAK_PIN="$(mktemp)"; BAK_TERM="$(mktemp)"
cp "$LAYOUT_JS" "$BAK_LAYOUT"; cp "$PIN_JS" "$BAK_PIN"; cp "$TERM_JS" "$BAK_TERM"
trap 'cp "$BAK_LAYOUT" "$LAYOUT_JS"; cp "$BAK_PIN" "$PIN_JS"; cp "$BAK_TERM" "$TERM_JS";
      rm -f "$BAK_LAYOUT" "$BAK_PIN" "$BAK_TERM"' EXIT

survived=0
killed_count=0
total_count=0

restore_all() {
  cp "$BAK_LAYOUT" "$LAYOUT_JS"; cp "$BAK_PIN" "$PIN_JS"; cp "$BAK_TERM" "$TERM_JS"
}

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" py="$3"
  total_count=$((total_count + 1))
  restore_all
  python3 - "$file" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:80])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "SKIP $name (target moved)"; survived=1; return; fi
  if node "$TEST" >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=1
  else
    echo "killed    $name"
    killed_count=$((killed_count + 1))
  fi
}

# --- terminal-layout.js: requestFitAfterTransition ------------------------

mutate "missing-element fallback silently drops the refit instead of firing immediately" "$LAYOUT_JS" \
  "if (!el) { requestFit(reason); return; }||=>||if (!el) { return; }"

mutate "transitionend listener never wired - only the fallback ceiling can ever fire" "$LAYOUT_JS" \
  "el.addEventListener('transitionend', waiter.listener);||=>||void 0;"

mutate "propertyName check dropped - fires on ANY transition on the element, not just the one asked for" "$LAYOUT_JS" \
  "if (evt.target === el && evt.propertyName === propertyName) settle();||=>||if (evt.target === el) settle();"

mutate "settle() stops calling requestFit at all" "$LAYOUT_JS" \
  $'            requestFit(reason);\n        };||=>||        };'

mutate "settle() forwards no reason (log line becomes unattributable)" "$LAYOUT_JS" \
  $'            requestFit(reason);\n        };||=>||            requestFit();\n        };'

mutate "the fallback timer is never armed - a missed transitionend is lost forever" "$LAYOUT_JS" \
  "waiter.timer = setTimeout(settle, fallbackMs);||=>||void 0;"

mutate "requestFitAfterTransition is dropped from the public API" "$LAYOUT_JS" \
  $'        requestFitAfterTransition,\n        DEBOUNCE_MS,||=>||        DEBOUNCE_MS,'

# --- session-sidebar-pin.js: the geometry-sync call itself -----------------

mutate "docked-state change never triggers a geometry sync at all" "$PIN_JS" \
  "if (docked !== lastEffective) {||=>||if (false) {"

mutate "geometry sync fires on every apply(), even when nothing changed" "$PIN_JS" \
  "if (docked !== lastEffective) {||=>||if (true) {"

mutate "the wrong CSS property is watched (padding-left -> padding-top)" "$PIN_JS" \
  $'                    \'padding-left\',\n                    LAYOUT_SETTLE_MS,||=>||                    \'padding-top\',\n                    LAYOUT_SETTLE_MS,'

mutate "the refit is no longer attributed to the pin (reason string dropped)" "$PIN_JS" \
  "'sidebar-pin',||=>||'unknown',"

# --- terminal.js: sendResize's three-outcome contract -----------------------

mutate "no-session no-op goes back to a silent, unnamed return" "$TERM_JS" \
  "console.warn(\`[TERM-RESIZE] not delivered: no session attached, source=\${source}\`); return { delivered: false, reason: 'no-session' };||=>||return;"

mutate "the no-op is named but never actually observable (warn dropped)" "$TERM_JS" \
  "console.warn(\`[TERM-RESIZE] not delivered: no session attached, source=\${source}\`);||=>||void 0;"

mutate "a successful send stops reporting the shared named-outcome shape" "$TERM_JS" \
  "return { delivered: true, cols, rows };||=>||return;"

restore_all
echo
echo "MUTATION SUMMARY: ${killed_count}/${total_count} killed"
if [ "$survived" -eq 1 ]; then
  echo "MUTATION CHECK FAILED: at least one mutation survived (see SURVIVED/SKIP above)"
  exit 1
fi
echo "MUTATION CHECK PASSED: every mutation was killed"
exit 0
