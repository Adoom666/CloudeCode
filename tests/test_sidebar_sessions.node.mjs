// Node test for feat/sidebar-sessions: pinning sessions, a user-defined
// order, the density control, and the three-outcome obligations that come
// with all three.
//
// WHAT THIS ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. Every assertion
// below reads either the actual HTML string a render function wrote, the
// actual state of a DOM node a handler touched, the actual bytes a module
// wrote to storage, or the actual text of the shipped stylesheet - never
// a state object produced along the way. That rule exists here for a
// specific reason: this project shipped a feature with 282 green state
// assertions that rendered zero pixels.
//
// REAL PIXELS AND REAL KEY EVENTS ARE MEASURED ELSEWHERE. This repo has
// no bundled layout engine, so a Node process cannot compute a box.
// scripts/verify_sidebar_sessions.py drives
// tests/manual/sidebar-sessions-geometry-harness.html in a REAL headless
// Chromium at 1280x900, against the nine real session names from a
// read-only copy of the shipped cloude.db, and measures
// getBoundingClientRect(). Verified numbers, 2026-08-19, viewport
// asserted from window.innerWidth == 1280:
//
//   MEASURED ROW HEIGHTS  compact=24.00px  cozy=46.00px  detailed=66.00px
//   pinned row top=65.00 vs next row top=128.34 (pinned really is above)
//   family pill painted height 19.00 in every density, all three states
//   docked on home: #launchpad-screen settled padding-left=340px,
//   panel width=320, .launchpad-container left=400 (content not covered)
//   Alt+ArrowUp by real key event moved index 2 -> 1 and focus survived
//   the repaint; a second Alt+ArrowUp refused to cross the pinned band
//
// One trap that cost real time and is worth naming: the docked offset is
// a 160ms `transition: padding-left`, and getComputedStyle DURING a
// transition returns the current animated value. Measured too early it
// read back the pre-dock 20px and looked exactly like a rule that never
// applied - a false FAIL manufactured inside the verification step. The
// verifier now polls until two frames agree instead of sleeping a guess.
//
// Run with: node tests/test_sidebar_sessions.node.mjs

import assert from 'node:assert/strict';
import {
    ROOT, test, results, repoFile, loadModules, fakeStorage, Doc, plain, names,
} from './lib-sidebar-sessions.mjs';

const ARRANGEMENT_KEY = 'cloude.session.sidebar.arrangement';
const DENSITY_KEY = 'cloude.session.sidebar.density';

/**
 * Description: build one merged session row for the markup functions.
 * Inputs: over (object) - fields to override.
 * Output: object.
 */
function row(over = {}) {
    return Object.assign({
        name: 'cloude_a',
        created_by_cloude: true,
        created_at_epoch: 1000,
        is_active: true,
        is_this_tab: false,
        is_pinned: false,
        status: 'working',
        unread: false,
        session_id: null,
        agent_family: null,
        agent_family_source: 'unknown',
        pinned_theme: null,
    }, over);
}

/**
 * Description: load the arrangement module alone against a given storage.
 * Inputs: seed (object|null) - stored value for the arrangement key.
 *   throws (boolean) - make every storage access throw.
 * Output: object - {A (the module), storage}.
 */
function loadArrangement(seed, throws = false) {
    const storage = fakeStorage(seed === null ? {} : { [ARRANGEMENT_KEY]: seed }, throws);
    const { window } = loadModules(['session-sidebar-store.js', 'session-sidebar-arrangement.js'], { storage });
    return { A: window.SessionSidebarArrangement, storage };
}

/**
 * Description: load the row-markup module with its shared collaborators
 *   stubbed to the empty string, so what is asserted is THIS module's
 *   markup and not theirs.
 * Inputs: none.
 * Output: object - the SessionSidebarRows module.
 */
function loadRows() {
    const { window } = loadModules(['session-listing-state.js', 'session-sidebar-rows.js']);
    window.SessionStatusUI = { dotHtml: () => '<span class="status-dot"></span>', markUnreadHtml: () => '' };
    window.SessionRowActions = { html: () => '<button data-session-action="close"></button>' };
    return window.SessionSidebarRows;
}

// =====================================================================
// ITEM 46a - PINNING A SESSION, and the storage it lives in.
// =====================================================================

await test('ITEM 46: a pinned session sorts above every unpinned one', () => {
    const { A } = loadArrangement(null);
    A.save(['b'], ['a', 'b', 'c']);
    const out = A.arrange([row({ name: 'a' }), row({ name: 'b' }), row({ name: 'c' })]);
    assert.deepEqual(names(out.rows), ['b', 'a', 'c']);
    assert.equal(out.rows[0].is_pinned, true, 'the row must carry the pin so the markup can draw it');
});

await test('ITEM 46: the pin is persisted under the arrangement key, versioned', () => {
    const { A, storage } = loadArrangement(null);
    A.togglePin('b', ['a', 'b', 'c']);
    const raw = storage.map.get(ARRANGEMENT_KEY);
    assert.ok(raw, `nothing was written to ${ARRANGEMENT_KEY}`);
    const parsed = JSON.parse(raw);
    assert.equal(parsed.v, A.VERSION);
    assert.deepEqual(plain(parsed.pinned), ['b']);
    assert.deepEqual(plain(parsed.order), ['a', 'b', 'c'],
        'the write records the list as the user is looking at it');
});

await test('ITEM 46: pinning is a TOGGLE - a second press really unpins', () => {
    const { A, storage } = loadArrangement(null);
    assert.equal(A.togglePin('b', ['a', 'b']), true, 'first press pins');
    assert.equal(A.togglePin('b', ['b', 'a']), false, 'second press must UNPIN, not re-pin');
    assert.deepEqual(plain(JSON.parse(storage.map.get(ARRANGEMENT_KEY)).pinned), []);
    assert.equal(A.isPinned('b'), false);
    assert.deepEqual(
        names(A.arrange([row({ name: 'a' }), row({ name: 'b' })]).rows), ['b', 'a'],
        'an unpinned row keeps its place in the order, it does not jump to the bottom');
});

