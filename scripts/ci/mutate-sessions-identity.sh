#!/bin/bash
# Mutation check for the EIGHT defects found by the S4 adversarial review.
#
# Sibling of mutate-sessions-origin.sh, which covers the original S4
# guarantees. This one covers the guarantees added when those eight were
# closed, and it exists as its own file for one reason: every mutation
# here reintroduces a specific defect that ALREADY SHIPPED ONCE and was
# only caught by an adversarial review. A test that passes is evidence
# only if it can also fail, and each of these is the proof for one fix.
#
# D1 the probe classifier: a socket we could not reach reported as a
#    complete answer of zero sessions, which walks through the first-run
#    import gate and stamps a one-way latch over the user's history.
# D2 the listing parser: the caller-controlled session NAME moved out of
#    last position, or the bounded split unbounded, either of which makes
#    the instance triple forgeable from a tmux session name.
# D3 the ownership badge: the (name, None) wildcard restored, which
#    disables the epoch tier for exactly the sessions it protects.
# D4 the identity write path: the session-id discriminator ignored, or
#    the adopt lifecycle guard removed.
# D5 the import: origin hardcoded again on either write path.
# D6 the latch: the two keys split apart so only one is written.
# D7 main.py: the probed socket dropped again.
# D8 the result blob: an unreadable latch record read as absent.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_s4_adversarial.py tests/test_s4_regressions.py \
tests/test_s4_import_regressions.py \
tests/test_tmux_listing_parse.py tests/test_session_import.py \
tests/test_session_store.py tests/test_session_ownership_origin.py \
tests/test_db_migration.py \
tests/test_tmux_row_delimiter.py \
tests/test_sessions_epoch_and_discriminator.py \
tests/test_schema_version_three_outcomes.py"

FILES=(
  "src/core/tmux_stderr.py"
  "src/core/tmux_listing.py"
  "src/core/tmux_listing_parse.py"
  "src/core/session_identity.py"
  "src/core/session_reconcile.py"
  "src/core/session_import.py"
  "src/core/session_import_mapping.py"
  "src/core/session_manager.py"
  "src/core/db_models.py"
  "src/core/db_steps.py"
  "src/main.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

restore_all() {
    mutate_restore_files
}

# mutate <name> <file> <old||=>||new>
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
  if mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=$((survived + 1))
  else
    echo "killed   $name"
    killed=$((killed + 1))
  fi
}

echo "--- D1: a probe that COULD NOT LOOK must never report zero sessions ---"

# Repointed after the V4 fix moved the per-line errno decision into
# _classify_connect_line. Same defect, same assertion, new address.
mutate "the errno is ignored again, so any connect error means no server" \
  "src/core/tmux_stderr.py" \
  '    match = _CONNECT_ERRNO_RE.search(line)||=>||    return STDERR_NO_SERVER
    match = _CONNECT_ERRNO_RE.search(line)'

mutate "the connect-error allowlist is widened to permission denied" \
  "src/core/tmux_stderr.py" \
  '_NO_SERVER_CONNECT_ERRNOS = frozenset({"no such file or directory"})||=>||_NO_SERVER_CONNECT_ERRNOS = frozenset({"no such file or directory", "permission denied"})'

mutate "an UNPARSEABLE connect line degrades to no_server instead of unknown" \
  "src/core/tmux_stderr.py" \
  '    if match is None:
        # A connect failure whose cause we cannot read. Not an answer.
        return STDERR_UNRECOGNISED||=>||    if match is None:
        return STDERR_NO_SERVER'

# REPLACED, WITH A PROOF, not dropped because it was inconvenient.
#
# The mutant that used to live here added "error connecting to" to
# _NO_SERVER_MARKERS. Before the V4 fix that reintroduced D1. After it,
# the mutant is EQUIVALENT: classify_tmux_stderr resolves every line
# starting with the connect marker through _classify_connect_line and
# RETURNS from that branch, so the marker list is unreachable for exactly
# the lines this marker could match. Verified structurally and by
# differential execution of both variants over a 1067-input corpus of
# real and synthetic tmux stderr (paths containing markers, every errno,
# multi-line, embedded newlines, empty) - not one input distinguished
# them.
#
# Its replacement asserts the property the fix actually added: the
# markers are matched at the START of a stderr line, never as a bare
# substring, so text tmux merely MENTIONS cannot assert a verdict. That
# is observable only away from the connect path, which is why the
# accompanying test had to be written before this mutation could die.
mutate "the no-server markers are matched unanchored again" \
  "src/core/tmux_stderr.py" \
  '        if any(lowered.startswith(marker) for marker in _NO_SERVER_MARKERS):||=>||        if any(marker in lowered for marker in _NO_SERVER_MARKERS):'

