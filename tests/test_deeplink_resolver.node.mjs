// Node-based smoke test for the deep-link duplicate-session regression fix
// (client/js/launchpad.js: openProjectByName / _findRunningSessionBySlug /
// selectProject's create-guard).
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha — there
// is no existing JS test harness (verified: no package.json, no
// jest.config*, no *.test.js anywhere outside node_modules). The bug this
// fixes is entirely client-side (client/js/launchpad.js), so a purely
// server-side pytest can't exercise the actual resolver. This script loads
// launchpad.js into a minimal DOM-free `vm` sandbox and drives
// `Launchpad.openProjectByName()` directly, in-process, with a fake
// `window.API` that RECORDS every call — including asserting
// `createSession` is never called — which is the strongest possible check
// for "does this create a duplicate session".
//
// Run with: node tests/test_deeplink_resolver.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 and prints the failing
// assertion otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const launchpadSrc = fs.readFileSync(
    path.join(__dirname, '..', 'client', 'js', 'launchpad.js'),
    'utf8'
);

let failures = 0;
let passes = 0;

function test(name, fn) {
    return (async () => {
        try {
            await fn();
            passes++;
            console.log(`ok - ${name}`);
        } catch (err) {
            failures++;
            console.error(`NOT OK - ${name}`);
            console.error(err && err.stack ? err.stack : err);
        }
    })();
}

/**
 * Build a fresh sandboxed `Launchpad` instance with a fake window/document
 * and a `window.API` that records every call. Each test gets its own
 * instance so router/API call logs never leak between tests.
 */
function makeSandbox({ createSessionShouldThrow = true } = {}) {
    const calls = [];
    const rejectedTargets = [];
    const errorsShown = [];

    const fakeDocument = {
        getElementById() { return null; },
        querySelectorAll() { return []; },
        createElement() {
            return { addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, dataset: {} };
        },
    };

    const fakeWindow = {
        API: {
            async listAttachableSessions() {
                calls.push(['listAttachableSessions']);
                return fakeWindow.__attachable || [];
            },
            async listSessions() {
                calls.push(['listSessions']);
                return fakeWindow.__live || [];
            },
            async getCurrentSession() {
                calls.push(['getCurrentSession']);
                return null;
            },
            async adoptSession(name, x) {
                calls.push(['adoptSession', name, x]);
                return { session: { tmux_session: name, working_dir: '/tmp/whatever' } };
            },
            async getSession(id, opts) {
                calls.push(['getSession', id, opts]);
                return { id, tmux_session: fakeWindow.__activeTmuxName || null };
            },
            async createSession(payload) {
                calls.push(['createSession', payload]);
                if (createSessionShouldThrow) {
                    throw new Error('TEST HARNESS: createSession must never be called from a deep-link resolve');
                }
                return { id: 'should-not-happen' };
            },
            async createProject() {
                calls.push(['createProject']);
                return {};
            },
        },
        App: {
            hideAllScreens() {},
            returnToExistingTerminal(info) { calls.push(['returnToExistingTerminal', info]); },
        },
        TerminalController: null,
        Router: {
            rejectTarget(name) { rejectedTargets.push(name); },
        },
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) { this.type = type; this.detail = opts && opts.detail; },
        requestAnimationFrame(cb) { cb(); },
    };
    fakeWindow.window = fakeWindow;

    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
    };
    vm.createContext(context);
    vm.runInContext(launchpadSrc, context, { filename: 'launchpad.js' });

    // showError on the instance records via alert() normally; stub it.
    context.window.Launchpad.showError = (msg) => { errorsShown.push(msg); };

    return {
        launchpad: context.window.Launchpad,
        fakeWindow,
        calls,
        rejectedTargets,
        errorsShown,
        assertNoCreate() {
            const createCalls = calls.filter(c => c[0] === 'createSession');
            assert.equal(createCalls.length, 0, `expected zero createSession calls, got ${createCalls.length}: ${JSON.stringify(createCalls)}`);
        },
    };
}

