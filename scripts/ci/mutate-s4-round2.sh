#!/bin/bash
# Mutation check for the EIGHT findings of the SECOND adversarial round.
#
# Sibling of mutate-sessions-identity.sh, which covers the first round.
# Same reasoning for keeping it separate: every mutation here
# reintroduces a defect that survived a full round of hardening AND its
# mutation suite, and was only caught by a second adversarial pass. A
# passing test is evidence only if it can also fail.
#
# V1 the ROW delimiter: str.splitlines() recognises ten line boundaries,
#    tmux rejects only seven, so a session name containing NEL, LS or PS
#    turned one tmux row into several parser rows and forged the identity
#    triple. The field parser was already hardened; the hole was one
#    layer up in the caller.
# V2 the import's step-5 path wrote a NULL discriminator whether or not
#    the persisted entry knew one, silently unarming the
#    instance-mismatch guard.
# V3 a synthesized epoch of 0 entered the identity key, so the ownership
#    resolver issued a permanent CONFIDENT NEGATIVE against a name.
# V4 the no-server markers were an unanchored substring test over
#    user-controlled text (the socket path is echoed into stderr).
# V5 an unparseable meta.schema_version collapsed to 0, which silently
#    disabled the pre-migration backup on a populated database.
# V6 the errno allowlist is one English string and glibc localises
#    strerror, so a non-English locale made the check never clear.
# V7 the trail called a retry after a CLEAN failure an interrupt.
# V8 nothing deletes a sessions row, so a wrong owned row is permanent.
#
# All mutated files are restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
PY="${ROOT}/venv/bin/python3"
TESTS="tests/test_tmux_row_delimiter.py \
tests/test_sessions_epoch_and_discriminator.py \
tests/test_schema_version_three_outcomes.py \
tests/test_tmux_listing_parse.py tests/test_session_import.py \
tests/test_session_store.py tests/test_db_migration.py \
tests/test_s4_adversarial.py"

