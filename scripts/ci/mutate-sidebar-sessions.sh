#!/bin/bash
# Mutation check for feat/sidebar-sessions: pinning a session, a
# user-defined order, the density control, and the three-outcome
# obligations attached to all three.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this sidebar can silently hand the
# user a wrong answer, and every one must turn the node suite red.
# Modelled on scripts/ci/mutate-home-screen-mechanics.sh - same harness
# shape, same restore-on-exit discipline, same baseline gate.
#
# WHAT THIS SUITE CANNOT KILL, AND WHERE THAT IS COVERED INSTEAD: a
# mutation that changes a real PIXEL without changing the stylesheet text
# this suite reads (say, a padding that is still declared but no longer
# applies because a more specific rule beat it) is only caught by
# measuring a real box. That is scripts/verify_sidebar_sessions.py, in a
# real Chromium, and it is not optional - it is the check that found the
# home screen's docked offset losing to `#launchpad-screen`'s ID
# specificity while the class rule sat right above it looking correct.
#
# Client-only change, so this mutates and re-runs only the node suites.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
NODE_TESTS=(
  "tests/test_sidebar_sessions.node.mjs"
  "tests/test_session_sidebar_rows.node.mjs"
  "tests/test_session_sidebar_pin.node.mjs"
  "tests/test_session_ownership_badge.node.mjs"
  "tests/test_sidebar_groups_rename.node.mjs"
)

FILES=(
  "client/js/session-sidebar.js"
  "client/js/session-sidebar-rows.js"
  "client/js/session-sidebar-arrangement.js"
  "client/js/session-sidebar-store.js"
  "client/js/session-sidebar-groups.js"
  "client/js/session-sidebar-rename.js"
  "client/js/session-sidebar-clicks.js"
  "client/css/session-sidebar-groups.css"
  "client/js/session-sidebar-density.js"
  "client/js/session-sidebar-reorder.js"
  "client/js/session-sidebar-fetch.js"
  "client/js/session-listing-state.js"
  "client/js/app.js"
  "client/js/launchpad.js"
  "client/css/session-sidebar.css"
  "client/css/session-sidebar-density.css"
  "client/css/styles.css"
  "client/index.html"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# Run every node suite; non-zero from ANY of them is a kill.
run_suites() {
  local t
  for t in "${NODE_TESTS[@]}"; do
    mutate_run node "$t" >/dev/null 2>&1 || return 1
  done
  return 0
}

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline would make every mutant read as
# killed for free.
echo "--- baseline: every suite must be GREEN before anything is mutated ---"
if ! run_suites; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
echo "baseline green"

restore_all() {
    mutate_restore_files
}

# Apply one textual mutation, run the suites, expect RED.
#   mutate <name> <file> <old||=>||new>
# A target that no longer exists counts as SURVIVED, never as a skip: a
# mutant that could not be applied tested nothing.
mutate() {
  local name="$1" file="$2" expr="$3"
  restore_all
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
    echo "CANNOT_DETERMINE $name (target moved - anchor stale, mutant not evaluated)"
    cannot_determine=$((cannot_determine + 1))
    return
  fi
  if ! run_suites; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- BLOCK 1: a pinned session must actually reach the top, and stay ---"

mutate "the pinned band is not sorted first, so a pin only decorates" \
  "client/js/session-sidebar-arrangement.js" \
  "        return { rows: pinnedBand.concat(restBand), missing };||=>||        return { rows: restBand.concat(pinnedBand), missing };"

mutate "pinning stops partitioning at all - one band, pin is cosmetic" \
  "client/js/session-sidebar-arrangement.js" \
  "            (row.is_pinned ? pinnedBand : restBand).push(row);||=>||            restBand.push(row);"

mutate "the pin is never written to storage, so it dies on reload" \
  "client/js/session-sidebar-store.js" \
  "            localStorage.setItem(STORAGE_KEY, JSON.stringify({
                v: VERSION,
                pinned: nextPinned,
                order: nextOrder,
                collapsed: nextCollapsed,
            }));||=>||            void nextOrder;"

