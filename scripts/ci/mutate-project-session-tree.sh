#!/bin/bash
# Mutation check for S8: the home-screen project-to-session tree.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this build step can silently hand
# the user a wrong answer, and every one must turn the node suite red.
# Modelled on scripts/ci/mutate-s9-recent-and-pills.sh - same harness
# shape, same restore-on-exit discipline, same baseline gate.
#
# Client-only change (client/js/launchpad.js), so this mutates and
# re-runs only the node test, not pytest.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
NODE_TEST="tests/test_project_session_tree.node.mjs"

FILES=(
  "client/js/launchpad.js"
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

echo "--- BLOCK 1: 'none' and 'unknown' must never collapse into each other ---"

mutate "'none' collapsed into 'unknown' - an actionable answer becomes NEEDS ATTENTION" \
  "client/js/launchpad.js" \
  "            if (attribution === 'unknown') {||=>||            if (attribution === 'unknown' || attribution === 'none') {"

mutate "'unknown' collapsed into 'none' - an unproven answer renders as measured" \
  "client/js/launchpad.js" \
  "            if (attribution === 'unknown') {
                needsAttention.push({
                    session: s,
                    reason: 'working directory could not be read',
                });
            } else if (attribution === 'none') {||=>||            if (attribution === '__never_matches__') {
                needsAttention.push({
                    session: s,
                    reason: 'working directory could not be read',
                });
            } else if (attribution === 'none' || attribution === 'unknown') {"

echo "--- BLOCK 2: the missing-project guard must survive in the tree ---"

mutate "presence-disabled state is never computed, so a missing project's actions are never refused" \
  "client/js/launchpad.js" \
  "            const isDisabled = presenceState === 'missing' || presenceState === 'unreachable';||=>||            const isDisabled = false;"

mutate "the MISSING badge text is silently dropped" \
  "client/js/launchpad.js" \
  "                presenceBadge = \`<div class=\"project-presence-badge project-presence-badge-missing\">MISSING - folder not found</div>\`;||=>||                presenceBadge = '';"

echo "--- BLOCK 3: the child-row render must not be skipped ---"

mutate "child session rows are never rendered under their project, even when matched" \
  "client/js/launchpad.js" \
  "            const sessionsHtml = hasChildren
                ? \`<div class=\"project-node__sessions\" id=\"project-node-sessions-\${this._escapeHtml(nodeKey)}\" style=\"\${collapsed ? 'display:none;' : ''}\">\${children.map(s => this._renderTreeSessionRowHtml(s)).join('')}</div>\`
                : '';||=>||            const sessionsHtml = '';"

mutate "the toggle chevron is never rendered, so a populated project looks like it has no sessions" \
  "client/js/launchpad.js" \
  "            const chevronHtml = foldable||=>||            const chevronHtml = false && foldable"

echo "--- BLOCK 4: attribution listing failure must force NEEDS ATTENTION, never a guess ---"

mutate "a failed attribution fetch is silently ignored - sessions render as if attribution succeeded" \
  "client/js/launchpad.js" \
  "            if (!this.sessionAttributionListingOk) {||=>||            if (false) {"

mutate "a session missing from the attribution map is silently skipped instead of flagged" \
  "client/js/launchpad.js" \
  "                rec = this.sessionAttribution.get(s.name) || null;
            }
            if (!rec) {||=>||                rec = this.sessionAttribution.get(s.name) || {project_attribution: 'none', project_id: null};
            }
            if (false) {"

echo "--- BLOCK 5: an attribution row with no id must never be guessed onto a project ---"

mutate "a project_id-less row is silently dropped from NEEDS ATTENTION instead of flagged" \
  "client/js/launchpad.js" \
  "            } else {
                // Defensive: an attribution string that isn't 'none' or
                // 'unknown' but carries no id is not a shape this build
                // should trust - never guess which project it meant.
                needsAttention.push({
                    session: s,
                    reason: 'project attribution missing an id',
                });
            }||=>||            }"

echo "--- BLOCK 6: collapse state must be read on every render ---"

mutate "a node collapsed by the user renders expanded again on the next render" \
  "client/js/launchpad.js" \
  "            const collapsed = this._collapsedProjectNodes.has(nodeKey);||=>||            const collapsed = false;"

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
