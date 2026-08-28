#!/bin/bash
# Mutation check for feat/home-screen-mechanics: the home screen's fold,
# its slim project row, its renamed projects section, its header help
# control, its restructured add menu, and the two paint fixes.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this screen can silently hand the
# user a wrong answer, and every one must turn the node suite red.
# Modelled on scripts/ci/mutate-project-session-tree.sh - same harness
# shape, same restore-on-exit discipline, same baseline gate.
#
# Client-only change (client/js/launchpad.js, client/css/styles.css,
# client/index.html), so this mutates and re-runs only the node test, not
# pytest.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
NODE_TEST="tests/test_home_screen_mechanics.node.mjs"

FILES=(
  "client/js/launchpad.js"
  "client/css/styles.css"
  "client/index.html"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline would make every mutant read as
# killed for free.
echo "--- baseline: the suite must be GREEN before anything is mutated ---"
if ! mutate_run node "$NODE_TEST" >/dev/null 2>&1; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
echo "baseline green"

restore_all() {
    mutate_restore_files
}

# Apply one textual mutation, run the node suite, expect RED.
#   mutate <name> <file> <old||=>||new>
# A target that no longer exists counts as SURVIVED, never as a skip.
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
  if ! mutate_run node "$NODE_TEST" >/dev/null 2>&1; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- BLOCK 1: the fold must actually move pixels, and the right ones ---"

mutate "the original bug: the fold walks to the toggle's sibling and finds the wrong element" \
  "client/js/launchpad.js" \
  "        const node = toggle.closest('.project-node');
        if (!node) return false;
        const display = collapsed ? 'none' : '';
        node.querySelectorAll('.project-node__sessions').forEach((el) => {
            el.style.display = display;
        });||=>||        const node = toggle.closest('.project-node');
        if (!node) return false;
        const display = collapsed ? 'none' : '';
        const sib = toggle.nextElementSibling;
        if (sib && sib.classList && sib.classList.contains('project-node__sessions')) {
            sib.style.display = display;
        }"

mutate "the fold flips aria-expanded but never touches the children" \
  "client/js/launchpad.js" \
  "        node.querySelectorAll('.project-node__sessions').forEach((el) => {
            el.style.display = display;
        });||=>||"

mutate "the fold hides the sessions but leaves the description behind" \
  "client/js/launchpad.js" \
  "        node.querySelectorAll('.project-description').forEach((el) => {
            el.style.display = display;
        });||=>||"

mutate "a toggle with no node root reports SUCCESS - a fold nobody performed" \
  "client/js/launchpad.js" \
  "        if (!node) return false;||=>||        if (!node) return true;"

