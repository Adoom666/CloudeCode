// THE COMPOSITION ROOT'S KEYBOARD DISPATCH.
//
// WHAT WAS BROKEN, measured 2026-09-01. `ArchiveKeys.resolve()` is pure
// and was already well tested, and it RESOLVED all twelve actions
// correctly the whole time. The defect was one layer over, in
// `archive-screen.js::onKeydown`, which was an if-chain: six resolved
// actions had no branch at all, so pressing the key produced nothing,
// no error, no log. An action that resolves and is never performed is an
// ABSENT BRANCH - indistinguishable from a key nobody pressed.
//
// SO EVERY TEST BELOW DRIVES A REAL `keydown` DISPATCH ON THE DOCUMENT,
// never a direct call to a handler. Calling the handler proves the
// handler works, which was never in question; only dispatch can fail the
// way this bug failed.
//
// The if-chain is now a TABLE, which turns the same mistake from an
// invisible absent branch into a MISSING KEY - and D1 below antijoins it
// in both directions, because a handler under a typo'd action name is
// the identical defect wearing the other hat: present, correct, and
// unreachable forever.
//
// Run with: node tests/test_archive_screen_keys.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

/** The transcript used wherever one has to be open. @type {number} */
const OPEN_TRANSCRIPT = 5767;

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 *
 * IT AWAITS, and every call site must be `await test(...)`. A harness
 * that does not await records a pass the moment the promise is created,
 * so every assertion afterwards runs past the verdict and throws into an
 * unhandled rejection with the suite already green - a verification step
 * that cannot fail.
 *
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing or rejecting fails it.
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
 * Every module the composition root wires together, in load order. The
 * screen is loaded LAST because its IIFE calls buildShell() at script
 * load - deliberately, so the status-light re-parent target exists
 * before App.showArchive() reaches for it - and that needs
 * `#archive-screen` already in the document.
 * @type {string[]}
 */
const MODULES = [
    'modal-stack.js',
    'archive-outcome.js', 'archive-mask.js', 'archive-format.js',
    'archive-outcome-view.js', 'archive-state.js', 'archive-keys.js',
    'archive-keys-help.js',
    'archive-virtual-list.js', 'archive-body-gate.js', 'archive-body-cache.js',
    'archive-line-render.js', 'archive-reader-dom.js', 'archive-reader-paging.js',
        'archive-reader-select.js', 'archive-reader-body.js',
        'archive-reader.js',
    'archive-nav-row.js', 'archive-nav-card.js', 'archive-nav-info.js', 'archive-nav-tree.js',
                        'archive-nav.js',
    'archive-fuzzy.js', 'archive-tlist-row.js', 'archive-tlist-filter.js',
    'archive-transcript-list.js', 'archive-search-render.js', 'archive-search.js',
    'archive-deeplink.js', 'archive-screen-reader.js', 'archive-export.js',
    // The conversation view, which is the reader pane's DEFAULT. The
    // composition root builds it at wire() time, so leaving it out here
    // fails as "Cannot read properties of undefined" inside wire() -
    // which reports the wrong cause.
    'archive-chat-block.js', 'archive-chat-info.js',
    'archive-chat-subagents.js', 'archive-chat-turn.js',
    'archive-chat-estimate.js', 'archive-chat-stack.js',
    'archive-chat-clicks.js', 'archive-chat-view.js',
    'archive-chat-screen.js', 'archive-screen-views.js',
    // The composition root was split for the 500-line cap: the shell DOM,
    // the cross-pane toolbar, the pane resizers and the crumb's
    // vocabulary are four modules it now composes, and it builds its
    // shell at ITS OWN script load - so all four must be in the context
    // before archive-screen.js runs, exactly as in index.html.
    'archive-pane-resize.js', 'archive-crumb.js',
    'archive-screen-shell.js', 'archive-screen-tools.js',
    'archive-screen.js',
];

/**
 * Build a whole archive screen in one vm context sharing one window.
 *
 * The api answers every call with a transport failure. That is honest
 * for this file: nothing here asserts anything about server data, and a
 * fake success would make the reader paint rows this suite would then be
 * tempted to assert on. The spine is installed directly instead.
 *
 * @returns {object} {screen, keys, document, window, warns, root}
 */
