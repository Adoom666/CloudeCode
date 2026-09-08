// A fork label is a name the app ITSELF produces - `<parent title>(fork)`
// (src/core/session_fork.py `fork_label`) - and it used to bounce off the
// deep-link router's own validator. Measured in the browser 2026-09-08:
// navigating to /session/Punchlist%20Browser%20Rename(fork) showed the red
// "Invalid project name in URL" banner and returned to `/`, while clicking
// the SAME session's card worked because the card's URL uses the
// tmux-derived slug (`Punchlist_Browser_Rename_fork`) instead. Two links to
// one session, one of which the router refused outright.
//
// This file has two halves:
//   1. client/js/router.js - the parse-and-validate step (`isLegalDeepLinkName`
//      / `_parseCurrentPath`), run directly, no DOM.
//   2. client/js/launchpad.js - the resolve step (`_findRunningSessionBySlug`
//      via `openProjectByName`), proving a link by TITLE and a link by SLUG
//      land on the identical row, and an unresolvable name still produces
//      exactly one rejectTarget() banner call rather than a silent bounce.
//
// Run with: node tests/test_deeplink_fork_name.node.mjs

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
 * Run one named assertion block (sync or async), recording pass/fail
 * rather than throwing.
 * @param {string} name - Test description.
 * @param {() => void|Promise<void>} fn - Body; throwing marks it failed.
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
 * Read a file under the repo root.
 * @param {...string} parts - Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

/**
 * Load client/js/router.js into a sandbox with a given pathname.
 * @param {string} pathname - value for window.location.pathname.
 * @returns {object} window.Router.
 */
function loadRouter(pathname) {
    const fakeWindow = {
        location: { pathname },
        history: { replaceState() {}, pushState() {} },
    };
    const fakeDocument = {
        getElementById() { return null; },
    };
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(read('client', 'js', 'router.js'), context, { filename: 'router.js' });
    return context.window.Router;
}

/**
 * Load client/js/launchpad.js into a sandbox with a fake window/document
 * and a fake window.API - same harness shape as test_deeplink_resolver.node.mjs.
 * @param {object} [opts] - {createSessionShouldThrow}.
 * @returns {object} {launchpad, rejectedTargets, calls}.
 */
function makeSandbox({ createSessionShouldThrow = true } = {}) {
    const calls = [];
    const rejectedTargets = [];

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
    vm.runInContext(read('client', 'js', 'launchpad.js'), context, { filename: 'launchpad.js' });

    context.window.Launchpad.showError = () => {};

    return {
        launchpad: context.window.Launchpad,
        fakeWindow,
        calls,
        rejectedTargets,
        assertNoCreate() {
            const createCalls = calls.filter(c => c[0] === 'createSession');
            assert.equal(createCalls.length, 0, `expected zero createSession calls, got ${createCalls.length}: ${JSON.stringify(createCalls)}`);
        },
    };
}

const FORK_TITLE = 'Punchlist Browser Rename(fork)';
const FORK_SLUG = 'Punchlist_Browser_Rename_fork';
const FORK_TMUX_NAME = `cloude_${FORK_SLUG}`;

// ---------------------------------------------------------------------
// Part 1: router.js validation - a name the app can produce must parse.
// ---------------------------------------------------------------------

await test('a fork label with parens and spaces is accepted, not rejected', () => {
    const Router = loadRouter(`/session/${encodeURIComponent(FORK_TITLE)}`);
    const parsed = Router._parseCurrentPath();
    assert.equal(parsed.match, true);
    assert.equal(parsed.project, FORK_TITLE, 'decoded title should round-trip exactly');
});

await test('the tmux-derived slug form of the same session also parses', () => {
    const Router = loadRouter(`/session/${encodeURIComponent(FORK_SLUG)}`);
    const parsed = Router._parseCurrentPath();
    assert.equal(parsed.match, true);
    assert.equal(parsed.project, FORK_SLUG);
});

await test('".." is rejected', () => {
    const Router = loadRouter('/session/..');
    const parsed = Router._parseCurrentPath();
    assert.equal(parsed.match, true);
    assert.equal(parsed.project, null, 'must reject, not resolve to a name');
});

