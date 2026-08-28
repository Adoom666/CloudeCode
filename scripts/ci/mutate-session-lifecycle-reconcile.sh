#!/bin/bash
# Mutation check for the session lifecycle reconciler (the REAPER).
#
# A test that passes is only evidence if it can also FAIL. Every mutant
# below reintroduces one specific way this reconciler could hand the user
# a wrong answer, and unlike most of this repo's mutants these are wrong
# answers written DURABLY TO DISK, where nothing downstream can tell them
# from a measurement.
#
# BLOCK 1 - THE ok GATE. The one that matters. A failed probe carries no
# rows BY CONTRACT, so reaping against one marks every session on the
# machine 'stopped' the first time tmux hiccups. Flipped, deleted and
# short-circuited; all three must turn the suite red.
#
# BLOCK 2 - ok IS NOT complete. list_attachable_sessions refuses rows it
# cannot parse and carries on, so one malformed row means a LIVE session
# is missing from an ok=True listing. Removing the completeness gate, the
# unreadable-identity gate, or the backend's count of refused rows all
# re-open the same hole from different sides.
#
# BLOCK 3 - IDENTITY. Matching must be on (name, epoch), never the name.
# Drop the epoch and a dead session whose name has been reused never falls
# to 'stopped' at all - the S4 identity work, undone from the reaper's end.
#
# BLOCK 4 - THE ROW FILTERS. Socket, lifecycle and NULL-triple. Each one
# widens the blast radius of a single probe to rows it says nothing about.
#
# BLOCK 5 - WHAT IS WRITTEN. Wrong lifecycle, wrong source, archived_at
# resurrected into RECENT, and the UPDATE's own re-assertion of 'running'.
#
# BLOCK 6 - THE WIRING. A perfect reconciler nothing calls is worth
# nothing, and a datastore problem must never break the launcher. The
# commit itself is deliberately NOT mutated - see the proof in that block.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_session_lifecycle_reconcile.py \
tests/test_session_lifecycle_structure.py \
tests/test_session_lifecycle_wiring.py \
tests/test_tmux_listing_consumers.py \
tests/test_s9_recent_and_pills.py tests/test_session_import.py"

FILES=(
  "src/core/session_lifecycle.py"
  "src/core/session_manager.py"
  "src/core/tmux_backend.py"
  "src/core/tmux_listing.py"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

restore_all() {
    mutate_restore_files
}

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline makes every mutant read as
# killed for free and inflates the count. See
# scripts/ci/mutate-adoption-attribution.sh for the incident that made
# this mandatory.
echo "--- baseline: the suite must be GREEN before anything is mutated ---"
if ! mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
echo "baseline green"

# Apply one textual mutation, run the suite, expect RED.
#   mutate <name> <file> <old||=>||new>
# A target that no longer exists counts as SURVIVED, never as a skip - a
# mutant that cannot be applied is testing nothing and must not be scored.
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
    return
  fi
  killed=$((killed + 1))
  echo "killed   $name"
}

echo "--- BLOCK 1: the ok gate. A failed probe must never write a lifecycle ---"

mutate "the ok gate is deleted, so a failed probe reaps the whole machine" \
  "src/core/session_lifecycle.py" \
  '    if not listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )||=>||    if False:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )'

mutate "the ok gate is INVERTED, refusing good probes and reaping on bad ones" \
  "src/core/session_lifecycle.py" \
  '    if not listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE,||=>||    if listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE,'

mutate "the gate refuses by falling through instead of returning" \
  "src/core/session_lifecycle.py" \
  '    if not listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )
    if not listing.complete:||=>||    if not listing.ok:
        _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )
    if not listing.complete:'

mutate "the writer gains a second call site, on the ungated branch" \
  "src/core/session_lifecycle.py" \
  '    if not listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )||=>||    if not listing.ok:
        return _reap_absent_instances(
            conn, listing=listing, socket=socket, now=now
        )'

echo "--- BLOCK 2: ok is not complete. A partial list cannot prove absence ---"

mutate "the completeness gate is deleted; a refused row reads as a dead session" \
  "src/core/session_lifecycle.py" \
  '    if not listing.complete:||=>||    if False:'

mutate "TmuxListing.complete ignores refused rows entirely" \
  "src/core/tmux_listing.py" \
  '        return self.ok and self.refused_rows == 0||=>||        return self.ok'

mutate "the backend stops counting the rows its parser refused" \
  "src/core/tmux_backend.py" \
  '                refused_rows += 1||=>||                pass'

mutate "the manager drops refused_rows when it rewraps the listing" \
  "src/core/session_manager.py" \
  '                                    refused_rows=listing.refused_rows)||=>||                                    refused_rows=0)'

mutate "an unreadable listing row is skipped instead of refusing the listing" \
  "src/core/session_lifecycle.py" \
  '        if not isinstance(name, str) or not name:
            return None||=>||        if not isinstance(name, str) or not name:
            continue'

mutate "a non-integer epoch is accepted, so the key can never match" \
  "src/core/session_lifecycle.py" \
  '        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return None||=>||        if isinstance(epoch, bool) or not isinstance(epoch, int):
            continue'

