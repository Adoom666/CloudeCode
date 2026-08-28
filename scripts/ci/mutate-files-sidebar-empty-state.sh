#!/bin/bash
# Mutation check for the files sidebar's empty-state fix
# (fix/files-sidebar-empty-state): the launcher-context bug, the two
# removed bookkeeping strings, and the empty-vs-unreadable distinction
# THE THREE-OUTCOME RULE (CLAUDE.md) requires.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one specific way this fix can silently regress, and
# every one must turn at least one of the two node suites this feature
# owns red. Modelled on scripts/ci/mutate-project-session-tree.sh - same
# harness shape, same restore-on-exit discipline, same baseline gate.
#
# Client-only change (client/js/config-editor-roots.js,
# client/js/config-editor-panel.js), so this mutates and re-runs only the
# two node tests, not pytest.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1
NODE_TESTS=(
  "tests/test_config_editor_roots.node.mjs"
  "tests/test_files_sidebar_render.node.mjs"
)

FILES=(
  "client/js/config-editor-roots.js"
  "client/js/config-editor-panel.js"
)

mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0
cannot_determine=0
killed=0

run_suites() {
  for t in "${NODE_TESTS[@]}"; do
    if ! mutate_run node "$t" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

# BASELINE GATE. A mutation run measures the DIFFERENCE between a green
# suite and a mutated one; a red baseline would make every mutant read as
# killed for free.
echo "--- baseline: both suites must be GREEN before anything is mutated ---"
if ! run_suites; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
echo "baseline green"

restore_all() {
    mutate_restore_files
}

# Apply one textual mutation, run both node suites, expect at least one RED.
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
  if ! run_suites; then
    killed=$((killed + 1))
    echo "killed   $name"
    return
  fi
  echo "SURVIVED $name"
  survived=$((survived + 1))
}

echo "--- BLOCK 1: the launcher must never resolve a stale/detached session as a project ---"

mutate "sessionActive check dropped - a detached session's stale working_dir resolves again on the launcher" \
  "client/js/config-editor-roots.js" \
  "    if (!tc || !tc.sessionActive || !tc._currentSession) return { path: null, reason: 'no-session' };||=>||    if (!tc || !tc._currentSession) return { path: null, reason: 'no-session' };"

echo "--- BLOCK 2: 'no-session' on the launcher must stay silent, never narrated ---"

mutate "the removed launcher notice comes back for 'no-session'" \
  "client/js/config-editor-roots.js" \
  "function projectRootsNotice(reason) {
    if (reason === 'no-working-dir') {||=>||function projectRootsNotice(reason) {
    if (reason === 'no-session') {
        return 'no session attached, so only ~/.claude is listed.';
    }
    if (reason === 'no-working-dir') {"

echo "--- BLOCK 3: a project with no .claude/ (measured absence) must render nothing, not a notice ---"

mutate "an absent project .claude root gets a notice again instead of silently rendering nothing" \
  "client/js/config-editor-panel.js" \
  "            if (err.status === 400 && rootDef.id === 'project') {
                return null;
            }||=>||            if (err.status === 400 && rootDef.id === 'project') {
                const li = document.createElement('li');
                li.appendChild(this._errorEl(rootDef.label + ': not present in ' + projectPath));
                return li;
            }"

echo "--- BLOCK 4: a vanished workdir (could-not-evaluate) must never collapse into silence ---"

mutate "workdir's own could-not-evaluate 400 is silently dropped like a measured absence" \
  "client/js/config-editor-panel.js" \
  "            if (err.status === 400 && rootDef.id === 'workdir') {
                const li = document.createElement('li');
                li.appendChild(this._errorEl(
                    window.ConfigEditorRoots.workdirUnavailableNotice(rootDef.label, projectPath),
                ));
                return li;
            }||=>||            if (err.status === 400 && rootDef.id === 'workdir') {
                return null;
            }"

echo "--- BLOCK 5: a root that resolves to zero entries must render as a normal (empty) node, never be dropped ---"

mutate "the old empty-root bug returns - a genuinely empty, successfully-read root is dropped like it never existed" \
  "client/js/config-editor-panel.js" \
  "        // Zero entries is a successfully-read, genuinely empty root - not
        // a failure and not worth a sentence. Render it exactly like any
        // other root: the path/label, with nothing underneath.
        const rootNode = { name: rootDef.label, rel_path: '', is_dir: true, children: nodes };||=>||        if (rootDef.id !== 'user' && nodes.length === 0) { return null; }
        const rootNode = { name: rootDef.label, rel_path: '', is_dir: true, children: nodes };"

echo "--- BLOCK 6: the workdir-vanished wording must never regress to the removed bookkeeping copy ---"

mutate "workdirUnavailableNotice regresses to the exact removed 'not present in' copy" \
  "client/js/config-editor-roots.js" \
  "    return \`\${label}: could not reach \${where} - it may have been moved, \`
        + 'deleted, or unmounted.';||=>||    return \`\${label}: not present in \${where}.\`;"

echo "--- BLOCK 7: an unreadable subdirectory must never render like an empty one (THE THREE-OUTCOME RULE) ---"

mutate "node.list_error is ignored - a permission-denied directory renders identically to an empty one" \
  "client/js/config-editor-panel.js" \
  "                if (node.list_error) {||=>||                if (false && node.list_error) {"

echo "--- BLOCK 8: a measured-absence root (null) must never be appended as a row ---"

mutate "a null root element (measured absence) gets appended anyway instead of being skipped" \
  "client/js/config-editor-panel.js" \
  "            if (el) list.appendChild(el);||=>||            list.appendChild(el);"

restore_all
echo
echo "killed ${killed}, survived ${survived}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
