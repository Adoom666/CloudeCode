#!/bin/bash
# Mutation check for items 64, 65 and 66 - the grouped list, dragging
# across the pinned boundary, and the inline rename.
#
# WHY THIS SUITE EXISTS SEPARATELY FROM mutate-sidebar-sessions.sh: its
# DETECTOR is a real browser. Every mutant below breaks something that
# has no representation in a DOM stub - which band a pointer is over,
# whether a collapsed group left its rows in the document, whether the
# chevron actually rotates, whether a double-click opens an editor. A
# node suite cannot kill any of them, and listing them there would have
# reported survivors that are in fact covered. A false RED erodes a
# suite's credibility exactly as fast as a false green does.
#
# The cost is real: each run drives Chromium, so this is minutes rather
# than seconds. That is the price of mutating behaviour whose only honest
# detector is a rendered page. Run it before shipping a change to the
# group, drag or rename paths, not on every save.
#
# THREE OUTCOMES from the detector, and only one of them is a kill:
#   exit 0  the verifier measured everything and it held  -> SURVIVED
#   exit 1  the verifier measured something and it was wrong -> killed
#   exit 2  the verifier could not measure at all (no playwright, no
#           browser) -> NOT a kill. Counting a could-not-evaluate as a
#           kill would make this whole suite pass on a machine with no
#           browser installed, which is the single worst false green
#           available here.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# The verifier needs playwright, which is NOT in the project venv. A brew
# python has it. Overridable so CI can point at whatever it has.
PY="${MUTATE_PY:-/opt/homebrew/bin/python3}"
# BOTH verifiers, because the mutants below span both their remits: the
# group and rename mutants are caught by verify_sidebar_groups.py, and
# the DENSITY CONTRACT mutants are caught by verify_sidebar_sessions.py,
# which is where the per-mode row heights are asserted by number. Running
# only the first left the min-height mutant surviving while the check
# that would have killed it sat one file away, unrun.
VERIFIERS=(
  "scripts/verify_sidebar_groups.py"
  "scripts/verify_sidebar_sessions.py"
)

FILES=(
  "client/js/session-sidebar-groups.js"
  "client/js/session-sidebar-rename.js"
  "client/js/session-sidebar-reorder.js"
  "client/js/session-sidebar-arrangement.js"
  "client/js/session-sidebar-store.js"
  "client/js/session-sidebar-rows.js"
  "client/js/session-sidebar-clicks.js"
  "client/css/session-sidebar-groups.css"
  "client/css/session-sidebar-density.css"
)

BAKDIR="$(mktemp -d)"
for f in "${FILES[@]}"; do
  mkdir -p "${BAKDIR}/$(dirname "$f")"
  cp "${ROOT}/${f}" "${BAKDIR}/${f}"
done
# RESTORE ON EVERY EXIT PATH, including a kill. A mutation suite that
# leaves a mutant on disk when interrupted turns a test tool into a
# source of corruption.
trap 'for f in "${FILES[@]}"; do cp "${BAKDIR}/${f}" "${ROOT}/${f}"; done; rm -rf "${BAKDIR}"' EXIT

survived=0
killed=0
undetermined=0

restore_all() {
  for f in "${FILES[@]}"; do
    cp "${BAKDIR}/${f}" "${ROOT}/${f}"
  done
}

# Run the browser verifier. Returns its exit code untouched so the three
# outcomes stay distinguishable.
# A FAIL from EITHER verifier is a kill. A CANNOT DETERMINE from either
# makes the whole run undetermined, and it wins over a fail: if one
# verifier could not measure at all, the other one's verdict does not
# make the tree evaluated.
run_verifier() {
  local v rc worst=0
  for v in "${VERIFIERS[@]}"; do
    (cd "$ROOT" && "$PY" "$v" >/dev/null 2>&1)
    rc=$?
    if [ "$rc" -eq 2 ]; then return 2; fi
    if [ "$rc" -ne 0 ]; then worst=1; fi
  done
  return $worst
}

echo "--- preflight: can this machine measure anything at all? ---"
if ! "$PY" -c "import playwright" >/dev/null 2>&1; then
  echo "CANNOT DETERMINE: ${PY} cannot import playwright, so no mutant can be"
  echo "  evaluated. This is NOT a pass. Set MUTATE_PY to an interpreter that has it."
  exit 2
fi

echo "--- baseline: the verifier must be GREEN before anything is mutated ---"
run_verifier
base=$?
if [ "$base" -eq 2 ]; then
  echo "CANNOT DETERMINE: the verifier could not measure the unmutated tree."
  exit 2
fi
if [ "$base" -ne 0 ]; then
  echo "BASELINE IS RED. Every mutant would read as killed. Refusing to run."
  exit 2
fi
echo "baseline green"

# mutate <name> <file> <old||=>||new>
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
    echo "SURVIVED $name (target moved - the mutant tests nothing now)"
    survived=$((survived + 1))
    return
  fi
  run_verifier
  local rc=$?
  if [ "$rc" -eq 1 ]; then
    killed=$((killed + 1))
    echo "killed   $name"
  elif [ "$rc" -eq 2 ]; then
    undetermined=$((undetermined + 1))
    echo "UNDETERMINED $name (the verifier could not measure this tree)"
  else
    survived=$((survived + 1))
    echo "SURVIVED $name"
  fi
}

echo "--- BLOCK 1: dragging across the pinned boundary ---"

mutate "the drag reads the band from the neighbouring ROW, so an empty group is undroppable" \
  "client/js/session-sidebar-reorder.js" \
  "        const targetPinned = overBand === null ? arrangement.isPinned(drag.name) : overBand;||=>||        const targetPinned = arrangement.isPinned(drag.name);"

