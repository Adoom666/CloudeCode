#!/bin/bash
# Mutation check for fix/header-icons-and-menu: the help button's white
# square, its move beside the title, the home-screen sidebar toggle's
# placement, the rename affordance's three states, and folding
# open-from-folder into the new-claude-project flow.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this header can silently hand the
# user a wrong answer, and every one must turn the suite red.
#
# WHY THIS SCRIPT RUNS TWO CHECKERS, unlike its siblings. The defect this
# branch started from was INVISIBLE to the DOM: a button whose markup,
# class list, aria and inline SVG were all correct while it painted as a
# near-white user-agent square. So a mutant is killed if EITHER the fast
# structural suite (tests/test_header_help_and_toggle.node.mjs) OR the
# real-browser measurement (scripts/verify_header_icons_and_menu.py, which
# reads getComputedStyle in headless Chromium) goes red. Several mutants
# below are deliberately invisible to the node suite and can only be
# caught by the browser - they are marked PIXEL-ONLY, and they are the
# reason the browser checker is not optional here.
#
# Client-only change (client/js/launchpad.js, client/css/styles.css,
# client/index.html), so this mutates and re-runs only those checkers,
# not pytest.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
NODE_TEST="tests/test_header_help_and_toggle.node.mjs"
VERIFIER="scripts/verify_header_icons_and_menu.py"

# playwright is NOT importable under the project venv. Find an interpreter
# that has it. Without one the browser half cannot run, and a mutation run
# that silently degraded to structure-only would report a kill count for
# checks it never performed - the exact false green this branch is about.
PYBIN=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import playwright' >/dev/null 2>&1; then
    PYBIN="$cand"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "CANNOT DETERMINE: no interpreter with playwright was found, so the"
  echo "pixel half of this suite cannot run. Refusing to report a kill count"
  echo "for checks that were never performed."
  exit 2
fi
echo "browser checker interpreter: $PYBIN"

FILES=(
  "client/js/launchpad.js"
  "client/css/styles.css"
  "client/index.html"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

restore_all() {
    mutate_restore_files
}

# Run both checkers. Returns 0 when BOTH are green.
run_suite() {
  mutate_run node "$NODE_TEST" >/dev/null 2>&1 || return 1
  mutate_run "$PYBIN" "$VERIFIER" >/dev/null 2>&1 || return 1
  return 0
}

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline would make every mutant read as
# killed for free.
echo "--- baseline: both checkers must be GREEN before anything is mutated ---"
if ! mutate_run node "$NODE_TEST" >/dev/null 2>&1; then
  echo "BASELINE IS RED (node suite). Refusing to run."
  exit 2
fi
if ! mutate_run "$PYBIN" "$VERIFIER" >/dev/null 2>&1; then
  echo "BASELINE IS RED (browser verifier). Refusing to run."
  exit 2
fi
echo "baseline green (structure + pixels)"

# Apply one textual mutation, run both checkers, expect RED.
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
  if ! run_suite; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- BLOCK 1: the white square (61a) ---"

mutate "the original bug: the help button opts into no class and falls to the user-agent stylesheet" \
  "client/index.html" \
  'id="launchpad-help-btn" class="btn-icon"||=>||id="launchpad-help-btn"'

# PIXEL-ONLY. The class is still on the element and every DOM assertion
# still holds; only the painted result changes.
mutate "PIXEL-ONLY: .btn-icon keeps its class but loses the background that hides the UA square" \
  "client/css/styles.css" \
  '.btn-icon {
    background: var(--color-accent-bg);||=>||.btn-icon {
    background: ButtonFace;'

# PIXEL-ONLY. A square icon button among round ones - the "circles" report.
mutate "PIXEL-ONLY: icon buttons stop being round" \
  "client/css/styles.css" \
  'border-radius: var(--radius-full);
    cursor: pointer;||=>||border-radius: 0;
    cursor: pointer;'

echo "--- BLOCK 2: beside the title (61b) ---"

mutate "the help button goes back to the header corner, outside the title" \
  "client/index.html" \
  '<button type="button" id="launchpad-help-btn" class="btn-icon"||=>||<button type="button" hidden id="launchpad-help-btn-moved" class="btn-icon"'

# PIXEL-ONLY. Still a child of the h1, still after the text, but no longer
# beside it - only a measured gap can see this.
mutate "PIXEL-ONLY: the help button drifts far from the title text" \
  "client/css/styles.css" \
  'flex-shrink: 0;
    margin-left: 8px;||=>||flex-shrink: 0;
    margin-left: 400px;'

mutate "the help button becomes shrinkable, so title-fit's budget and the real layout disagree" \
  "client/css/styles.css" \
  'flex-shrink: 0;
    margin-left: 8px;||=>||flex-shrink: 1;
    margin-left: 8px;'

echo "--- BLOCK 3: the sidebar toggle on the home screen (59) ---"

mutate "the toggle stops being ordered to the content edge and the spacer pushes it inboard again" \
  "client/css/styles.css" \
  '.header--home #session-sidebar-toggle:not(.hidden) {
    order: -1;
}||=>||.header--home #session-sidebar-toggle:not(.hidden) {
    order: 0;
}'