mutate "togglePin always pins, so nothing can be unpinned" \
  "client/js/session-sidebar-arrangement.js" \
  "        const next = !S.isPinned(name);||=>||        const next = true;"

mutate "the row never carries its pin, so the markup cannot draw it" \
  "client/js/session-sidebar-arrangement.js" \
  "            row.is_pinned = S.isPinned(row.name);||=>||            row.is_pinned = false;"

echo "--- BLOCK 2: the user order must survive the poll, the reload, and new rows ---"

mutate "the stored order is ignored and the incoming poll order wins" \
  "client/js/session-sidebar-arrangement.js" \
  "        const known = new Set(S.current().order);||=>||        const known = new Set();"

mutate "a session the user never arranged jumps to the TOP instead of the tail" \
  "client/js/session-sidebar-arrangement.js" \
  "        for (const row of list) {
            if (!known.has(row.name)) ordered.push(row);
        }||=>||        for (const row of list) {
            if (!known.has(row.name)) ordered.unshift(row);
        }"

mutate "a move is applied in memory but never persisted" \
  "client/js/session-sidebar-arrangement.js" \
  "        S.save(pinnedSet, mergeOrder(next));
        return { order: next, crossed, pinned: nowPinned };||=>||        return { order: next, crossed, pinned: nowPinned };"

# ITEM 65 REVERSED THE BAND-EDGE RULE, so the three mutations that used
# to live here are gone: they asserted a refusal the user has since asked
# us to remove. Reintroducing that refusal is now itself the defect, and
# the replacements below say so. The protection did not disappear with
# the rule - it MOVED, from "refuse the crossing" to "perform it and say
# so out loud", and each of the three ways that can silently fail gets a
# mutant.

mutate "a move REFUSES to cross the band edge again, so pinning is pointer-only" \
  "client/js/session-sidebar-arrangement.js" \
  "        const crossed = neighbourBand !== band;||=>||        const crossed = false;
        if (neighbourBand !== band) return null;"

mutate "a crossing move reorders but never moves the PIN, so the row snaps back" \
  "client/js/session-sidebar-arrangement.js" \
  "        const pinnedSet = crossed
            ? (nowPinned ? S.current().pinned.concat([name]) : S.current().pinned.filter((n) => n !== name))
            : S.current().pinned;||=>||        const pinnedSet = S.current().pinned;"

mutate "a DROP ignores the band it landed in, so dragging across does nothing" \
  "client/js/session-sidebar-arrangement.js" \
  "        const want = !!targetPinned;||=>||        const want = isPinnedNow(name);
        function isPinnedNow(n) { return S.isPinned(n); }"

mutate "a DROP always pins, so dragging OUT of the pinned group cannot unpin" \
  "client/js/session-sidebar-arrangement.js" \
  "        const pinnedSet = want
            ? S.current().pinned.concat([name])
            : S.current().pinned.filter((n) => n !== name);||=>||        const pinnedSet = S.current().pinned.concat([name]);"

# THE TWO DRAG MUTANTS THAT USED TO SIT HERE HAVE MOVED to
# scripts/ci/mutate-sidebar-groups.sh, which uses the real-Chromium
# verifier as its detector. They cannot be killed by a node suite: what
# they break is which BAND a pointer is over, and there is no pointer
# here. Leaving them in this file would have reported two survivors that
# are in fact covered - a false RED, which erodes the suite's credibility
# exactly as fast as a false green.

mutate "the slot-preserving merge is dropped, so a gone session loses its place" \
  "client/js/session-sidebar-arrangement.js" \
  "        let i = 0;
        for (const name of S.current().order) {
            if (visible.has(name)) {
                if (i < incoming.length) out.push(incoming[i++]);
            } else {
                out.push(name);
            }
        }
        while (i < incoming.length) out.push(incoming[i++]);||=>||        for (const name of incoming) out.push(name);
        for (const name of state.order) { if (!visible.has(name)) out.push(name); }"