mutate "bandAt never reports the pinned group, so no drop can ever pin" \
  "client/js/session-sidebar-reorder.js" \
  "            if (clientY < box.bottom) return g.getAttribute('data-group') === 'pinned';||=>||            if (clientY < box.bottom) return false;"

mutate "bandAt always reports pinned, so dragging OUT cannot unpin" \
  "client/js/session-sidebar-reorder.js" \
  "            if (clientY < box.bottom) return g.getAttribute('data-group') === 'pinned';||=>||            if (clientY < box.bottom) return true;"

echo "--- BLOCK 2: the group rendering rules ---"

mutate "an EMPTY pinned group renders a bare header instead of nothing" \
  "client/js/session-sidebar-groups.js" \
  "        if (bands.pinned.length === 0 && !dragging) {||=>||        if (false) {"

mutate "the pinned group is drawn SECOND, so a pin no longer means the top" \
  "client/js/session-sidebar-groups.js" \
  "            sectionHtml('pinned', bands.pinned, density, isFolded('pinned'))
            + sectionHtml('other', bands.other, density, isFolded('other'))||=>||            sectionHtml('other', bands.other, density, isFolded('other'))
            + sectionHtml('pinned', bands.pinned, density, isFolded('pinned'))"

mutate "a collapsed group keeps its rows in the DOM, so the reorder counts invisible ones" \
  "client/js/session-sidebar-groups.js" \
  "        const body = collapsed
            ? ''
            : rows.map((r) => window.SessionSidebarRows.rowHtml(r, density)).join('');||=>||        const body = rows.map((r) => window.SessionSidebarRows.rowHtml(r, density)).join('');"

mutate "the header stops reporting aria-expanded, so the fold is shape-only" \
  "client/js/session-sidebar-groups.js" \
  "            + \`aria-expanded=\"\${collapsed ? 'false' : 'true'}\" \`||=>||            + 'aria-expanded=\"true\" '"

mutate "the collapsed count is dropped, so a folded group looks like an empty one" \
  "client/js/session-sidebar-groups.js" \
  "            + \`<span class=\"session-sidebar-group__count\">\${count}</span>\`||=>||            + ''"

echo "--- BLOCK 3: the fold must survive, and the chevron must move ---"

mutate "the fold is never persisted, so it dies on reload" \
  "client/js/session-sidebar-store.js" \
  "        save(state.pinned, state.order, collapsed);||=>||        void collapsed;"

mutate "the chevron never rotates, so the open/shut state is invisible" \
  "client/css/session-sidebar-groups.css" \
  ".session-sidebar-group__header[aria-expanded=\"true\"] .session-sidebar-group__chevron {
    transform: rotate(90deg);
}||=>||"

echo "--- BLOCK 4: the density contract is a DECLARED number ---"

# NOT "cozy loses its min-height": that is an EQUIVALENT MUTANT here and
# it is worth saying why rather than quietly dropping it. Measured with
# the rule removed, a cozy row still comes out at exactly 46.00px, because
# the floor was chosen to match what the padding and line-height already
# produced. Removing it changes no pixel TODAY. Its value is that it stops
# the number moving TOMORROW, when a control is added or removed - which
# is a property no single-tree mutation can express.
#
# So the mutant that IS load-bearing changes the declared number instead.
# It proves the assertion reads the stylesheet the browser actually
# applies, rather than passing on a coincidence.
mutate "cozy's declared min-height is changed, so the contract is not what it says" \
  "client/css/session-sidebar-density.css" \
  ".session-sidebar-panel[data-density=\"cozy\"] .session-sidebar-row {
    min-height: 46px;
}||=>||.session-sidebar-panel[data-density=\"cozy\"] .session-sidebar-row {
    min-height: 62px;
}"

# Compact is the same equivalent-mutant case as cozy above: with the rule
# removed the row still measures 24.00px. Same treatment, same reason.
mutate "compact's declared min-height is changed, so the thinnest mode is not 24px" \
  "client/css/session-sidebar-density.css" \
  "    margin-bottom: 1px;
    min-height: 24px;||=>||    margin-bottom: 1px;
    min-height: 33px;"

mutate "the header loses its declared height, so the list geometry drifts" \
  "client/css/session-sidebar-groups.css" \
  "    min-height: 22px;||=>||    min-height: 40px;"

echo "--- BLOCK 5: inline rename ---"

mutate "the editor opens on a row that CANNOT be renamed, taking input that will fail" \
  "client/js/session-sidebar-rename.js" \
  "        if (state !== 'renameable' || !sessionId || !name) {||=>||        if (false) {"

mutate "a rename failure is swallowed, so the row silently keeps the old name" \
  "client/js/session-sidebar-rename.js" \
  "            showError(ctx, \`rename failed: \${detail}\`);||=>||            teardown(ctx);"

mutate "Escape COMMITS instead of cancelling" \
  "client/js/session-sidebar-rename.js" \
  "        if (e.key === 'Escape') {
            e.preventDefault();
            cancel(ctx);||=>||        if (e.key === 'Escape') {
            e.preventDefault();
            commit(ctx);"

mutate "the single click is never deferred, so a double-click switches conversation first" \
  "client/js/session-sidebar-rename.js" \
  "    function deferActivation(e, rowEl, activate) {||=>||    function deferActivation(e, rowEl, activate) {
        return false;"

echo
echo "killed ${killed}, survived ${survived}, undetermined ${undetermined}"
if [ "$undetermined" -gt 0 ]; then
  echo "MUTATION CHECK INCONCLUSIVE: ${undetermined} mutant(s) could not be evaluated."
  exit 2
fi
if [ "$survived" -gt 0 ]; then
  echo "MUTATION CHECK FAILED"
  exit 1
fi
echo "MUTATION CHECK PASSED"
