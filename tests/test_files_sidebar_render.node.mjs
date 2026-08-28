// Node test for the file browser sidebar's RENDERED output, across the
// four states the user actually hits: a project with files, a project
// with no .claude/, the launcher with no project attached at all, and a
// directory that cannot be read.
//
// THE TWO BUGS THIS EXISTS TO CATCH.
//
// 1. THE LAUNCHER BUG. Reported verbatim: on the launcher, not in any
//    project, the sidebar said "project .claude: not present in
//    /Users/jsugamele/Development/scrolltest" and "project files: nothing
//    to list in /Users/jsugamele/Development/scrolltest" - a real path,
//    from a session the user had already left. TerminalController never
//    nulls out `_currentSession` on detach/return-to-launcher (it is kept
//    around on purpose for other readers - see config-editor-roots.js);
//    only `sessionActive` flips to false. Reading `_currentSession` alone
//    therefore resolves a STALE project. This suite drives the real
//    config-editor-panel.js against a fake TerminalController in that
//    exact shape (sessionActive: false, _currentSession: still populated)
//    and asserts the rendered tree carries no project-scoped text at all.
//
// 2. THE NOISE BUG. Even inside a real project, "project .claude: not
//    present in X" / "project files: nothing to list in X" are internal
//    bookkeeping, not information the user asked for - a project with no
//    .claude/ needs no announcement, any more than a Finder window
//    announces every folder it isn't showing a badge on. This suite
//    proves those two exact strings can never appear in rendered output,
//    while a directory the panel genuinely COULD NOT READ (permission
//    denied, ENOENT, unmounted, or - for the "workdir" root specifically -
//    the project's own directory vanishing) still renders a distinct,
//    visible notice. Collapsing that distinction back into "render
//    nothing" is the regression THE THREE-OUTCOME RULE (CLAUDE.md) exists
//    to catch, and is asserted explicitly below.
//
// WHY THIS ASSERTS RENDERED DOM TEXT, NOT STATE. Same rule
// tests/test_project_authority_render.node.mjs documents: a passing state
// assertion proves nothing about what actually painted. Every assertion
// below reads `.textContent` off the real <li> elements
// config-editor-panel.js's _loadTree()/_buildRootEl()/_buildNodeEl()
// built, through a small hand-rolled DOM (this repo has no bundled jsdom
// or Playwright - see that same file's header) that is honest about what
// it does NOT support: no querySelector, no click simulation. Neither is
// exercised here; only the initial static render is inspected, exactly
// what a user sees the instant the picker opens.
//
// Run with: node tests/test_files_sidebar_render.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => Promise<void>|void} fn  Body; throwing/rejecting fails it.
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

// ---------------------------------------------------------------------
// Minimal DOM. Hand-rolled on purpose (no jsdom/Playwright in this repo -
// see the file header). Supports exactly what config-editor-panel.js
// needs to build and hand back a static tree: createElement, appendChild,
// className, dataset, setAttribute/getAttribute, addEventListener
// (recorded, never fired), and BOTH textContent (used by _noticeLi and
// _errorEl - the notice/error paths this suite cares about most) and
// innerHTML (used by the toggle/file rows for their icon+label markup).
// textContent reads innerHTML with tags stripped when no textContent was
// set directly, so "what text actually painted" is answerable for every
// node in the tree, not just the ones built through .textContent.
// ---------------------------------------------------------------------

