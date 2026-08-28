#!/bin/bash
# Mutation check for the session-attribution evidence ladder (Stages B, C, D).
#
# A test that passes is only evidence if it can also FAIL. Every mutation
# below reintroduces one specific way this migration can silently hand a
# user the wrong answer about who created his own terminal sessions.
#
# The first block is the one the design doc calls out by name: tier 2,
# the tmux_ext_<name>.pipe file. That file records the app's OWN verdict
# that a session was external, and that verdict was produced by the bug
# this whole import exists to correct. Trusting it would launder the
# defect into the migration invisibly, because the file looks exactly
# like an independent measurement. If no test dies in that block, nothing
# is stopping the next person from "improving" the ladder by reading it.
#
# The second block is tier 4's epoch gate. A CLOUDECODE_ORIGIN marker
# proves nothing on a session created before this install had a
# create-path write site, and an install whose boundary cannot be
# determined must treat the tier as INADMISSIBLE rather than valid.
#
# The third block is tiers 5 and 6 deciding anything at all. Name shape
# and working directory are display hints. Writing either into origin is
# the invented verdict this codebase keeps re-learning not to write.
#
# The fourth block is the Stage-D re-run: promote, never demote, and
# never re-ask a question the user has already answered.
#
# All mutated files are restored on exit, including on SIGINT and SIGTERM
# (see scripts/ci/lib/mutate-trap.sh).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_session_import_ladder.py \
tests/test_session_import_ladder_wiring.py \
tests/test_session_import.py \
tests/test_s4_import_regressions.py"

FILES=(
  "src/core/session_import_ladder.py"
  "src/core/session_import_ladder_types.py"
  "src/core/session_import_tiers.py"
  "src/core/session_import_evidence.py"
  "src/core/session_import_promote.py"
  "src/core/session_stage_a_boundary.py"
  "src/core/session_import.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" expr="$3"
  mutate_restore_files
  "$PY" - "${ROOT}/${file}" "$expr" <<'PYEOF'
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
  if mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=$((survived + 1))
  else
    echo "killed   $name"
    killed=$((killed + 1))
  fi
}

echo "--- TIER 2: the app's own verdict is not evidence about itself ---"

mutate "tier 2 becomes constructible, so it can enter a verdict" \
  "src/core/session_import_ladder_types.py" \
  'INADMISSIBLE_TIERS: Tuple[int, ...] = (2,)||=>||INADMISSIBLE_TIERS: Tuple[int, ...] = ()'

mutate "an ext_ pipe alone decides THEIRS" \
  "src/core/session_import_ladder.py" \
  '    hit = next((o for o in outcomes if o.result == TIER_HIT), None)||=>||    if readopted and not any(o.result == TIER_HIT for o in outcomes):
        return SessionVerdict(
            tmux_name=session.tmux_name, epoch=session.epoch,
            verdict=LADDER_THEIRS, reason="ext_pipe",
            tiers=tuple(outcomes), hints=hints,
        )
    hit = next((o for o in outcomes if o.result == TIER_HIT), None)'

echo "--- TIER 4: the marker is epoch gated, and undatable means unusable ---"

mutate "the Stage-A epoch gate is removed, so an old marker is trusted" \
  "src/core/session_import_tiers.py" \
  '    if int(session.epoch) < int(evidence.stage_a_boundary_epoch):||=>||    if False:'

mutate "an undeterminable boundary is treated as valid instead of inadmissible" \
  "src/core/session_import_tiers.py" \
  '            result=TIER_UNEVALUATED,
            detail=(
                "a CLOUDECODE_ORIGIN marker is present but this install'"'"'s "||=>||            result=TIER_HIT,
            detail=(
                "a CLOUDECODE_ORIGIN marker is present but this install'"'"'s "'

mutate "an absent boundary reads as epoch zero rather than CANNOT DETERMINE" \
  "src/core/session_stage_a_boundary.py" \
  '    if raw is None or str(raw).strip() == "":
        return None||=>||    if raw is None or str(raw).strip() == "":
        return 0'

echo "--- TIERS 5 and 6: hints never decide ---"

mutate "the auto-name shape is promoted to a verdict" \
  "src/core/session_import_ladder.py" \
  '    hit = next((o for o in outcomes if o.result == TIER_HIT), None)||=>||    if hints:
        return SessionVerdict(
            tmux_name=session.tmux_name, epoch=session.epoch,
            verdict=LADDER_OURS, reason="name_shape",
            tiers=tuple(outcomes), hints=hints,
        )
    hit = next((o for o in outcomes if o.result == TIER_HIT), None)'

echo "--- THE THIRD OUTCOME: could-not-evaluate is not no-evidence ---"

mutate "an unreadable owned set reads as an empty one" \
  "src/core/session_import_tiers.py" \
  '    if evidence.owned_tmux_names is None:
        return TierOutcome(
            tier=1,
            name="owned_set",
            result=TIER_UNEVALUATED,||=>||    if evidence.owned_tmux_names is None:
        return TierOutcome(
            tier=1,
            name="owned_set",
            result=TIER_MISS,'

mutate "an ABSENT log directory reads as an empty one" \
  "src/core/session_import_evidence.py" \
  '    if log_dir is None:
        return (None, None)||=>||    if log_dir is None:
        return (frozenset(), frozenset())'

mutate "an unreadable log directory reads as an empty one (scan failure)" \
  "src/core/session_import_evidence.py" \
  '        return (None, None)

    created: Set[str] = set()||=>||        return (frozenset(), frozenset())

    created: Set[str] = set()'

echo "--- STAGE D: promote, never demote, and never re-ask ---"

mutate "the re-run may touch a row the user declined" \
  "src/core/session_import_promote.py" \
  '    return "origin = ? AND user_declined_at IS NULL"||=>||    return "origin = ? OR user_declined_at IS NOT NULL"'

mutate "the re-run may touch a row that is already ours" \
  "src/core/session_import_promote.py" \
  '    return "origin = ? AND user_declined_at IS NULL"||=>||    return "origin != ? AND user_declined_at IS NULL"'

mutate "the promote writes observed, i.e. it demotes" \
  "src/core/session_import_promote.py" \
  '        (
            SESSION_ORIGIN_CREATED,
            lifecycle_source,||=>||        (
            SESSION_ORIGIN_OBSERVED,
            lifecycle_source,'

mutate "a stamp with no version key reads as CURRENT, locking the user out" \
  "src/core/session_import.py" \
  '    raw = blob.get(RESULT_KEY_SESSIONS_EVIDENCE_VERSION)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0||=>||    raw = blob.get(RESULT_KEY_SESSIONS_EVIDENCE_VERSION)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return EVIDENCE_LADDER_VERSION'

mutate "the re-run falls through to the full import and re-inserts" \
  "src/core/session_import.py" \
  '    if prior_version is not None:
        return _rerun_promote_only(||=>||    if False:
        return _rerun_promote_only('

mutate "the unattributed list is never written" \
  "src/core/session_import.py" \
  '    unattributed = ladder.unattributed_records()
    set_meta(||=>||    unattributed = []
    set_meta('

mutate_restore_files

echo
echo "killed=$killed survived=$survived cannot_determine=$cannot_determine"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