function buildScreen() {
    const env = createEnvironment();
    const doc = env.document;

    // The shell's host element, which index.html owns in production.
    const screenRoot = doc.createElement('div');
    screenRoot.setAttribute('id', 'archive-screen');
    // onKeydown refuses to act unless the screen is the active one, so
    // an archive key press cannot fire while the terminal is on screen.
    screenRoot.classList.add('active');
    doc.body.appendChild(screenRoot);

    const warns = [];
    const fakeWindow = { document: doc, innerWidth: 1400 };
    // No `location` and no `history`: ArchiveDeeplink.syncUrl returns
    // null rather than throwing when they are absent, which keeps the
    // address bar entirely out of a keyboard test.
    const context = {
        window: fakeWindow,
        document: doc,
        console: {
            log() {}, error() {}, debug() {},
            /** Record warnings so D4 can assert one names its action. */
            warn(...args) { warns.push(args.join(' ')); },
        },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        setTimeout, clearTimeout,
        /** The reader's scheduler, run inline so renders are synchronous. */
        requestAnimationFrame(fn) { fn(); return 0; },
    };
    context.globalThis = context;
    vm.createContext(context);

    fakeWindow.API = {
        /** @returns {Promise<object>} always a transport failure */
        getArchiveTranscript() {
            return Promise.resolve({ envelope: null, httpStatus: 0,
                headers: null, transportError: 'not wired in this test' });
        },
        /** @returns {Promise<object>} always a transport failure */
        listArchiveLines() {
            return Promise.resolve({ envelope: null, httpStatus: 0,
                headers: null, transportError: 'not wired in this test' });
        },
        /** @returns {Promise<object>} always a transport failure */
        listArchiveTranscripts() {
            return Promise.resolve({ envelope: null, httpStatus: 0,
                headers: null, transportError: 'not wired in this test' });
        },
        /**
         * The body cache REFUSES to be built without this method, by
         * name, at construction - so its absence is a wiring error the
         * reader will not paper over. No test here reads a body.
         * @returns {Promise<object>} always a transport failure
         */
        getArchiveBody() {
            return Promise.resolve({ envelope: null, httpStatus: 0,
                headers: null, transportError: 'not wired in this test' });
        },
        /** @returns {Promise<object>} always a transport failure */
        listArchiveHosts() {
            return Promise.resolve({ envelope: null, httpStatus: 0,
                headers: null, transportError: 'not wired in this test' });
        },
    };

    for (const f of MODULES) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return {
        screen: fakeWindow.ArchiveScreen,
        keys: fakeWindow.ArchiveKeys,
        window: fakeWindow,
        document: doc,
        root: screenRoot,
        warns,
    };
}

/**
 * Press a key the way a person does: a real bubbling `keydown` on a real
 * node. `event.key` carries the CHARACTER PRODUCED, which is why '?'
 * arrives as '?' and never as '/' plus shiftKey.
 * @param {object} node - the node to dispatch from, usually document.body.
 * @param {string} key - the event.key value.
 * @returns {object} the dispatched event.
 */
function press(node, key) {
    return node.dispatchEvent('keydown', {
        key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    });
}

/**
 * A small spine: one ordinary row followed by a foldable progress run.
 * groupRows leaves a run of ONE alone, so three progress rows are the
 * smallest thing that folds.
 * @returns {Array<object>} spine rows.
 */
function smallSpine() {
    const rows = [{ line_no: 1, record_type: 'assistant', role: 'assistant',
        body_id: null, body_chars: 400 }];
    for (let i = 2; i <= 4; i++) {
        rows.push({ line_no: i, record_type: 'progress', role: null,
            body_id: null, body_chars: 40 });
    }
    return rows;
}

// =====================================================================
// D1 - HANDLER COVERAGE, BOTH DIRECTIONS
// =====================================================================

await test('D1: every ACTIONS value has a handler, and every handler key is a real action', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const table = h.screen.handlerTable();
    const actions = Object.keys(h.keys.ACTIONS).map((k) => h.keys.ACTIONS[k]);
    const handled = Object.keys(table);

    assert.ok(actions.length >= 12,
        `only ${actions.length} actions found; the map did not load`);

    // DIRECTION ONE: an action with no handler is the original bug - it
    // resolves, dispatch finds nothing, and nothing happens.
    const unhandled = actions.filter((a) => !handled.includes(a));
    assert.equal(unhandled.length, 0,
        `resolved actions with NO handler, so pressing their keys does ` +
        `nothing: ${unhandled.join(', ')}`);

    // DIRECTION TWO, and it is not symmetry for its own sake: a handler
    // under a name `resolve()` never produces is present, correct, and
    // unreachable forever. It reads as coverage and is not. A typo in an
    // action name produces exactly this and nothing else notices.
    const orphans = handled.filter((k) => !actions.includes(k));
    assert.equal(orphans.length, 0,
        `handlers keyed to names that are not ArchiveKeys.ACTIONS values, ` +
        `so they can never fire: ${orphans.join(', ')}`);

    for (const a of actions) {
        assert.equal(typeof table[a], 'function',
            `the handler for "${a}" is not a function`);
    }
});

