#!/bin/bash
# Mutation check for src/core/agent_fingerprint.py.
#
# A test that passes is only evidence if it can also FAIL. This repo's
# agent fingerprint patterns were previously wrong in a way every test
# passed against: they matched only the first-launch trust dialog, so
# every one of the user's nine long-running Claude Code sessions
# fingerprinted as None on his live machine while the local test suite
# stayed green, because the suite never captured real running-session
# scrollback. Every mutation below reintroduces one specific way the
# detector could go wrong again - a steady-state anchor deleted, a
# box-drawing anchor loosened until it matches prose, the two-outcome
# (found / not found) contract collapsing into a three-outcome
# (found / not found / ambiguous) bug, or the ambiguity path silently
# resolving to a guess instead of None. Each one must turn the real-capture
# and edge-case tests in tests/test_agent_fingerprint.py red. The source
# file is restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
TARGET="$ROOT/src/core/agent_fingerprint.py"
TEST="$ROOT/tests/test_agent_fingerprint.py"
PYTEST="$ROOT/venv/bin/python3"
mutate_arm_trap "$ROOT" "$TARGET"

survived=0
cannot_determine=0
killed=0
total=0

restore() {
  mutate_restore_files
}

# Baseline gate: the real (unmutated) source must pass before any mutant is
# meaningful. A mutation "kill count" measured against a red baseline is
# not evidence of anything.
restore
if ! mutate_run "$PYTEST" -m pytest "$TEST" >/dev/null 2>&1; then
  echo "BASELINE FAILED: tests/test_agent_fingerprint.py does not pass against the unmutated source."
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
  if mutate_run "$PYTEST" -m pytest "$TEST" >/dev/null 2>&1; then
    echo "SURVIVED  $name"
    survived=1
  else
    echo "killed    $name"
    killed=$((killed + 1))
  fi
}

# --- steady-state claude anchors deleted or narrowed --------------------
mutate "bypass-permissions footer string deleted" \
  '"⏵⏵ bypass permissions on",||=>||'

mutate "manual-mode footer string deleted" \
  '"⏸ manual mode on",||=>||'

mutate "verbose transcript footer string deleted" \
  '"Showing detailed transcript · ctrl+o to toggle",||=>||'

mutate "oauth login regex deleted" \
  're.compile(r"claude\.com/cai/oauth/authorize"),||=>||'

mutate "wide/narrow box banner regex deleted" \
  're.compile(r"^\s*╭[─\s]*Claude Code(?:\s+v[\d.]+)?\s*[─╮]", re.M),||=>||'

mutate "compact one-line banner regex deleted" \
  're.compile(r"▐▛███▜▌\s+Claude Code(?:\s+v[\d.]+)?"),||=>||'

# --- box banner anchor loosened until it matches plain prose ------------
mutate "box banner regex no longer requires the box corner" \
  're.compile(r"^\s*╭[─\s]*Claude Code(?:\s+v[\d.]+)?\s*[─╮]", re.M),||=>||re.compile(r"Claude Code"),'

mutate "compact banner regex no longer requires the logo glyph" \
  're.compile(r"▐▛███▜▌\s+Claude Code(?:\s+v[\d.]+)?"),||=>||re.compile(r"Claude Code"),'

# --- version-number requirement made mandatory (breaks the bare-name box) ---
mutate "box banner regex requires a version number" \
  're.compile(r"^\s*╭[─\s]*Claude Code(?:\s+v[\d.]+)?\s*[─╮]", re.M),||=>||re.compile(r"^\s*╭[─\s]*Claude Code\s+v[\d.]+\s*[─╮]", re.M),'

# --- ambiguity handling: the whole point of _resolve -----------------------
mutate "ambiguous match falls back to table-order priority (the old bug)" \
  $'    hits = _matching_families(tail)\n    if len(hits) == 1:\n        return next(iter(hits))\n    if len(hits) > 1:\n        logger.warning(\n            "agent_fingerprint: ambiguous match across families %s - "\n            "returning None rather than guessing",\n            ", ".join(sorted(hits)),\n        )\n    return None||=>||    hits = _matching_families(tail)\n    if hits:\n        return sorted(hits)[0]\n    return None'

mutate "ambiguity check inverted (single match now returns None)" \
  "if len(hits) == 1:||=>||if len(hits) != 1:"

mutate "ambiguity warning silenced (absence indistinguishable from conflict)" \
  $'        logger.warning(\n            "agent_fingerprint: ambiguous match across families %s - "\n            "returning None rather than guessing",\n            ", ".join(sorted(hits)),\n        )||=>||        pass'

mutate "ambiguity warning text no longer says ambiguous" \
  '"agent_fingerprint: ambiguous match across families %s - "||=>||"agent_fingerprint: match across families %s - "'

# --- family-match collection itself ----------------------------------------
# (Note: removing the "break" after hits.add(agent_type) - continuing to
# test the family's remaining patterns after it already matched - is a
# PROVEN EQUIVALENT mutant here: hits is a set, so adding the same
# agent_type again is a no-op, and no other state depends on which pattern
# within the family fired. It is deliberately not included; it would
# survive for a reason that says nothing about a real defect. The mutant
# below is the one that actually matters: the wrong object being tracked.)
mutate "hits set collects the raw pattern instead of the family name" \
  "hits.add(agent_type)||=>||hits.add(pattern)"

mutate "family match short-circuits the whole loop instead of collecting all families" \
  $'    hits: set[str] = set()\n    for agent_type, patterns in AGENT_FINGERPRINTS.items():\n        for pattern in patterns:||=>||    hits: set[str] = set()\n    for agent_type, patterns in AGENT_FINGERPRINTS.items():\n        if hits:\n            break\n        for pattern in patterns:'

# --- two-pass window logic --------------------------------------------------
mutate "second pass (2000-line) window removed, boot banners scrolled out of range never found" \
  $'    if len(lines) <= 50:\n        return None  # already scanned everything\n    tail2k = "\\n".join(lines[-2000:])\n    return _resolve(tail2k)||=>||    return None'

mutate "first-pass window widened past 50 (masks the tail-vs-full distinction the two-pass test relies on)" \
  $'tail = "\\n".join(lines[-50:])||=>||tail = "\\n".join(lines[-2000:])'

# --- empty input guard -------------------------------------------------------
# (Note: narrowing the guard from "if not scrollback" to "if scrollback is
# None" is a PROVEN EQUIVALENT mutant for this codebase - "" still resolves
# to None because an empty tail matches no pattern - so it is deliberately
# not included here; it would survive for a reason that says nothing about
# a real defect. The guard's removal, below, is the mutant that actually
# matters: it turns detect_agent_type(None) from a documented "never
# raises, returns None" into an AttributeError.)
mutate "empty/None guard removed entirely (detect_agent_type(None) now raises instead of returning None)" \
  $'    if not scrollback:\n        return None\n    lines = scrollback.splitlines()||=>||    lines = scrollback.splitlines()'

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo ""
  echo "$killed/$total mutants killed"
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo ""
echo "$killed/$total mutants killed"
echo "MUTATION CHECK PASSED"
