#!/bin/bash
# Mutation check for tests/test_terminal_input_kind.node.mjs.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one form of the bug the user reported - moving the
# mouse jumping the view to the bottom - or breaks the behaviour that must
# survive the fix (typing still goes back to live, the bytes still reach
# the session). Every one must turn the suite red. Both mutated files are
# restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
KIND="$ROOT/client/js/terminal-input-kind.js"
TERM_JS="$ROOT/client/js/terminal.js"
HTML="$ROOT/client/index.html"
mutate_arm_trap "$ROOT" "$KIND" "$TERM_JS" "$HTML"

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
    sys.exit('mutation target not found: ' + old[:60])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "CANNOT_DETERMINE $name (target moved)"; cannot_determine=1; return; fi
  if mutate_run node "$ROOT/tests/test_terminal_input_kind.node.mjs" >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=1
  else
    echo "killed    $name"
  fi
}

# --- the bug itself: nothing is ever recognised as a mouse report -------
mutate "no payload is ever a mouse report" "$KIND" \
  "if (typeof data !== 'string') return false;||=>||if (true) return false;"

mutate "SGR motion reports stop matching" "$KIND" \
  "/^\x1b\[<\d{1,5};\d{1,5};\d{1,5}[Mm]\$/||=>||/^\x1b\[<NEVER\$/"

mutate "the release form is dropped so drags still re-pin" "$KIND" \
  "[Mm]\$/;||=>||[M]\$/;"

mutate "X10 form stops matching" "$KIND" \
  "/^\x1b\[M[\s\S]{3}\$/||=>||/^\x1b\[MNEVER\$/"

# --- the opposite failure: typing stops taking the user back to live ----
mutate "everything is treated as a mouse report" "$KIND" \
  "return SGR_MOUSE_REPORT.test(data) || X10_MOUSE_REPORT.test(data);||=>||return true;"

mutate "the anchors are dropped so typing around a report matches" "$KIND" \
  "var SGR_MOUSE_REPORT = /^\x1b\[<||=>||var SGR_MOUSE_REPORT = /\x1b\[<"

# --- the call site: the module is right but nobody asks ----------------
mutate "pinToBottom guard removed from onData" "$TERM_JS" \
  "if (window.TerminalScroll && !isMouse) window.TerminalScroll.pinToBottom(this.term);||=>||if (window.TerminalScroll) window.TerminalScroll.pinToBottom(this.term);"

mutate "mouse motion arms the altscreen typing quiet period again" "$TERM_JS" \
  "if (window.AltScreenScroll && !isMouse) window.AltScreenScroll.noteUserInput();||=>||if (window.AltScreenScroll) window.AltScreenScroll.noteUserInput();"

mutate "onData stops classifying at all" "$TERM_JS" \
  "window.TerminalInputKind.isMouseReport(data)||=>||false"

mutate "mouse reports are swallowed instead of forwarded" "$TERM_JS" \
  "this.ws.send(new TextEncoder().encode(data));||=>||void 0;"

# --- load order: the module exists but is not on the page --------------
mutate "terminal-input-kind.js is not loaded" "$HTML" \
  '<script src="/static/js/terminal-input-kind.js"></script>||=>||'

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