// =====================================================================
// D2 - THE PREVIOUSLY-DEAD KEYS, EACH THROUGH A REAL DISPATCH
// =====================================================================

await test('D2a: j and k move the READER selection', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const reader = h.screen.views().reader;
    reader.setSpine(smallSpine(), true);
    assert.equal(reader.selectedIndex(), -1, 'nothing is selected to begin with');

    press(h.document.body, 'j');
    assert.equal(reader.selectedIndex(), 0,
        'j did not reach the reader - this is the exact silent drop being fixed');
    press(h.document.body, 'j');
    assert.equal(reader.selectedIndex(), 1);
    press(h.document.body, 'k');
    assert.equal(reader.selectedIndex(), 0, 'k did not move the selection back');

    // The arrow aliases go through the same table, so they cannot drift
    // away from the letter bindings.
    press(h.document.body, 'ArrowDown');
    assert.equal(reader.selectedIndex(), 1);
    press(h.document.body, 'ArrowUp');
    assert.equal(reader.selectedIndex(), 0);
});

await test('D2b: Enter opens the selected row', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const reader = h.screen.views().reader;
    reader.setSpine(smallSpine(), true);
    assert.equal(reader.items().length, 2, 'the progress run must fold');

    // Select the run, then Enter. A run TOGGLES, which is the one
    // "open" whose effect is visible in the DOM with no server behind
    // it - so the assertion is on rendered rows, not on a spy.
    press(h.document.body, 'j');
    press(h.document.body, 'j');
    assert.equal(reader.selectedIndex(), 1);
    assert.equal(reader.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]').length, 0);

    press(h.document.body, 'Enter');
    const shown = [...reader.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]')]
        .map((n) => n.getAttribute('data-line-no'));
    assert.equal(shown.join(','), '2,3,4',
        `Enter did not open the selected run (saw ${JSON.stringify(shown)})`);

    press(h.document.body, 'Enter');
    assert.equal(reader.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]').length, 0,
        'Enter did not toggle the run closed again');
});

await test('D2c: / focuses the nav filter input', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const nav = h.screen.views().nav;
    assert.notEqual(h.document.activeElement, nav.filterInput);

    press(h.document.body, '/');
    assert.equal(h.document.activeElement, nav.filterInput,
        'the / key did not focus the filter box');

    // And the binding does not fire from INSIDE a text field, or typing
    // a slash into the filter would re-focus it and swallow the
    // character. resolve() reads the target's tagName for this.
    h.document.activeElement = null;
    press(nav.filterInput, '/');
    assert.equal(h.document.activeElement, null,
        'a / typed inside a text field was treated as a binding');
});

await test('D2d: t advances the scheme filter', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const list = h.screen.views().list;
    const defs = h.window.ArchiveTranscriptList.SCHEME_DEFS.map((d) => d.v);
    // THE CYCLE STARTS AT THE DEFAULT, WHICH IS NOT defs[0]. The list now
    // opens on the uuid (top-level session) scheme rather than on `all`,
    // so this walks the cycle from wherever the default sits rather than
    // hardcoding an index - the ORDER is what `t` is being tested for,
    // and pinning the start would make this test fail every time the
    // default is retuned without the cycle being broken at all.
    const start = defs.indexOf(h.window.ArchiveTranscriptList.DEFAULT_SCHEME);
    assert.notEqual(start, -1, 'the default scheme is not one of SCHEME_DEFS');
    assert.equal(list.scheme(), defs[start]);

    press(h.document.body, 't');
    assert.equal(list.scheme(), defs[(start + 1) % defs.length],
        't did not advance the scheme filter');
    press(h.document.body, 't');
    assert.equal(list.scheme(), defs[(start + 2) % defs.length]);
    // It CYCLES rather than stopping at the end, or the third press
    // would be a key that silently does nothing again.
    press(h.document.body, 't');
    assert.equal(list.scheme(), defs[start],
        't did not wrap back around the cycle');
});

