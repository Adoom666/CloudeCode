// Node test for the file editor's root plan: which roots get listed, and
// what the user is told when one is not.
//
// THE BUG THIS EXISTS TO CATCH. The panel used to skip the "project" and
// "workdir" roots with a bare `continue` whenever the working directory
// did not resolve. The tree then rendered ~/.claude alone, with no
// message, and the user read it as "the file editor is no longer showing
// my project files" - which is what was reported. A short tree that says
// nothing is indistinguishable from a complete one.
//
// So there are two independent contracts here, and both must hold:
//   1. a resolved working directory yields ALL THREE roots and no notice;
//   2. an unresolved one yields the user root PLUS a notice that names
//      what is missing and why - never a silently short plan.
// Deleting the notice from planRoots(), or restoring the `continue` by
// dropping the project roots without one, fails a test below.
//
// The unwrap is covered too: the session object is a SessionInfo WRAPPER
// on the rejoin/deep-link paths and a bare Session on create/adopt, and
// working_dir lives at `.session.working_dir` in the first case. Reading
// it unwrapped is the recurring bug class in this codebase (the PID
// display was fixed four times for it), so both shapes are asserted.
//
// Run with: node tests/test_config_editor_roots.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Load config-editor-roots.js in a bare sandbox. The module is pure
 * string/object work with no DOM dependency, which is the reason it was
 * extracted out of the panel in the first place.
 * @returns {object}  window.ConfigEditorRoots.
 */
function loadRoots() {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'config-editor-roots.js'),
        'utf8',
    );
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = { window: fakeWindow, console: { log() {} } };
    vm.createContext(context);
    vm.runInContext(src, context);
    return fakeWindow.ConfigEditorRoots;
}

const Roots = loadRoots();

/**
 * Ids of the roots a plan actually lists.
 * @param {Array<object>} plan  planRoots() output.
 * @returns {string[]}  Root ids in plan order.
 */
function rootIds(plan) {
    return plain(plan).filter((s) => s.kind === 'root').map((s) => s.def.id);
}

/**
 * Notice messages a plan carries.
 * @param {Array<object>} plan  planRoots() output.
 * @returns {string[]}  Messages in plan order.
 */
function notices(plan) {
    return plain(plan).filter((s) => s.kind === 'notice').map((s) => s.message);
}

/**
 * Strip the vm realm off a value so assert's prototype-sensitive deep
 * comparison can be used: objects built inside vm.runInContext have a
 * different Object.prototype and are never "reference-equal" to a literal
 * declared out here, however identical their contents.
 * @param {any} value  Any JSON-serializable value from the sandbox.
 * @returns {any}  The same value, rebuilt in this realm.
 */
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

const WORKDIR = '/Users/someone/Development/project';

test('the three roots are declared in order, workdir collapsed by default', () => {
    assert.deepEqual(plain(Roots.ROOTS.map((r) => r.id)), ['user', 'project', 'workdir']);
    const workdir = Roots.ROOTS.find((r) => r.id === 'workdir');
    assert.equal(workdir.label, 'project files');
    assert.equal(workdir.defaultExpanded, false);
});

test('a SessionInfo WRAPPER resolves working_dir from .session', () => {
    const tc = { sessionActive: true, _currentSession: { session: { id: 'ses_1', working_dir: WORKDIR }, tmux_session: 'x' } };
    assert.deepEqual(plain(Roots.resolveProjectContext(tc)), { path: WORKDIR, reason: 'ok' });
});

test('a BARE Session resolves working_dir from the top level', () => {
    const tc = { sessionActive: true, _currentSession: { id: 'ses_1', working_dir: WORKDIR } };
    assert.deepEqual(plain(Roots.resolveProjectContext(tc)), { path: WORKDIR, reason: 'ok' });
});

test('no attached session reports reason no-session, not a bare null', () => {
    assert.deepEqual(plain(Roots.resolveProjectContext({ _currentSession: null, sessionActive: false })), { path: null, reason: 'no-session' });
    assert.deepEqual(plain(Roots.resolveProjectContext(null)), { path: null, reason: 'no-session' });
});

test('REGRESSION: a stale _currentSession from a detached session is not a project context on the launcher', () => {
    // detachSession() (terminal.js) and every path back to the launchpad
    // set sessionActive = false but deliberately leave _currentSession in
    // place for other readers (launchpad self-adopt filter, debug). A
    // stale session object with sessionActive false must NOT resolve a
    // working directory - that was the bug: the sidebar showed
    // "project .claude: not present in /Users/.../scrolltest" while the
    // user sat on the launcher, nowhere near a project, because the
    // resolver only checked `_currentSession` truthiness.
    const tc = {
        sessionActive: false,
        _currentSession: { session: { id: 'ses_1', working_dir: WORKDIR } },
    };
    assert.deepEqual(plain(Roots.resolveProjectContext(tc)), { path: null, reason: 'no-session' });
});