await test('ITEM 46: a reload re-reads the pin from storage, it is not in-memory only', () => {
    const { A, storage } = loadArrangement(null);
    A.togglePin('b', ['a', 'b', 'c']);
    // A "reload" is a fresh module against the SAME bytes.
    const { window } = loadModules(['session-sidebar-store.js', 'session-sidebar-arrangement.js'], { storage });
    const fresh = window.SessionSidebarArrangement;
    fresh.load();
    assert.equal(fresh.isPinned('b'), true);
    assert.deepEqual(
        names(fresh.arrange([row({ name: 'a' }), row({ name: 'b' })]).rows),
        ['b', 'a'],
    );
});

// =====================================================================
// ITEM 46b - A USER ORDER THE APP MUST NOT SILENTLY OVERRIDE.
// =====================================================================

await test('ITEM 46: a stored order beats the incoming poll order', () => {
    const { A } = loadArrangement(JSON.stringify({ v: 1, pinned: [], order: ['c', 'a', 'b'] }));
    A.load();
    // The poll returns them in a completely different sequence, which is
    // exactly what the default sort would have produced.
    const out = A.arrange([row({ name: 'a' }), row({ name: 'b' }), row({ name: 'c' })]);
    assert.deepEqual(names(out.rows), ['c', 'a', 'b']);
});

await test('ITEM 46: a session the arrangement has never seen lands at the END of its band', () => {
    const { A } = loadArrangement(JSON.stringify({ v: 1, pinned: ['c'], order: ['c', 'a'] }));
    A.load();
    const out = A.arrange([row({ name: 'a' }), row({ name: 'b' }), row({ name: 'c' })]);
    assert.deepEqual(names(out.rows), ['c', 'a', 'b'],
        'a brand new session must not jump above one the user placed');
});

// ITEM 65 REVERSES ITEM 46'S BAND-EDGE RULE ON PURPOSE. The edge used to
// be a hard refusal, on the reasoning that a move must never change a pin
// the user could not see themselves perform. The user has since asked for
// exactly that crossing ("not sure how hard to be able to drag the items
// in and our of pinned group"), so the protection MOVES rather than
// disappearing: a crossing move reports `crossed: true` and the caller
// announces the pin change in words. An invisible unpin was the thing
// being prevented, not the unpin itself.
//
// The tests below are the old ITEM 46 pair, inverted. They are kept in
// place and re-argued rather than deleted, so the history of the rule is
// legible to whoever reads this next.

await test('ITEM 65: a move ACROSS the band edge pins or unpins, and says it did', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);

    // Up out of the unpinned band: this now PINS.
    const up = plain(A.move('a', -1, ['p', 'a', 'b']));
    assert.deepEqual(up.order, ['a', 'p', 'b']);
    assert.equal(up.crossed, true, 'a crossing move must report that it crossed');
    assert.equal(up.pinned, true, 'moving up past the boundary pins the row');
    assert.equal(A.isPinned('a'), true, 'and the pin is actually applied, not just reported');
});

await test('ITEM 65: a move DOWN out of the pinned band unpins, the other direction', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    const down = plain(A.move('p', 1, ['p', 'a', 'b']));
    assert.deepEqual(down.order, ['a', 'p', 'b']);
    assert.equal(down.crossed, true);
    assert.equal(down.pinned, false, 'moving down past the boundary unpins the row');
    assert.equal(A.isPinned('p'), false);
});

await test('ITEM 65: a move WITHIN a band is not a crossing and changes no pin', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    const within = plain(A.move('b', -1, ['p', 'a', 'b']));
    assert.deepEqual(within.order, ['p', 'b', 'a']);
    assert.equal(within.crossed, false,
        'or every ordinary reorder would announce a pin change that did not happen');
    assert.equal(within.pinned, false);
});

await test('ITEM 65: the LIST edges are still hard refusals, unlike the band edge', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    assert.equal(A.move('p', -1, ['p', 'a', 'b']), null,
        'there is no row above the first one to cross past');
    assert.equal(A.move('b', 1, ['p', 'a', 'b']), null,
        'nor below the last one');
    assert.equal(A.isPinned('p'), true, 'a refused move must not have changed a pin either');
});

// The drag twin of the four tests above. `placeAt` replaces `moveBefore`:
// the BAND IS NOW AN EXPLICIT ARGUMENT rather than something inferred
// from the neighbouring row. That is not a style choice - a drop into an
// EMPTY pinned group has no neighbouring row to infer a band from, and
// inferring one from the nearest row would make the empty group
// undroppable, which is precisely the case the feature exists for.

await test('ITEM 65: a drag INTO the pinned group pins the row', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    const res = plain(A.placeAt('b', true, 'p', ['p', 'a', 'b']));
    assert.deepEqual(res.order, ['b', 'p', 'a']);
    assert.equal(res.crossed, true);
    assert.equal(res.pinned, true);
    assert.equal(A.isPinned('b'), true);
});

await test('ITEM 65: a drag OUT of the pinned group unpins it, the other direction', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    const res = plain(A.placeAt('p', false, null, ['p', 'a', 'b']));
    assert.deepEqual(res.order, ['a', 'b', 'p']);
    assert.equal(res.crossed, true);
    assert.equal(res.pinned, false);
    assert.equal(A.isPinned('p'), false, 'dragging to the bottom of the other group unpins');
});

await test('ITEM 65: a drag into an EMPTY pinned group still pins', () => {
    const { A } = loadArrangement(null);
    A.save([], ['a', 'b']);
    // No pinned row exists, so there is no neighbour to infer a band
    // from. The band comes from the DROP TARGET, which is the only reason
    // this can work at all.
    const res = plain(A.placeAt('b', true, null, ['a', 'b']));
    assert.equal(res.pinned, true);
    assert.equal(res.crossed, true);
    assert.equal(A.isPinned('b'), true);
});

