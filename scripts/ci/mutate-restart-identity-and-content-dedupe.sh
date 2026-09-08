#!/bin/bash
# Mutation check for the two fixes in this change:
#   (1) RESTART of a stopped session must carry its identity and its
#       conversation, and must never present a blank session as a resumed
#       one.
#   (2) Transcript ingest idempotency must be CONTENT-addressed, not
#       path-addressed, and a mass re-archive must be loud.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific, real way this build step can silently
# hand the user a wrong answer, and every one must turn a suite red.
#
# BLOCK 1 - THE IDENTITY THE RESTART CARRIES. These mutants put the code
# back the way it was: the title absent from the button markup, the uuid
# never read out of the dataset, the handler taking a bare working dir. If
# the suite stays green under any of them, the tests are asserting on a
# plan object rather than on what was rendered and what was called.
#
# BLOCK 2 - THE THREE OUTCOMES OF A RESTART. 'resumed', 'none_recorded'
# and 'unknown' must produce three different user-visible sentences, and a
# row with no session_uuid must not start a blank session in silence. Each
# mutant collapses one of those into another.
#
# BLOCK 3 - THE SERVER SIDE. Resolution must key on the durable
# session_uuid (not the reusable tmux name), must report a row with no
# claude_session_uuid as its own outcome, and must never report an absent
# row as one that simply has no conversation.
#
# BLOCK 4 - CONTENT ADDRESSING. The lookup must be GLOBAL (the whole
# defect was a path scope), the duplicate row must store a sentinel and
# point at the row holding the bytes, and the line index must be copied.
#
# BLOCK 5 - PREFIX DEDUPE MUST SURVIVE. The two mechanisms share
# superseded_by_archive_id. A mutant that lets a GROWN file take the
# content-duplicate branch, or that breaks the chain walk, must be caught.
#
# BLOCK 6 - THE FINDING. A mass re-archive was completely silent. A
# mutant that suppresses the finding, or that fires it on every pass
# (furniture), must be caught.
#
# All mutated files are restored on exit, including on failure and on
# SIGINT/SIGTERM - see scripts/ci/lib/mutate-trap.sh.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_transcript_content_dedupe.py \
tests/test_session_restart_source.py \
tests/test_transcript_corpus_ingest.py \
tests/test_transcript_prefix_dedupe.py"
NODE_TESTS="tests/test_session_restart_identity.node.mjs \
tests/test_recent_sessions.node.mjs"

FILES=(
  "src/core/transcript_corpus_ingest.py"
  "src/core/transcript_content_dedupe.py"
  "src/core/session_restart.py"
  "client/js/launchpad.js"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline would make every mutant read as
# killed for free.
echo "--- baseline: every suite must be GREEN before anything is mutated ---"
if ! mutate_run "$PY" -m pytest $TESTS -q -p no:randomly >/dev/null 2>&1; then
  echo "BASELINE IS RED (python). Every mutant would read as killed. Refusing to run."
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
# A target that no longer exists counts as CANNOT_DETERMINE, never as a
# skip and never as a kill - an anchor that moved means the mutant was
# never evaluated, which is not evidence of anything.
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
    : # python green, fall through to node
  else
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
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

echo "--- BLOCK 1: the identity a RESTART carries ---"

mutate "the RECENT button drops data-title (the original defect, verbatim)" \
  "client/js/launchpad.js" \
  'class="recent-session-restart" data-uuid="${uuid}" data-title="${this._escapeHtml((row.title && String(row.title).trim()) || '"'"''"'"')}"||=>||class="recent-session-restart" data-uuid="${uuid}"'

mutate "the TREE ended button drops data-title" \
  "client/js/launchpad.js" \
  'class="ended-session-restart" data-uuid="${uuid}" data-title="${this._escapeHtml((s.title && String(s.title).trim()) || '"'"''"'"')}"||=>||class="ended-session-restart" data-uuid="${uuid}"'

mutate "the RECENT handler never reads data-uuid (it was in the dataset all along)" \
  "client/js/launchpad.js" \
  '                sessionUuid: btn.getAttribute('"'"'data-uuid'"'"'),||=>||                sessionUuid: null,'

mutate "the TREE handler never reads data-uuid" \
  "client/js/launchpad.js" \
  '                        sessionUuid: restart.getAttribute('"'"'data-uuid'"'"'),||=>||                        sessionUuid: null,'

mutate "the RECENT handler never reads data-title" \
  "client/js/launchpad.js" \
  '                title: btn.getAttribute('"'"'data-title'"'"'),||=>||                title: null,'

mutate "the TREE handler never reads data-title" \
  "client/js/launchpad.js" \
  '                        title: restart.getAttribute('"'"'data-title'"'"'),||=>||                        title: null,'

mutate "the title is dropped from the unidentified-restart payload" \
  "client/js/launchpad.js" \
  '        if (title) payload.project_name = title;||=>||        if (false) payload.project_name = title;'

echo "--- BLOCK 2: the three outcomes of a RESTART must not collapse ---"

mutate "a known uuid falls through to a blank create anyway" \
  "client/js/launchpad.js" \
  '        if (sessionUuid) {
            return { mode: '"'"'restart'"'"', sessionUuid, payload: null, notice: null };
        }||=>||        if (false) {
            return { mode: '"'"'restart'"'"', sessionUuid, payload: null, notice: null };
        }'

