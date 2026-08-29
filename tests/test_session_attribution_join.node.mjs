// Node test for the tmux-identity -> stored-row join used by the
// home-screen project-session tree: client/js/launchpad.js
// _resolveSessionAttribution() / loadSessionAttribution() /
// _buildProjectSessionGroups().
//
// THE INCIDENT THIS GUARDS AGAINST, measured live on the owner's
// machine: GET /sessions/records returns newest-first and INCLUDES
// archived rows (see the listSessionRecords docstring in
// client/js/api.js). The old code built its tmux_name -> row map with a
// plain `map.set()` loop over that list, so the OLDEST row won (a
// forward scan over a newest-first list is last-write-wins-on-the-
// oldest) and an archived row was never excluded. Two tmux instances
// legitimately share a tmux_name (a session recreated after its pane
// died reuses the name with a new tmux_created_epoch); when the OLDER
// one got archived, its row still shadowed the newer, running one, and
// the running session vanished from its project entirely - a session
// disappearing being the worst possible rendering of "attribution is
// unknown".
//
// WHY THIS ASSERTS AGAINST THE RESOLVER'S OUTPUT DIRECTLY, not just
// rendered markup: _resolveSessionAttribution() is a pure function
// (rows in, three structures out) and is exactly the unit the defect
// lived in. tests/test_project_session_tree.node.mjs already covers the
// rendered-markup side end to end; this file is the join logic itself,
// plus one end-to-end pass through _buildProjectSessionGroups() so the
// two never drift apart.
//
// Run with: node tests/test_session_attribution_join.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

// Must match the separator _resolveSessionAttribution() uses in
// client/js/launchpad.js - a NUL byte, chosen so no tmux name can ever
// collide with another key by containing what looks like the
// separator plus digits.
const SEP = '\u0000';

let failures = 0;
let passes = 0;

/**
 * Description: run one named assertion block, recording pass/fail
 *   rather than throwing, so one bad assertion does not hide the rest.
 * Inputs: name (string), fn (function|async function).
 * Output: Promise<void>.
 */
