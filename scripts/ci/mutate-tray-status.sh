#!/bin/bash
# Mutation check for the menu-bar status light (macOS/tray-status.js and
# macOS/tray-api.js).
#
# The defect class this guards is not "the icon is the wrong picture", it is
# "the icon says everything is fine when nothing was measured". So the
# mutations below reintroduce exactly that: collapsing an unreachable server
# into ok, collapsing an empty session list into unknown, fabricating an empty
# list when a poll fails, and losing the difference between a stop the user
# asked for and a crash.
#
# Oracle: tests/test_tray_status.node.mjs. Exit 0 = mutant survived, 1 =
# killed, anything else is CANNOT DETERMINE and is scored as neither.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

SUITE="$ROOT/tests/test_tray_status.node.mjs"
OUT=/tmp/mutate-tray-status-out.log
FILES=("macOS/tray-status.js" "macOS/tray-api.js")
mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0; cannot_determine=0; killed=0

run_check() { mutate_run node "$SUITE" >"$OUT" 2>&1; return $?; }

echo "--- baseline ---"
run_check
if [ $? -ne 0 ]; then
  echo "BASELINE IS RED. Refusing to run."; tail -20 "$OUT"; exit 2
fi
echo "baseline green"

mutate() {
  local name="$1" file="$2" expr="$3"
  mutate_restore_files
  python3 - "${ROOT}/${file}" "$expr" <<'PYEOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (anchor stale)"; cannot_determine=$((cannot_determine+1)); return
  fi
  run_check
  local status=$?
  if [ "$status" -eq 1 ]; then killed=$((killed+1)); echo "killed   $name"; return; fi
  if [ "$status" -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (exit $status)"; cannot_determine=$((cannot_determine+1)); return
  fi
  echo "SURVIVED $name"; survived=$((survived+1))
}

echo "--- the false green ---"

mutate "an unreachable session list falls through to ok" \
  "macOS/tray-status.js" \
  "  if (!sessionsReachable) {
    return {
      ...base,
      state: 'unknown',||=>||  if (false) {
    return {
      ...base,
      state: 'unknown',"

mutate "unreachable is treated as an empty measured list instead of unknown" \
  "macOS/tray-status.js" \
  "      ? Boolean(input.sessionsReachable)||=>||      ? true"

mutate "a failed session poll fabricates an empty list" \
  "macOS/tray-api.js" \
  "      return { reachable: false, sessions: null, error: result.error };||=>||      return { reachable: true, sessions: [], error: null };"

mutate "a transport failure is reported as a successful request" \
  "macOS/tray-api.js" \
  "      return {
        ok: false,
        status: 0,
        data: null,
        error: String((transportError && transportError.message) || transportError),
      };||=>||      return { ok: true, status: 200, data: [], error: null };"

mutate "a missing TOTP secret still claims a token" \
  "macOS/tray-api.js" \
  "    if (!code) return { token: null, error: 'no TOTP secret available' };||=>||    if (!code) return { token: 'none', error: null };"

echo "--- attention detection ---"

mutate "a session waiting on the user no longer counts as attention" \
  "macOS/tray-status.js" \
  "const ATTENTION_STATUSES = Object.freeze(['question', 'finished_unread', 'dead']);||=>||const ATTENTION_STATUSES = Object.freeze(['finished_unread', 'dead']);"

mutate "a finished-unread session no longer counts as attention" \
  "macOS/tray-status.js" \
  "const ATTENTION_STATUSES = Object.freeze(['question', 'finished_unread', 'dead']);||=>||const ATTENTION_STATUSES = Object.freeze(['question', 'dead']);"

mutate "an unknown session status is counted as an alarm instead of an unknown" \
  "macOS/tray-status.js" \
  "const ATTENTION_STATUSES = Object.freeze(['question', 'finished_unread', 'dead']);||=>||const ATTENTION_STATUSES = Object.freeze(['question', 'finished_unread', 'dead', 'unknown']);"

mutate "an available update outranks a session needing attention" \
  "macOS/tray-status.js" \
  "  if (counts.attention > 0) {||=>||  if (updateStatus === 'update_available') {
    return { ...base, state: 'update', reason: 'an update is available' };
  }
  if (counts.attention > 0) {"

echo "--- server state ---"

mutate "a crash is reported as an ordinary stop" \
  "macOS/tray-status.js" \
  "    if (input && input.lastExitUnexpected) {||=>||    if (false) {"

echo "--- token handling ---"

mutate "the cached token is ignored, spending a TOTP code on every poll" \
  "macOS/tray-api.js" \
  "    if (this.accessToken && this.now() + marginMs < this.accessTokenExpiresAt) {||=>||    if (false) {"

mutate "changing the bind address keeps the old origin's token" \
  "macOS/tray-api.js" \
  "    this.baseUrl = next;
    this.forgetToken();||=>||    this.baseUrl = next;"

echo "--- icon assets ---"

mutate "a coloured state is flagged as a template image, discarding its colour" \
  "macOS/tray-status.js" \
  "    isTemplate: false,||=>||    isTemplate: true,"

mutate_restore_files
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"; exit 1
fi
echo "MUTATION CHECK PASSED"
