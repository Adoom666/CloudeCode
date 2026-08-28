#!/bin/bash
# Mutation check for the tmux three-outcome listing.
#
# A guard you have not proven can fail is not a guard. Every mutation below
# takes one place that says "I could not evaluate this" and makes it say "I
# evaluated it and the answer is zero" - which is the exact defect the whole
# change removes. Each one must turn the suite RED. A survivor means the
# test that supposedly covers that guard would also pass against the bug.
#
# The mutations come in three families:
#   A. the TYPE - unavailable() stops meaning unavailable
#   B. the SPLIT - "no server running" and a real error collapse together,
#      in each direction (a one-way test would miss one of them)
#   C. the CONSUMERS - the reconciler, the route and the client stop
#      honouring ok=False even though the type still reports it honestly
#
# Every mutated file is restored on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
LISTING="$ROOT/src/core/tmux_listing.py"
BACKEND="$ROOT/src/core/tmux_backend.py"
MANAGER="$ROOT/src/core/session_manager.py"
ROUTES="$ROOT/src/api/routes.py"
LAUNCHPAD="$ROOT/client/js/launchpad.js"

STDERR="$ROOT/src/core/tmux_stderr.py"

FILES=("$LISTING" "$STDERR" "$BACKEND" "$MANAGER" "$ROUTES" "$LAUNCHPAD")
mutate_arm_trap "$ROOT" "${FILES[@]}"

PY="$ROOT/venv/bin/python3"
if [ ! -x "$PY" ]; then PY="/Users/jsugamele/Development/CloudeCode/venv/bin/python3"; fi

survived=0
cannot_determine=0
killed=0

restore_all() {
  mutate_restore_files
}

# Runs the two suites that own this behaviour. A mutant is KILLED if either
# goes red. Scoped to these two files on purpose: the point is whether THESE
# tests can detect the bug, not whether the repo happens to notice elsewhere.
# Each suite runs through mutate_run so a signal that reaches only this
# script's own PID can still interrupt a suite that is mid-run (see
# scripts/ci/lib/mutate-trap.sh for why a plain foreground call cannot be
# interrupted that way).
run_suites() {
  mutate_run "$PY" -m pytest "$ROOT/tests/test_tmux_listing.py" \
    "$ROOT/tests/test_tmux_listing_consumers.py" -q >/dev/null 2>&1 || return 1
  mutate_run node "$ROOT/tests/test_running_sessions_unknown.node.mjs" >/dev/null 2>&1 || return 1
  return 0
}

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" expr="$3"
  restore_all
  python3 - "$file" "$expr" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "CANNOT_DETERMINE $name (target moved)"; cannot_determine=$((cannot_determine + 1)); return; fi
  if run_suites; then
    echo "SURVIVED $name"
    survived=$((survived + 1))
  else
    echo "killed   $name"
    killed=$((killed + 1))
  fi
}

# --- A. the type itself stops distinguishing the two outcomes ----------
mutate "unavailable() reports ok=True" "$LISTING" \
  "return cls(ok=False, sessions=[], reason=reason, detail=detail)||=>||return cls(ok=True, sessions=[], reason=reason, detail=detail)"

# --- B. the split collapses, in BOTH directions ------------------------
mutate "every failure is read as 'no server'" "$STDERR" \
  "    return classify_tmux_stderr(stderr_text) == STDERR_NO_SERVER||=>||    return True"

mutate "'no server' is read as a real error" "$STDERR" \
  "    return classify_tmux_stderr(stderr_text) == STDERR_NO_SERVER||=>||    return False"

mutate "classify_listing_failure always answers zero" "$LISTING" \
  "    if verdict == STDERR_NO_SERVER:||=>||    if True:"

# --- A2. the backend's own could-not-evaluate branches -----------------
mutate "missing tmux binary reports zero sessions" "$BACKEND" \
  "                TmuxListing.unavailable(
                    REASON_TMUX_MISSING, detail=\"tmux not found on PATH\"
                ),||=>||                TmuxListing.answered([]),"

mutate "a timed-out probe reports zero sessions" "$BACKEND" \
  "                TmuxListing.unavailable(
                    REASON_TIMEOUT,
                    detail=f\"tmux did not answer within {LIST_TIMEOUT_SECONDS}s\",
                ),||=>||                TmuxListing.answered([]),"

mutate "the listing subprocess loses its timeout" "$BACKEND" \
  "                timeout=LIST_TIMEOUT_SECONDS,||=>||                timeout=None,"

# --- C. the consumers stop honouring ok=False --------------------------
mutate "the reconciler prunes against an unavailable listing" "$MANAGER" \
  "        listing = coerce_listing(probe.discover_existing())
        if not listing.ok:||=>||        listing = coerce_listing(probe.discover_existing())
        if False:"

mutate "the attachable route answers 200 [] on a failed probe" "$ROUTES" \
  "    listing = coerce_listing(session_manager.list_attachable_sessions())
    if not listing.ok:||=>||    listing = coerce_listing(session_manager.list_attachable_sessions())
    if False:"

mutate "the client latches the listing back to ok" "$LAUNCHPAD" \
  "        st.ok = false;||=>||        st.ok = true;"

mutate "the attention block is never rendered" "$LAUNCHPAD" \
  "        if (!st || st.ok) return '';||=>||        if (st || !st.ok) return '';"

mutate "the heading reports a count it never measured" "$LAUNCHPAD" \
  "            el.textContent = 'count could not be determined';||=>||            el.textContent = \`\${(this.runningSessions || []).length} running\`;"

mutate "the failed fetch falls back to an empty list again" "$LAUNCHPAD" \
  "            this.runningSessions = [];
            this._noteListingUnknown('attachable',||=>||            this.runningSessions = [];
            if (false) this._noteListingUnknown('attachable',"

mutate "the failed live merge is swallowed again" "$LAUNCHPAD" \
  "            if (status !== 404) {||=>||            if (false) {"

restore_all
echo
echo "killed $killed, survived $survived"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
