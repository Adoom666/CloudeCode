// THE PANE WIDTHS ARE DRAGGABLE, REMEMBERED, CLAMPED, AND THEY SURVIVE
// A STORE THAT REFUSES TO ANSWER.
//
// WHAT THIS EXISTS TO CATCH. Three separate ways a remembered layout
// takes a screen down, none of which errors:
//   1. `localStorage` THROWS on access in a private window or with site
//      data blocked. It is not merely empty - reaching for it raises.
//      An unguarded read means the archive screen does not render at
//      all, for a preference whose entire value is saving one drag.
//   2. A stored width was written against a DIFFERENT viewport. A list
//      pane of 900px, saved on a 1600px monitor, parks the reader off
//      screen at 1024px. The value is valid, parses fine, and is wrong.
//   3. A drag past the edge collapses a pane to zero width, which is not
//      recoverable by dragging: there is nothing left to grab.
//
// AND THE THIRD OUTCOME. Consulting the store has THREE answers, not
// two - a value was restored, the store answered and held nothing, or
// the store could not be consulted at all. Collapsing the third into the
// second would report "nobody has dragged yet" about a browser that will
// never remember anything, which is the one case worth telling somebody
// about.
//
// Run with: node tests/test_archive_pane_resize.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 *
 * IT AWAITS, for the reason spelled out in test_archive_nav_list.node.mjs:
 * a harness that calls fn() without awaiting records a pass the instant
 * the promise is created, and every assertion inside then runs after the
 * verdict. That is a verification step that cannot fail.
 *
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
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
 * A localStorage stand-in whose behaviour can be chosen per test.
 * @param {object} opts - {throwOnRead, throwOnWrite, seed}.
 * @returns {object} A Storage-shaped object with a `writes` log.
 */
function makeStore(opts = {}) {
    const data = new Map(Object.entries(opts.seed || {}));
    return {
        writes: [],
        getItem(k) {
            if (opts.throwOnRead) throw new Error('SecurityError: storage blocked');
            const v = data.get(k);
            return v === undefined ? null : v;
        },
        setItem(k, v) {
            if (opts.throwOnWrite) throw new Error('QuotaExceededError');
            this.writes.push([k, v]);
            data.set(k, String(v));
        },
        removeItem(k) { data.delete(k); },
        _raw: data,
    };
}

/**
 * Load archive-pane-resize.js into a vm context with a mini DOM.
 * @param {number} gridWidth - What getBoundingClientRect reports, in px.
 * @returns {object} {mod, document, grid, warns}
 */