mutate "the spacer stops giving back the toggle width, so the title goes off centre" \
  "client/css/styles.css" \
  'width: calc(var(--home-header-flank-w) - var(--home-header-toggle-w));||=>||width: var(--home-header-flank-w);'

mutate "the spacer over-compensates - off centre the other way" \
  "client/css/styles.css" \
  '--home-header-toggle-w: 36px;||=>||--home-header-toggle-w: 72px;'

mutate "the compensation becomes unconditional, breaking the toggle-hidden case" \
  "client/css/styles.css" \
  '.header--home:has(#session-sidebar-toggle:not(.hidden)) .header-home-spacer {||=>||.header--home .header-home-spacer.always {'

echo "--- BLOCK 4: the rename affordance's three states (68) ---"

mutate "the original bug: the pencil is gated on session_id and silently vanishes" \
  "client/js/launchpad.js" \
  'const renamePencil = this._renderRenamePencilHtml(s, escapedName);||=>||const renamePencil = s.session_id ? this._renderRenamePencilHtml(s, escapedName) : "";'

mutate "a genuinely unknown ownership is folded into EXTERNAL, inventing an answer" \
  "client/js/launchpad.js" \
  'const reason = s.created_by_cloude == null||=>||const reason = false'

mutate "the unavailable pencil stops saying why, leaving a dimmed control with no explanation" \
  "client/js/launchpad.js" \
  ' aria-label="${this._escapeHtml(reason)}"`
            + ` title="${this._escapeHtml(reason)}"||=>|| aria-label=""`
            + ` title=""'

mutate "the unavailable pencil stops swallowing its click and opens the session instead" \
  "client/js/launchpad.js" \
  "if (e.target.closest('.running-session-rename-unavailable')) {
                e.stopPropagation();
                return;
            }||=>||if (false) {
                return;
            }"

mutate "the unavailable pencil shares the live class, so it can reach the rename call" \
  "client/js/launchpad.js" \
  'running-session-rename-unavailable" aria-disabled="true"||=>||running-session-rename" aria-disabled="true"'

echo "--- BLOCK 5: the add menu (53b) ---"

mutate "'open from folder' comes back as a top-level add-menu item" \
  "client/js/launchpad.js" \
  '<button class="new-fab__item" type="button" role="menuitem" data-action="connect-openclaw" tabindex="-1">||=>||<button class="new-fab__item" type="button" role="menuitem" data-action="open-folder" tabindex="-1"><span class="new-fab__label">open from folder</span></button>
                                <button class="new-fab__item" type="button" role="menuitem" data-action="connect-openclaw" tabindex="-1">'

mutate "the folder option disappears from the new-claude-project chooser" \
  "client/js/launchpad.js" \
  "                { key: 'folder', label: 'open an existing folder', sub: 'a folder already on this machine' },||=>||"

mutate "the folder choice is offered but routes nowhere" \
  "client/js/launchpad.js" \
  "if (how === 'folder') return this.openProjectFromFolder();||=>||"

mutate "the new-claude-project item loses the real app icon file" \
  "client/js/launchpad.js" \
  'src="/static/assets/icons/header-icon.png" srcset="/static/assets/icons/header-icon.png 1x, /static/assets/icons/header-icon@2x.png 2x"||=>||src="/static/assets/icons/nope.png"'

restore_all

echo
echo "killed:   $killed"
echo "survived: $survived"
echo "cannot determine: $cannot_determine"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "RESULT: FAIL - $survived mutant(s) survived, $cannot_determine could not be evaluated"
  exit 1
fi
echo "RESULT: PASS - every mutant was killed"
exit 0