await test('ITEM 65: a drag WITHIN a band reorders and changes no pin', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    const res = plain(A.placeAt('b', false, 'a', ['p', 'a', 'b']));
    assert.deepEqual(res.order, ['p', 'b', 'a']);
    assert.equal(res.crossed, false);
    assert.equal(A.isPinned('b'), false);
});

await test('ITEM 65: a drop that changes NEITHER band nor sequence is not a move', () => {
    const { A } = loadArrangement(null);
    A.save(['p'], ['p', 'a', 'b']);
    assert.equal(A.placeAt('a', false, 'b', ['p', 'a', 'b']), null,
        'or a live drag writes storage and repaints on every pointer sample');
    assert.equal(A.placeAt('a', false, 'a', ['p', 'a', 'b']), null,
        'a row dropped on itself is not a move either');
});

await test('ITEM 46: a move is PERSISTED, not just applied in memory', () => {
    const { A, storage } = loadArrangement(null);
    A.save([], ['a', 'b', 'c']);
    A.move('c', -1, ['a', 'b', 'c']);
    assert.deepEqual(JSON.parse(storage.map.get(ARRANGEMENT_KEY)).order, ['a', 'c', 'b']);
});

// =====================================================================
// THREE OUTCOMES - a gone session, and an order that cannot be read.
// =====================================================================

await test('a remembered session that no longer exists KEEPS ITS SLOT and is reported', () => {
    const { A } = loadArrangement(JSON.stringify({
        v: 1, pinned: ['ghost'], order: ['ghost', 'a', 'b'],
    }));
    A.load();
    const out = A.arrange([row({ name: 'a' }), row({ name: 'b' })]);
    assert.deepEqual(names(out.rows), ['a', 'b'], 'it renders no row');
    assert.deepEqual(plain(out.missing), ['ghost'],
        'and it is NAMED, not silently dropped and not treated as an error');
});

await test('a gone session that is PINNED but not in the order is still reported', () => {
    // The two lists are written together but nothing forces them to stay
    // in step, and a pin is the stronger statement of the two. Checking
    // only the order would let the more deliberate of the user's two
    // choices disappear without a word.
    const { A } = loadArrangement(JSON.stringify({
        v: 1, pinned: ['ghost'], order: ['a'],
    }));
    A.load();
    const out = A.arrange([row({ name: 'a' })]);
    assert.deepEqual(plain(out.missing), ['ghost']);
});

await test('a gone session that comes back returns to ITS OWN SLOT, not to the bottom', () => {
    const { A, storage } = loadArrangement(JSON.stringify({
        v: 1, pinned: [], order: ['a', 'ghost', 'b'],
    }));
    A.load();
    // The user reorders the two VISIBLE rows while ghost is away.
    A.move('b', -1, ['a', 'b']);
    assert.deepEqual(JSON.parse(storage.map.get(ARRANGEMENT_KEY)).order, ['b', 'ghost', 'a'],
        'the absent name held index 1 through a reorder of the rows around it');
    const back = A.arrange([row({ name: 'a' }), row({ name: 'b' }), row({ name: 'ghost' })]);
    assert.deepEqual(names(back.rows), ['b', 'ghost', 'a']);
    assert.deepEqual(plain(back.missing), []);
});

await test('an ABSENT arrangement is not a failure and announces nothing', () => {
    const { A } = loadArrangement(null);
    const st = A.load();
    assert.equal(st.status, 'default');
    assert.equal(st.reason, null);
});

await test('a CORRUPT arrangement falls back to the default AND says so', () => {
    for (const [bad, why] of [
        ['{not json', 'not valid JSON'],
        ['[]', 'not an arrangement object'],
        ['{"v":9,"pinned":[],"order":[]}', 'version'],
        ['{"v":1,"pinned":[3],"order":[]}', 'list of session names'],
        ['{"v":1,"pinned":[],"order":"nope"}', 'list of session names'],
    ]) {
        const { A } = loadArrangement(bad);
        const st = A.load();
        assert.equal(st.status, 'unreadable', `${bad} must be unreadable, not silently accepted`);
        assert.match(st.reason, new RegExp(why), `${bad} must say WHY`);
        assert.deepEqual(plain(st.pinned), []);
        assert.deepEqual(
            names(A.arrange([row({ name: 'b' }), row({ name: 'a' })]).rows),
            ['b', 'a'], 'the incoming default order is used unchanged',
        );
    }
});

await test('a CORRUPT arrangement is NOT overwritten behind the user back', () => {
    const { A, storage } = loadArrangement('{not json');
    A.load();
    A.arrange([row({ name: 'a' })]);
    assert.equal(storage.map.get(ARRANGEMENT_KEY), '{not json',
        'the bytes stay on disk, inspectable, until a deliberate change replaces them');
});

await test('storage that THROWS is unreadable, not a crash and not a default', () => {
    const { A } = loadArrangement(null, true);
    const st = A.load();
    assert.equal(st.status, 'unreadable');
    assert.match(st.reason, /storage unavailable/);
});

await test('a deliberate arrangement change CLEARS the unreadable verdict', () => {
    const { A } = loadArrangement('{not json');
    A.load();
    A.togglePin('a', ['a', 'b']);
    assert.equal(A.current().status, 'ok',
        'once the user has told us an order there is no longer a lost one to warn about');
});

// =====================================================================
// ITEM 47 - DENSITY.
// =====================================================================

await test('ITEM 47: the density preference persists under its own key', () => {
    const storage = fakeStorage();
    const doc = new Doc();
    const { window } = loadModules(['session-sidebar-density.js'], { storage, document: doc });
    window.SessionSidebar = { panel: doc.createElement('aside'), repaint() {} };
    const D = window.SessionSidebarDensity;
    D.init();
    assert.equal(D.currentMode(), 'cozy', 'the default is the row as it shipped');
    D.setMode('compact');
    assert.equal(storage.map.get(DENSITY_KEY), 'compact');
    assert.equal(window.SessionSidebar.panel.getAttribute('data-density'), 'compact',
        'the panel must really carry the attribute the stylesheet keys off');
});