test('an attached, active session resolves normally', () => {
    const tc = {
        sessionActive: true,
        _currentSession: { session: { id: 'ses_1', working_dir: WORKDIR } },
    };
    assert.deepEqual(plain(Roots.resolveProjectContext(tc)), { path: WORKDIR, reason: 'ok' });
});

test('an attached session with no working_dir is its OWN reason', () => {
    const tc = { sessionActive: true, _currentSession: { session: { id: 'ses_1' } } };
    assert.deepEqual(plain(Roots.resolveProjectContext(tc)), { path: null, reason: 'no-working-dir' });
});

test('REGRESSION: a resolved working dir plans all three roots, no notice', () => {
    const plan = Roots.planRoots({ path: WORKDIR, reason: 'ok' });
    assert.deepEqual(rootIds(plan), ['user', 'project', 'workdir'],
        'project and workdir must be planned whenever the working directory resolved');
    assert.deepEqual(notices(plan), [], 'nothing to explain when every root is listed');
});

test('REGRESSION: no session on the launcher is a MEASURED absence - silent, no notice', () => {
    // Flipped deliberately from this suite's earlier contract. 'no-session'
    // is the ORDINARY state of the launcher (the user has no project open,
    // on the one screen whose whole point is not being in one) - narrating
    // it is itself the noise the user reported ("on the launcher screen
    // ... but I'm not even in a project"). A short plan here is not a bug
    // to explain; it is the correct plan.
    const plan = Roots.planRoots({ path: null, reason: 'no-session' });
    assert.deepEqual(rootIds(plan), ['user']);
    assert.deepEqual(notices(plan), [], 'the launcher with no project needs no explanation at all');
});

test('REGRESSION: an unresolvable working dir on an ATTACHED session is never silent', () => {
    // Different in kind from 'no-session': a session IS attached (the
    // user really is in a project) and it reports no working directory
    // anyway - that should never happen in ordinary use, so unlike
    // 'no-session' it stays could-not-evaluate and keeps its notice.
    const plan = Roots.planRoots({ path: null, reason: 'no-working-dir' });
    assert.deepEqual(rootIds(plan), ['user']);
    const msgs = notices(plan);
    assert.equal(msgs.length, 1);
    assert.match(msgs[0], /no working directory/);
    assert.match(msgs[0], /project \.claude and project files/);
});

test('only the could-not-evaluate reason carries a notice; the measured-absence reason does not', () => {
    assert.deepEqual(notices(Roots.planRoots({ path: null, reason: 'no-session' })), []);
    assert.equal(notices(Roots.planRoots({ path: null, reason: 'no-working-dir' })).length, 1);
});

test('workdirUnavailableNotice names the root and the directory, and reads as could-not-evaluate', () => {
    const msg = Roots.workdirUnavailableNotice('project files', WORKDIR);
    assert.match(msg, /project files/);
    assert.ok(msg.includes(WORKDIR), 'the notice must say WHERE it looked');
    assert.match(msg, /could not reach/, 'must read as could-not-evaluate, not a measured absence');
});

test('REGRESSION: no notice function narrates a project with no .claude/ or an empty root', () => {
    // The two strings the user reported as noise ("project .claude: not
    // present in X" / "project files: nothing to list in X") must not be
    // producible by ANY exported function any more - those are measured
    // absences and the panel renders nothing for them (see
    // config-editor-panel.js's _buildRootEl). Only workdirUnavailableNotice
    // survives, and it is reserved for the could-not-evaluate case.
    assert.equal(Roots.missingRootNotice, undefined, 'missingRootNotice must be removed, not just unused');
    for (const key of Object.keys(Roots)) {
        const fn = Roots[key];
        if (typeof fn !== 'function' || fn.length === 0) continue;
        let sample;
        try {
            sample = key === 'workdirUnavailableNotice'
                ? fn('project files', WORKDIR)
                : key === 'projectRootsNotice' ? fn('no-working-dir')
                    : key === 'listErrorNotice' ? fn('Permission denied')
                        : null;
        } catch (_) { sample = null; }
        if (typeof sample !== 'string') continue;
        assert.ok(!/not present in/.test(sample), `${key} must never produce the removed "not present in" copy`);
        assert.ok(!/nothing to list in/.test(sample), `${key} must never produce the removed "nothing to list in" copy`);
    }
});

