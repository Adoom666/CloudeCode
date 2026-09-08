// Node test for RESTART carrying a stopped session's identity:
// client/js/launchpad.js _renderRecentSessionRowHtml() /
// _renderEndedTreeSessionRowHtml() / _restartPlan() / _restartNotice() /
// _restartRecentSession().
//
// THE DEFECT THIS LOCKS DOWN. The restart control built its request from
// working_dir and agent_type alone. The row's TITLE was never put into the
// button markup at all, and its session_uuid was in the row's dataset but
// never passed to the handler - so restarting a named session with hours of
// conversation behind it produced an unnamed blank console in the right
// directory. TWO call sites lost the same information: the RECENT list and
// the project tree's ended rows. Both are asserted here, separately,
// because a fix applied to one of them is exactly how this returns.
//
// WHY THIS ASSERTS ON RENDERED MARKUP AND ON THE ACTUAL CALL ARGUMENTS.
// Same reason as tests/test_recent_sessions.node.mjs: this project shipped
// a feature with 282 green state assertions that rendered zero pixels. Every
// markup assertion below reads the HTML string the renderer wrote into the
// stub container; every payload assertion reads what the API stub was
// actually handed, not what a plan object said it would be handed.
//
// THREE-OUTCOME RULE, asserted twice:
//   - a row with NO session_uuid must not silently start a blank session
//     while implying it resumed one. It gets a notice naming what could not
//     be determined.
//   - a server response of conversation='none_recorded' or 'unknown' must
//     produce a DIFFERENT user-visible sentence from 'resumed'.
//
// TRAP NOTED FOR THE NEXT READER: deepStrictEqual compares prototypes, and
// an object built inside vm.runInContext has a different Object.prototype
// from this module's, so two identical empty objects FAIL that check. Every
// payload assertion below reads Object.keys(...).length and named fields.
//
// Run with: node tests/test_session_restart_identity.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * MUST be awaited by every caller - an un-awaited async body reports a pass
 * before it has run, which is a green suite that measured nothing.
 * @param {string} name  Test description.
 * @param {() => (void|Promise<void>)} fn  Body; throwing marks it failed.
 * @returns {Promise<void>}
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
 * Build one stub element that records what the renderer writes into it.
 * @param {string} id  Element id, for getElementById lookup.
 * @returns {object} Stub with innerHTML, textContent, style and dataset.
 */
function makeEl(id) {
    return {
        id,
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this._attrs, name)
                ? this._attrs[name] : null;
        },
        appendChild() {},
        addEventListener() {},
        closest() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    };
}

/**
 * Load launchpad.js in a vm sandbox with recording API stubs.
 * @param {object} [opts]
 * @param {object|Error} [opts.restartResult] Value restartSession resolves
 *   to, or an Error it rejects with.
 * @returns {{lp: object, calls: object, errors: string[], byId: object}}
 */
function loadLaunchpad(opts = {}) {
    const calls = { restart: [], create: [] };
    const errors = [];
    const byId = {
        'recent-sessions-list': makeEl('recent-sessions-list'),
        'recent-sessions-count': makeEl('recent-sessions-count'),
        'recent-sessions-section': makeEl('recent-sessions-section'),
        'project-list': makeEl('project-list'),
    };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
        body: makeEl('body'),
    };
    const fakeWindow = {
        API: {
            async listRecentSessions() {
                return { state: 'ok', sessions: [], notice: null };
            },
            async restartSession(uuid) {
                calls.restart.push(uuid);
                if (opts.restartResult instanceof Error) throw opts.restartResult;
                return opts.restartResult === undefined
                    ? { success: true, conversation: 'resumed', lineage_recorded: true }
                    : opts.restartResult;
            },
            async createSession(payload) {
                calls.create.push(payload);
                return { session_id: 'ses_new' };
            },
        },
        SessionStatusUI: { dotHtml() { return '<i class="dot"></i>'; } },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, o) { this.type = type; this.detail = o && o.detail; },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    fakeWindow.window = fakeWindow;
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
    const lp = context.window.Launchpad;
    // Capture every user-visible sentence instead of rendering a banner.
    lp.showError = (m) => { errors.push(String(m)); };
    lp.loadRunningSessions = async () => {};
    lp.loadRecentSessions = async () => {};
    lp.loadSessionAttribution = async () => {};
    lp.renderProjectList = () => {};
    return { lp, calls, errors, byId };
}