// ---------------------------------------------------------------------
// Test 1: URL slug round-trip — build from a tmux name, resolve back to
// the SAME session.
// ---------------------------------------------------------------------
await test('slug round-trip: tmux name -> display slug -> resolves to same session', async () => {
    const { launchpad } = makeSandbox();
    const tmuxName = 'cloude_claude-config-sync-2';
    const slug = launchpad._deriveRunningSessionDisplayName(tmuxName);
    assert.equal(slug, 'claude-config-sync-2');

    launchpad.runningSessions = [
        { name: tmuxName, created_by_cloude: true, is_active: false, session_id: 'sid-1' },
    ];
    const resolved = launchpad._findRunningSessionBySlug(slug);
    assert.ok(resolved, 'expected a match');
    assert.equal(resolved.name, tmuxName);
});

// ---------------------------------------------------------------------
// Test 2: existing ADOPTED (active) session resolves by URL without
// creating anything.
// ---------------------------------------------------------------------
await test('adopted/active session resolves via openProjectByName without creating', async () => {
    const sb = makeSandbox();
    const tmuxName = 'cloude_claude-config-sync-2';
    sb.fakeWindow.__attachable = []; // active sessions are filtered out of /attachable server-side
    sb.fakeWindow.__live = [
        { tmux_session: tmuxName, activity_status: 'running', session: { id: 'sid-active' } },
    ];
    sb.fakeWindow.__activeTmuxName = tmuxName;

    await sb.launchpad.openProjectByName('claude-config-sync-2');

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 0, 'should not have rejected');
    const returned = sb.calls.filter(c => c[0] === 'returnToExistingTerminal');
    assert.equal(returned.length, 1, 'expected exactly one return-to-terminal call');
});

// ---------------------------------------------------------------------
// Test 3: un-adopted but RUNNING tmux session (EXTERNAL row) resolves via
// adopt, without creating.
// ---------------------------------------------------------------------
await test('un-adopted running tmux session resolves via adopt without creating', async () => {
    const sb = makeSandbox();
    const tmuxName = 'cloude_claude-config-sync-2';
    sb.fakeWindow.__attachable = [
        { name: tmuxName, created_by_cloude: true, created_at_epoch: 0, window_count: 1 },
    ];
    sb.fakeWindow.__live = [];

    await sb.launchpad.openProjectByName('claude-config-sync-2');

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 0, 'should not have rejected');
    const adopted = sb.calls.filter(c => c[0] === 'adoptSession');
    assert.equal(adopted.length, 1, 'expected exactly one adoptSession call');
    assert.equal(adopted[0][1], tmuxName);
});

// ---------------------------------------------------------------------
// Test 4: unknown name — no live session anywhere, EVEN IF a launcher
// project of that name exists — shows the error banner and creates
// nothing. This is the actual regression scenario: previously a
// launcher-project match here would call selectProject() -> createSession().
// ---------------------------------------------------------------------
await test('unknown name (but matching launcher project) shows error, creates nothing', async () => {
    const sb = makeSandbox();
    sb.fakeWindow.__attachable = [];
    sb.fakeWindow.__live = [];
    // Simulate a launcher project entry existing with the SAME name as
    // the requested deep link, but with NO corresponding live tmux
    // session — this is exactly the pre-fix duplicate-session trigger.
    sb.launchpad.projects = [
        { name: 'claude-config-sync-2', path: '/tmp/claude-config-sync-2' },
    ];

    await sb.launchpad.openProjectByName('claude-config-sync-2');

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 1, 'expected exactly one rejectTarget call');
    assert.equal(sb.rejectedTargets[0], 'claude-config-sync-2');
});

// ---------------------------------------------------------------------
// Test 5: guard clause — selectProject() itself refuses to create while
// _resolvingDeepLink is set, even if called directly (defense in depth
// against a future refactor re-wiring openProjectByName into it).
// ---------------------------------------------------------------------
await test('selectProject refuses to create while _resolvingDeepLink is set', async () => {
    const sb = makeSandbox();
    sb.launchpad._resolvingDeepLink = true;
    await assert.rejects(
        () => sb.launchpad.selectProject({ name: 'anything', path: '/tmp/x' }, { model: null }),
        /refusing to create a session/
    );
    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 1);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    process.exit(1);
} else {
    console.log('ALL PASS');
    process.exit(0);
}