test('copy stays in the lowercase UI voice, with no dashes or emoji', () => {
    const strings = [
        Roots.projectRootsNotice('no-working-dir'),
        Roots.workdirUnavailableNotice('project files', WORKDIR),
    ];
    for (const s of strings) {
        assert.ok(s, 'every could-not-evaluate state needs a sentence');
        assert.ok(!/[–—]/.test(s), `no en/em dashes: ${s}`);
        assert.ok(!/[A-Z]/.test(s.replace(WORKDIR, '')), `lowercase copy: ${s}`);
    }
    assert.equal(Roots.projectRootsNotice('ok'), null, 'ok has nothing to say');
    assert.equal(Roots.projectRootsNotice('no-session'), null, 'no-session is a measured absence, not a sentence');
});

test('the panel reads its root table and plan from this module', () => {
    const panel = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'config-editor-panel.js'),
        'utf8',
    );
    assert.ok(panel.includes('window.ConfigEditorRoots.ROOTS'),
        'the panel must not keep a second copy of the root table');
    assert.ok(panel.includes('window.ConfigEditorRoots.planRoots'),
        'the panel must render the plan, not re-derive which roots to skip');
    assert.ok(!/continue; \/\/ no session attached/.test(panel),
        'the silent continue that dropped both project roots must stay gone');
});

test('index.html loads the roots module before the panel', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    const roots = html.indexOf('<script src="/static/js/config-editor-roots.js">');
    const panel = html.indexOf('<script src="/static/js/config-editor-panel.js">');
    assert.ok(roots !== -1, 'config-editor-roots.js must be served');
    assert.ok(roots < panel, 'the panel reads ConfigEditorRoots.ROOTS at definition time');
});

test('REGRESSION: roots.js and panel.js share a global scope without colliding', () => {
    // These ship as classic <script> tags, so both files' top-level
    // bindings land in ONE global scope. Both legitimately want the name
    // CONFIG_EDITOR_ROOTS, and a duplicate top-level `const` is an
    // uncaught SyntaxError that kills the SECOND file at parse time -
    // window.ConfigEditorPanel never gets defined and the file editor
    // button does nothing at all. Loading them back to back in one
    // context is the only check that reproduces it; reading either file
    // alone cannot. roots.js keeps its bindings inside a closure.
    const read = (name) => fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', name), 'utf8',
    );
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        console: { log() {} },
        document: { createElement: () => ({ style: {} }) },
    };
    vm.createContext(context);
    vm.runInContext(read('config-editor-roots.js'), context);
    vm.runInContext(read('config-editor-panel.js'), context);
    assert.ok(fakeWindow.ConfigEditorPanel,
        'the panel must still define window.ConfigEditorPanel after roots.js loaded');
    assert.deepEqual(
        plain(fakeWindow.ConfigEditorRoots.ROOTS.map((r) => r.id)),
        ['user', 'project', 'workdir'],
    );
});

// ---- three-outcome rule: an unreadable node must say so, distinctly ---
//
// Regression coverage for the "no skills shown" investigation: the fix
// was NOT in this module's root plan (that part was already correct -
// ~/.claude and <project>/.claude were both reachable, "skills" was
// already allow-listed) but in how an OSError from iterdir() got
// swallowed into an empty result. listErrorNotice() is the client-side
// half: the exact sentence rendered for TreeNode.list_error, kept as a
// pure function so it is asserted here rather than only visible by
// opening the picker on a machine with a permission-mangled directory.

test('listErrorNotice names the failure, never reads like "empty"', () => {
    const s = Roots.listErrorNotice('Permission denied');
    assert.ok(s.includes('Permission denied'), 'must surface the actual reason');
    assert.ok(/could not/i.test(s), 'must say it could not evaluate, not that there is nothing');
    assert.ok(!/[–—]/.test(s), 'no en/em dashes');
});

test('REGRESSION: an unreadable node and a genuinely empty node render different strings', () => {
    // The bug this whole module exists to kill, restated for a single
    // node instead of a root: config_files.py used to catch OSError from
    // iterdir() and leave `children: []` with no marker, which is
    // BYTE-IDENTICAL to a directory that was read successfully and truly
    // has nothing in it. If a future edit removes list_error and goes
    // back to that, this is the assertion that catches it - not by
    // testing the empty case (there is nothing to test there, that's the
    // point), but by proving the two rendered strings can never collapse
    // into one.
    const emptyDirRendering = null; // an empty-but-successfully-read dir: no notice at all
    const unreadableRendering = Roots.listErrorNotice('Permission denied');
    assert.notEqual(unreadableRendering, emptyDirRendering,
        'an unreadable directory must never render identically to an empty one');
});

test('the panel renders list_error through the shared notice builder, not an inline string', () => {
    const panel = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'config-editor-panel.js'),
        'utf8',
    );
    assert.ok(panel.includes('node.list_error'),
        'the panel must check TreeNode.list_error before rendering a directory\'s children');
    assert.ok(panel.includes('window.ConfigEditorRoots.listErrorNotice(node.list_error)'),
        'the wording must come from the tested pure function, not a second inline copy');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
