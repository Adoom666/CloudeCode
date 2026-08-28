#!/bin/bash
# Mutation check for the MODEL_ID_PATTERN colon fix (src/models.py -
# MODEL_ID_PATTERN, is_valid_model_id, describe_model_id_rejection).
#
# The pattern used to reject every OpenRouter model-variant id (:free,
# :nitro, :online, :extended, :beta) outright, blocking session-create and
# POST /api/v1/providers/models entirely. The fix widens the pattern to
# accept exactly one non-leading, non-trailing colon while keeping every
# other shell-injection restriction in place (leading-hyphen guard, length
# cap, disallowed characters, plus a new ".." guard). Each mutation below
# reintroduces one specific way that could regress: the colon accepted in
# more places than intended (letting a leading/trailing/doubled colon
# through, which is a real shape a malicious model id could take), or one
# of the pre-existing guards (hyphen, length, charset) silently dropped
# while widening the pattern, or the three-outcome rejection-reason
# reporting collapsing back into indistinguishable messages. Each must
# turn tests/test_model_id_colon.py red. The source file is restored on
# exit, including on failure.
#
# Mutation anchors are the FULL quoted raw-string literals (with the
# leading r" and closing "), not bare regex fragments - MODEL_ID_PATTERN's
# own definition is echoed in nearby comments for readability, so a bare
# fragment like "(?=.{1,120}$)" matches the comment prose first and
# str.replace(..., count=1) silently mutates the wrong occurrence, giving
# a false SURVIVED that says nothing about the real code path.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
TARGET="$ROOT/src/models.py"
TEST="$ROOT/tests/test_model_id_colon.py"
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
  echo "BASELINE FAILED: tests/test_model_id_colon.py does not pass against the unmutated source."
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
  if mutate_run "$PYTEST" -m pytest "$TEST" >/dev/null 2>&1; then
    echo "SURVIVED  $name"
    survived=1
  else
    echo "killed    $name"
    killed=$((killed + 1))
  fi
}

# The two raw-string literal lines that make up MODEL_ID_PATTERN, quoted
# exactly as they appear in source (each is unique - see header comment).
LINE1='r"^(?!-)(?!.*\.\.)(?=.{1,120}$)"'
LINE2='r"[A-Za-z0-9._~/-]+(?::[A-Za-z0-9._~/-]+)?$"'

# --- leading-hyphen guard (pre-existing; must survive the rewrite) --------
mutate "leading-hyphen lookahead removed entirely" \
  "$LINE1||=>||r\"^(?!.*\\.\\.)(?=.{1,120}\$)\""

# --- length cap (pre-existing; must survive the rewrite to a lookahead) ---
mutate "length cap widened from 120 to 1200" \
  "$LINE1||=>||r\"^(?!-)(?!.*\\.\\.)(?=.{1,1200}\$)\""

mutate "length cap removed entirely" \
  "$LINE1||=>||r\"^(?!-)(?!.*\\.\\.)\""

# --- the ".." path-traversal guard, newly added --------------------------
mutate "path-traversal guard removed entirely" \
  "$LINE1||=>||r\"^(?!-)(?=.{1,120}\$)\""

# --- the colon widening itself, done wrong -------------------------------
mutate "colon added to the flat charset instead of a bounded optional segment (re-opens leading/trailing/double colon)" \
  "$LINE2||=>||r\"[A-Za-z0-9._~/:-]+\$\""

mutate "colon segment made unbounded (multiple colons now accepted, not just one)" \
  "$LINE2||=>||r\"[A-Za-z0-9._~/-]+(?::[A-Za-z0-9._~/-]+)*\$\""

mutate "colon segment made to allow an empty tail (re-opens trailing colon)" \
  "$LINE2||=>||r\"[A-Za-z0-9._~/-]+(?::[A-Za-z0-9._~/-]*)?\$\""

# --- describe_model_id_rejection: three-outcome rule ----------------------
mutate "empty-string reason collapsed to the generic pattern message" \
  "return \"model id must not be empty\"||=>||return f\"model id does not match required format {MODEL_ID_PATTERN}\""

mutate "leading-hyphen reason collapsed to the generic pattern message" \
  "return \"model id must not start with '-' (would be parsed as a shell flag)\"||=>||return f\"model id does not match required format {MODEL_ID_PATTERN}\""

mutate "colon-placement reason collapsed to the generic pattern message" \
  "return \"model id must not start or end with ':'\"||=>||return f\"model id does not match required format {MODEL_ID_PATTERN}\""

mutate "double-colon reason collapsed to the generic pattern message" \
  "return \"model id must contain at most one ':' variant separator\"||=>||return f\"model id does not match required format {MODEL_ID_PATTERN}\""

mutate "path-traversal reason collapsed to the generic pattern message" \
  "return \"model id must not contain '..'\"||=>||return f\"model id does not match required format {MODEL_ID_PATTERN}\""

if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo ""
  echo "$killed/$total mutants killed"
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo ""
echo "$killed/$total mutants killed"
echo "MUTATION CHECK PASSED"