await test('ITEM 47: a stored density that is not a mode falls back rather than being applied', () => {
    const storage = fakeStorage({ [DENSITY_KEY]: 'enormous' });
    const doc = new Doc();
    const { window } = loadModules(['session-sidebar-density.js'], { storage, document: doc });
    window.SessionSidebar = { panel: doc.createElement('aside'), repaint() {} };
    window.SessionSidebarDensity.init();
    assert.equal(window.SessionSidebarDensity.currentMode(), 'cozy');
});

await test('ITEM 47: each density draws a DIFFERENT row, and the difference is in the markup', () => {
    const Rows = loadRows();
    const compact = Rows.rowHtml(row(), 'compact');
    const cozy = Rows.rowHtml(row(), 'cozy');
    const detailed = Rows.rowHtml(row(), 'detailed');
    assert.ok(!compact.includes('session-sidebar-row-badge'), 'compact drops the badge');
    assert.ok(cozy.includes('session-sidebar-row-badge'), 'cozy keeps the badge');
    assert.ok(!cozy.includes('session-sidebar-row-meta'), 'cozy has no second line');
    assert.ok(detailed.includes('session-sidebar-row-meta'), 'detailed adds the second line');

    // ITEM 63: NO FAMILY PILL, AT ANY DENSITY. This loop used to assert
    // the pill appeared exactly once in each mode; it now asserts it does
    // not appear at all. Counting the pill's unique data attribute rather
    // than the class token is still the right check either way: the class
    // list is `family-pill family-pill--unknown`, so a naive
    // /family-pill/ count reads 2 for one pill.
    for (const [name, html] of [['compact', compact], ['cozy', cozy], ['detailed', detailed]]) {
        assert.equal((html.match(/data-family-source=/g) || []).length, 0,
            `${name} must draw no family pill`);
        assert.ok(!html.includes('family-pill'), `${name} must not emit the pill class either`);
    }

    // AND THE BADGE IS EMITTED EXACTLY ONCE PER ROW, wherever it sits.
    // Detailed moves it to the second line rather than drawing it twice,
    // which is the specific way this refactor could have gone wrong.
    assert.equal((detailed.match(/session-sidebar-row-badge/g) || []).length, 1,
        'detailed moves the badge down, it does not duplicate it');
    assert.equal((cozy.match(/session-sidebar-row-badge/g) || []).length, 1);
    assert.equal((compact.match(/session-sidebar-row-badge/g) || []).length, 0);
});

await test('ITEM 47: the stylesheet really declares three different row paddings', () => {
    const css = repoFile('client', 'css', 'session-sidebar-density.css');
    const pad = (mode) => {
        const m = css.match(new RegExp(
            `\\[data-density="${mode}"\\][^\\{]*\\.session-sidebar-row-main\\s*\\{([^\\}]*)\\}`));
        assert.ok(m, `no row-main rule for ${mode}`);
        const p = m[1].match(/padding:\s*([^;]+);/);
        assert.ok(p, `${mode} declares no padding`);
        return p[1].trim();
    };
    const [c, z, d] = ['compact', 'cozy', 'detailed'].map(pad);
    assert.notEqual(c, z, 'compact and cozy must not be the same box');
    assert.notEqual(z, d, 'cozy and detailed must not be the same box');
});

await test('no sidebar row paints an inline-start rail, in any stylesheet', () => {
    // "also the border on the sidebar. get rid of the thick left bar like
    // on the homepage and clean it like the homepage looks now."
    //
    // THREE stylesheets painted one, and they were three different bars:
    // a 2px accent rail on [data-pinned="1"] here, a 3px session-accent
    // rail on [data-session-theme] in session-theme-tint.css, and nothing
    // at all on [data-active="1"] - the current session never had one, so
    // it lost nothing.
    //
    // Matching the SHAPE rather than any one declaration: an inset shadow
    // offset along X with a zero Y offset is a left-side bar whatever token
    // it is written in, so a rail reintroduced in a different colour still
    // trips this. Comment lines are stripped first, so the prose above
    // explaining the rail is not itself a violation.
    //
    // THIS IS A TEXT GUARD AGAINST REINTRODUCTION, NOT THE PROOF. The
    // proof is scripts/verify_sidebar_row_edges.py, which screenshots real
    // pixels and requires each row's left edge band to MATCH its right -
    // which no grep can do, because a grep cannot see what a later
    // stylesheet paints.
    const rail = /inset\s+[1-9]\d*px\s+0\s+0/;
    for (const file of ['session-sidebar.css', 'session-sidebar-density.css',
                        'session-sidebar-groups.css', 'session-theme-tint.css']) {
        const live = repoFile('client', 'css', file).split('\n')
            .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('/*'))
            .join('\n');
        assert.doesNotMatch(live, rail,
            `${file} declares an inline-start rail on a sidebar row`);
    }
});

await test('a sidebar row carries the home screen ONE border, four sides', () => {
    // The rails were also doing separation work in a dense list. That job
    // moved to the same ring `.running-session-row` already uses, so the
    // list still reads without any edge encoding state.
    const css = repoFile('client', 'css', 'session-sidebar.css');
    const m = css.match(/\.session-sidebar-row\s*\{([^}]*)\}/);
    assert.ok(m, 'no .session-sidebar-row rule');
    assert.match(m[1], /border:\s*1px solid var\(--color-border\)/,
        'a transparent border here would leave the dense list with no separation '
        + 'at all once the rails are gone');
    assert.ok(!/border-left/.test(m[1]),
        'a border-left override is the same left-side bar wearing a different property');
});

// =====================================================================
// THE FAMILY PILL - three states, every density.
// =====================================================================