mutate "the markers are consulted BEFORE the errno, so injected text wins" \
  "src/core/tmux_stderr.py" \
  '    connect_verdicts = [||=>||    for line in lines:
        lowered = line.lower()
        if any(lowered.startswith(marker) for marker in _NO_SERVER_MARKERS):
            return STDERR_NO_SERVER
    connect_verdicts = ['

mutate "connect_failed is folded back into an ok=True empty answer" \
  "src/core/tmux_listing.py" \
  '    if verdict == STDERR_CONNECT_FAILED:
        return TmuxListing.unavailable(REASON_CONNECT_FAILED, detail=detail)||=>||    if verdict == STDERR_CONNECT_FAILED:
        return TmuxListing.answered([], reason=REASON_NO_SERVER, detail=detail)'

echo "--- D2: the instance triple must not be forgeable from a session name ---"

mutate "the caller-controlled NAME moves back to the front of the format" \
  "src/core/tmux_listing_parse.py" \
  'LISTING_FORMAT = "#{session_id}|#{session_created}|#{session_windows}|#{session_name}"||=>||LISTING_FORMAT = "#{session_name}|#{session_created}|#{session_windows}|#{session_id}"'

mutate "the split is unbounded again, so a pipe in a name splits the row" \
  "src/core/tmux_listing_parse.py" \
  '    parts = stripped.split(FIELD_SEPARATOR, _MAXSPLIT)||=>||    parts = stripped.split(FIELD_SEPARATOR)'

mutate "the session-id field is no longer validated as tmux-generated" \
  "src/core/tmux_listing_parse.py" \
  '    if not _SESSION_ID_RE.match(session_id):
        return None||=>||    if False:
        return None'

mutate "a non-numeric epoch is coerced to 0 instead of refusing the row" \
  "src/core/tmux_listing_parse.py" \
  '    if not _INTEGER_RE.match(created_raw):
        return None||=>||    if not _INTEGER_RE.match(created_raw):
        created_raw = "0"'

mutate "the sanitizer permits the delimiter again" \
  "src/core/session_manager.py" \
  '_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:|\x00-\x08\x0e-\x1f\x7f]")||=>||_TMUX_FORBIDDEN_CHARS = re.compile(r"[.:]")'

echo "--- D3: the legacy name set must not defeat a stored epoch ---"

mutate "the (name, None) wildcard is restored in the resolver" \
  "src/core/tmux_listing_parse.py" \
  '        if (name, created_at_epoch) in owned_instances:
            return True||=>||        if (name, created_at_epoch) in owned_instances or (
            name, None
        ) in owned_instances:
            return True'

mutate "the NEGATIVE db opinion tier is removed, so the name set rescues a stranger" \
  "src/core/tmux_listing_parse.py" \
  '        if any(
            owned_name == name and owned_epoch is not None
            for owned_name, owned_epoch in owned_instances
        ):
            return False||=>||        if False:
            return False'

mutate "the manager folds the legacy set back in with a wildcard epoch" \
  "src/core/session_manager.py" \
  '        from_db = self._owned_instances_from_db()
        if from_db is None:
            return None
        return set(from_db)||=>||        from_db = self._owned_instances_from_db()
        legacy = {(name, None) for name in self.owned_tmux_sessions}
        if from_db is None:
            return legacy or None
        return set(from_db) | legacy'

echo "--- D4: adoption must not transfer to a stranger ---"

mutate "the session-id mismatch refusal is removed" \
  "src/core/session_reconcile.py" \
  '        incoming_session_id is not None
        and stored_session_id is not None||=>||        False
        and stored_session_id is not None'

mutate "a MISSING session id counts as a disagreement (refuses every upgrade)" \
  "src/core/session_reconcile.py" \
  '        incoming_session_id is not None
        and stored_session_id is not None
        and str(stored_session_id) != str(incoming_session_id)||=>||        str(stored_session_id) != str(incoming_session_id)'

# NOTE: dropping only the `stored_session_id is None` half of the backfill
# guard is an EQUIVALENT MUTANT and is deliberately NOT tested. Tier 1
# returns early whenever the two ids differ, so any merge reaching the
# backfill with both ids present has them EQUAL - the overwrite then
# writes the identical value and no input can tell the two forms apart.
# Verified exhaustively over {None, "$3", "$9"}^2. The mutation below
# drops BOTH halves, which is not equivalent: it writes the string "None"
# over a recorded id whenever the caller has no id to hand.
mutate "the merge OVERWRITES a recorded session id with a null, destroying the evidence" \
  "src/core/session_reconcile.py" \
  '    if incoming_session_id is not None and stored_session_id is None:||=>||    if True:'