async function test(name, fn) {
    try {
        await fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Load launchpad.js into a fresh vm sandbox and return its Launchpad
 * singleton, undriven - no DOM, no fetch. Callers exercise
 * `_resolveSessionAttribution` and `_buildProjectSessionGroups`
 * directly, neither of which touches the DOM.
 * @returns {object} the `window.Launchpad` instance.
 */
function loadLaunchpad() {
    const fakeWindow = {
        API: {},
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    fakeWindow.window = fakeWindow;
    const fakeDocument = {
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() {
            return {
                innerHTML: '', textContent: '', style: {}, dataset: {},
                setAttribute() {}, getAttribute() { return null; },
                addEventListener() {}, closest() { return null; },
                querySelector() { return null; }, querySelectorAll() { return []; },
                classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            };
        },
    };
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: fakeWindow.localStorage,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    return context.window.Launchpad;
}

/**
 * Description: one SessionRecord-shaped row, as GET /sessions/records
 *   returns it.
 * Inputs: overrides (object).
 * Output: object.
 */
function record(overrides = {}) {
    return Object.assign({
        session_uuid: 'uuid-' + Math.random().toString(36).slice(2),
        origin: 'created',
        owned: true,
        adopted_at: null,
        tmux_socket: 'cloude',
        tmux_name: 'cloude_a',
        tmux_created_epoch: 1000,
        lifecycle: 'running',
        lifecycle_checked_at: null,
        lifecycle_source: null,
        project_id: 1,
        project_attribution: 'derived_deepest',
        working_dir: '/p',
        agent_type: 'claude',
        agent_family: null,
        agent_family_source: null,
        model: null,
        archived_at: null,
        title: null,
    }, overrides);
}

/**
 * Description: one running-session row, the shape
 *   `this.runningSessions` entries carry (loadRunningSessions()'s
 *   merge output).
 * Inputs: overrides (object).
 * Output: object.
 */
function running(overrides = {}) {
    return Object.assign({
        name: 'cloude_a',
        created_by_cloude: true,
        created_at_epoch: 1000,
        window_count: 1,
        is_active: false,
        status: 'idle',
        unread: false,
        agent_family: null,
        agent_family_source: null,
        session_row_id: null,
        parent_session_id: null,
        label: null,
    }, overrides);
}

// ---------------------------------------------------------------------
// 1. THE INCIDENT ITSELF: an archived older-instance row must never
//    shadow a live newer instance of the same tmux_name, regardless of
//    array order (the server ships newest-first; this checks both
//    orders so the fix is not order-dependent either).
// ---------------------------------------------------------------------

for (const order of ['newest-first (server order)', 'oldest-first']) {
    await test(`archived row never shadows a live one sharing its tmux_name - ${order}`, async () => {
        const lp = loadLaunchpad();
        const archivedOld = record({
            session_uuid: 'row-3', tmux_name: 'Media_Compression',
            tmux_created_epoch: 1787686975, lifecycle: 'stopped',
            archived_at: '2026-08-19T00:00:00Z', project_id: 9,
        });
        const liveNew = record({
            session_uuid: 'row-4', tmux_name: 'Media_Compression',
            tmux_created_epoch: 1788016091, lifecycle: 'running',
            archived_at: null, project_id: 9,
        });
        const rows = order === 'newest-first (server order)'
            ? [liveNew, archivedOld]
            : [archivedOld, liveNew];
        const { byName, byInstance, ambiguous } = lp._resolveSessionAttribution(rows);

        assert.equal(ambiguous.has('Media_Compression'), false,
            'two rows with DIFFERENT epochs must not be ambiguous');
        assert.equal(byName.get('Media_Compression').session_uuid, 'row-4',
            'the name-only fallback must resolve to the LIVE row, never the archived one');

        // Instance-exact lookups must each resolve to their own row,
        // archived or not - that is what lets the caller distinguish
        // "this specific instance was deleted" from "some other
        // instance of this name was deleted".
        assert.equal(byInstance.get(`Media_Compression${SEP}1788016091`).session_uuid, 'row-4');
        assert.equal(byInstance.get(`Media_Compression${SEP}1787686975`).session_uuid, 'row-3');
        assert.equal(byInstance.get(`Media_Compression${SEP}1787686975`).archived_at, '2026-08-19T00:00:00Z');

        // End-to-end through the real consumer: the running session must
        // land under its project, not vanish and not land in
        // needsAttention.
        lp.runningSessions = [running({
            name: 'Media_Compression', created_at_epoch: 1788016091,
        })];
        lp.sessionAttribution = byName;
        lp.sessionAttributionByInstance = byInstance;
        lp.sessionAttributionAmbiguous = ambiguous;
        lp.sessionAttributionListingOk = true;
        lp.sessionAttributionListingDetail = null;
        const groups = lp._buildProjectSessionGroups();
        assert.equal(groups.needsAttention.length, 0,
            'the live session must not be flagged as unattributable');
        // Spread FIRST, then map: groups.byProjectId's array was built
        // inside the vm sandbox, so it is a different realm's Array -
        // `.map()` called directly on it returns another sandbox-realm
        // array, and node:assert/strict's deepEqual (deepStrictEqual)
        // treats that as unequal to an outer-realm array literal even
        // when every element matches. The spread here runs in THIS
        // realm and produces a plain array `.map()` can safely follow.
        assert.deepEqual([...groups.byProjectId.get(9)].map((s) => s.name), ['Media_Compression'],
            'the live session must render as project 9\'s child');
    });
}

// ---------------------------------------------------------------------
// 2. Newest tmux_created_epoch wins over an older non-archived row
//    sharing the same name, regardless of input array order.
// ---------------------------------------------------------------------

for (const order of ['old-then-new', 'new-then-old']) {
    await test(`newest epoch wins over an older same-name row (both non-archived) - ${order}`, async () => {
        const lp = loadLaunchpad();
        const older = record({ session_uuid: 'r-old', tmux_name: 'cloude_x', tmux_created_epoch: 500, project_id: 1 });
        const newer = record({ session_uuid: 'r-new', tmux_name: 'cloude_x', tmux_created_epoch: 900, project_id: 2 });
        const rows = order === 'old-then-new' ? [older, newer] : [newer, older];
        const { byName, ambiguous } = lp._resolveSessionAttribution(rows);
        assert.equal(ambiguous.has('cloude_x'), false);
        assert.equal(byName.get('cloude_x').session_uuid, 'r-new');
    });
}

// ---------------------------------------------------------------------
// 3. A third, older candidate must not confuse a real tie between the
//    two NEWEST rows - the tie is about the maximum epoch, not about
//    "the two rows nearest each other in the list".
// ---------------------------------------------------------------------

await test('a tie on the true maximum epoch is ambiguous even with an older third candidate present', async () => {
    const lp = loadLaunchpad();
    const rows = [
        record({ session_uuid: 'r-oldest', tmux_name: 'cloude_y', tmux_created_epoch: 100, project_id: 1 }),
        record({ session_uuid: 'r-tie-a', tmux_name: 'cloude_y', tmux_created_epoch: 900, project_id: 2 }),
        record({ session_uuid: 'r-tie-b', tmux_name: 'cloude_y', tmux_created_epoch: 900, project_id: 3 }),
    ];
    const { byName, ambiguous } = lp._resolveSessionAttribution(rows);
    assert.equal(ambiguous.has('cloude_y'), true);
    assert.equal(byName.has('cloude_y'), false,
        'an ambiguous name must be absent from byName, never an arbitrary pick');
});

// ---------------------------------------------------------------------
// 4. Indistinguishable rows must render as could-not-evaluate through
//    the real consumer - reported via needsAttention, not silently
//    dropped and not silently guessed.
// ---------------------------------------------------------------------

await test('an indistinguishable (tied-epoch) attribution renders as NEEDS ATTENTION, not a silent pick', async () => {
    const lp = loadLaunchpad();
    const rows = [
        record({ session_uuid: 'r-tie-a', tmux_name: 'cloude_z', tmux_created_epoch: 4242, project_id: 1 }),
        record({ session_uuid: 'r-tie-b', tmux_name: 'cloude_z', tmux_created_epoch: 4242, project_id: 5 }),
    ];
    const { byName, byInstance, ambiguous } = lp._resolveSessionAttribution(rows);
    // The instance-exact key itself collides too (same name AND epoch),
    // so it must be absent from byInstance as well.
    assert.equal(byInstance.has(`cloude_z${SEP}4242`), false);

    lp.runningSessions = [running({
        name: 'cloude_z',
        // Deliberately does NOT match either stored epoch, so the
        // instance-exact path misses and the caller falls through to
        // the ambiguous name-only path.
        created_at_epoch: 0,
    })];
    lp.sessionAttribution = byName;
    lp.sessionAttributionByInstance = byInstance;
    lp.sessionAttributionAmbiguous = ambiguous;
    lp.sessionAttributionListingOk = true;
    lp.sessionAttributionListingDetail = null;
    const groups = lp._buildProjectSessionGroups();
    assert.equal(groups.needsAttention.length, 1);
    assert.equal(groups.needsAttention[0].session.name, 'cloude_z');
    assert.match(groups.needsAttention[0].reason, /could not be told apart/);
    assert.equal(groups.byProjectId.size, 0,
        'an ambiguous session must not land under EITHER candidate project');
});

// ---------------------------------------------------------------------
// 5. A row with no recorded epoch can never be ranked against another
//    same-name row - also ambiguous, not a silent last-write-wins.
// ---------------------------------------------------------------------

await test('a same-name row with no recorded epoch cannot be ranked and is ambiguous', async () => {
    const lp = loadLaunchpad();
    const rows = [
        record({ session_uuid: 'r-noepoch', tmux_name: 'cloude_w', tmux_created_epoch: null, project_id: 1 }),
        record({ session_uuid: 'r-epoch', tmux_name: 'cloude_w', tmux_created_epoch: 777, project_id: 2 }),
    ];
    const { byName, ambiguous } = lp._resolveSessionAttribution(rows);
    assert.equal(ambiguous.has('cloude_w'), true);
    assert.equal(byName.has('cloude_w'), false);
});

// ---------------------------------------------------------------------
// 6. Deleted-wins is preserved for the EXACT instance the user deleted,
//    without letting that deletion shadow a live different instance of
//    the same name (the direct regression test for the bug this fix
//    replaces the old single-map behaviour with).
// ---------------------------------------------------------------------

await test('deleting the CURRENTLY-RUNNING instance\'s own record still hides it (deleted wins)', async () => {
    const lp = loadLaunchpad();
    const rows = [
        record({
            session_uuid: 'r-self-deleted', tmux_name: 'cloude_v',
            tmux_created_epoch: 2000, archived_at: '2026-08-20T00:00:00Z',
            project_id: 1,
        }),
    ];
    const { byName, byInstance, ambiguous } = lp._resolveSessionAttribution(rows);
    lp.runningSessions = [running({ name: 'cloude_v', created_at_epoch: 2000 })];
    lp.sessionAttribution = byName;
    lp.sessionAttributionByInstance = byInstance;
    lp.sessionAttributionAmbiguous = ambiguous;
    lp.sessionAttributionListingOk = true;
    lp.sessionAttributionListingDetail = null;
    const groups = lp._buildProjectSessionGroups();
    assert.equal(groups.needsAttention.length, 0,
        'a deliberately-deleted record must not read as an unresolved attribution error');
    assert.equal(groups.byProjectId.size, 0,
        'a deliberately-deleted record must not attach the session to its old project either');
    assert.equal(groups.noProject.length, 0);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