// =====================================================================
// ITEM 63 - THE PILL IS GONE FROM THE SIDEBAR, AND STILL RIGHT AT HOME.
//
// "i dont think we need the pills in the sidebare take out for now."
//
// Removing it also removed a real defect, and the shape of that defect is
// why the tests below assert what they do. The sidebar's builder put a
// LITERAL `~` in front of a guessed family, and `.family-pill--guess`
// carries a `::before { content: "~" }` in client/css/styles.css that
// adds another. So a guessed family RENDERED as `~~claude` while every
// DOM assertion read a single, correct `~claude`. A test that reads DOM
// text cannot see a `::before`. The home screen's builder never added the
// literal, so only the sidebar was ever wrong - which is exactly why
// deleting the sidebar's pill must not be allowed to disturb the home
// screen's.
// =====================================================================

await test('ITEM 63: the sidebar builder no longer exposes a pill builder at all', () => {
    const Rows = loadRows();
    assert.equal(typeof Rows.familyPillHtml, 'undefined',
        'a removed feature should not leave a live builder behind for the next caller');
    // CODE LINES ONLY. The file's docblock explains the `~~claude` defect
    // by name, and prose ABOUT a construct is not a use of it - the same
    // distinction this repo's own portability lint had to learn. Stripping
    // comments first is what keeps this assertion about the markup.
    const src = repoFile('client', 'js', 'session-sidebar-rows.js')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\/\/.*/g, '');
    assert.ok(!/family-pill/.test(src),
        'no remaining reference to the pill class in the sidebar row MARKUP');
});

await test('ITEM 63: the HOME SCREEN still draws the pill, in all three states', () => {
    // The launcher owns its own builder and nothing in the sidebar feeds
    // it, so this is the regression guard on the blast radius of item 63.
    const src = repoFile('client', 'js', 'launchpad.js');
    for (const cls of ['family-pill--fact', 'family-pill--guess', 'family-pill--unknown']) {
        assert.ok(src.includes(cls), `the home screen must still render ${cls}`);
    }
});

await test('ITEM 63: the home screen renders a SINGLE tilde, and the CSS is the only source', () => {
    // THE PIXEL-LEVEL FORM OF THIS ASSERTION. The tilde a user sees comes
    // from exactly one place. If the builder ever adds a literal one back
    // while the stylesheet keeps its `::before`, this fails - which is the
    // check the `~~claude` defect needed and did not have.
    const js = repoFile('client', 'js', 'launchpad.js');
    const css = repoFile('client', 'css', 'styles.css');

    const guessRule = css.match(/\.family-pill--guess::before\s*\{([^}]*)\}/);
    assert.ok(guessRule, 'the stylesheet must still carry the guess tilde');
    assert.match(guessRule[1], /content:\s*"~"/, 'and it is a tilde');

    // The builder must NOT prepend its own. `~${' + '...}` in a template
    // literal, or a '~' + concat, are the two shapes this can take.
    const label = js.match(/const label = known \? ([^;]+);/);
    assert.ok(label, 'the launchpad label expression must still be findable');
    assert.ok(!label[1].includes('~'),
        'the builder must not add a second tilde on top of the stylesheet one');
});

// =====================================================================
// NO RESTART FOR AN UNKNOWN LIFECYCLE.
// =====================================================================

await test('no row offers a RESTART control, at any density or status', () => {
    const Rows = loadRows();
    for (const density of ['compact', 'cozy', 'detailed']) {
        for (const status of ['working', 'idle', 'dead', 'unknown']) {
            const html = Rows.rowHtml(row({ status }), density);
            assert.ok(!/restart/i.test(html),
                `${density}/${status} must not offer a restart - the sidebar cannot tell `
                + 'a stopped session from one whose state it could not determine');
        }
    }
});

await test('the sidebar row builder never receives a lifecycle it could act on', () => {
    // Structural, not incidental: the attachable probe carries no
    // `lifecycle` field at all, so there is nothing here that could grow
    // into a restart control by accident.
    const src = repoFile('client', 'js', 'session-sidebar-rows.js');
    assert.ok(!/r\.lifecycle/.test(src), 'the row markup must not read a lifecycle it does not have');
});

// =====================================================================
// A FAILED LISTING IS NOT AN EMPTY LIST.
// =====================================================================

await test('zero rows from a FAILED listing renders CANNOT DETERMINE, never the empty state', () => {
    const Rows = loadRows();
    const html = Rows.listHtml([], 'cozy', { ok: false, reason: 'tmux_missing', detail: 'tmux is gone' }, [], null);
    assert.ok(html.includes('CANNOT DETERMINE'), 'the failure must be stated');
    assert.ok(html.includes('tmux is gone'), 'and it must say what failed');
    assert.ok(html.includes('data-listing-reason="tmux_missing"'), 'machine-readable too');
    assert.ok(!html.includes('session-sidebar-empty'),
        'the confident empty state is the false green this exists to remove');
    assert.ok(!html.includes('data-session-action'),
        'a block about sessions we cannot confirm must offer no actions');
});

await test('zero rows from a listing that ANSWERED still renders the honest empty state', () => {
    const Rows = loadRows();
    const html = Rows.listHtml([], 'cozy', { ok: true }, [], null);
    assert.ok(html.includes('no other conversations'));
    assert.ok(!html.includes('CANNOT DETERMINE'));
});

await test('the reason and detail come from the SERVER verdict, not a browser guess', () => {
    const { window } = loadModules(['session-listing-state.js']);
    const S = window.SessionListingState;
    const err = new Error('boom');
    err.detail = { listing_reason: 'tmux_missing', listing_detail: 'tmux is not installed' };
    assert.equal(S.reasonFromError(err, 503), 'tmux_missing');
    assert.equal(S.detailFromError(err, 503), 'tmux is not installed');
    // And it never returns a blank cell.
    assert.equal(S.reasonFromError(new Error(''), 0), 'network_error');
    assert.ok(S.detailFromError(new Error(''), 0).length > 0);
    assert.equal(S.attentionHtml({ ok: true }), '', 'a healthy listing draws nothing');
});

