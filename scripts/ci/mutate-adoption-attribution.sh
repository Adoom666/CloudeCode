#!/bin/bash
# Mutation check for S7: durable adoption, and project attribution.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this build step can silently hand
# the user a wrong answer, and every one must turn the suite red.
#
# BLOCK 1 - ADOPTION MUST STICK. The pre-S7 state was that adoption was
# recorded nowhere: the tmux name was deliberately kept out of an
# in-memory set that is rebuilt from a live listing anyway, so a claim
# did not survive a page reload. These mutants unwire the persistence,
# demote an adopted row back to observed on the next sighting, and make
# adopted_at last-write-wins. The last one is subtle and constant: the UI
# re-enters the adopt path on every session re-open, so a moving
# timestamp rewrites history several times a day.
#
# BLOCK 2 - ZERO ROWS IS NOT SUCCESS. Adoption is an UPDATE. When it
# matches nothing the honest answers are "no row carries that instance"
# and "that row is a corpse", and they are different facts. These mutants
# collapse them into each other, into success, and into an exception.
#
# BLOCK 3 - THE LISTING GATE. An unavailable probe carries no rows BY
# CONTRACT, so a missing name proves nothing. Reading it as "the session
# died" tells the user his live session is gone because tmux timed out.
#
# BLOCK 4 - ATTRIBUTION, THE PART THAT WAS ACTUALLY BROKEN ON HIS BOX.
# Nine sessions came out unknown because the input was never collected,
# not because the rule was wrong. So the mutants here attack BOTH: the
# rule (string prefixes instead of components, string length instead of
# depth, no expanduser, a forbidden resolve) and the WIRING (the probe
# removed from the import call site, the backfill neutered). A rule that
# works on paper while nothing feeds it is exactly the state the live
# database was already in.
#
# BLOCK 5 - THE THREE-OUTCOME COLLAPSES, on the wire and on screen.
# 'none' and 'unknown' rendered as one string, an unprobeable row written
# as though it were measured, an unrecognised origin defaulted to the
# most believable value.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_adoption_persists.py tests/test_adoption_three_outcomes.py \
tests/test_project_attribution.py tests/test_attribution_wiring.py \
tests/test_session_ownership_origin.py tests/test_session_store.py \
tests/test_session_import.py"
NODE_TESTS="tests/test_session_detail.node.mjs"

