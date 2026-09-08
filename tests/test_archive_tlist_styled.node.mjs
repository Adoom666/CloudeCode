// THE TRANSCRIPT LIST HAS A STYLESHEET, AND EVERY CLASS IT EMITS IS IN IT.
//
// WHAT THIS EXISTS TO CATCH. `archive-transcript-list.js` shipped at
// 9d190df emitting a complete BEM tree under `.archive-tlist`, and
// `grep -rn 'archive-tlist' client/css/` returned ZERO matches across
// all 30 stylesheets. The list - the archive's primary browse surface -
// rendered entirely in Chrome's user-agent defaults. Measured live at
// 1440x900 before client/css/archive-tlist.css existed:
//
//   .archive-tlist__row    padding 0px, background rgba(0,0,0,0)
//   .archive-tlist__open   background rgb(239,239,239), color rgb(0,0,0),
//                          border 2px outset, font-size 13.3333px
//
// A white bevelled button with 13.33px Arial, in every one of the 23
// themes. Nothing errored, no test failed, and every theme token the
// rest of the archive used correctly simply never reached this pane.
//
// THE ASSERTION IS AN ANTIJOIN, NOT A CHECKLIST. The class list is read
// out of the JS at run time, so a class added to the renderer tomorrow
// is covered tomorrow without anyone remembering to add it here. A
// hand-maintained list would have been written on the day the gap
// existed and would have listed nothing.
//
// Run with: node tests/test_archive_tlist_styled.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name - Test description.
 * @param {() => void} fn - Body; throwing marks it failed.
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
 * Read a file under the repo root.
 * @param {...string} parts - Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

// The list was split for the 500-line cap: the pure row renderers and
// the vocabulary live in archive-tlist-row.js, the stateful view in
// archive-transcript-list.js. These assertions are about the FEATURE,
// which now spans both, so both are read. Reading only one would let a
// class name move across the seam and silently pass.
const JS = read('client', 'js', 'archive-tlist-row.js')
    + read('client', 'js', 'archive-transcript-list.js');
// THE FEATURE SPANS TWO STYLESHEETS NOW, so both are read. The pane
// file owns the truncation rules, the resizers and the title/source
// treatments; reading only the first would let a class move across the
// seam and silently pass the antijoin below - the same reasoning that
// already makes this file read BOTH halves of the split renderer.
const CSS = read('client', 'css', 'archive-tlist.css')
    + read('client', 'css', 'archive-panes.css');
const HTML = read('client', 'index.html');

/**
 * Strip /* *\/ comments from CSS.
 *
 * ASSERTING THE ABSENCE OF A STRING OVER RAW SOURCE FINDS IT IN THE
 * COMMENT THAT DOCUMENTS ITS REMOVAL. This exact test failed that way
 * first: the `border-left` absence check matched the ITEM 37 comment
 * inside the very rule block that had correctly removed the border-left.
 * A confident, accurate, entirely wrong FAIL, manufactured inside the
 * verification step. Any assertion about what a rule DECLARES has to
 * read declarations, not prose.
 *
 * @param {string} css - Stylesheet source.
 * @returns {string} The same source with every comment blanked.
 */