/** @returns {object} A fresh fake `document` plus a node-factory closure. */
function makeFakeDocument() {
    function stripTags(html) {
        return String(html).replace(/<[^>]*>/g, '');
    }
    // config-editor-panel.js's _esc() escapes a name for safe innerHTML
    // interpolation via the standard browser trick: assign as
    // textContent, then read innerHTML back out. That only works if this
    // fake actually escapes on the way through, exactly like a real
    // <div> would - otherwise every node name renders as an empty
    // string and this whole suite would pass while proving nothing.
    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    function createElement(tag) {
        const el = {
            tagName: String(tag).toUpperCase(),
            style: {},
            dataset: {},
            _attrs: {},
            _text: undefined,
            _html: '',
            children: [],
            appendChild(child) {
                this.children.push(child);
                return child;
            },
            querySelector() {
                // Deliberately unsupported - see file header. Any test
                // that needs this belongs in a real-browser harness.
                throw new Error('fake DOM: querySelector is not implemented');
            },
            addEventListener() { /* recorded nowhere; clicks are never fired */ },
            setAttribute(k, v) { this._attrs[k] = String(v); },
            getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
            get className() { return this._className || ''; },
            set className(v) { this._className = v; },
            get innerHTML() { return this._html; },
            set innerHTML(v) { this._html = v; this._text = undefined; this.children = []; },
            get textContent() {
                if (this._text !== undefined) return this._text;
                let out = stripTags(this._html);
                for (const c of this.children) out += c.textContent;
                return out;
            },
            set textContent(v) {
                this._text = String(v == null ? '' : v);
                // Mirror the real DOM's textContent->innerHTML escaping so
                // _esc()'s "assign textContent, read innerHTML back" trick
                // behaves the same as it does in a browser.
                this._html = escapeHtml(this._text);
                this.children = [];
            },
        };
        return el;
    }
    return { createElement };
}

/**
 * Load the real client modules this feature is made of, in the same
 * order index.html requires (asserted by test_config_editor_roots' own
 * "index.html loads the roots module before the panel" case - not
 * re-proven here), into one vm context with the fake DOM + fake
 * SessionStatusUI icons + real ConfigEditorTreeState (backed by an
 * in-memory localStorage so its persistence logic runs for real).
 * @returns {{ConfigEditorPanel: object, window: object}}
 */
function loadPanel() {
    const read = (name) => fs.readFileSync(path.join(ROOT, 'client', 'js', name), 'utf8');
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const store = new Map();
    const localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
    };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {} },
        document: makeFakeDocument(),
        localStorage,
    };
    vm.createContext(context);
    // config-editor-tree-state.js and session-status-ui.js are the real
    // shipped modules, unstubbed - both are pure enough (no fetch, no
    // element lookups at load time) to run as-is, which means this suite
    // exercises the same collapse/icon logic production does.
    vm.runInContext(read('config-editor-tree-state.js'), context);
    vm.runInContext(read('session-status-ui.js'), context);
    vm.runInContext(read('config-editor-roots.js'), context);
    vm.runInContext(read('config-editor-panel.js'), context);
    fakeWindow.document = context.document;
    return { ConfigEditorPanel: fakeWindow.ConfigEditorPanel, window: fakeWindow };
}

/**
 * Fresh ConfigEditorPanelController wired to a fake `treeEl`, a fake
 * `window.API.getConfigFileTree`, and a given TerminalController shape.
 * @param {object|null} terminalController  window.TerminalController shape.
 * @param {(root: string, projectPath: string|null) => Promise<{tree: Array}>} getTree
 *   Fake server response/throw per root id.
 * @returns {{panel: object, treeEl: object, window: object}}
 */
function setup(terminalController, getTree) {
    const { ConfigEditorPanel, window: win } = loadPanel();
    win.TerminalController = terminalController;
    win.API = { getConfigFileTree: getTree };
    const treeEl = win.document.createElement('div');
    ConfigEditorPanel.treeEl = treeEl;
    return { panel: ConfigEditorPanel, treeEl, window: win };
}

const PROJECT_PATH = '/Users/jsugamele/Development/scrolltest';

/** Rendered text of every top-level <li> the tree built, one per root/notice. */
function topLevelTexts(treeEl) {
    const list = treeEl.children[0];
    return list.children.map((li) => li.textContent);
}

// ---- 1. a project with files: shows the roots and their files, nothing else