FILES=(
  "src/core/session_identity.py"
  # Mutated by the merge-demotion case. Anything mutate() targets MUST be
  # listed here: an unlisted file is never restored, the mutation leaks
  # onto disk, and the next run backs the mutant up as if it were source.
  "src/core/session_reconcile.py"
  "src/core/session_adopt_persist.py"
  "src/core/project_attribution.py"
  "src/core/session_attribution.py"
  "src/core/session_import_mapping.py"
  "src/core/tmux_session_cwd.py"
  "src/core/session_manager.py"
  "src/api/routes.py"
  "src/main.py"
  "client/js/session-detail.js"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one. If the suite is already red, every mutant
# "kills" and the score is meaningless - which happened for real during
# this step's development, when a sibling harness left a file mutated on
# disk and this script backed the mutant up as if it were the source.
# Same three-outcome reasoning as everything else here: "could not
# measure" must not report as "passed".
echo "--- baseline: the suites must be GREEN before anything is mutated ---"
if ! mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
for nt in $NODE_TESTS; do
  if ! mutate_run node "$nt" >/dev/null 2>&1; then
    echo "BASELINE IS RED ($nt). Refusing to run."
    exit 2
  fi
done
echo "baseline green"

restore_all() {
    mutate_restore_files
}

# Apply one textual mutation, run the suites, expect RED.
#   mutate <name> <file> <old||=>||new>
# A target that no longer exists counts as SURVIVED, never as a skip: a
# mutant whose anchor moved is a mutant that stopped testing anything,
# and silently forgiving it is how a suite's kill count drifts away from
# what it actually proves.
mutate() {
  local name="$1" file="$2" expr="$3"
  restore_all
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
  local red=1
  if mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
    red=0
  fi
  if [ "$red" -eq 1 ]; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  # Python was green. The client mutants are only observable in node.
  for nt in $NODE_TESTS; do
    if ! mutate_run node "$nt" >/dev/null 2>&1; then
      killed=$((killed + 1))
      echo "killed   $name"
      return
    fi
  done
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- BLOCK 1: ADOPTION MUST STICK ---"

mutate "the adopt route stops persisting origin entirely (pre-S7 state)" \
  "src/core/session_adopt_persist.py" \
  '    claim = claim_instance(||=>||    claim = _skip_the_claim('

mutate "origin is written as observed, so an adoption never badges ours" \
  "src/core/session_identity.py" \
  '    values: List[Any] = [SESSION_ORIGIN_ADOPTED, stamp, stamp]||=>||    values: List[Any] = ["observed", stamp, stamp]'

# REPLACED, and the original is PROVABLY EQUIVALENT rather than merely
# unkilled. It added "origin" to _OPTIONAL_INSERT_COLUMNS, but that tuple
# only gates which **fields keys record_instance accepts on INSERT, and
# ``origin`` already travels as its own named parameter - no caller ever
# passes it in **fields, and the MERGE path does not consult the tuple at
# all. So the mutated program is observationally identical to the
# original on every input. The demotion it was meant to model lives in
# the merge, so it is aimed there instead.
mutate "a later observed sighting DEMOTES an adopted session" \
  "src/core/session_reconcile.py" \
  '        "lifecycle = ?",||=>||        "origin = ?, lifecycle = ?",'

mutate "adopted_at becomes last-write-wins, rewriting history on re-open" \
  "src/core/session_identity.py" \
  '        "adopted_at = COALESCE(adopted_at, ?)",||=>||        "adopted_at = ?",'

# REPLACED, and the original is PROVABLY EQUIVALENT. It recorded the
# sighting as ``created`` instead of ``observed`` - but the very next
# statement is the claim, which overwrites ``origin`` with ``adopted``
# unconditionally on the same row in the same transaction, so no
# observable state ever differs. The risk it was reaching for is the
# sighting failing to create a row at all, which IS observable: a tmux
# session started after the one-way first-run import has no row, and
# adoption must never invent one, so without the sighting it cannot be
# claimed.
mutate "the sighting is skipped, so a post-import session cannot be adopted" \
  "src/core/session_adopt_persist.py" \
  '    sighting = record_instance(||=>||    sighting = _skip_the_sighting('

mutate "the sighting records the session as already stopped" \
  "src/core/session_adopt_persist.py" \
  '        lifecycle=SESSION_LIFECYCLE_RUNNING,||=>||        lifecycle="stopped",'

mutate "SessionManager loses the persist wire the route depends on" \
  "src/core/session_manager.py" \
  '    def persist_adoption(self, name: str):||=>||    def persist_adoption_DISABLED(self, name: str):'

echo "--- BLOCK 2: ZERO ROWS UPDATED IS NOT SUCCESS ---"

mutate "a claim matching zero rows reports success anyway" \
  "src/core/session_identity.py" \
  '    if cursor.rowcount > 0:
        claimed = get_instance(||=>||    if cursor.rowcount >= 0:
        claimed = get_instance('

mutate "no-such-instance and stopped-row collapse into one answer" \
  "src/core/session_identity.py" \
  '            outcome=ADOPT_NO_SUCH_INSTANCE,||=>||            outcome=ADOPT_NOT_RUNNING,'

mutate "the datastore-unavailable case is reported as a missing session" \
  "src/core/session_identity.py" \
  '            outcome=ADOPT_NO_DATASTORE,||=>||            outcome=ADOPT_NO_SUCH_INSTANCE,'

mutate "no_datastore claims to be a measurement" \
  "src/core/session_identity.py" \
  '        return self.outcome != ADOPT_NO_DATASTORE||=>||        return True'

mutate "the lifecycle guard is dropped, so a corpse can be adopted" \
  "src/core/session_identity.py" \
  '        "AND tmux_name = ? AND tmux_created_epoch = ? AND lifecycle != ?",||=>||        "AND tmux_name = ? AND tmux_created_epoch = ? AND ? IS NOT NULL",'

mutate "claimed is True for every outcome, so failures read as success" \
  "src/core/session_identity.py" \
  '        return self.outcome == ADOPT_CLAIMED||=>||        return True'

mutate "a gone session raises a bare RuntimeError and becomes a 500" \
  "src/core/session_manager.py" \
  '            raise AdoptTargetGoneError(||=>||            raise RuntimeError('

mutate "the route stops answering 409 and lets the gone error 500" \
  "src/api/routes.py" \
  '        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_gone",||=>||        raise HTTPException(
            status_code=500,
            detail={
                "error": "session_gone",'

mutate "the gone response drops the refresh instruction" \
  "src/api/routes.py" \
  '                "refresh": True,||=>||                "refresh": False,'

echo "--- BLOCK 3: THE LISTING GATE ---"

mutate "an unavailable listing is read as the session being gone" \
  "src/core/session_adopt_persist.py" \
  '    if not listing.ok:||=>||    if False:'

mutate "a gone session is reported as merely unavailable, hiding the fact" \
  "src/core/session_adopt_persist.py" \
  '            outcome=PERSIST_SESSION_GONE,||=>||            outcome=PERSIST_LISTING_UNAVAILABLE,'

mutate "the liveness lookup matches any row, so name reuse is claimed" \
  "src/core/session_adopt_persist.py" \
  '        if isinstance(row, dict) and row.get("name") == name:
            return row||=>||        if isinstance(row, dict):
            return row'

echo "--- BLOCK 4: THE ATTRIBUTION RULE, AND ITS WIRING ---"

mutate "matching goes back to string prefixes, so /a/bc matches /a/b" \
  "src/core/project_attribution.py" \
  '        if root not in candidates:
            continue||=>||        if not normalized.startswith(root):
            continue'

# REPLACED, and the original is PROVABLY EQUIVALENT - which is worth
# stating because it is NOT obvious and the code was still changed. The
# mutant ranked by len(root) instead of by path depth. Every root that
# reaches this comparison is a member of _ancestor_chain(normalized),
# and that chain is totally ordered by containment: any two of its
# members are nested, so one is a proper component-prefix of the other,
# so its string is a proper prefix and therefore strictly shorter. Length
# order and depth order coincide on exactly the set being compared, and
# the mutant cannot change any outcome. The code still uses _depth
# because len() is right only by accident of the input set, and the next
# person to widen that set inherits a silent bug. The observable risk is
# a tie-break that does not discriminate at all, which is mutated here.
mutate "the deepest-match tie-break stops discriminating, first root wins" \
  "src/core/project_attribution.py" \
  '    return len(PurePosixPath(normalized).parts)||=>||    return 1'

mutate "the shallowest root wins instead of the deepest" \
  "src/core/project_attribution.py" \
  '        if depth > best_depth:||=>||        if depth < best_depth or best_depth == -1:'

mutate "expanduser is dropped, so a tilde path can never match" \
  "src/core/project_attribution.py" \
  '        expanded = Path(text).expanduser()||=>||        expanded = Path(text)'

mutate "resolve() is reintroduced, rewriting the user's symlinked path" \
  "src/core/project_attribution.py" \
  '        expanded = Path(text).expanduser()||=>||        expanded = Path(text).expanduser().resolve()'

mutate "project roots stop being normalised, so a tilde root never matches" \
  "src/core/project_attribution.py" \
  '        normalized = normalize_path_for_match(root)
        if normalized is None:
            continue||=>||        normalized = root
        if normalized is None:
            continue'

mutate "a relative working dir is situated anyway instead of refused" \
  "src/core/project_attribution.py" \
  '    if not expanded.is_absolute():
        return None||=>||    if False:
        return None'

mutate "the import call site loses the working-dir probe (the live bug)" \
  "src/main.py" \
  '                        working_dir_probe=make_working_dir_probe(
                            session_manager.tmux_socket_name()
                        ),||=>||                        working_dir_probe=None,'

mutate "the backfill never probes, so his NULL working_dirs stay NULL" \
  "src/core/session_attribution.py" \
  '        if not working_dir and working_dir_probe is not None and tmux_name:||=>||        if False:'

mutate "the backfill stops writing, so nothing is ever repaired" \
  "src/core/session_attribution.py" \
  '        conn.execute(
            "UPDATE sessions SET project_id = ?, project_attribution = ?, "||=>||        _ = (
            "UPDATE sessions SET project_id = ?, project_attribution = ?, "'

mutate "the backfill overwrites already-measured attributions" \
  "src/core/session_attribution.py" \
  '        "WHERE project_attribution = ?",
        (SESSION_ATTRIBUTION_UNKNOWN,),||=>||        "WHERE project_attribution IS NOT ?",
        ("impossible-sentinel",),'

mutate "the probed working dir is not stored, so every pass re-probes" \
  "src/core/session_attribution.py" \
  '            (project_id, attribution, working_dir, stamp, row_id),||=>||            (project_id, attribution, None, stamp, row_id),'

# The home-directory fallback is aimed at the branch that is actually
# REACHED. Placed on the final `text or None` it was unreachable: tmux
# writes nothing to stdout when it fails, so a non-zero exit returns
# before that line, and a zero exit with empty stdout does not occur.
# Here it sits in the failure branch, which is exactly where
# session_manager._resolve_external_cwd puts its own ~ fallback - right
# for that function, catastrophic here, because his projects table has a
# project rooted at the home directory.
mutate "the cwd probe falls back to the home directory on failure" \
  "src/core/tmux_session_cwd.py" \
  '        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()||=>||        return str(__import__("pathlib").Path.home())
    text = completed.stdout.decode("utf-8", errors="replace").strip()'

mutate "a timed-out or erroring probe guesses instead of answering None" \
  "src/core/tmux_session_cwd.py" \
  '            note="working directory CANNOT BE DETERMINED; attribution unknown",
        )
        return None||=>||            note="working directory CANNOT BE DETERMINED; attribution unknown",
        )
        return str(__import__("pathlib").Path.home())'

mutate "the probe drops the exact-match = prefix, so fs matches fs2" \
  "src/core/tmux_session_cwd.py" \
  '                f"={name}:",||=>||                f"{name}:",'

mutate "the probe drops the trailing colon and silently reads nothing" \
  "src/core/tmux_session_cwd.py" \
  '                f"={name}:",||=>||                f"={name}",'

mutate "a name carrying a tmux target separator is probed anyway" \
  "src/core/tmux_session_cwd.py" \
  '    if any(sep in name for sep in _TARGET_SEPARATORS):||=>||    if False:'

echo "--- BLOCK 5: THREE-OUTCOME COLLAPSES ---"

mutate "an unprobeable working dir is reported as 'no project'" \
  "src/core/project_attribution.py" \
  '    normalized = normalize_path_for_match(working_dir)
    if normalized is None:
        return None, SESSION_ATTRIBUTION_UNKNOWN||=>||    normalized = normalize_path_for_match(working_dir)
    if normalized is None:
        return None, SESSION_ATTRIBUTION_NONE'

mutate "a matched-nothing directory is reported as unmeasurable" \
  "src/core/project_attribution.py" \
  '        return None, SESSION_ATTRIBUTION_NONE
    return best_id, SESSION_ATTRIBUTION_DERIVED_DEEPEST||=>||        return None, SESSION_ATTRIBUTION_UNKNOWN
    return best_id, SESSION_ATTRIBUTION_DERIVED_DEEPEST'

mutate "'none' counts as undetermined, dragging it into NEEDS ATTENTION" \
  "src/core/project_attribution.py" \
  '    return attribution != SESSION_ATTRIBUTION_UNKNOWN||=>||    return attribution == SESSION_ATTRIBUTION_DERIVED_DEEPEST'

mutate "the backfill writes unknown over an unprobeable row every boot" \
  "src/core/session_attribution.py" \
  '            still_unknown += 1
            continue||=>||            still_unknown += 1'

mutate "the detail view renders 'no project' and 'unknown' identically" \
  "client/js/session-detail.js" \
  "        none: 'No project',||=>||        none: 'Could not determine',"

mutate "the detail view's two project notes become one string" \
  "client/js/session-detail.js" \
  "        none:
            'The working directory was read and it is not inside any known project.',||=>||        none:
            'The working directory could not be read, so no project could be matched.',"

mutate "an unrecognised origin defaults to the most believable value" \
  "client/js/session-detail.js" \
  "        var label = known
            ? ORIGIN_LABELS[origin]
            : 'Unknown';||=>||        var label = ORIGIN_LABELS[origin] || ORIGIN_LABELS.created;"

mutate "an adopted session stops reading as OURS in the detail view" \
  "client/js/session-detail.js" \
  "        return origin === 'created' || origin === 'adopted';||=>||        return origin === 'created';"

mutate "created and adopted render identically, losing the distinction" \
  "client/js/session-detail.js" \
  "        adopted: 'Adopted from tmux',||=>||        adopted: 'Started by Cloude Code',"

mutate "a project name is shown beside an unmatched attribution" \
  "client/js/session-detail.js" \
  "        var name = (attribution === 'explicit' || attribution === 'derived_deepest')
            ? (projectName || 'Unnamed project')
            : label;||=>||        var name = projectName || label;"

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