mutate "a row with NO session_uuid starts a blank session in SILENCE" \
  "client/js/launchpad.js" \
  '                if (plan.notice) this.showError(plan.notice);||=>||                if (false) this.showError(plan.notice);'

mutate "'none_recorded' is reported exactly like a clean resume" \
  "client/js/launchpad.js" \
  '        if (kind === '"'"'none_recorded'"'"') {||=>||        if (false) {'

mutate "an UNKNOWN conversation verdict is treated as a resume" \
  "client/js/launchpad.js" \
  '        if (kind !== '"'"'resumed'"'"') {||=>||        if (false) {'

mutate "a missing response body reads as a clean pass" \
  "client/js/launchpad.js" \
  '        if (!result) {||=>||        if (false) {'

mutate "a resumed restart whose lineage failed is reported as clean" \
  "client/js/launchpad.js" \
  '        if (result.lineage_recorded === false) {||=>||        if (false) {'

echo "--- BLOCK 3: the server must resolve the RIGHT row, and say which case it is ---"

mutate "resolution keys on the reusable tmux name instead of the durable uuid" \
  "src/core/session_restart.py" \
  '        "WHERE session_uuid = ? LIMIT 1",||=>||        "WHERE tmux_name = ? LIMIT 1",'

mutate "a row with no conversation is reported as RESUMABLE" \
  "src/core/session_restart.py" \
  '    if not uuid:
        return RestartSource(
            RESTART_NO_CONVERSATION,||=>||    if False:
        return RestartSource(
            RESTART_NO_CONVERSATION,'

mutate "an ABSENT row is reported as merely having no conversation" \
  "src/core/session_restart.py" \
  '    if row is None:
        return RestartSource(
            RESTART_UNRESOLVED,||=>||    if row is None:
        return RestartSource(
            RESTART_NO_CONVERSATION,'

mutate "the title is not carried out of the stored row" \
  "src/core/session_restart.py" \
  '    title = data.get("title") or label_from_tmux_name(data.get("tmux_name"))||=>||    title = None'

echo "--- BLOCK 4: content addressing must be GLOBAL, and the row shape must hold ---"

mutate "the content lookup is re-scoped to source_path - the ORIGINAL 3.78 GB defect" \
  "src/core/transcript_content_dedupe.py" \
  '        " WHERE content_sha256 = ?"||=>||        " WHERE content_sha256 = ? AND source_path = ?"'

mutate "the content check is skipped entirely, so a moved corpus re-archives" \
  "src/core/transcript_corpus_ingest.py" \
  '    match = find_archive_by_content(conn, current_sha)||=>||    match = None'

mutate "the duplicate row does not point at the row holding its bytes" \
  "src/core/transcript_content_dedupe.py" \
  '            match.archive_id,
            stamp,
            source_mtime,||=>||            None,
            stamp,
            source_mtime,'

mutate "the duplicate row is not marked, so nothing can tell it apart" \
  "src/core/transcript_content_dedupe.py" \
  '            DEDUPE_KIND_CONTENT_DUPLICATE,
            match.archive_id,||=>||            None,
            match.archive_id,'

mutate "the duplicate row gets NO line index, silently under-reporting every join" \
  "src/core/transcript_content_dedupe.py" \
  '    conn.execute(
        "INSERT INTO transcript_records"||=>||    _skip = lambda *a, **k: None
    _skip(
        "INSERT INTO transcript_records"'

mutate "a duplicate attaches to another sentinel rather than the content holder" \
  "src/core/transcript_content_dedupe.py" \
  '        " ORDER BY (superseded_by_archive_id IS NOT NULL) ASC, id ASC"||=>||        " ORDER BY id DESC"'

echo "--- BLOCK 5: prefix dedupe must still work for a genuinely GROWN file ---"

mutate "a grown file is misreported as a content duplicate" \
  "src/core/transcript_corpus_ingest.py" \
  '    match = find_archive_by_content(conn, current_sha)
    if match is not None:||=>||    match = find_archive_by_content(conn, current_sha) or (
        find_archive_by_content(conn, existing["content_sha256"])
        if existing else None
    )
    if match is not None:'

mutate "the content branch runs BEFORE the unchanged-file fast path, so a re-run duplicates" \
  "src/core/transcript_corpus_ingest.py" \
  '    if existing is not None and existing["content_sha256"] == current_sha:||=>||    if False:'

echo "--- BLOCK 6: a mass re-archive must be LOUD, and an ordinary one must be QUIET ---"

mutate "the mass-rearchive finding is never emitted - the original silence" \
  "src/core/transcript_corpus_ingest.py" \
  '    if report.content_duplicates <= MASS_REARCHIVE_THRESHOLD:
        return False||=>||    if True:
        return False'

mutate "the finding fires on EVERY pass with any duplicate at all (furniture)" \
  "src/core/transcript_corpus_ingest.py" \
  '    if report.content_duplicates <= MASS_REARCHIVE_THRESHOLD:||=>||    if report.content_duplicates < 1:'

mutate "the finding detail loses the count it exists to report" \
  "src/core/transcript_content_dedupe.py" \
  '        f"{duplicate_count} files in one pass had a content_sha256 already "||=>||        f"some files in one pass had a content_sha256 already "'

mutate "an unrecordable finding is allowed to kill the whole ingest pass" \
  "src/core/transcript_corpus_ingest.py" \
  '    except (sqlite3.Error, ValueError) as exc:||=>||    except (ZeroDivisionError,) as exc:'

restore_all
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