mutate "the unreadable-identity refusal is ignored by the writer" \
  "src/core/session_lifecycle.py" \
  '    live = live_instance_keys(listing.sessions)
    if live is None:||=>||    live = live_instance_keys(listing.sessions) or set()
    if False:'

echo "--- BLOCK 3: identity. The epoch is what makes a name not an identity ---"

mutate "the epoch is DROPPED from the absence comparison, matching on name alone" \
  "src/core/session_lifecycle.py" \
  '        if key in live:
            continue||=>||        if any(k[0] == key[0] for k in live):
            continue'

mutate "the comparison is inverted: present instances are reaped, absent ones kept" \
  "src/core/session_lifecycle.py" \
  '        if key in live:
            continue||=>||        if key not in live:
            continue'

echo "--- BLOCK 4: the row filters. One probe must not speak for every row ---"

mutate "the socket filter is removed; a listing of one socket reaps all of them" \
  "src/core/session_lifecycle.py" \
  '"FROM sessions WHERE tmux_socket = ? AND lifecycle = ? "||=>||"FROM sessions WHERE (tmux_socket = ? OR 1=1) AND lifecycle = ? "'

mutate "the lifecycle filter is removed; 'unknown' rows are promoted to 'stopped'" \
  "src/core/session_lifecycle.py" \
  '"FROM sessions WHERE tmux_socket = ? AND lifecycle = ? "||=>||"FROM sessions WHERE tmux_socket = ? AND (lifecycle = ? OR 1=1) "'

mutate "the NULL-triple filter is removed; rows with no instance are judged absent" \
  "src/core/session_lifecycle.py" \
  '"AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL",||=>||"",'

mutate "the pre-v2 table check is removed" \
  "src/core/session_lifecycle.py" \
  '    if not sessions_table_ready(conn):||=>||    if False:'

echo "--- BLOCK 5: what is written, and what must never be ---"

mutate "the archived guard is removed: archived_at is cleared and RECENT resurrects it" \
  "src/core/session_lifecycle.py" \
  '            "UPDATE sessions SET lifecycle = ?, lifecycle_source = ?, "||=>||            "UPDATE sessions SET archived_at = NULL, lifecycle = ?, lifecycle_source = ?, "'

mutate "the UPDATE stops re-asserting lifecycle, resting on the SELECT alone" \
  "src/core/session_lifecycle.py" \
  '            "WHERE id = ? AND lifecycle = ?",||=>||            "WHERE id = ? AND ? IS NOT NULL",'

mutate "the reaped row is written 'unknown' instead of 'stopped'" \
  "src/core/session_lifecycle.py" \
  '                SESSION_LIFECYCLE_STOPPED,
                SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,||=>||                "unknown",
                SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,'

mutate "the source is recorded as probe_failed, the one token that must never be written" \
  "src/core/session_lifecycle.py" \
  '                SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,
                stamp,||=>||                "probe_failed",
                stamp,'

mutate "last_seen_running_at is overwritten, erasing when the session was alive" \
  "src/core/session_lifecycle.py" \
  '            "lifecycle_checked_at = ?, updated_at = ? "||=>||            "lifecycle_checked_at = ?, last_seen_running_at = ?, updated_at = ? "'

mutate "the outcome forgets which rows it stopped, so the caller never commits" \
  "src/core/session_lifecycle.py" \
  '        stopped.append(str(row["session_uuid"]))||=>||        pass'

echo "--- BLOCK 6: the wiring. An uncalled reconciler reconciles nothing ---"

mutate "the reconciler is never called from the home-screen listing" \
  "src/core/session_manager.py" \
  '        self.reconcile_lifecycle(listing)||=>||        pass'

# NOT MUTATED: `if outcome.changed: conn.commit()`. PROVED EQUIVALENT.
# src.core.db.connect opens with isolation_level=None (db.py line 146), so
# every UPDATE is already durable when it executes and the commit is a
# no-op - removing it changes nothing observable and the mutant would
# survive for a correct reason. Measured, not assumed: a row inserted and
# the connection closed with no commit is still there on reopen. The
# assumption is pinned by test_the_datastore_connection_is_autocommit, so
# if it ever stops holding, the suite says so before this comment misleads
# anyone. The two mutants below replace it and are observable.

mutate "a missing datastore raises instead of skipping, breaking the launcher" \
  "src/core/session_manager.py" \
  '        conn = self._writable_datastore_connection()
        if conn is None:
            return ReconcileOutcome(||=>||        conn = self._writable_datastore_connection()
        if False:
            return ReconcileOutcome('

mutate "ReconcileOutcome.changed always says nothing happened" \
  "src/core/session_lifecycle.py" \
  '        return bool(self.stopped_uuids)||=>||        return False'

mutate "the manager reconciles against the CONFIGURED socket, not the probed one" \
  "src/core/session_manager.py" \
  '                socket=self._last_probe_socket or self._tmux_socket_name(),||=>||                socket="a_socket_the_probe_never_used",'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