function recentRow(overrides = {}) {
    return {
        session_uuid: 'uuid-1',
        origin: 'created',
        owned: true,
        tmux_socket: 'cloude',
        tmux_name: 'cloude_media',
        tmux_created_epoch: 1700000000,
        lifecycle: 'stopped',
        project_attribution: 'none',
        working_dir: '/home/x/proj',
        agent_type: 'claude',
        agent_family: 'claude',
        agent_family_source: 'reserved_name',
        archived_at: null,
        title: 'Media Pipeline',
        ...overrides,
    };
}

function treeRow(overrides = {}) {
    return {
        name: 'cloude_media',
        session_uuid: 'uuid-tree-1',
        created_by_cloude: true,
        working_dir: '/home/x/proj',
        agent_type: 'claude',
        agent_family: 'claude',
        agent_family_source: 'reserved_name',
        title: 'Media Pipeline',
        ...overrides,
    };
}

// =====================================================================
// 1. CALL SITE ONE - the RECENT list. The button must carry BOTH the
//    session uuid and the title, not just the launch context.
// =====================================================================

await test('RECENT restart button carries data-uuid AND data-title', async () => {
    const { lp } = loadLaunchpad();
    const html = lp._renderRecentSessionRowHtml(recentRow());
    assert.ok(html.includes('recent-session-restart'), 'no restart control rendered');
    assert.ok(html.includes('data-uuid="uuid-1"'),
        `restart control must carry the session uuid, got: ${html}`);
    assert.ok(html.includes('data-title="Media Pipeline"'),
        `restart control must carry the title, got: ${html}`);
    assert.ok(html.includes('data-working-dir="/home/x/proj"'));
    assert.ok(html.includes('data-agent-type="claude"'));
});

await test('RECENT restart button escapes a title carrying markup', async () => {
    const { lp } = loadLaunchpad();
    const html = lp._renderRecentSessionRowHtml(
        recentRow({ title: '<img src=x onerror=1>' }));
    assert.ok(!html.includes('<img src=x'),
        `a title must never reach the markup unescaped, got: ${html}`);
    assert.ok(html.includes('data-title="&lt;img'));
});

await test('RECENT restart button renders an EMPTY data-title for a row with none', async () => {
    const { lp } = loadLaunchpad();
    const html = lp._renderRecentSessionRowHtml(recentRow({ title: null }));
    assert.ok(html.includes('data-title=""'),
        `the attribute must exist and be empty, never the string "null": ${html}`);
    assert.ok(!html.includes('data-title="null"'));
});

// =====================================================================
// 2. CALL SITE TWO - the project tree's ended rows. The SAME loss lived
//    here, and a fix applied only to the RECENT list is how it returns.
// =====================================================================

await test('TREE ended restart button carries data-uuid AND data-title', async () => {
    const { lp } = loadLaunchpad();
    const html = lp._renderEndedTreeSessionRowHtml(treeRow());
    assert.ok(html.includes('ended-session-restart'), 'no restart control rendered');
    assert.ok(html.includes('data-uuid="uuid-tree-1"'),
        `tree restart control must carry the session uuid, got: ${html}`);
    assert.ok(html.includes('data-title="Media Pipeline"'),
        `tree restart control must carry the title, got: ${html}`);
    assert.ok(html.includes('data-working-dir="/home/x/proj"'));
});

await test('TREE ended restart button escapes a title carrying markup', async () => {
    const { lp } = loadLaunchpad();
    const html = lp._renderEndedTreeSessionRowHtml(
        treeRow({ title: '"><script>x</script>' }));
    assert.ok(!html.includes('<script>'),
        `a title must never reach the markup unescaped, got: ${html}`);
});

// =====================================================================
// 3. THE PLAN. A known session_uuid routes to the restart endpoint and
//    sends the uuid; the server owns everything else.
// =====================================================================

await test('a known session_uuid plans mode "restart" and carries the uuid', async () => {
    const { lp } = loadLaunchpad();
    const plan = lp._restartPlan({
        sessionUuid: 'uuid-1', title: 'Media Pipeline',
        workingDir: '/home/x/proj', agentType: 'claude',
    });
    assert.equal(plan.mode, 'restart');
    assert.equal(plan.sessionUuid, 'uuid-1');
    assert.equal(plan.notice, null, 'a resolvable restart needs no caveat');
});

await test('the restart call actually receives the uuid, not the working dir', async () => {
    const { lp, calls } = loadLaunchpad();
    await lp._restartRecentSession({
        sessionUuid: 'uuid-1', title: 'Media Pipeline',
        workingDir: '/home/x/proj', agentType: 'claude',
    });
    assert.equal(calls.restart.length, 1,
        `expected exactly one restart call, got ${calls.restart.length}`);
    assert.equal(calls.restart[0], 'uuid-1');
    assert.equal(calls.create.length, 0,
        'a resolvable restart must NOT fall through to a blank create');
});