await test('a fetch whose ATTACHABLE probe failed reports unavailable, not ok', async () => {
    const { window } = loadModules(['session-listing-state.js', 'session-sidebar-fetch.js']);
    const err = new Error('nope');
    err.status = 503;
    err.detail = { listing_reason: 'timeout', listing_detail: 'the probe timed out' };
    window.API = {
        async listAttachableSessions() { throw err; },
        async listSessions() { return []; },
    };
    const out = await window.SessionSidebarFetch.load(null);
    assert.equal(out.listing.ok, false);
    assert.equal(out.listing.reason, 'timeout');
    assert.deepEqual(plain(out.rows), []);
});

await test('a fetch whose LIVE list failed is still ok - that probe answers a different question', async () => {
    const { window } = loadModules(['session-listing-state.js', 'session-sidebar-fetch.js']);
    window.API = {
        async listAttachableSessions() {
            return [{ name: 'cloude_a', created_by_cloude: true, created_at_epoch: 1 }];
        },
        async listSessions() { throw new Error('no backend in this tab'); },
    };
    const out = await window.SessionSidebarFetch.load(null);
    assert.equal(out.listing.ok, true,
        'not knowing which rows have a live backend is not the same as not knowing what exists');
    assert.equal(out.rows.length, 1);
});

// =====================================================================
// THE ARRANGEMENT NOTICE AND THE MISSING NOTE ARE REALLY RENDERED.
// =====================================================================

await test('an unreadable arrangement renders a notice ALONGSIDE the fallback rows', () => {
    const Rows = loadRows();
    const html = Rows.listHtml([row({ name: 'a' })], 'cozy', { ok: true }, [],
        { status: 'unreadable', reason: 'stored value is not valid JSON' });
    assert.ok(html.includes('CANNOT LOAD your saved order'));
    assert.ok(html.includes('stored value is not valid JSON'), 'it must say why');
    assert.ok(html.includes('data-name="a"'), 'and the list still works');
});

await test('a healthy arrangement renders NO notice - a check that never clears is furniture', () => {
    const Rows = loadRows();
    for (const st of [null, { status: 'ok' }, { status: 'default' }]) {
        assert.ok(!Rows.listHtml([row()], 'cozy', { ok: true }, [], st).includes('CANNOT LOAD'));
    }
});

await test('held slots for gone sessions are counted on screen, and only when there are any', () => {
    const Rows = loadRows();
    assert.equal(Rows.missingNoteHtml([]), '');
    const one = Rows.missingNoteHtml(['ghost']);
    assert.ok(one.includes('data-order-missing="1"'));
    assert.ok(one.includes('remembered position is held'));
    assert.ok(Rows.missingNoteHtml(['a', 'b']).includes('data-order-missing="2"'));
});

// =====================================================================
// THE REORDER INTERACTION - real events into a real element tree.
// =====================================================================

/**
 * Description: build a live sidebar list with three rows wired to the
 *   real reorder handlers, so a dispatched key event travels the real
 *   code path rather than a method call standing in for one.
 * Inputs: pinned (Array<string>), order (Array<string>).
 * Output: object - {window, list, rows, storage, repaints}.
 */
function mountList(pinned = [], order = ['a', 'b', 'c']) {
    const doc = new Doc();
    const storage = fakeStorage({
        [ARRANGEMENT_KEY]: JSON.stringify({ v: 1, pinned, order }),
    });
    const { window } = loadModules(
        ['session-sidebar-store.js', 'session-sidebar-arrangement.js', 'session-sidebar-reorder.js'],
        { storage, document: doc },
    );
    window.SessionSidebarArrangement.load();
    const list = doc.createElement('div');
    list.id = 'session-sidebar-list';
    doc.body.appendChild(list);
    const live = doc.createElement('div');
    live.id = 'session-sidebar-live';
    live.textContent = '';
    doc.body.appendChild(live);

    const state = { names: order.slice(), repaints: 0 };
    const paint = () => {
        // A BROWSER DROPS FOCUS WHEN THE FOCUSED ELEMENT IS DETACHED, and
        // this stub must too. Without it a repaint left activeElement
        // pointing at an element no longer in the tree, so "focus
        // survived the repaint" passed even with the restore deleted -
        // the assertion was reading a corpse.
        list.childNodes.slice().forEach((c) => {
            if (doc.activeElement === c) doc.activeElement = doc.body;
            list.removeChild(c);
        });
        state.names.forEach((n, i) => {
            const el = doc.createElement('div');
            el.setAttribute('class', 'session-sidebar-row');
            el.dataset.name = n;
            el._box = { top: i * 20, height: 20 };
            list.appendChild(el);
        });
    };
    window.SessionSidebar = {
        listEl: list,
        repaint() {
            state.repaints++;
            const A = window.SessionSidebarArrangement;
            const cur = A.current();
            const known = new Set(cur.order);
            const ordered = cur.order.filter((n) => state.names.includes(n))
                .concat(state.names.filter((n) => !known.has(n)));
            state.names = ordered.filter((n) => A.isPinned(n))
                .concat(ordered.filter((n) => !A.isPinned(n)));
            paint();
        },
        activateRow() { state.activated = true; },
    };
    paint();
    window.SessionSidebarReorder.init();
    return { window, doc, list, storage, state, live };
}

await test('ITEM 46: Alt+ArrowUp on a focused row MOVES it, by a dispatched key event', () => {
    const { window, list, storage } = mountList();
    const rowB = list.querySelectorAll('.session-sidebar-row')[1];
    rowB.focus();
    rowB.dispatchEvent('keydown', { key: 'ArrowUp', altKey: true });
    assert.deepEqual(
        plain(list.querySelectorAll('.session-sidebar-row').map((e) => e.dataset.name)),
        ['b', 'a', 'c'], 'the DOM order must actually change');
    assert.deepEqual(JSON.parse(storage.map.get(ARRANGEMENT_KEY)).order, ['b', 'a', 'c'],
        'and the move must be persisted');
    assert.equal(window.document.activeElement.dataset.name, 'b',
        'focus must survive the repaint or a held key moves the row exactly once');
});