await test('a project with files renders the roots and file names, no bookkeeping strings', async () => {
    const tc = { sessionActive: true, _currentSession: { session: { id: 's1', working_dir: PROJECT_PATH } } };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [{ name: 'CLAUDE.md', rel_path: 'CLAUDE.md', is_dir: false, children: [] }] };
        if (root === 'project') return { tree: [{ name: 'settings.json', rel_path: 'settings.json', is_dir: false, children: [] }] };
        if (root === 'workdir') return { tree: [{ name: 'index.html', rel_path: 'index.html', is_dir: false, children: [] }] };
        throw new Error('unexpected root ' + root);
    };
    const { panel, treeEl, window: win } = setup(tc, getTree);
    // "workdir" (like any non-"user"/non-"project" node) starts COLLAPSED
    // by default (CONFIG_EDITOR_ROOTS' own declared default - see
    // config-editor-roots.js), so its file is not built into the DOM
    // until expanded. Simulate a returning user who already had it open,
    // through the real persistence module's public API - exactly what
    // their localStorage would hold - the same technique used below for
    // the list_error case, rather than skipping the lazy-render contract.
    win.ConfigEditorTreeState.setNodeCollapsed('workdir:__root__', false);
    await panel._loadTree();
    const whole = treeEl.textContent;
    assert.match(whole, /index\.html/, 'the workdir file must be visible');
    assert.match(whole, /settings\.json/, 'the project .claude file must be visible');
    assert.match(whole, /CLAUDE\.md/, 'the user file must be visible');
    assert.doesNotMatch(whole, /not present in/, 'no removed bookkeeping string');
    assert.doesNotMatch(whole, /nothing to list in/, 'no removed bookkeeping string');
    assert.equal(topLevelTexts(treeEl).length, 3, 'exactly the three roots, no extra notice rows');
});

// ---- 2. a project with no .claude/: measured absence, nothing announced

await test('a project with no .claude/ announces nothing about it', async () => {
    const tc = { sessionActive: true, _currentSession: { session: { id: 's1', working_dir: PROJECT_PATH } } };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [] };
        if (root === 'project') {
            const err = new Error('unknown or unavailable root: project');
            err.status = 400;
            throw err;
        }
        if (root === 'workdir') return { tree: [{ name: 'index.html', rel_path: 'index.html', is_dir: false, children: [] }] };
        throw new Error('unexpected root ' + root);
    };
    const { panel, treeEl } = setup(tc, getTree);
    await panel._loadTree();
    const whole = treeEl.textContent;
    // "~/.claude" (the user root's own label) legitimately contains
    // ".claude" - what must be absent is any mention of the PROJECT
    // .claude root, which is the one that does not exist here.
    assert.doesNotMatch(whole, /project \.claude/, 'the absent project .claude root must not be named at all');
    assert.doesNotMatch(whole, /not present/, 'no removed bookkeeping string');
    assert.equal(topLevelTexts(treeEl).length, 2, 'only user + workdir roots render - no notice row for the absent one');
});

// ---- 3. the launcher, no project attached: no project-scoped text at all,
//         even with a STALE _currentSession left over from a detached session

await test('REGRESSION: the launcher with a stale detached session renders no project-scoped text', async () => {
    // This is the exact shape terminal.js leaves behind after
    // detachSession(): sessionActive false, _currentSession still holding
    // the last session's working_dir. Before the fix, resolveProjectContext
    // read _currentSession alone and this rendered
    // "project .claude: not present in /Users/.../scrolltest" on the
    // launcher screen, where no project is open at all.
    const tc = { sessionActive: false, _currentSession: { session: { id: 's1', working_dir: PROJECT_PATH } } };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [{ name: 'CLAUDE.md', rel_path: 'CLAUDE.md', is_dir: false, children: [] }] };
        throw new Error('project/workdir must not be fetched when no session is attached, got ' + root);
    };
    const { panel, treeEl } = setup(tc, getTree);
    await panel._loadTree();
    const whole = treeEl.textContent;
    assert.doesNotMatch(whole, /scrolltest/, 'the stale project path must never appear on the launcher');
    assert.doesNotMatch(whole, /project \.claude/, 'no project-scoped root label on the launcher');
    assert.doesNotMatch(whole, /project files/, 'no project-scoped root label on the launcher');
    assert.doesNotMatch(whole, /not present in/, 'no removed bookkeeping string');
    assert.doesNotMatch(whole, /nothing to list in/, 'no removed bookkeeping string');
});

await test('the true launcher (no session ever attached) also renders no project-scoped text', async () => {
    const tc = { sessionActive: false, _currentSession: null };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [] };
        throw new Error('project/workdir must not be fetched when no session is attached, got ' + root);
    };
    const { panel, treeEl } = setup(tc, getTree);
    await panel._loadTree();
    const whole = treeEl.textContent;
    assert.doesNotMatch(whole, /project \.claude/);
    assert.doesNotMatch(whole, /project files/);
});

// ---- 4. a directory that cannot be read: could-not-evaluate, visibly
//         distinct from an empty directory