mutate "the adopt lifecycle guard is removed, so a corpse can be claimed" \
  "src/core/session_identity.py" \
  '        "AND tmux_name = ? AND tmux_created_epoch = ? AND lifecycle != ?",||=>||        "AND tmux_name = ? AND tmux_created_epoch = ? AND ? IS NOT NULL",'

mutate "the session id is never recorded, so the discriminator is always NULL" \
  "src/core/session_identity.py" \
  '    if session_id is not None:
        fields.setdefault("tmux_session_id", session_id)||=>||    if False:
        fields.setdefault("tmux_session_id", session_id)'

mutate "the listing drops the session id before it reaches the import" \
  "src/core/session_import_mapping.py" \
  '            "tmux_session_id": row.get("tmux_session_id"),||=>||            "tmux_session_id": None,'

echo "--- D5: the import must not badge the user's own session external ---"

mutate "step 5 hardcodes observed again" \
  "src/core/session_import.py" \
  '            epoch=_stopped_epoch(entry),
            origin=observed_origin_for(name, owned),||=>||            epoch=_stopped_epoch(entry),
            origin=SESSION_ORIGIN_OBSERVED,'

mutate "step 4 hardcodes observed" \
  "src/core/session_import.py" \
  '            origin=observed_origin_for(name, owned),
            lifecycle=SESSION_LIFECYCLE_RUNNING,||=>||            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,'

echo "--- D6: both latch keys, written together or not at all ---"

mutate "the helper stops writing the key the route reports" \
  "src/core/session_import.py" \
  '    set_meta(conn, META_IMPORTED_FROM_JSON_AT, stamp)


def run_first_run_import(||=>||


def run_first_run_import('

mutate "the helper stops writing the key the guard reads" \
  "src/core/session_import.py" \
  '            RESULT_KEY_SESSIONS_STAGE: stamp,
            RESULT_KEY_SESSIONS_DETAIL: detail,||=>||            RESULT_KEY_SESSIONS_DETAIL: detail,'

mutate "the latch call is hoisted above the failed-probe gate" \
  "src/core/session_import.py" \
  '    # --- step 3: THE GATE ------------------------------------------------||=>||    _latch_sessions_stage(conn, stamp, {})
    # --- step 3: THE GATE ------------------------------------------------'

echo "--- D7: the import must record the socket the probe ran against ---"

mutate "main.py drops the socket again" \
  "src/main.py" \
  '                        socket=session_manager.tmux_socket_name(),||=>||'

mutate "the import ignores the socket it was given" \
  "src/core/session_import.py" \
  '    project_result = import_from_config(conn, projects, now=stamp)||=>||    socket = DEFAULT_TMUX_SOCKET
    project_result = import_from_config(conn, projects, now=stamp)'

echo "--- D8: an unreadable latch record is CANNOT DETERMINE ---"

mutate "an unparseable blob is treated as absent again" \
  "src/core/session_import.py" \
  '        raise ImportLatchUnreadable(
            "meta.imported_from_json_result is present but unparseable; "
            "cannot determine whether the sessions import already ran"
        ) from exc||=>||        return {}'

mutate "a non-object blob is treated as absent" \
  "src/core/session_import.py" \
  '        raise ImportLatchUnreadable(
            "meta.imported_from_json_result parsed to "
            f"{type(parsed).__name__}, expected a JSON object"
        )||=>||        return {}'

echo "--- SCHEMA: the v3 step that carries the discriminator ---"

mutate "the v3 step is not idempotent, so an interrupted retry can never finish" \
  "src/core/db_steps.py" \
  '    if "tmux_session_id" in existing:
        return||=>||    if False:
        return'

# Retargeted by feat/db-is-authoritative, which added the v3 -> v4 step
# for projects.last_opened_at. The mutant's INTENT is unchanged - bump the
# constant WITHOUT adding a matching step, and prove the suite notices -
# so it now bumps past the current version rather than onto it. Left as a
# literal pair rather than computed, because a mutant that derived the
# number from the source it is mutating could never disagree with it.
mutate "the schema version is bumped without a step to reach it" \
  "src/core/db_models.py" \
  'CURRENT_SCHEMA_VERSION: int = 4||=>||CURRENT_SCHEMA_VERSION: int = 5'

mutate "the identity index absorbs the session id, making it a durable key" \
  "src/core/db_models.py" \
  '    "ON sessions (tmux_socket, tmux_name, tmux_created_epoch) "||=>||    "ON sessions (tmux_socket, tmux_name, tmux_created_epoch, tmux_session_id) "'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