echo "--- BLOCK 3: three outcomes on the stored arrangement ---"

mutate "unparseable JSON is treated as 'nothing stored', so the loss is silent" \
  "client/js/session-sidebar-store.js" \
  "            state = {
                status: 'unreadable',
                reason: 'stored value is not valid JSON',||=>||            state = {
                status: 'default',
                reason: null,"

mutate "a wrong-shaped pin/order list is accepted rather than refused" \
  "client/js/session-sidebar-store.js" \
  "        if (!isNameArray(parsed.pinned) || !isNameArray(parsed.order)) {||=>||        if (false) {"

mutate "a future schema version is guessed at instead of being refused" \
  "client/js/session-sidebar-store.js" \
  "        if (parsed.v !== VERSION) {||=>||        if (false) {"

mutate "storage that throws reports 'nothing stored' rather than 'could not read'" \
  "client/js/session-sidebar-store.js" \
  "            state = {
                status: 'unreadable',
                reason: 'storage unavailable',||=>||            state = {
                status: 'default',
                reason: null,"

mutate "the unreadable value is overwritten on load, destroying the evidence" \
  "client/js/session-sidebar-store.js" \
  "        if (raw === null || raw === undefined || raw === '') {||=>||        if (true) { localStorage.setItem(STORAGE_KEY, ''); }
        if (raw === null || raw === undefined || raw === '') {"

mutate "the CANNOT LOAD notice is never rendered, so the default masquerades as his" \
  "client/js/session-sidebar-rows.js" \
  "        if (!arrangement || arrangement.status !== 'unreadable') return '';||=>||        return '';"

mutate "the notice renders but says nothing about WHY" \
  "client/js/session-sidebar-rows.js" \
  "            \`<div class=\"session-sidebar-notice__detail\">\${reason}. showing the default order \` +||=>||            '<div class=\"session-sidebar-notice__detail\">showing the default order ' +"

mutate "the notice is shown even when the arrangement is fine - furniture, not a monitor" \
  "client/js/session-sidebar-rows.js" \
  "        if (!arrangement || arrangement.status !== 'unreadable') return '';||=>||        if (!arrangement) return '';"

echo "--- BLOCK 4: a gone session is reported, not dropped and not an error ---"

mutate "remembered names with no row are silently dropped" \
  "client/js/session-sidebar-arrangement.js" \
  "        const missing = remembered.filter((n) => !byName.has(n));||=>||        const missing = [];"

mutate "only the order is checked for gone names, so a gone PINNED one vanishes" \
  "client/js/session-sidebar-arrangement.js" \
  "        const remembered = S.dedupe(S.current().order.concat(S.current().pinned));||=>||        const remembered = S.dedupe(S.current().order);"

mutate "the held-slot count is never rendered, so nothing is observable" \
  "client/js/session-sidebar-rows.js" \
  "        if (!missing || !missing.length) return '';||=>||        return '';"

mutate "the note renders even with nothing missing - a check that never clears" \
  "client/js/session-sidebar-rows.js" \
  "        if (!missing || !missing.length) return '';||=>||        if (!missing) return '';"

echo "--- BLOCK 5: a failed listing is not an empty list ---"

mutate "zero rows after a FAILED probe renders the confident empty state" \
  "client/js/session-sidebar-rows.js" \
  "            if (listing && !listing.ok) return notice + attention;||=>||"

mutate "the attention block is emitted for a HEALTHY listing too" \
  "client/js/session-listing-state.js" \
  "        if (!listing || listing.ok) return '';||=>||        if (!listing) return '';"

mutate "the server's own reason is thrown away for a browser guess" \
  "client/js/session-listing-state.js" \
  "        if (d && typeof d === 'object' && typeof d.listing_reason === 'string' && d.listing_reason) {
            return d.listing_reason;
        }||=>||"

mutate "the detail is allowed to be a blank cell" \
  "client/js/session-listing-state.js" \
  "        if (d && typeof d === 'object') {
            if (typeof d.listing_detail === 'string' && d.listing_detail) return d.listing_detail;
            if (typeof d.message === 'string' && d.message) return d.message;
        }||=>||        return '';"

mutate "a failed ATTACHABLE probe is reported as a healthy listing" \
  "client/js/session-sidebar-fetch.js" \
  "            listing = window.SessionListingState
                ? window.SessionListingState.fromError(err, statusOf(err))
                : { ok: false, reason: 'probe_error', detail: 'the server could not be reached' };||=>||"

mutate "a failed LIVE list is escalated into a fleet-wide CANNOT DETERMINE" \
  "client/js/session-sidebar-fetch.js" \
  "            // No live backend for this tab is an ordinary state, not a
            // probe failure: the attachable rows above are still the
            // truth about what exists.
            console.warn('SessionSidebar: listSessions unavailable:', err && err.message);||=>||            listing = { ok: false, reason: 'probe_error', detail: String(err) };"

echo "--- BLOCK 6: density really changes the row, and never drops a pill state ---"

mutate "every density draws the same row - the control does nothing" \
  "client/js/session-sidebar-rows.js" \
  "        const inlineBadge = (mode === 'cozy') ? badgeHtml : '';||=>||        const inlineBadge = badgeHtml;"

mutate "detailed loses its second line, collapsing into cozy" \
  "client/js/session-sidebar-rows.js" \
  "        const secondLine = mode === 'detailed'||=>||        const secondLine = false"

# ITEM 63 REMOVED THE PILL FROM THIS SURFACE, so the six pill mutations
# that lived here no longer have a target. They are replaced by the
# mutants for what the removal has to preserve: the pill must stay gone
# from the sidebar, the badge must be emitted exactly once wherever it
# sits, and the HOME SCREEN's pill - a different builder that this change
# must not touch - must keep rendering a SINGLE tilde.

mutate "the badge is emitted on BOTH lines at detailed, so the row draws it twice" \
  "client/js/session-sidebar-rows.js" \
  "        const inlineBadge = (mode === 'cozy') ? badgeHtml : '';||=>||        const inlineBadge = (mode === 'compact') ? '' : badgeHtml;"

mutate "detailed's second line loses the badge, so the line is about nothing" \
  "client/js/session-sidebar-rows.js" \
  "            ? ('<div class=\"session-sidebar-row-meta\">'
                + badgeHtml||=>||            ? ('<div class=\"session-sidebar-row-meta\">'
                + ''"

mutate "the home screen's pill builder adds a literal tilde back, rendering ~~claude" \
  "client/js/launchpad.js" \
  "        const label = known ? agentFamily : 'unknown family';||=>||        const label = known ? \`~\${agentFamily}\` : 'unknown family';"

mutate "the stylesheet drops the guess tilde, so a guess and a fact read alike" \
  "client/css/styles.css" \
  ".family-pill--guess::before {
    content: \"~\";||=>||.family-pill--guess::before {
    content: \"\";"

mutate "the sidebar row starts drawing a family pill again" \
  "client/js/session-sidebar-rows.js" \
  "            inlineBadge +||=>||            inlineBadge + '<span class=\"family-pill\" data-family-source=\"x\">x</span>' +"

mutate "compact and cozy get the same declared padding" \
  "client/css/session-sidebar-density.css" \
  ".session-sidebar-panel[data-density=\"compact\"] .session-sidebar-row-main {
    padding: 2px 8px;||=>||.session-sidebar-panel[data-density=\"compact\"] .session-sidebar-row-main {
    padding: 10px 10px;"

mutate "detailed and cozy get the same declared padding" \
  "client/css/session-sidebar-density.css" \
  ".session-sidebar-panel[data-density=\"detailed\"] .session-sidebar-row-main {
    padding: 10px 10px 2px 10px;||=>||.session-sidebar-panel[data-density=\"detailed\"] .session-sidebar-row-main {
    padding: 10px 10px;"

mutate "the density is never persisted, so the choice dies on reload" \
  "client/js/session-sidebar-density.js" \
  "            localStorage.setItem(STORAGE_KEY, mode);||=>||"

mutate "an unrecognised stored density is applied verbatim" \
  "client/js/session-sidebar-density.js" \
  "        if (MODES.indexOf(raw) === -1) {||=>||        if (false) {"

mutate "the panel never gets data-density, so no CSS rule can key off it" \
  "client/js/session-sidebar-density.js" \
  "        if (panel) panel.setAttribute('data-density', mode);||=>||"

echo "--- BLOCK 7: no RESTART for a lifecycle nobody measured ---"

mutate "a restart control appears on rows whose lifecycle is unknowable" \
  "client/js/session-sidebar-rows.js" \
  "            pinButtonHtml(r.name, !!r.is_pinned) +||=>||            pinButtonHtml(r.name, !!r.is_pinned) +
            '<button class=\"session-sidebar-row-restart\" title=\"restart\">restart</button>' +"

echo "--- BLOCK 8: the reorder must be operable without a mouse ---"

mutate "Alt+Arrow no longer moves, so reorder is pointer-only" \
  "client/js/session-sidebar-reorder.js" \
  "            if (e.altKey) { moveRow(name, step); return; }||=>||"

mutate "a bare arrow REORDERS instead of navigating, so the list cannot be browsed" \
  "client/js/session-sidebar-reorder.js" \
  "            if (e.altKey) { moveRow(name, step); return; }||=>||            { moveRow(name, step); return; }"

mutate "focus is not restored after the repaint, so a held key moves a row once" \
  "client/js/session-sidebar-reorder.js" \
  "        setFocusRow(name, true);
    }

    /**
     * Description: move the named row one slot up or down||=>||    }

    /**
     * Description: move the named row one slot up or down"

mutate "every row is a tab stop, so tabbing walks the whole list" \
  "client/js/session-sidebar-reorder.js" \
  "            el.setAttribute('tabindex', el === target ? '0' : '-1');||=>||            el.setAttribute('tabindex', '0');"

mutate "the pin key is dropped, so pinning needs a mouse" \
  "client/js/session-sidebar-reorder.js" \
  "        if (e.key === 'p' || e.key === 'P') {||=>||        if (false) {"

mutate "Home and End stop working" \
  "client/js/session-sidebar-reorder.js" \
  "        if (e.key === 'Home' || e.key === 'End') {||=>||        if (false) {"

mutate "Enter no longer activates a row, so the keyboard cannot switch conversations" \
  "client/js/session-sidebar-reorder.js" \
  "            if (sidebar) sidebar.activateRow(row);||=>||"

mutate "a refused move says nothing, so the user keeps pressing a dead key" \
  "client/js/session-sidebar-reorder.js" \
  "            announce(\`\${name} is already at the \${delta < 0 ? 'top' : 'bottom'} of the list\`);||=>||"

mutate "a poll tick repaints mid-drag, reordering under the user's finger" \
  "client/js/session-sidebar.js" \
  "        if (window.SessionSidebarReorder && window.SessionSidebarReorder.isDragging()) return;||=>||"

echo "--- BLOCK 9: the bar on the home screen ---"

mutate "the home screen hides the sidebar again" \
  "client/js/app.js" \
  "        if (window.SessionSidebar) {
            window.SessionSidebar.setActiveSession(null, null);
            window.SessionSidebar.show();
        }
        this.currentScreen = 'launchpad';||=>||        if (window.SessionSidebar) window.SessionSidebar.hide();
        this.currentScreen = 'launchpad';"

mutate "leaving a screen persists 'closed', so a pinned bar comes back closed" \
  "client/js/session-sidebar.js" \
  "        this.close({ persist: false });||=>||        this.close();"

mutate "close() stops persisting entirely, so a real user close does not stick" \
  "client/js/session-sidebar.js" \
  "        const persist = !opts || opts.persist !== false;||=>||        const persist = false;"

mutate "the home screen's docked offset rule is dropped - the bar covers the content" \
  "client/css/session-sidebar.css" \
  "body.session-sidebar-pinned #launchpad-screen {||=>||body.session-sidebar-pinned #launchpad-screen-DISABLED {"

mutate "the docked offset REPLACES the screen padding instead of adding to it" \
  "client/css/session-sidebar.css" \
  "    padding-left: calc(var(--sidebar-dock-w) + var(--screen-pad-x));||=>||    padding-left: var(--sidebar-dock-w);"

mutate "the screen goes back to a hardcoded 20px, so the two numbers drift apart" \
  "client/css/styles.css" \
  "#launchpad-screen {
    flex-direction: column;
    padding: var(--screen-pad-x);||=>||#launchpad-screen {
    flex-direction: column;
    padding: 20px;"

echo "--- BLOCK 10: the repaint must actually see the change ---"

mutate "the signature ignores density, so switching mode paints nothing" \
  "client/js/session-sidebar-rows.js" \
  "            density: density || 'cozy',||=>||            density: 'cozy',"

mutate "the signature ignores the pin, so a pin does not paint until something else moves" \
  "client/js/session-sidebar-rows.js" \
  "                pinned: !!r.is_pinned,||=>||"

# REPLACES a provably-equivalent mutant. The original deleted an explicit
# `i` index field from the mapped row. That mutant was unkillable by
# construction, not by a gap in the tests: signature() maps in array order
# and JSON.stringify preserves array order, so two different orderings of
# the same rows already serialise differently, and an index fully
# determined by position adds nothing a comparison could ever see. The
# redundant field was removed from the code; this mutant attacks what
# position-sensitivity ACTUALLY rests on - that the row order is
# serialised as given and never normalised on the way in.
mutate "the signature sorts rows before hashing, so a reorder never repaints" \
  "client/js/session-sidebar-rows.js" \
  "            rows: (rows || []).map((r) => ({||=>||            rows: (rows || []).slice().sort((x, y) => String(x.name).localeCompare(String(y.name))).map((r) => ({"

mutate "the signature ignores the listing verdict, so a probe failure never paints" \
  "client/js/session-sidebar-rows.js" \
  "            listing: listing && !listing.ok
                ? ['unavailable', listing.reason || '', listing.detail || '']
                : ['ok'],||=>||            listing: ['ok'],"

echo "--- BLOCK 11: the markup contract ---"

mutate "the density control is dropped from the shipped markup" \
  "client/index.html" \
  "id=\"session-sidebar-density\"||=>||id=\"session-sidebar-density-DISABLED\""

mutate "the live region is dropped, so reorder feedback is visual only" \
  "client/index.html" \
  "id=\"session-sidebar-live\"||=>||id=\"session-sidebar-live-DISABLED\""

mutate "the list loses its listbox role, so the roving tabindex means nothing" \
  "client/index.html" \
  "<div id=\"session-sidebar-list\" class=\"session-sidebar-list\"
             role=\"listbox\"||=>||<div id=\"session-sidebar-list\" class=\"session-sidebar-list\"
             data-role=\"listbox\""

mutate "the arrangement module is served AFTER the sidebar that reads it" \
  "client/index.html" \
  "    <script src=\"/static/js/session-sidebar-arrangement.js\"></script>||=>||"

mutate "the reorder module is never served at all" \
  "client/index.html" \
  "    <script src=\"/static/js/session-sidebar-reorder.js\"></script>||=>||"

mutate "the density stylesheet is never linked, so no density has a box" \
  "client/index.html" \
  "    <link rel=\"stylesheet\" href=\"/static/css/session-sidebar-density.css\" />||=>||"

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