// =====================================================================
// 4. THREE-OUTCOME RULE, FIRST HALF. No session_uuid is a DIFFERENT case
//    from one that resolved: the title still travels, and the user is
//    told what could not be determined instead of getting a silent blank.
// =====================================================================

await test('no session_uuid plans "create_unidentified" and still carries the title', async () => {
    const { lp } = loadLaunchpad();
    const plan = lp._restartPlan({
        sessionUuid: '', title: 'Media Pipeline',
        workingDir: '/home/x/proj', agentType: 'codex',
    });
    assert.equal(plan.mode, 'create_unidentified');
    assert.equal(plan.payload.project_name, 'Media Pipeline',
        'the title is the one piece of identity this mode CAN carry');
    assert.equal(plan.payload.working_dir, '/home/x/proj');
    assert.equal(plan.payload.agent_type, 'codex');
    assert.equal(Object.keys(plan.payload).length, 3,
        `expected exactly three payload keys, got ${JSON.stringify(plan.payload)}`);
});

await test('no session_uuid produces a CANNOT BE DETERMINED notice, never silence', async () => {
    const { lp, calls, errors } = loadLaunchpad();
    await lp._restartRecentSession({
        sessionUuid: null, title: 'Media Pipeline',
        workingDir: '/home/x/proj', agentType: '',
    });
    assert.equal(calls.restart.length, 0);
    assert.equal(calls.create.length, 1);
    assert.equal(calls.create[0].project_name, 'Media Pipeline');
    assert.equal(errors.length, 1,
        `the user must be told; got ${errors.length} notices`);
    assert.ok(/CANNOT BE DETERMINED/.test(errors[0]),
        `the notice must name the unknown, got: ${errors[0]}`);
    assert.ok(!/resumed the conversation/.test(errors[0]),
        'it must never imply a resume happened');
});

await test('an empty payload stays an object with zero keys, not undefined', async () => {
    const { lp } = loadLaunchpad();
    const plan = lp._restartPlan({});
    assert.equal(plan.mode, 'create_unidentified');
    assert.equal(typeof plan.payload, 'object');
    assert.equal(Object.keys(plan.payload).length, 0);
    assert.ok(plan.notice, 'even a wholly unidentified restart says so');
});

// =====================================================================
// 5. THREE-OUTCOME RULE, SECOND HALF. The three server verdicts must
//    produce three DIFFERENT sentences. A blank session reported like a
//    resumed one is the whole defect, one layer further in.
// =====================================================================

await test("conversation 'resumed' with lineage recorded says nothing at all", async () => {
    const { lp } = loadLaunchpad();
    assert.equal(lp._restartNotice({
        conversation: 'resumed', lineage_recorded: true, title_carried: 'Media',
    }), null);
});

await test("conversation 'none_recorded' says a NEW conversation was started", async () => {
    const { lp, errors } = loadLaunchpad({
        restartResult: {
            success: true, conversation: 'none_recorded',
            lineage_recorded: true, title_carried: 'Media',
        },
    });
    await lp._restartRecentSession({ sessionUuid: 'uuid-1', title: 'Media' });
    assert.equal(errors.length, 1, 'this case must never be silent');
    assert.ok(/NEW conversation/.test(errors[0]), `got: ${errors[0]}`);
    assert.ok(/nothing was resumed/.test(errors[0]), `got: ${errors[0]}`);
});

await test("conversation 'unknown' is its own sentence, not folded into either", async () => {
    const { lp } = loadLaunchpad();
    const unknown = lp._restartNotice({ conversation: 'unknown', title_carried: 'Media' });
    const none = lp._restartNotice({ conversation: 'none_recorded', title_carried: 'Media' });
    assert.ok(unknown, 'unknown must produce a sentence');
    assert.ok(/CANNOT BE DETERMINED/.test(unknown), `got: ${unknown}`);
    assert.notEqual(unknown, none,
        'could-not-evaluate and definitely-none must not read identically');
});

await test('a missing response body is CANNOT DETERMINE, never a clean pass', async () => {
    const { lp } = loadLaunchpad();
    const notice = lp._restartNotice(null);
    assert.ok(notice && /CANNOT DETERMINE/.test(notice), `got: ${notice}`);
});

// =====================================================================
// 6. LINEAGE. A resumed restart whose parent link did NOT land is neither
//    a failure nor a clean success, and gets said out loud.
// =====================================================================

await test('a resumed restart with row_reused false still reports the gap', async () => {
    const { lp, errors } = loadLaunchpad({
        restartResult: {
            success: true, conversation: 'resumed',
            row_reused: false, title_carried: 'Media', detail: null,
        },
    });
    await lp._restartRecentSession({ sessionUuid: 'uuid-1' });
    assert.equal(errors.length, 1);
    assert.ok(/could not keep its original record/.test(errors[0]),
        `got: ${errors[0]}`);
});