await test('ITEM 46: a BARE ArrowUp moves focus and leaves the order alone', () => {
    const { window, list } = mountList();
    const rows = list.querySelectorAll('.session-sidebar-row');
    rows[1].focus();
    rows[1].dispatchEvent('keydown', { key: 'ArrowUp' });
    assert.deepEqual(
        plain(list.querySelectorAll('.session-sidebar-row').map((e) => e.dataset.name)),
        ['a', 'b', 'c'], 'navigation must not reorder');
    assert.equal(window.document.activeElement.dataset.name, 'a');
});

await test('ITEM 46: the list is ONE tab stop - roving tabindex, not three', () => {
    const { list } = mountList();
    const rows = list.querySelectorAll('.session-sidebar-row');
    rows[1].focus();
    rows[1].dispatchEvent('keydown', { key: 'ArrowUp' });
    const tabbable = list.querySelectorAll('.session-sidebar-row')
        .filter((e) => e.getAttribute('tabindex') === '0');
    assert.equal(tabbable.length, 1, 'exactly one row may be in the tab order');
    assert.equal(tabbable[0].dataset.name, 'a');
});

await test('ITEM 46: "p" pins the focused row from the keyboard alone', () => {
    const { list, storage } = mountList();
    const rows = list.querySelectorAll('.session-sidebar-row');
    rows[2].focus();
    rows[2].dispatchEvent('keydown', { key: 'p' });
    assert.deepEqual(JSON.parse(storage.map.get(ARRANGEMENT_KEY)).pinned, ['c']);
    assert.equal(list.querySelectorAll('.session-sidebar-row')[0].dataset.name, 'c',
        'and it moves to the top immediately, not on the next poll');
});

await test('ITEM 46: Home and End reach the ends without a mouse', () => {
    const { window, list } = mountList();
    const rows = list.querySelectorAll('.session-sidebar-row');
    rows[1].focus();
    rows[1].dispatchEvent('keydown', { key: 'End' });
    assert.equal(window.document.activeElement.dataset.name, 'c');
    window.document.activeElement.dispatchEvent('keydown', { key: 'Home' });
    assert.equal(window.document.activeElement.dataset.name, 'a');
});

await test('ITEM 46: a refused move is ANNOUNCED, not silently swallowed', () => {
    const { list, live } = mountList(['a']);
    const rowA = list.querySelectorAll('.session-sidebar-row')[0];
    rowA.focus();
    rowA.dispatchEvent('keydown', { key: 'ArrowUp', altKey: true });
    assert.match(live.textContent, /already at the top/,
        'a dead key with no feedback is a key the user keeps pressing');
});

await test('ITEM 46: Enter activates the row - the keyboard can switch conversations', () => {
    const { list, state } = mountList();
    const rowA = list.querySelectorAll('.session-sidebar-row')[0];
    rowA.focus();
    rowA.dispatchEvent('keydown', { key: 'Enter' });
    assert.equal(state.activated, true);
});

await test('a poll tick is DROPPED mid-drag rather than reordering under the finger', async () => {
    const src = repoFile('client', 'js', 'session-sidebar.js');
    assert.match(src, /isDragging\(\)\) return;/,
        'the fetch path must bail while a drag is in flight');
});

// =====================================================================
// ITEM 45 - THE BAR ON THE HOME SCREEN.
// =====================================================================

await test('ITEM 45: the home screen SHOWS the sidebar instead of hiding it', () => {
    const src = repoFile('client', 'js', 'app.js');
    const start = src.indexOf('showLaunchpad() {');
    const body = src.slice(start, src.indexOf('showTerminal(', start));
    assert.ok(body.includes('window.SessionSidebar.show()'),
        'the home screen must mount the bar');
    assert.ok(!/SessionSidebar\.hide\(\)/.test(body),
        'and must not hide it, which is what dropped a pinned bar on every trip home');
});

await test('ITEM 45: leaving a screen does NOT clobber the persisted open state', () => {
    const src = repoFile('client', 'js', 'session-sidebar.js');
    const hide = src.slice(src.indexOf('    hide() {'), src.indexOf('setActiveSession('));
    assert.match(hide, /close\(\{ persist: false \}\)/,
        'hide() used to persist "closed", so a pinned-open bar came back closed');
    assert.match(src, /const persist = !opts \|\| opts\.persist !== false;/,
        'close() must default to persisting so a real user close still sticks');
});

await test('ITEM 45: the docked offset really reaches the home screen ID selector', () => {
    const css = repoFile('client', 'css', 'session-sidebar.css');
    assert.match(css, /body\.session-sidebar-pinned #launchpad-screen \{/,
        'a class-only rule loses to #launchpad-screen and the bar covered the content');
    assert.match(css, /calc\(var\(--sidebar-dock-w\) \+ var\(--screen-pad-x\)\)/,
        'the dock width must ADD to the screen padding, not replace it');
    const styles = repoFile('client', 'css', 'styles.css');
    assert.match(styles, /--screen-pad-x:\s*20px;/, 'the token must exist');
    assert.match(styles, /#launchpad-screen \{[^}]*padding: var\(--screen-pad-x\);/,
        'and the screen must actually use it, or the two numbers drift apart again');
});

// =====================================================================
// THE REPAINT DIFF - a change the user just made must actually paint.
// =====================================================================
//
// The list skips its innerHTML rewrite when the signature matches, which
// is what stops the 5s poll thrashing focus and scroll position. That
// makes the signature a correctness surface, not an optimisation: any
// field it forgets is a change the user made that does not appear until
// something unrelated happens to move.

await test('the signature tracks DENSITY, or switching mode paints nothing', () => {
    const Rows = loadRows();
    const rows = [row()];
    assert.notEqual(Rows.signature(rows, 'compact', { ok: true }, []),
        Rows.signature(rows, 'cozy', { ok: true }, []));
    assert.notEqual(Rows.signature(rows, 'cozy', { ok: true }, []),
        Rows.signature(rows, 'detailed', { ok: true }, []));
});