FILES=(
  "src/core/tmux_stderr.py"
  "src/core/db_version_gate.py"
  "src/core/trail_reader.py"
  "src/core/tmux_listing.py"
  "src/core/tmux_listing_parse.py"
  "src/core/tmux_backend.py"
  "src/core/session_identity.py"
  "src/core/session_import.py"
  "src/core/session_import_mapping.py"
  "src/core/session_store.py"
  "src/core/db.py"
  "src/core/db_migration.py"
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

echo "--- V1: one tmux row must be exactly one parser row ---"

mutate "the row splitter goes back to str.splitlines()" \
  "src/core/tmux_listing_parse.py" \
  '    for raw in (stdout_text or "").split(ROW_SEPARATOR):||=>||    for raw in (stdout_text or "").splitlines():'

mutate "the row separator is widened to a unicode boundary" \
  "src/core/tmux_listing_parse.py" \
  'ROW_SEPARATOR = "\n"||=>||ROW_SEPARATOR = " "'

mutate "the defence-in-depth name check is disabled" \
  "src/core/tmux_listing_parse.py" \
  '    return any(char in name for char in LINE_BOUNDARY_CHARS)||=>||    return False'

mutate "the boundary check is removed from the parser" \
  "src/core/tmux_listing_parse.py" \
  '    if name_has_line_boundary(name):
        return None||=>||    if False:
        return None'

mutate "the three tmux-ACCEPTED boundaries drop out of the constant" \
  "src/core/tmux_listing_parse.py" \
  '    "\x85",      # NEL     tmux ACCEPTS||=>||    "\x0b",      # duplicate VT, NEL removed'

mutate "list_attachable_sessions reverts to splitlines" \
  "src/core/tmux_backend.py" \
  '        raw_lines = split_listing_rows(stdout_text)||=>||        raw_lines = stdout_text.splitlines()'

mutate "discover_existing reverts to splitlines" \
  "src/core/tmux_backend.py" \
  '        names = split_listing_rows(stdout_text)||=>||        names = stdout_text.splitlines()'

mutate "list_pane_status_all reverts to splitlines" \
  "src/core/tmux_backend.py" \
  '        for line in split_listing_rows(stdout_text):||=>||        for line in stdout_text.splitlines():'

echo "--- V2: the discriminator is passed, or its absence is measured ---"

mutate "step 5 stops passing the session id again" \
  "src/core/session_import.py" \
  '            session_id=_persisted_session_id(entry),
||=>||'

mutate "the app session id is written into the tmux discriminator" \
  "src/core/session_import_mapping.py" \
  '    value = entry.get("tmux_session_id")||=>||    value = entry.get("tmux_session_id") or entry.get("session_id")'

echo "--- V3: an epoch nobody measured must not enter the identity key ---"

mutate "_stopped_epoch synthesizes a 0 again" \
  "src/core/session_import_mapping.py" \
  '            except (TypeError, ValueError):
                continue
    return None||=>||            except (TypeError, ValueError):
                continue
    return 0'

mutate "owned_instances stops filtering NULL epochs" \
  "src/core/session_store.py" \
  '        "AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL",||=>||        "AND tmux_name IS NOT NULL",'

mutate "record_instance coerces a None epoch to 0 on insert" \
  "src/core/session_identity.py" \
  '        None if epoch is None else int(epoch),||=>||        0 if epoch is None else int(epoch),'

mutate "get_instance drops its None-epoch guard" \
  "src/core/session_store.py" \
  '    if epoch is None:
        return None||=>||    if False:
        return None'

echo "--- V5: an unreadable version is could-not-evaluate, not zero ---"

mutate "an unparseable version collapses back to 0" \
  "src/core/db.py" \
  '        return SchemaVersionRead(SCHEMA_VERSION_UNREADABLE, raw=str(raw))||=>||        return SchemaVersionRead(SCHEMA_VERSION_PARSED, value=0, raw=str(raw))'

mutate "the migration stops checking whether the version was readable" \
  "src/core/db_version_gate.py" \
  '    if not version_read.readable:||=>||    if False:'

mutate "the populated-table discriminator always says empty" \
  "src/core/db.py" \
  '        return int(count[0]) > 0||=>||        return False'

mutate "a probe that could not answer authorises the fresh-install path" \
  "src/core/db_version_gate.py" \
  '    if populated is not False:||=>||    if populated is True:'

mutate "the absent-version guard is removed entirely" \
  "src/core/db_version_gate.py" \
  '    if current != 0:
        return VersionGate(version=current)||=>||    return VersionGate(version=current)
    if False:'

echo "--- V6: the listing subprocess speaks the language the parser reads ---"

mutate "the locale pin is dropped from the listing environment" \
  "src/core/tmux_stderr.py" \
  'LISTING_ENV_OVERRIDES = {"LC_ALL": "C"}||=>||LISTING_ENV_OVERRIDES = {}'

mutate "the listing subprocess stops using the pinned environment" \
  "src/core/tmux_backend.py" \
  '                env=listing_env(),
||=>||'

echo "--- V7: an interrupt is an UNCLOSED entry, not any started line ---"

mutate "the interrupt scan goes back to every started line" \
  "src/core/trail_reader.py" \
  '    for entry in reversed(find_unclosed(read.entries)):||=>||    for entry in reversed(read.entries):'

echo "--- V8: the missing repair path must stay documented ---"

# NOTE ON WHAT IS WORTH MUTATING IN PROSE. The first version of this
# mutation reworded the opening sentence of the repair-path note and
# SURVIVED, correctly: the test asserts that the note names a reaper and
# names its safety constraint, and a reworded sentence still did both.
# Mutating prose for its own sake tests nothing. What is worth pinning is
# the LOAD-BEARING CLAUSE - that any future reaper must be gated on a
# listing that actually ran - because losing that sentence is how someone
# later builds a reaper that deletes the user's history the first time
# tmux fails to answer.
mutate "the reaper's ok=True gate is dropped from the design note" \
  "src/core/session_store.py" \
  'only against a listing with ``ok=True``||=>||against whatever listing is to hand'

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