function decls(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

const CSS_DECLS = decls(CSS);

/**
 * Every BEM element class the renderer emits, read from the source.
 *
 * The renderer builds them as `ROOT_CLASS + '__name'`, so the modifier
 * names are string literals in the file and can be recovered exactly.
 * @returns {string[]} Sorted unique class names, e.g. 'archive-tlist__ref'.
 */
function emittedClasses() {
    const out = new Set();
    for (const m of JS.matchAll(/ROOT_CLASS\s*\+\s*'(__[a-z0-9-]+)'/g)) {
        out.add('archive-tlist' + m[1]);
    }
    // MODIFIERS ARE LITERALS AND ARE MATCHED GENERICALLY. They are built
    // as a pair in one concatenation (`ROOT_CLASS + '__badge ' +
    // ROOT_CLASS + '__badge--no-project'`) or read off a table, so the
    // modifier half appears as its own string either way. The pattern is
    // `__x--y` rather than a hardcoded `__badge--`, because a hardcoded
    // prefix covers the modifiers that existed the day it was written and
    // silently covers no new family - which is the same
    // written-on-the-day-the-gap-existed failure this whole file exists
    // to avoid.
    for (const m of JS.matchAll(/'(__[a-z0-9]+--[a-z0-9-]+)'/g)) {
        out.add('archive-tlist' + m[1]);
    }
    return [...out].sort();
}

const EMITTED = emittedClasses();

// ---- POSITIVE CONTROL --------------------------------------------------
// An antijoin over an EMPTY left side passes trivially and proves
// nothing. This is the same discipline the archive's own outcome tests
// use: prove the extractor can produce a non-empty answer before
// trusting the answer it produces.

test('POSITIVE CONTROL: the class extractor finds classes to check', () => {
    assert.ok(EMITTED.length >= 10,
        `only ${EMITTED.length} classes extracted from archive-transcript-list.js; ` +
        'the extractor is broken and every assertion below is vacuous');
    for (const required of ['archive-tlist__row', 'archive-tlist__open',
                            'archive-tlist__ref', 'archive-tlist__rows']) {
        assert.ok(EMITTED.includes(required),
            `the extractor missed ${required}, which the renderer definitely emits`);
    }
});

test('POSITIVE CONTROL: the stylesheet is non-trivial', () => {
    assert.ok(CSS.length > 2000,
        'archive-tlist.css is too small to be styling anything');
    assert.ok(/\.archive-tlist__open\s*\{/.test(CSS),
        'the stylesheet does not style the card at all');
});

// ---- 1. NO EMITTED CLASS IS UNSTYLED -----------------------------------

test('every class the renderer emits has at least one rule', () => {
    const unstyled = EMITTED.filter(
        (cls) => !new RegExp(`\\.${cls}(?![a-z0-9_-])`).test(CSS));
    assert.deepEqual(unstyled, [],
        'these classes reach the DOM with no rule anywhere, so they render ' +
        'in user-agent defaults: ' + unstyled.join(', '));
});

// ---- 2. THE STYLESHEET IS ACTUALLY LOADED ------------------------------
// A file on disk that no <link> references is a silent no-op - it has
// tests, it passes them, and the browser never parses a byte of it.

test('index.html loads archive-tlist.css, and loads it before the align layer', () => {
    const tlist = HTML.indexOf('/static/css/archive-tlist.css');
    const align = HTML.indexOf('/static/css/archive-align.css');
    const reader = HTML.indexOf('/static/css/archive-reader.css');
    assert.ok(tlist > -1, 'archive-tlist.css has no <link> tag');
    assert.ok(align > -1, 'archive-align.css has no <link> tag');
    assert.ok(align > reader,
        'the alignment layer must load AFTER archive-reader.css - ordering is ' +
        'the entire mechanism by which it applies, since every selector in it ' +
        'is deliberately no more specific than the rule it aligns');
});

// ---- 3. THE FIVE FIELDS ARE SEPARATED ----------------------------------
// Measured before this stylesheet, one row's visible text was:
//   ea67e1c2-...-2fcc5564d9f2transcript 210398 lines12.1 KiB2026-08-30 12:03:31
// Five spans, no separators. "transcript 210398 lines" is transcript 2103
// with 98 lines, and there is no way to see where one field ends.

test('the name takes its own line and the metadata fields carry delimiters', () => {
    assert.match(CSS, /\.archive-tlist__ref\s*\{[^}]*flex:\s*0 0 100%/,
        'the session_ref does not claim a full line, so it runs into the fields after it');
    for (const cls of ['lines', 'bytes', 'ingested']) {
        assert.ok(
            new RegExp(`\\.archive-tlist__${cls}::before`).test(CSS),
            `.archive-tlist__${cls} has no ::before delimiter, so it abuts its neighbour`);
    }
    assert.match(CSS, /content:\s*"\\00b7"/,
        'the delimiter is not a middot; whitespace alone is what made the ' +
        'fields unreadable in the first place');
    // __id is deliberately FIRST on the metadata line, so it must NOT
    // carry a leading delimiter dangling off the left edge.
    assert.ok(!/\.archive-tlist__id::before/.test(CSS),
        'the first metadata field carries a leading delimiter pointing at nothing');
});

// ---- 4. THE STATE CONTRACT WITH THE JS OWNER ---------------------------
// The archive JS emits `data-selected="true"` on a keyboard-selected row
// and `aria-pressed="true"` on an active filter. The styling half is
// written ahead of the DOM carrying it, so the styling is never the
// thing that is missing when it lands.

test('data-selected and aria-pressed are styled', () => {
    assert.ok(/\[data-selected="true"\]/.test(CSS),
        'a keyboard-selected row has no styling, so keyboard navigation is invisible');
    assert.ok(/\.archive-tlist__scheme\[aria-pressed="true"\]/.test(CSS),
        'the active scheme filter has no styling, so which filter is on is invisible');
});

test('selection is distinguishable from hover WITHOUT relying on colour', () => {
    // Both can be true at once and they mean different things: where the
    // pointer is, versus where the keyboard cursor is. terminal, gameboy
    // and legacy_apple deliberately zero every radius token and gameboy
    // is two shades of green, so the difference has to be geometry.
    const hover = /\.archive-tlist__open:hover\s*\{([^}]*)\}/.exec(CSS_DECLS);
    const sel = /\[data-selected="true"\][^{]*\{([^}]*)\}/.exec(CSS_DECLS);
    assert.ok(hover && sel, 'one of the two states has no rule block at all');
    const railOf = (block) => {
        const m = /inset\s+(\d+)px\s+0/.exec(block[1]);
        return m ? Number(m[1]) : null;
    };
    assert.equal(railOf(hover), 3, 'the hover rail is not the 3px app rail');
    assert.equal(railOf(sel), 6,
        'selection does not widen the accent rail, so in a zero-radius, ' +
        'two-colour theme it is indistinguishable from hover');
});

test('the active filter states itself in TEXT, not only in a fill', () => {
    assert.match(CSS, /\.archive-tlist__scheme::before\s*\{\s*content:\s*"\[ \] "/,
        'the inactive filter carries no text marker');
    assert.match(CSS,
        /\.archive-tlist__scheme\[aria-pressed="true"\]::before\s*\{\s*content:\s*"\[x\] "/,
        'the active filter carries no text marker, so which one is on depends ' +
        'entirely on a colour some themes cannot express');
});

// ---- 5. THE APP'S VALUES, NOT NEW ONES ---------------------------------

test('the card reuses .project-item geometry rather than inventing more', () => {
    const styles = read('client', 'css', 'styles.css');
    const projectItem = /\.project-item\s*\{([^}]*)\}/.exec(styles);
    assert.ok(projectItem, 'fixture drift: .project-item no longer parses');
    const card = /\.archive-tlist__open\s*\{([\s\S]*?)\n\}/.exec(CSS_DECLS);
    assert.ok(card, '.archive-tlist__open has no rule block');
    // Radius, transition and the accent-rail MECHANISM must match. The
    // rail is an inset shadow and never a border-left: styles.css records
    // that a mismatched border shorthand is mitered along the corner arc
    // under a radius, which is the two-tone corner smear (ITEM 37).
    assert.match(card[1], /border-radius:\s*var\(--radius-md\)/,
        'the card does not use the app default radius token');
    assert.match(card[1], /transition:\s*all 0\.3s ease/,
        'the card does not use the app transition');
    assert.match(card[1], /box-shadow:\s*inset 3px 0 0 var\(--color-accent\)/,
        'the accent rail is not the app inset-shadow rail');
    assert.ok(!/border-left/.test(card[1]),
        'the card reintroduces the ITEM 37 border-left rail, which smears ' +
        'at the corners under any non-zero border-radius');
});

test('archive-align.css removes the border-left rail from .archive-row--turn', () => {
    const align = decls(read('client', 'css', 'archive-align.css'));
    const turn = /\.archive-row--turn\s*\{([^}]*)\}/.exec(align);
    assert.ok(turn, 'the alignment layer does not address .archive-row--turn');
    assert.match(turn[1], /border-left:\s*0/,
        'the ITEM 37 border-left rail is not cleared');
    assert.match(turn[1], /box-shadow:\s*inset 3px 0 0/,
        'the rail was removed without being replaced, so the turn marker is gone');
});

// ---- 6. THE TYPE SCALE IS THE APP'S -----------------------------------
// Measured before: twelve distinct sizes across the archive, eight of
// them between 11.2px and 13.8px - differences nobody can see, all cost
// and no signal.

test('the stylesheet uses only sizes that already exist in styles.css', () => {
    const allowed = new Set(['0.7rem', '0.8rem', '0.85rem', '0.9rem', '1rem', '0.7em']);
    const used = new Set(
        [...CSS_DECLS.matchAll(/font-size:\s*([0-9.]+r?em)\s*;/g)].map((m) => m[1]));
    const strays = [...used].filter((v) => !allowed.has(v));
    assert.deepEqual(strays, [],
        'these sizes are not on the app scale: ' + strays.join(', '));
    assert.ok(used.size > 0, 'no font sizes found at all; the regex is broken');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