mutate "the collapsed description springs back open on the next render" \
  "client/js/launchpad.js" \
  "            const descriptionHtml = hasDescription
                ? \`<div class=\"project-description\"\${collapsed ? ' style=\"display:none;\"' : ''}>\${description}</div>\`||=>||            const descriptionHtml = hasDescription
                ? \`<div class=\"project-description\">\${description}</div>\`"

echo "--- BLOCK 2: the slim row ---"

mutate "the 'no description' filler line comes back" \
  "client/js/launchpad.js" \
  "            const rawDescription = (project.description || '').trim();||=>||            const rawDescription = (project.description || 'no description').trim();"

mutate "an empty description renders an empty element that still costs a line" \
  "client/js/launchpad.js" \
  "            const hasDescription = rawDescription.length > 0;||=>||            const hasDescription = true;"

mutate "a description is interpolated raw again" \
  "client/js/launchpad.js" \
  "            const description = this._escapeHtml(rawDescription);||=>||            const description = rawDescription;"

mutate "a description-only project loses its fold control" \
  "client/js/launchpad.js" \
  "            const foldable = hasChildren || hasDescription;||=>||            const foldable = hasChildren;"

mutate "a childless project gets a count chip claiming zero sessions" \
  "client/js/launchpad.js" \
  "            const countHtml = hasChildren||=>||            const countHtml = true || hasChildren"

echo "--- BLOCK 3: naming ---"

mutate "the section calls itself a recency list again" \
  "client/js/launchpad.js" \
  "                            projects
                        </button>||=>||                            recent projects
                        </button>"

echo "--- BLOCK 4: the help control ---"

mutate "the header help button is dropped from the markup" \
  "client/index.html" \
  "id=\"launchpad-help-btn\"||=>||id=\"launchpad-help-btn-DISABLED\""

mutate "the header control is wired to a copy instead of the live disclosure" \
  "client/js/launchpad.js" \
  "            const details = document.querySelector('#launchpad-screen .adopt-disclosure');||=>||            const details = document.getElementById('adopt-disclosure-clone');"

mutate "a missing header control is reported as if it had been wired" \
  "client/js/launchpad.js" \
  "            console.warn('Launchpad: header help button missing - help control not wired');
            return false;||=>||            return true;"

mutate "the in-pane summary is left in the layout, so there are two help controls" \
  "client/css/styles.css" \
  "#launchpad-screen .adopt-disclosure > summary {
    display: none;
}||=>||#launchpad-screen .adopt-disclosure > summary {
    display: block;
}"

mutate "the help button is shown on every screen, not just home" \
  "client/css/styles.css" \
  ".header--home #launchpad-help-btn {
    display: inline-flex;||=>||.header--home #launchpad-help-btn {
    display: block;"

echo "--- BLOCK 5: the add menu ---"

# REMOVED 2026-08-20: "clone from github goes back to being a peer of new
# project" used to live here, targeting a top-level
# data-action="open-folder" <button> as the anchor to reintroduce a
# sibling data-action="clone-github" <button> next to it. That anchor was
# rewritten when both "open from folder" and "clone from github" were
# folded out of the top-level add menu and into the New Claude Project
# chooser's item list - there is no more peer-level open-folder button to
# anchor on, so this mutant's sed target has been permanently stale since
# that refactor. Per the target-moved handling above it reported
# CANNOT_DETERMINE, not SURVIVED, but a mutant that can never again find
# its target is not "could not evaluate this run" - it is dead code.
# Deleted rather than repaired: the exact regression it was written to
# catch (clone-github reappearing as a top-level menu peer) is already
# covered by scripts/verify_home_mechanics.py ITEM 53, which asserts
# directly against the rendered DOM that no top-level item has
# data-action="clone-github"; and the symmetrical case for the other
# folded-in item is exercised live by
# scripts/ci/mutate-header-icons-and-menu.sh's "'open from folder' comes
# back as a top-level add-menu item" mutant. Repairing this target to
# match the current markup would only duplicate that existing coverage.

mutate "the top item goes back to the unexplained 'create new project' name" \
  "client/js/launchpad.js" \
  "                                    <span class=\"new-fab__label\">new claude project</span>||=>||                                    <span class=\"new-fab__label\">create new project</span>"

mutate "the top item's icon is a hand-drawn path again instead of the real asset" \
  "client/js/launchpad.js" \
  "                                        <img class=\"new-fab__icon-img\" src=\"/static/assets/icons/header-icon.png\" srcset=\"/static/assets/icons/header-icon.png 1x, /static/assets/icons/header-icon@2x.png 2x\" alt=\"\" />||=>||                                        <svg viewBox=\"0 0 24 24\"><path d=\"M13 2L3 12l9 9 10-10V2z\"/></svg>"

mutate "new session with zero projects opens an empty picker instead of saying so" \
  "client/js/launchpad.js" \
  "        if (projects.length === 0) {||=>||        if (false) {"

mutate "a project list that could not be read is reported as 'you have no projects'" \
  "client/js/launchpad.js" \
  "        if (this.projectsListingOk === false) {||=>||        if (false) {"

mutate "the listing latch is never set false, so a failed fetch looks like an empty list" \
  "client/js/launchpad.js" \
  "            this.projectsListingOk = false;||=>||            this.projectsListingOk = true;"

mutate "a MISSING project is offered as a launchable choice" \
  "client/js/launchpad.js" \
  "                disabled: presence === 'missing' || presence === 'unreachable',||=>||                disabled: false,"

mutate "MISSING and CANNOT DETERMINE collapse into one reason string" \
  "client/js/launchpad.js" \
  "            const reason = presence === 'missing'
                ? 'MISSING - folder not found'||=>||            const reason = presence === 'missing'
                ? 'CANNOT DETERMINE - folder not found'"

mutate "clone from github is dropped out of the new-claude-project flow entirely" \
  "client/js/launchpad.js" \
  "                { key: 'clone', label: 'clone from github', sub: 'start from an existing repository' },||=>||"

mutate "the choice modal draws rows even when there are none to draw" \
  "client/js/launchpad.js" \
  "            const rowsHtml = items.length||=>||            const rowsHtml = true"

echo "--- BLOCK 6: the two paint fixes ---"

mutate "the mismatched border-left comes back, and with it the corner bleed" \
  "client/css/styles.css" \
  "    border: 1px solid var(--color-border);
    box-shadow: inset 3px 0 0 var(--color-accent);
    padding: 14px 40px 14px 16px;||=>||    border: 1px solid var(--color-border);
    border-left: 3px solid var(--color-accent);
    padding: 14px 40px 14px 16px;"

mutate "hover forgets to re-declare the rail, so the accent edge blinks off on hover" \
  "client/css/styles.css" \
  "    box-shadow: inset 3px 0 0 var(--color-accent), 0 0 8px var(--color-accent-border-soft);||=>||    box-shadow: 0 0 8px var(--color-accent-border-soft);"

mutate "MISSING and CANNOT DETERMINE are painted the same rail colour" \
  "client/css/styles.css" \
  ".project-item.project-presence-unreachable {
    box-shadow: inset 3px 0 0 var(--color-warning);||=>||.project-item.project-presence-unreachable {
    box-shadow: inset 3px 0 0 var(--color-danger);"

mutate "the themed home row gets its 3px rail back beside the ownership border" \
  "client/css/styles.css" \
  ".launchpad-container .running-session-row[data-session-theme] {
    box-shadow: inset 0 0 0 1px var(--session-theme-ring);||=>||.launchpad-container .running-session-row[data-session-theme] {
    box-shadow: inset 3px 0 0 var(--session-theme-accent), inset 0 0 0 1px var(--session-theme-ring);"

mutate "the project card drops back to an 8 percent fill" \
  "client/css/styles.css" \
  "    background-color: color-mix(in srgb, var(--color-bg, #1e1e1e) 80%, transparent);
    background-image: linear-gradient(var(--color-accent-bg-soft), var(--color-accent-bg-soft));
    /* ITEM 37 - ONE BORDER, ONE COLOUR.||=>||    background: var(--color-accent-bg-soft);
    /* ITEM 37 - ONE BORDER, ONE COLOUR."

mutate "hover repaints the row with the shorthand, dropping the fill to 14 percent" \
  "client/css/styles.css" \
  ".running-session-row:hover {
    background-image: linear-gradient(rgba(215, 119, 87, 0.14), rgba(215, 119, 87, 0.14));||=>||.running-session-row:hover {
    background: rgba(215, 119, 87, 0.14);"

mutate "the fill is hardcoded for a dark theme instead of read from the theme token" \
  "client/css/styles.css" \
  "    /* ITEM 41, same two-layer fill as .project-item / .running-session-row. */
    background-color: color-mix(in srgb, var(--color-bg, #1e1e1e) 80%, transparent);||=>||    background-color: rgba(30, 30, 30, 0.8);"

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