await test('the signature tracks the PIN, or a pin does not paint until something else moves', () => {
    const Rows = loadRows();
    assert.notEqual(
        Rows.signature([row({ is_pinned: true })], 'cozy', { ok: true }, []),
        Rows.signature([row({ is_pinned: false })], 'cozy', { ok: true }, []));
});

await test('the signature tracks POSITION, or a reorder does not repaint', () => {
    const Rows = loadRows();
    const a = row({ name: 'a' });
    const b = row({ name: 'b' });
    assert.notEqual(
        Rows.signature([a, b], 'cozy', { ok: true }, []),
        Rows.signature([b, a], 'cozy', { ok: true }, []),
        'two rows swapped is a different picture and must force a rewrite');
});

await test('the signature tracks the LISTING VERDICT, or a probe failure never paints', () => {
    const Rows = loadRows();
    const rows = [row()];
    assert.notEqual(
        Rows.signature(rows, 'cozy', { ok: true }, []),
        Rows.signature(rows, 'cozy', { ok: false, reason: 'timeout', detail: 'x' }, []));
    assert.notEqual(
        Rows.signature(rows, 'cozy', { ok: false, reason: 'timeout', detail: 'x' }, []),
        Rows.signature(rows, 'cozy', { ok: false, reason: 'tmux_missing', detail: 'y' }, []),
        'a different reason is a different block on screen');
});

await test('the signature tracks HELD SLOTS, so the note appears when it should', () => {
    const Rows = loadRows();
    assert.notEqual(
        Rows.signature([row()], 'cozy', { ok: true }, []),
        Rows.signature([row()], 'cozy', { ok: true }, ['ghost']));
});

await test('the signature is still STABLE across an idle poll tick', () => {
    const Rows = loadRows();
    const rows = [row({ name: 'a' }), row({ name: 'b' })];
    assert.equal(
        Rows.signature(rows, 'cozy', { ok: true }, []),
        Rows.signature(rows.map((r) => ({ ...r })), 'cozy', { ok: true }, []),
        'or the 5s poll rewrites the DOM every tick and throws away focus');
});

// =====================================================================
// HOUSEKEEPING - the rules this repo enforces on itself.
// =====================================================================

await test('every new sidebar file stays under the 500-line budget', () => {
    const files = [
        ['js', 'session-sidebar.js'], ['js', 'session-sidebar-rows.js'],
        ['js', 'session-sidebar-arrangement.js'], ['js', 'session-sidebar-density.js'],
        ['js', 'session-sidebar-reorder.js'], ['js', 'session-sidebar-fetch.js'],
        ['js', 'session-listing-state.js'], ['css', 'session-sidebar-density.css'],
        ['css', 'session-sidebar.css'],
    ];
    for (const [dir, f] of files) {
        const lines = repoFile('client', dir, f).split('\n').length;
        assert.ok(lines <= 500, `${f} is ${lines} lines, over the 500 budget`);
    }
});

await test('nothing added here uses an em-dash, an en-dash, or an emoji', () => {
    const files = [
        ['client', 'js', 'session-sidebar-arrangement.js'],
        ['client', 'js', 'session-sidebar-density.js'],
        ['client', 'js', 'session-sidebar-reorder.js'],
        ['client', 'js', 'session-sidebar-fetch.js'],
        ['client', 'js', 'session-listing-state.js'],
        ['client', 'js', 'session-sidebar-rows.js'],
        ['client', 'js', 'session-sidebar.js'],
        ['client', 'css', 'session-sidebar-density.css'],
    ];
    for (const parts of files) {
        const text = repoFile(...parts);
        const em = (text.match(new RegExp(String.fromCharCode(8212), 'g')) || []).length;
        const en = (text.match(new RegExp(String.fromCharCode(8211), 'g')) || []).length;
        assert.equal(em, 0, `${parts.join('/')} has ${em} em-dashes`);
        assert.equal(en, 0, `${parts.join('/')} has ${en} en-dashes`);
    }
});

await test('every new module is actually SERVED - a file nobody loads is dead code', () => {
    const html = repoFile('client', 'index.html');
    for (const f of [
        'session-listing-state.js', 'session-sidebar-arrangement.js',
        'session-sidebar-fetch.js', 'session-sidebar-density.js',
        'session-sidebar-reorder.js',
    ]) {
        assert.ok(html.includes(`/static/js/${f}`), `${f} is not in index.html`);
    }
    assert.ok(html.includes('/static/css/session-sidebar-density.css'));
    // Load order is a real contract here: the row builder and the fetch
    // module both call into the listing-state module at parse-adjacent
    // time, and the arrangement must exist before the sidebar reads it.
    const idx = (s) => html.indexOf(`/static/js/${s}`);
    assert.ok(idx('session-listing-state.js') < idx('session-sidebar-fetch.js'));
    assert.ok(idx('session-sidebar-arrangement.js') < idx('session-sidebar.js'));
    assert.ok(idx('session-sidebar.js') < idx('session-sidebar-density.js'));
    assert.ok(idx('session-sidebar.js') < idx('session-sidebar-reorder.js'));
});

await test('the density control and the live region are in the shipped markup', () => {
    const html = repoFile('client', 'index.html');
    assert.ok(html.includes('id="session-sidebar-density"'));
    assert.ok(html.includes('id="session-sidebar-density-menu"'));
    assert.ok(html.includes('id="session-sidebar-live"'));
    for (const m of ['compact', 'cozy', 'detailed']) {
        assert.ok(html.includes(`data-density-mode="${m}"`), `no menu item for ${m}`);
    }
    assert.match(html, /role="menuitemradio"/, 'a three-way choice needs radio semantics');
    assert.match(html, /id="session-sidebar-list"[^>]*\srole="listbox"/,
        'the list needs listbox semantics for the roving tabindex to mean anything');
});

const { passes, failures } = results();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