// =====================================================================
// D3 - LOAD_MORE ROUTING, ALL THREE CASES
// =====================================================================

/**
 * Replace both pagers with counters, so which one moved is a fact
 * rather than an inference. Spying on ONLY the reader would let a
 * regression that pages neither look identical to a pass.
 * @param {object} h - a buildScreen() result.
 * @returns {object} {counts, restore}
 */
function spyPagers(h) {
    const views = h.screen.views();
    const counts = { reader: 0, list: 0 };
    const realReader = views.reader.requestMoreLines;
    const realList = views.list.loadMore;
    views.reader.requestMoreLines = function () {
        counts.reader++; return Promise.resolve('spy');
    };
    views.list.loadMore = function () {
        counts.list++; return Promise.resolve('spy');
    };
    return {
        counts,
        /** Put the real pagers back. @returns {void} */
        restore() {
            views.reader.requestMoreLines = realReader;
            views.list.loadMore = realList;
        },
    };
}

await test('D3a: focus INSIDE the reader pane pages the reader', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const spy = spyPagers(h);
    const scroller = h.screen.views().reader.root()
        .querySelector('.archive-reader__scroller');
    scroller.focus();

    press(h.document.body, 'm');
    assert.equal(spy.counts.reader, 1, 'the reader did not page');
    assert.equal(spy.counts.list, 0, 'the list paged instead of the reader');
    spy.restore();
});

await test('D3b: focus ELSEWHERE with a transcript open still pages the READER', () => {
    // THE MEASURED BUG. On /archive/t/5767 `document.activeElement` is
    // not inside the reader - nothing in the pane has been clicked - so
    // a focus-only rule pages the LIST while the person is staring at a
    // transcript, and the transcript never grows past its first 500
    // lines. The old code had one case and always chose the list.
    const h = buildScreen();
    h.screen.show({ view: 'transcript', transcriptId: OPEN_TRANSCRIPT });
    const spy = spyPagers(h);
    h.document.activeElement = null;
    assert.equal(h.screen.route().transcriptId, OPEN_TRANSCRIPT);

    press(h.document.body, 'm');
    assert.equal(spy.counts.reader, 1,
        'with a transcript open and focus elsewhere, m must page the reader');
    assert.equal(spy.counts.list, 0,
        'm paged the transcript list while a transcript was open');
    spy.restore();
});

await test('D3c: NO transcript open pages the LIST', () => {
    const h = buildScreen();
    h.screen.show({ view: 'project', projectId: 12 });
    const spy = spyPagers(h);
    h.document.activeElement = null;
    assert.equal(h.screen.route().transcriptId, null);

    press(h.document.body, 'm');
    assert.equal(spy.counts.list, 1, 'the list did not page');
    assert.equal(spy.counts.reader, 0,
        'm paged the reader with no transcript open at all');
    spy.restore();
});

// =====================================================================
// D4 - A RESOLVED ACTION WITH NO HANDLER IS NAMED, NOT SWALLOWED
// =====================================================================

await test('D4: an action with no handler warns BY NAME rather than returning quietly', () => {
    const h = buildScreen();
    h.screen.show({ view: 'root' });
    const table = h.screen.handlerTable();
    const action = h.keys.ACTIONS.NEXT_ROW;
    const real = table[action];

    // The gap is created here rather than by editing the client, and it
    // is put back below. This is the exact state the old if-chain was
    // in permanently for six actions: the action resolves and nothing
    // performs it.
    delete table[action];
    h.warns.length = 0;
    press(h.document.body, 'j');

    const named = h.warns.filter((w) => w.includes(action));
    assert.equal(named.length, 1,
        `pressing j with no handler produced ${h.warns.length} warnings and ` +
        `none named "${action}". A silent return here is the entire bug ` +
        `class: an action that resolves and never runs looks exactly like ` +
        `a key nobody pressed.`);
    assert.ok(named[0].includes('handlerTable'),
        'the warning does not say where to add the missing handler');

    table[action] = real;
    h.warns.length = 0;
    press(h.document.body, 'j');
    assert.equal(h.warns.length, 0,
        'a restored handler still warned, so the warn is not gated on absence');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