await test('an encoded slash inside the segment is rejected', () => {
    // %2F decodes to a literal '/' - must be caught AFTER decoding, since
    // the raw path segment itself contains no literal '/'.
    const Router = loadRouter('/session/foo%2Fbar');
    const parsed = Router._parseCurrentPath();
    assert.equal(parsed.match, true);
    assert.equal(parsed.project, null);
});

await test('an empty segment does not match the deep-link route at all', () => {
    const Router = loadRouter('/session/');
    const parsed = Router._parseCurrentPath();
    // `/session/` alone (nothing after the trailing slash) never matches
    // DEEPLINK_RX's `([^\/]+)` - there is no name to be a bad one.
    assert.equal(parsed.match, false);
});

await test('a control character is rejected', () => {
    const Router = loadRouter(`/session/${encodeURIComponent('bad\x01name')}`);
    const parsed = Router._parseCurrentPath();
    assert.equal(parsed.match, true);
    assert.equal(parsed.project, null);
});

await test('isLegalDeepLinkName direct: fork label true, hostile forms false', () => {
    const Router = loadRouter('/');
    assert.equal(Router._isLegalDeepLinkName(FORK_TITLE), true);
    assert.equal(Router._isLegalDeepLinkName(''), false);
    assert.equal(Router._isLegalDeepLinkName('.'), false);
    assert.equal(Router._isLegalDeepLinkName('..'), false);
    assert.equal(Router._isLegalDeepLinkName('a/b'), false);
    assert.equal(Router._isLegalDeepLinkName('a\\b'), false);
    assert.equal(Router._isLegalDeepLinkName('bad\nname'), false);
    // Unicode is fine - neither project-name nor title validation
    // refuses it.
    assert.equal(Router._isLegalDeepLinkName('café review'), true);
});

// ---------------------------------------------------------------------
// Part 2: launchpad.js resolution - by slug AND by title, same session.
// ---------------------------------------------------------------------

await test('deep link by SLUG resolves to the fork session', async () => {
    const sb = makeSandbox();
    sb.fakeWindow.__attachable = [];
    sb.fakeWindow.__live = [
        { tmux_session: FORK_TMUX_NAME, label: FORK_TITLE, activity_status: 'running',
          session: { id: 'sid-fork' } },
    ];
    sb.fakeWindow.__activeTmuxName = FORK_TMUX_NAME;

    await sb.launchpad.openProjectByName(FORK_SLUG);

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 0, 'slug form should have resolved');
    const returned = sb.calls.filter(c => c[0] === 'returnToExistingTerminal');
    assert.equal(returned.length, 1);
});

await test('deep link by TITLE (the fork label) resolves to the SAME session', async () => {
    const sb = makeSandbox();
    sb.fakeWindow.__attachable = [];
    sb.fakeWindow.__live = [
        { tmux_session: FORK_TMUX_NAME, label: FORK_TITLE, activity_status: 'running',
          session: { id: 'sid-fork' } },
    ];
    sb.fakeWindow.__activeTmuxName = FORK_TMUX_NAME;

    await sb.launchpad.openProjectByName(FORK_TITLE);

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 0, 'title form should have resolved, not bounced to /');
    const returned = sb.calls.filter(c => c[0] === 'returnToExistingTerminal');
    assert.equal(returned.length, 1, 'title lookup must land on the same session as the slug lookup');
});

await test('_findRunningSessionBySlug: slug and title both find the identical row object', () => {
    const sb = makeSandbox();
    const row = { name: FORK_TMUX_NAME, label: FORK_TITLE };
    sb.launchpad.runningSessions = [row];

    const bySlug = sb.launchpad._findRunningSessionBySlug(FORK_SLUG);
    const byTitle = sb.launchpad._findRunningSessionBySlug(FORK_TITLE);
    assert.equal(bySlug, row);
    assert.equal(byTitle, row);
});

await test('a name that exists nowhere still rejects with exactly one banner', async () => {
    const sb = makeSandbox();
    sb.fakeWindow.__attachable = [];
    sb.fakeWindow.__live = [];

    await sb.launchpad.openProjectByName('Nothing Here At All(fork)');

    sb.assertNoCreate();
    assert.equal(sb.rejectedTargets.length, 1, 'expected exactly one rejectTarget call');
    assert.equal(sb.rejectedTargets[0], 'Nothing Here At All(fork)');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    process.exit(1);
} else {
    console.log('ALL PASS');
    process.exit(0);
}
