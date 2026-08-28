#!/bin/bash
# Mutation check for macOS/terminal-launcher.js - the "Open Terminal Logs"
# tray item.
#
# WHY THIS SUITE EXISTS
#
# The original bug was invisible to every signal the project had. The old code
# ran `osascript -e 'tell application "Terminal" to do script "tail -f ..."'`,
# which exits 0, starts a real tail, and creates a real window. It just never
# RAISES Terminal, so the window is created behind everything and the user
# concludes nothing happened. Two orphaned `tail -f` processes with no visible
# window were found on the development machine. A test asserting "the click
# handler ran" passes against the broken version, because the broken version
# runs perfectly.
#
# So the oracle here is tests/test_terminal_launcher.node.mjs, which asserts
# the TEXT of the script that gets executed and the CALLS that actually get
# made, not that a function was invoked. The mutations below each reintroduce
# one real defect from this feature's history: dropping the activate,
# reordering it, collapsing the third outcome into a pass, and unpicking the
# two nested quoting layers.
#
# Oracle exit codes: 0 = all assertions held (mutant SURVIVED), 1 = at least
# one assertion failed (mutant KILLED). Anything else is CANNOT DETERMINE and
# is never scored as either, per the three-outcome rule.
#
# Modelled on scripts/ci/mutate-density-menu-placement.sh: same
# restore-on-exit discipline via lib/mutate-trap.sh, same baseline gate, same
# mutate() shape.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

SUITE="$ROOT/tests/test_terminal_launcher.node.mjs"
OUT=/tmp/mutate-terminal-launcher-out.log

FILES=(
  "macOS/terminal-launcher.js"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# Runs the real node suite against whatever is currently on disk.
run_check() {
  mutate_run node "$SUITE" >"$OUT" 2>&1
  return $?
}

echo "--- baseline: the suite must be GREEN before anything is mutated ---"
run_check
baseline_status=$?
if [ "$baseline_status" -ne 0 ]; then
  echo "BASELINE IS RED (exit ${baseline_status}). Every mutant would read as"
  echo "killed for free. Refusing to run."
  tail -25 "$OUT"
  exit 2
fi
echo "baseline green"

restore_all() {
  mutate_restore_files
}

# Apply one textual mutation, run the suite, expect exit 1 (killed).
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
  if [ "$status" -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (suite could not evaluate the mutant, exit ${status})"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- the original bug: the missing activate ---"

mutate "activate removed entirely - the exact pre-fix bug, window opens behind everything" \
  "macOS/terminal-launcher.js" \
  "    '  do script \"' + embedded + '\"',
    '  activate',||=>||    '  do script \"' + embedded + '\"',"

mutate "activate issued BEFORE do script - raises the previously frontmost window, not the new one" \
  "macOS/terminal-launcher.js" \
  "    '  do script \"' + embedded + '\"',
    '  activate',||=>||    '  activate',
    '  do script \"' + embedded + '\"',"

echo "--- the two nested quoting layers ---"

mutate "shell quoting switched to double quotes - a backtick in the path becomes command substitution" \
  "macOS/terminal-launcher.js" \
  "  return \"'\" + String(value).replace(/'/g, \"'\\\\''\") + \"'\";||=>||  return '\"' + String(value) + '\"';"

mutate "AppleScript escaping neutered - an embedded double quote terminates the AppleScript literal" \
  "macOS/terminal-launcher.js" \
  "  return String(value).replace(/\\\\/g, '\\\\\\\\').replace(/\"/g, '\\\\\"');||=>||  return String(value);"

mutate "shell quoting dropped from the fallback - spaces in Application Support split the tail argument" \
  "macOS/terminal-launcher.js" \
  "  const shellWord = shellSingleQuote(logPath);||=>||  const shellWord = logPath;"

mutate "the .command script stops single quoting the path" \
  "macOS/terminal-launcher.js" \
  "  const quoted = shellSingleQuote(logPath);||=>||  const quoted = logPath;"

echo "--- the .command document route ---"

mutate "the .command file is written non-executable - LaunchServices cannot run it" \
  "macOS/terminal-launcher.js" \
  "    deps.fs.writeFileSync(scriptPath, buildTailCommandScript(logPath), {
      mode: 0o755,
    });||=>||    deps.fs.writeFileSync(scriptPath, buildTailCommandScript(logPath), {
      mode: 0o644,
    });"

mutate "the shebang is dropped from the generated .command script" \
  "macOS/terminal-launcher.js" \
  "    '#!/bin/bash',
    '# Generated by Cloude Code.||=>||    '# Generated by Cloude Code."

echo "--- the third outcome, collapsed into the other two ---"

mutate "an undetermined handler is reported as a clean success - the false green this feature is about" \
  "macOS/terminal-launcher.js" \
  "      if (!handlerPath) return runFallback('no .command handler registered');||=>||      if (!handlerPath) return callback({ opened: true, handlerPath: null, usedFallback: false, error: null });"

mutate "a failed shell.openPath is swallowed and reported as opened" \
  "macOS/terminal-launcher.js" \
  "          if (openError) return runFallback(openError);||=>||          if (openError) { /* ignored */ }"

mutate "a failing handler probe is reported as a resolved handler" \
  "macOS/terminal-launcher.js" \
  "    if (error) return callback(null);||=>||    if (error) return callback('/System/Applications/Utilities/Terminal.app');"

mutate "an unwritable .command file throws instead of falling back" \
  "macOS/terminal-launcher.js" \
  "  } catch (writeError) {
    return runFallback(writeError);
  }||=>||  } catch (writeError) {
    throw writeError;
  }"

restore_all
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