function load(gridWidth = 1440) {
    const env = createEnvironment();
    const warns = [];
    const fakeWindow = { document: env.document, addEventListener() {}, innerWidth: gridWidth };
    const context = {
        window: fakeWindow,
        document: env.document,
        console: { log() {}, warn(...a) { warns.push(a.join(' ')); }, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-pane-resize.js'), 'utf8'),
        context, { filename: 'archive-pane-resize.js' }
    );
    const grid = env.document.createElement('div');
    grid.getBoundingClientRect = () => ({ width: gridWidth, height: 800, x: 0, y: 0 });
    return { mod: context.window.ArchivePaneResize, document: env.document, grid, warns };
}

// ---- POSITIVE CONTROL --------------------------------------------------
// Every assertion below rests on the module having loaded and on the
// mini DOM being able to carry a custom property. A stub whose `style`
// is a bare object silently drops `setProperty`, and every width
// assertion would then compare `undefined` to `undefined` and pass.

await test('POSITIVE CONTROL: the module loads and the harness can carry a custom property', () => {
    const { mod, grid } = load();
    assert.equal(typeof mod.create, 'function', 'the module did not load');
    grid.style.setProperty('--probe', '7px');
    assert.equal(grid.style.getPropertyValue('--probe'), '7px',
        'the harness cannot store a custom property, so every width ' +
        'assertion in this file would compare undefined to undefined');
});

// ---- 1. PERSIST AND RESTORE -------------------------------------------

await test('a drag is persisted, and a fresh mount restores it', () => {
    const store = makeStore();
    const a = load();
    const first = a.mod.create({ document: a.document, grid: a.grid, storage: store });
    assert.equal(first.storeState(), 'default',
        'an empty store must report `default`, not `restored`');
    assert.equal(first.widths().nav, a.mod.DEFAULTS.nav);

    first.apply({ nav: 300, list: 420 }, true);
    assert.equal(store.writes.length, 1, 'the drag was not persisted');
    assert.equal(store.writes[0][0], a.mod.STORE_KEY);

    // A SECOND, INDEPENDENT MOUNT - the restore path, not a read-back of
    // the object that just wrote. Reading the value back off `first`
    // would prove only that the setter works.
    const b = load();
    const second = b.mod.create({ document: b.document, grid: b.grid, storage: store });
    assert.equal(second.storeState(), 'restored');
    assert.equal(second.widths().nav, 300, 'the nav width was not restored');
    assert.equal(second.widths().list, 420, 'the list width was not restored');
    assert.equal(b.grid.style.getPropertyValue('--archive-nav-w'), '300px',
        'the restored width never reached the grid variable, so the ' +
        'template would still render the default');
    assert.equal(b.grid.style.getPropertyValue('--archive-list-w'), '420px');
});

await test('a THROWING localStorage still renders, at the defaults, and says so', () => {
    const store = makeStore({ throwOnRead: true });
    const { mod, document, grid, warns } = load();
    const panes = mod.create({ document, grid, storage: store });
    assert.equal(panes.storeState(), 'unavailable',
        'a store that throws must be `unavailable`, NOT `default` - ' +
        '"nobody has dragged yet" and "this browser will never remember" ' +
        'are different facts');
    assert.equal(panes.widths().nav, mod.DEFAULTS.nav);
    assert.equal(grid.style.getPropertyValue('--archive-nav-w'),
        mod.DEFAULTS.nav + 'px', 'the screen did not render at all');
    assert.ok(warns.some((w) => w.includes('localStorage could not be read')),
        'the failure was swallowed silently');
});

await test('an EMPTY store renders the defaults and is not confused with a failure', () => {
    const { mod, document, grid } = load();
    const panes = mod.create({ document, grid, storage: makeStore() });
    assert.equal(panes.storeState(), 'default');
    assert.equal(panes.widths().list, mod.DEFAULTS.list);
});

await test('a store that throws on WRITE keeps the layout the person dragged', () => {
    const store = makeStore({ throwOnWrite: true });
    const { mod, document, grid, warns } = load();
    const panes = mod.create({ document, grid, storage: store });
    panes.apply({ nav: 290, list: 380 }, true);
    assert.equal(panes.widths().nav, 290,
        'a failed WRITE reverted the on-screen layout, which the person ' +
        'can see and the store cannot');
    assert.ok(warns.some((w) => w.includes('could not be saved')));
});

await test('a CORRUPT stored value falls back rather than rendering a NaN pane', () => {
    for (const bad of ['not json', '{"nav":"wide","list":400}', '{"nav":null}', '{}']) {
        const store = makeStore({ seed: { 'cloude.archive.panes.v1': bad } });
        const { mod, document, grid } = load();
        const panes = mod.create({ document, grid, storage: store });
        assert.equal(panes.storeState(), 'default', `"${bad}" was accepted`);
        assert.ok(isFinite(panes.widths().nav) && panes.widths().nav > 0,
            `"${bad}" produced a non-finite width`);
    }
});

// ---- 2. THE MINIMUMS ---------------------------------------------------

await test('a pane cannot be dragged below its minimum', () => {
    const { mod, document, grid } = load(1440);
    const panes = mod.create({ document, grid, storage: makeStore() });

    const collapsed = panes.apply({ nav: 0, list: 0 }, false);
    assert.equal(collapsed.nav, mod.MIN.nav,
        'the rail collapsed past its minimum and cannot be grabbed again');
    assert.equal(collapsed.list, mod.MIN.list, 'the list collapsed past its minimum');

    const negative = panes.apply({ nav: -500, list: -500 }, false);
    assert.ok(negative.nav >= mod.MIN.nav && negative.list >= mod.MIN.list,
        'a drag past the left edge produced a negative width');
});

await test('the READER keeps its minimum too, which is the one nobody drags directly', () => {
    // The reader has no handle of its own - it is whatever is left. So
    // the only thing standing between it and zero is this clamp.
    const total = 1000;
    const { mod, document, grid } = load(total);
    const panes = mod.create({ document, grid, storage: makeStore() });
    const greedy = panes.apply({ nav: 900, list: 900 }, false);
    const readerLeft = total - greedy.nav - greedy.list;
    assert.ok(readerLeft >= mod.MIN.reader,
        `the reader was squeezed to ${readerLeft}px, below its ` +
        `${mod.MIN.reader}px minimum`);
});

await test('clamp() is PURE and is the one rule both dragging and restoring use', () => {
    const { mod } = load();
    const out = mod.clamp({ nav: 10, list: 10 }, 1440);
    assert.equal(out.nav, mod.MIN.nav);
    // Not a mutation of its input - a shared rule that mutates would
    // make a restore alter the stored object it was reading.
    const input = { nav: 10, list: 10 };
    mod.clamp(input, 1440);
    assert.equal(input.nav, 10, 'clamp mutated its argument');
});

await test('a width stored on a WIDER viewport is re-clamped, not trusted', () => {
    // The value is valid, parses fine, and is wrong for this window.
    const store = makeStore({
        seed: { 'cloude.archive.panes.v1': '{"nav":600,"list":700}' },
    });
    const { mod, document, grid } = load(1024);
    const panes = mod.create({ document, grid, storage: store });
    assert.equal(panes.storeState(), 'restored');
    const left = 1024 - panes.widths().nav - panes.widths().list;
    assert.ok(left >= mod.MIN.reader,
        `restoring a 1600px layout at 1024px left the reader ${left}px`);
});

// ---- 3. THE HANDLES ----------------------------------------------------

await test('two handles are built, focusable, and announce themselves as separators', () => {
    const { mod, document, grid } = load();
    const panes = mod.create({ document, grid, storage: makeStore() });
    const handles = panes.handles();
    assert.equal(handles.length, 2, 'the archive has two dividers');
    for (const h of handles) {
        assert.equal(h.el.getAttribute('role'), 'separator');
        assert.equal(h.el.getAttribute('tabindex'), '0',
            'a pointer-only divider is unusable by keyboard');
        assert.ok((h.el.getAttribute('aria-label') || '').length > 0);
        // The way back to the default has to be discoverable, and a
        // double-click nobody is told about is not.
        assert.match(h.el.getAttribute('title') || '', /reset/i);
    }
});

await test('the arrow keys resize and Home resets, so there IS a way back', () => {
    const { mod, document, grid } = load();
    const panes = mod.create({ document, grid, storage: makeStore() });
    const navHandle = panes.handles().find((h) => h.key === 'nav').el;

    const before = panes.widths().nav;
    navHandle.dispatchEvent('keydown', { key: 'ArrowRight', shiftKey: false });
    assert.ok(panes.widths().nav > before, 'ArrowRight did not widen the rail');

    navHandle.dispatchEvent('keydown', { key: 'Home', shiftKey: false });
    assert.equal(panes.widths().nav, mod.DEFAULTS.nav,
        'Home did not restore the default width');
});

await test('reset() FORGETS the stored value rather than leaving it to come back', () => {
    const store = makeStore();
    const a = load();
    const panes = a.mod.create({ document: a.document, grid: a.grid, storage: store });
    panes.apply({ nav: 310, list: 400 }, true);
    panes.reset();
    const b = load();
    const fresh = b.mod.create({ document: b.document, grid: b.grid, storage: store });
    assert.equal(fresh.storeState(), 'default',
        'the reset layout came back on the next mount, so reset only ' +
        'changed the screen and not what is remembered');
    assert.equal(fresh.widths().nav, b.mod.DEFAULTS.nav);
});

await test('the narrow breakpoint agrees with the screen module and the stylesheet', () => {
    const { mod } = load();
    const js = fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-screen.js'), 'utf8');
    const css = fs.readFileSync(path.join(ROOT, 'client', 'css', 'archive-screen.css'), 'utf8');
    assert.match(js, new RegExp('NARROW_MAX_PX = ' + mod.NARROW_MAX_PX),
        'the resizer and the screen disagree about where one column starts');
    assert.match(css, new RegExp('max-width:\\s*' + mod.NARROW_MAX_PX + 'px'),
        'the stylesheet collapses at a different width than the JS believes');
    // And the divider is removed from the one-column layout, where it
    // would name a boundary that does not exist.
    assert.match(css.replace(/\/\*[\s\S]*?\*\//g, ''),
        /\.archive-screen__resizer\s*\{\s*display:\s*none/,
        'the resizers are not hidden in the narrow layout');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
