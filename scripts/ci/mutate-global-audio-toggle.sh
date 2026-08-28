#!/bin/bash
# Mutation check for client/js/globalAudioToggle.js against
# tests/test_global_audio_toggle.node.mjs.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this control could go wrong again:
# the one stored boolean losing its key or its default, the three-outcome
# classify() collapsing two distinct states into one (the exact defect
# themeAudioStatus.js's whole file exists to retire, one layer up), the
# could-not-evaluate outcome being guessed instead of named, or the
# single-shared-button placement rule leaking a second instance or
# forgetting to detach on a barless screen. Every one must turn the
# suite red. The source file is restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
TARGET="$ROOT/client/js/globalAudioToggle.js"
TEST="$ROOT/tests/test_global_audio_toggle.node.mjs"
mutate_arm_trap "$ROOT" "$TARGET"

survived=0
cannot_determine=0
total=0

restore() {
  mutate_restore_files
}

# Baseline gate: the real (unmutated) source must pass before any mutant is
# meaningful. A mutation "kill count" measured against a red baseline is
# not evidence of anything.
restore
if ! mutate_run node "$TEST" >/dev/null 2>&1; then
  echo "BASELINE FAILED: tests/test_global_audio_toggle.node.mjs does not pass against the unmutated source."
  echo "MUTATION CHECK ABORTED (baseline must be green first)"
  exit 1
fi
echo "baseline green"

# mutate <name> <old||=>||new>
mutate() {
  local name="$1" py="$2"
  total=$((total + 1))
  restore
  python3 - "$TARGET" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:80])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=1
    return
  fi
  if mutate_run node "$TEST" >/dev/null 2>&1; then
    echo "SURVIVED  $name"
    survived=1
  else
    echo "killed    $name"
  fi
}

# --- the one stored boolean --------------------------------------------
mutate "the stored key changes without a matching default" \
  "var STORAGE_KEY = 'cloude.audio.enabled';||=>||var STORAGE_KEY = 'cloude.audio.enabledx';"

mutate "isOn() defaults to true instead of off" \
  "return localStorage.getItem(STORAGE_KEY) === 'on';||=>||return localStorage.getItem(STORAGE_KEY) !== 'off';"

mutate "toggle() stops persisting the new choice" \
  $'persist(next);\n        applyToActiveSession();||=>||applyToActiveSession();'

mutate "toggle() stops applying to the active session" \
  $'persist(next);\n        applyToActiveSession();\n        paint();||=>||persist(next);\n        paint();'

# --- three outcomes, never two: classify() ------------------------------
mutate "off collapses into whatever the verdict says" \
  $'if (!on) {\n            return { state: \'off\', label: \'audio is off, tap to turn on\' };\n        }||=>||'

mutate "could-not-evaluate is guessed as playing instead of named unknown" \
  $'if (!verdict) {\n            return { state: \'unknown\', label: \'audio state could not be read\' };\n        }||=>||if (!verdict) {\n            return { state: \'on-playing\', label: \'audio is on\' };\n        }'

mutate "no-track and load-failure collapse into the same state" \
  "return { state: 'on-no-track', label: 'audio is on, this theme has no music' };||=>||return { state: 'on-error', label: 'audio is on, this theme has no music' };"

mutate "no-session and no-track collapse into the same state" \
  "return { state: 'on-no-session', label: 'audio is on, nothing to play here' };||=>||return { state: 'on-no-track', label: 'audio is on, nothing to play here' };"

mutate "a settling track is painted as a fault" \
  $'if (verdict.settling) {\n            return { state: \'on-settling\', label: \'audio is on, starting\' };\n        }||=>||'

mutate "playing is never distinguished from any other on state" \
  $'if (verdict.playing) {\n            return { state: \'on-playing\', label: \'audio is on\' };\n        }||=>||'

# --- diagnose() degrades to could-not-evaluate, never a guess -----------
mutate "the function-type guard is inverted, so a real module is treated as absent" \
  "typeof window.ThemeAudioStatus.current !== 'function'||=>||typeof window.ThemeAudioStatus.current === 'function'"

mutate "a throwing ThemeAudioStatus.current() is not caught" \
  $'try {\n            return window.ThemeAudioStatus.current();\n        } catch (err) {\n            return null;\n        }||=>||return window.ThemeAudioStatus.current();'

# --- ONE shared button, placement mirrors _placeStatusLight -------------
mutate "place() builds a second button instead of reusing the one node" \
  $'function build() {\n        if (btnEl) return btnEl;||=>||function build() {\n        if (false) return btnEl;'

mutate "the auth screen (no bar) leaves the button attached instead of detaching it" \
  "if (btn.parentNode) btn.parentNode.removeChild(btn);||=>||void 0;"

mutate "launchpad and terminal targets are swapped" \
  $'var targetId = screen === \'launchpad\' ? \'home-bar-status\'\n            : screen === \'terminal\' ? \'terminal-bar-status\'\n                : null;||=>||var targetId = screen === \'launchpad\' ? \'terminal-bar-status\'\n            : screen === \'terminal\' ? \'home-bar-status\'\n                : null;'

# --- the deleted header toggle stays deleted -----------------------------
mutate "the deleted header id sneaks back in as the button's id" \
  "btn.id = 'globalAudioBtn';||=>||btn.id = 'audioToggleBtn';"

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED ($total mutants, some survived)"
  exit 1
fi
echo "MUTATION CHECK PASSED ($total mutants, 0 survived)"