await test('REGRESSION: an unreadable workdir root renders a distinct notice, not an empty tree', async () => {
    // "workdir" going 400 is NOT the ordinary "no .claude/" case - a
    // session's own working directory exists by construction when the
    // session starts, so 400 here means it was deleted/unmounted/renamed
    // out from under a still-attached session. Could-not-evaluate, must be
    // named (THE THREE-OUTCOME RULE), unlike the "project" root's 400.
    const tc = { sessionActive: true, _currentSession: { session: { id: 's1', working_dir: PROJECT_PATH } } };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [] };
        if (root === 'project') return { tree: [] };
        if (root === 'workdir') {
            const err = new Error('unknown or unavailable root: workdir');
            err.status = 400;
            throw err;
        }
        throw new Error('unexpected root ' + root);
    };
    const { panel, treeEl } = setup(tc, getTree);
    await panel._loadTree();
    const whole = treeEl.textContent;
    assert.match(whole, /could not reach/, 'the vanished workdir must be named as could-not-evaluate');
    assert.match(whole, /scrolltest/, 'it must say WHERE it looked');
    assert.equal(topLevelTexts(treeEl).length, 3,
        'user root + project root (both render normally, empty and unannounced) + the workdir error row');
});

await test('REGRESSION: an unreadable subdirectory (list_error) renders distinctly from an empty one', async () => {
    // Children are only built into the DOM once a directory node is
    // EXPANDED (the lazy-render contract config-editor-panel.js documents
    // at length). "project" is the one root that starts EXPANDED by
    // default (CONFIG_EDITOR_ROOTS), so its children build synchronously
    // during _loadTree() with no click simulation needed - this suite
    // deliberately does not implement querySelector/click (see header),
    // so the fixture is placed under the one root where that is not
    // required to see the real, live rendering.
    const tc = { sessionActive: true, _currentSession: { session: { id: 's1', working_dir: PROJECT_PATH } } };
    const getTree = async (root) => {
        if (root === 'user') return { tree: [] };
        if (root === 'project') {
            return {
                tree: [
                    { name: 'locked', rel_path: 'locked', is_dir: true, list_error: 'Permission denied', children: [] },
                    { name: 'empty-dir', rel_path: 'empty-dir', is_dir: true, children: [] },
                ],
            };
        }
        if (root === 'workdir') return { tree: [] };
        throw new Error('unexpected root ' + root);
    };
    const { panel, treeEl, window: win } = setup(tc, getTree);
    // "locked" is a non-root directory, which starts COLLAPSED by
    // default (config-editor-tree-state.js's startsCollapsed) - its
    // list_error notice only builds once it is expanded. Simulate a
    // returning user who already expanded it, through the real
    // persistence module's own public API (exactly what their
    // localStorage would hold), rather than skipping the lazy-render
    // contract this suite is not otherwise exercising.
    win.ConfigEditorTreeState.setNodeCollapsed('project:locked', false);
    await panel._loadTree();
    const projectRootLi = treeEl.children[0].children.find((li) => li.textContent.includes('project .claude'));
    assert.ok(projectRootLi, 'the project root must have rendered');
    // projectRootLi is [toggle, childList] - descend into the childList to
    // reach the two directory entries themselves.
    const projectChildList = projectRootLi.children[1];
    assert.ok(projectChildList, 'the project root must carry a children list');
    const lockedLi = projectChildList.children.find((li) => li.textContent.includes('locked'));
    const emptyDirLi = projectChildList.children.find((li) => li.textContent.includes('empty-dir'));
    assert.ok(lockedLi, 'the unreadable directory must still render as a node');
    assert.ok(emptyDirLi, 'the empty directory must still render as a normal node');
    assert.match(lockedLi.textContent, /could not list contents: Permission denied/,
        'the unreadable child must name the failure');
    // "empty-dir" is a genuinely empty, successfully-read directory: no
    // node.list_error, so it renders with zero children and NO notice -
    // the two must never collapse into the same rendered text.
    assert.doesNotMatch(emptyDirLi.textContent, /could not/, 'an empty directory must never read as could-not-evaluate');
    assert.notEqual(lockedLi.textContent, emptyDirLi.textContent,
        'an unreadable directory and an empty one must render different text');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
