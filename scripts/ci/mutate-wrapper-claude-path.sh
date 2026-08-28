#!/bin/bash
# Mutation check for fix/wrapper-resolves-claude-from-path:
# EXAMPLE_WRAPPER_CLDOR in src/core/agent_wrappers.py now resolves the
# claude binary via `command -v claude` on PATH instead of the hardcoded
# "$HOME/.local/bin/claude", and fails with a named message when claude
# is not on PATH instead of a raw shell error.
#
# The oracle is tests/test_wrapper_claude_path_resolution.py, run under
# this worktree's own venv (no playwright/browser involved here, unlike
# the sidebar mutation scripts - this bug lives entirely in a Python
# string constant and the real shell body it holds, both exercised
# directly by that test file).
#
# Modelled on scripts/ci/mutate-density-menu-placement.sh: same
# lib/mutate-trap.sh restore-on-exit discipline, same baseline gate, same
# mutate()/kill-count shape.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

PY="$ROOT/venv/bin/python3"
TEST_FILE="$ROOT/tests/test_wrapper_claude_path_resolution.py"

FILES=(
  "src/core/agent_wrappers.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# Runs the real pytest oracle. Exit 0 = every assertion held (survived).
# Nonzero = something the fix guarantees broke (killed - what we want for
# every mutation below). A missing venv/pytest is its own CANNOT DETERMINE
# case, checked once at baseline time below.
run_check() {
  mutate_run "$PY" -m pytest "$TEST_FILE" -q >/tmp/mutate-wrapper-claude-path-out.log 2>&1
  return $?
}

if [ ! -x "$PY" ]; then
  echo "CANNOT DETERMINE: no venv python3 at $PY. Refusing to run."
  exit 2
fi

echo "--- baseline: the real check must be GREEN before anything is mutated ---"
run_check
baseline_status=$?
if [ "$baseline_status" -ne 0 ]; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  tail -30 /tmp/mutate-wrapper-claude-path-out.log
  exit 2
fi
echo "baseline green"

restore_all() {
  mutate_restore_files
}

# Apply one textual mutation to src/core/agent_wrappers.py, run the real
# pytest oracle, expect a nonzero exit (killed).
#   mutate <name> <old||=>||new>
mutate() {
  local name="$1" expr="$2"
  restore_all
  python3 - "${ROOT}/src/core/agent_wrappers.py" "$expr" <<'PYEOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:80])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  run_check
  local status=$?
  if [ "$status" -ne 0 ]; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- the PATH-resolution fix ---"

mutate "full revert: both invocations regain the hardcoded path and the resolution guard is removed entirely - the exact pre-fix bug" \
'  local claude_bin

  openrouter_key="$(
    security find-generic-password \\
      -a "$USER" \\
      -s "claude-cldor-openrouter" \\
      -w 2>/dev/null
  )" || {
    echo "OpenRouter API key not found in macOS Keychain."
    return 1
  }

  claude_bin="$(command -v claude)" || {
    echo "claude not found on PATH."
    return 1
  }

  # Treat the first non-option argument as the model name||=>||
  openrouter_key="$(
    security find-generic-password \\
      -a "$USER" \\
      -s "claude-cldor-openrouter" \\
      -w 2>/dev/null
  )" || {
    echo "OpenRouter API key not found in macOS Keychain."
    return 1
  }

  # Treat the first non-option argument as the model name'

mutate "model-branch invocation alone reverts to the hardcoded path (guard and default-branch invocation untouched)" \
'    "$claude_bin" \\
      --dangerously-skip-permissions \\
      --model "$selected_model" \\
      "$@"||=>||    "$HOME/.local/bin/claude" \\
      --dangerously-skip-permissions \\
      --model "$selected_model" \\
      "$@"'

mutate "default-branch invocation alone reverts to the hardcoded path (guard and model-branch invocation untouched)" \
'    "$claude_bin" \\
      --dangerously-skip-permissions \\
      "$@"
  fi||=>||    "$HOME/.local/bin/claude" \\
      --dangerously-skip-permissions \\
      "$@"
  fi'

mutate "resolution guard bypassed: claude_bin is always set empty instead of via command -v, so PATH is never actually consulted" \
'  claude_bin="$(command -v claude)" || {
    echo "claude not found on PATH."
    return 1
  }||=>||  claude_bin=""'

mutate "named failure message dropped: guard still returns 1 on a missing claude, but prints nothing" \
'  claude_bin="$(command -v claude)" || {
    echo "claude not found on PATH."
    return 1
  }||=>||  claude_bin="$(command -v claude)" || {
    return 1
  }'

mutate 'tilde-form hardcode reintroduced instead of $HOME-form - proves the regression guard is not just a single-string match' \
'    "$claude_bin" \\
      --dangerously-skip-permissions \\
      --model "$selected_model" \\
      "$@"||=>||    "~/.local/bin/claude" \\
      --dangerously-skip-permissions \\
      --model "$selected_model" \\
      "$@"'

restore_all
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