await test('a resumed restart that DID reuse its row says nothing at all', async () => {
    // The normal outcome. A restart that kept its own record is an
    // ordinary success and must not narrate anything at the user.
    const { lp } = loadLaunchpad();
    assert.equal(
        lp._restartNotice({
            conversation: 'resumed', row_reused: true, title_carried: 'Media',
        }),
        null);
});

await test("the server's own detail wins over the generic reuse sentence", async () => {
    const { lp } = loadLaunchpad();
    assert.equal(
        lp._restartNotice({
            conversation: 'resumed', row_reused: false,
            detail: 'the datastore was unreadable',
        }),
        'the datastore was unreadable');
});

// =====================================================================
// 7. FAILURE PATH. A rejected request must surface, not vanish.
// =====================================================================

await test('a failed restart request is reported to the user', async () => {
    const { lp, errors } = loadLaunchpad({
        restartResult: new Error('the server could not be reached'),
    });
    await lp._restartRecentSession({ sessionUuid: 'uuid-1' });
    assert.equal(errors.length, 1);
    assert.ok(/failed to restart session/.test(errors[0]), `got: ${errors[0]}`);
});


// =====================================================================
// 8. THE WIRING, both call sites, end to end. Section 1 and 2 proved the
//    attributes are RENDERED; these prove the delegated listeners
//    actually READ them and hand them to the handler. That gap is
//    literally where the original defect lived: the uuid WAS in the
//    dataset and simply never reached the call.
// =====================================================================

/**
 * Build a button stub carrying the four restart attributes.
 * @param {object} attrs
 * @returns {object} element-like with getAttribute.
 */
function btnStub(attrs) {
    return {
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(attrs, name)
                ? attrs[name] : null;
        },
    };
}

await test('the RECENT delegated listener hands uuid AND title to the handler', async () => {
    const { lp, byId } = loadLaunchpad();
    let listener = null;
    byId['recent-sessions-list'].addEventListener = (type, fn) => {
        if (type === 'click') listener = fn;
    };
    lp._bindRecentSessionClicks();
    assert.ok(listener, 'no click listener was bound on the RECENT list');

    const seen = [];
    lp._restartRecentSession = async (opts) => { seen.push(opts); };
    const btn = btnStub({
        'data-uuid': 'uuid-1',
        'data-title': 'Media Pipeline',
        'data-working-dir': '/home/x/proj',
        'data-agent-type': 'claude',
    });
    await listener({
        target: {
            closest(sel) {
                return sel === '.recent-session-restart' ? btn : null;
            },
        },
    });
    assert.equal(seen.length, 1, 'the handler was not called');
    assert.equal(typeof seen[0], 'object',
        'the handler must receive an options object, not a bare working dir');
    assert.equal(seen[0].sessionUuid, 'uuid-1');
    assert.equal(seen[0].title, 'Media Pipeline');
    assert.equal(seen[0].workingDir, '/home/x/proj');
    assert.equal(seen[0].agentType, 'claude');
});


await test('the TREE delegated listener hands uuid AND title to the handler', async () => {
    const { lp, byId } = loadLaunchpad();
    let listener = null;
    byId['project-list'].addEventListener = (type, fn) => {
        if (type === 'click') listener = fn;
    };
    lp._bindProjectSessionRowClicks();
    assert.ok(listener, 'no click listener was bound on the project tree');

    const seen = [];
    lp._restartRecentSession = async (opts) => { seen.push(opts); };
    const row = {
        dataset: { ended: '1', name: 'cloude_media' },
        classList: { contains() { return false; } },
    };
    const btn = btnStub({
        'data-uuid': 'uuid-tree-1',
        'data-title': 'Media Pipeline',
        'data-working-dir': '/home/x/proj',
        'data-agent-type': 'claude',
    });
    await listener({
        stopPropagation() {},
        target: {
            closest(sel) {
                if (sel === '.project-session-row') return row;
                if (sel === '.ended-session-restart') return btn;
                return null;
            },
        },
    });
    assert.equal(seen.length, 1, 'the tree handler was not called');
    assert.equal(typeof seen[0], 'object',
        'the handler must receive an options object, not a bare working dir');
    assert.equal(seen[0].sessionUuid, 'uuid-tree-1');
    assert.equal(seen[0].title, 'Media Pipeline');
    assert.equal(seen[0].workingDir, '/home/x/proj');
    assert.equal(seen[0].agentType, 'claude');
});


console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
