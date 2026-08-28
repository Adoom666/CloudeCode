#!/bin/bash
# Mutation check for tests/test_altscreen_scroll.node.mjs.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below breaks one of the safety properties this module exists to hold;
# every one of them must turn the suite red. Restores the original file
# on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
SRC="$ROOT/client/js/altscreen-scroll.js"
# The per-gesture row cap USED to live in altscreen-scroll.js and now lives
# in altscreen-keys.js, which decomposes a distance into PageUp/PageDown
# plus arrows. This suite kept mutating the old location, so its cap check
# reported "target moved" and the whole run exited non-zero for a reason
# that had nothing to do with the code under test. A mutation suite that is
# permanently red is furniture: people stop reading it, and it stops
# killing anything. Both files are mutable here so the cap is checked where
# it actually is.
KEYS="$ROOT/client/js/altscreen-keys.js"
mutate_arm_trap "$ROOT" "$SRC" "$KEYS"

survived=0
cannot_determine=0

# mutate <name> <python-expression-on-text> [file, default altscreen-scroll.js]
mutate() {
  local name="$1" py="$2" target="${3:-$SRC}"
  mutate_restore_files
  python3 - "$target" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:60])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "CANNOT_DETERMINE $name (target moved)"; cannot_determine=1; return; fi
  if mutate_run node "$ROOT/tests/test_altscreen_scroll.node.mjs" >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=1
  else
    echo "killed    $name"
  fi
}

mutate "transcript footer no longer identifies the open view" \
  "return 'transcript';||=>||return 'live';"

mutate "any alternate screen is treated as claude" \
  "        return 'unknown';
    }

    /**
     * Record a real keystroke||=>||        return 'live';
    }

    /**
     * Record a real keystroke"

mutate "typing guard removed" \
  "if (isTyping()) return true;||=>||if (false) return true;"

mutate "typing guard removed from the delayed arrows" \
  "if (!isTyping()) sendKeys(arrowRun(rows));||=>||sendKeys(arrowRun(rows));"

mutate "in-flight open no longer suppresses a second gesture" \
  "if (openPending() && state !== 'transcript') return true;||=>||if (false) return true;"

mutate "arrows ride along in the same write as the toggle" \
  "sendKeys(CTRL_O);
            if (settleTimer)||=>||sendKeys(CTRL_O + arrowRun(rows));
            if (settleTimer)"

mutate "exitTranscript toggles blind" \
  "if (detectState(term) !== 'transcript') return false;||=>||if (false) return false;"

mutate "per-gesture row cap removed (now enforced in altscreen-keys.js)" \
  "var n = Math.min(Math.abs(Math.trunc(rows)), cap);||=>||var n = Math.abs(Math.trunc(rows));" \
  "$KEYS"

# NOT MUTATED, deliberately: replacing the `maxRows` argument with the
# MAX_ROWS default is an EQUIVALENT MUTANT here. altscreen-scroll.js's
# MAX_ROWS_PER_GESTURE and altscreen-keys.js's MAX_ROWS are both 240, and
# that is the only production caller, so honouring the argument and
# ignoring it are observationally identical. It was tried, it survived, and
# the correct response was to delete the mutation rather than invent a test
# for a distinction the product does not make. If a second caller ever
# passes a different cap, add it back - it stops being equivalent that day.

mutate "prompt frame accepts one rule instead of two" \
  "if (isRule(rows[j]) && isPromptLine(rows[j + 1]) && isRule(rows[j + 2])) {||=>||if (isPromptLine(rows[j + 1])) {"

mutate "main screen is claimed instead of falling through" \
  "if (state === 'main') return false;||=>||if (state === 'main') return true;"

mutate "unreadable buffer reported as main screen" \
  "if (typeof type !== 'string' || typeof baseY !== 'number') return 'unknown';||=>||if (typeof type !== 'string' || typeof baseY !== 'number') return 'main';"

mutate "gate goes back to the buffer label instead of scrollback" \
  "if (baseY > 0) return 'main';||=>||if (type !== 'alternate') return 'main';"

mutate "existing scrollback is hijacked instead of left to scrollLines" \
  "if (baseY > 0) return 'main';||=>||if (baseY < 0) return 'main';"

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
