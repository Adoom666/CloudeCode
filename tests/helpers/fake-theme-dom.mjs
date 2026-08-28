// Shared test double for the theme-effects DOM, plus the .mjs source mirror.
//
// WHY THIS IS A HELPER AND NOT COPY-PASTE. Two suites drive the effect
// modules: test_theme_effects.node.mjs (lifecycle, teardown, the third
// outcome) and test_theme_effects_visibility.node.mjs (does the effect
// deposit enough ink to be seen). Both need the same hand-rolled DOM and the
// same .mjs mirror. Duplicating them would let the two copies drift, and a
// drifting test double is how tests/helpers/fake-canvas-2d.mjs earned its own
// test file - see that header.
//
// The DOM stub is deliberately hand-rolled rather than jsdom: the point is to
// COUNT scheduled callbacks and registered listeners, which a real DOM hides.

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createFakeCtx } from './fake-canvas-2d.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..', '..');

/** Absolute path to the bundled themes directory. */
export const themesDir = path.join(repoRoot, 'client', 'css', 'themes');

// ---------------------------------------------------------------------
// Minimal DOM stub. Deliberately hand-rolled rather than jsdom: the point
// is to COUNT scheduled callbacks and registered listeners, which a real
// DOM hides.
// ---------------------------------------------------------------------

/**
 * Build a fresh fake environment and install it on globalThis.
 * @param {{reducedMotion?: boolean, hidden?: boolean, contextFails?: boolean,
 *   width?: number}} [opts] Environment knobs
 * @returns {object} Handles for inspecting scheduled work and listeners
 */
export function installEnv(opts = {}) {
    const rafPending = new Map();
    let rafSeq = 0;
    const winListeners = [];
    const docListeners = [];
    const mqlListeners = [];
    const body = { children: [] };

    // The 2D context double lives in tests/helpers/fake-canvas-2d.mjs. It
    // implements the full 2D surface with real save/restore and transform
    // semantics, so an effect is never forced to avoid a method the double
    // happens to lack. See that file's header for what it approximates and
    // what it refuses to answer.

    const doc = {
        hidden: !!opts.hidden,
        documentElement: { dataset: {} },
        body,
        createElement(tag) {
            const el = {
                tagName: String(tag).toUpperCase(),
                style: {},
                width: 0,
                height: 0,
                parentNode: null,
                _ctx: null,
                setAttribute(k, v) { this[k] = v; },
                getContext() {
                    if (opts.contextFails) return null;
                    if (!this._ctx) this._ctx = createFakeCtx(this);
                    return this._ctx;
                },
            };
            return el;
        },
        addEventListener(type, fn) { docListeners.push({ type, fn }); },
        removeEventListener(type, fn) {
            const i = docListeners.findIndex((l) => l.type === type && l.fn === fn);
            if (i >= 0) docListeners.splice(i, 1);
        },
    };
    body.appendChild = (el) => { el.parentNode = body; body.children.push(el); return el; };
    body.removeChild = (el) => {
        const i = body.children.indexOf(el);
        if (i >= 0) body.children.splice(i, 1);
        el.parentNode = null;
        return el;
    };

    const mql = {
        matches: !!opts.reducedMotion,
        addEventListener(type, fn) { mqlListeners.push({ type, fn }); },
        removeEventListener(type, fn) {
            const i = mqlListeners.findIndex((l) => l.type === type && l.fn === fn);
            if (i >= 0) mqlListeners.splice(i, 1);
        },
    };

    const win = {
        innerWidth: opts.width || 1440,
        innerHeight: 900,
        devicePixelRatio: 2,
        matchMedia: () => mql,
        addEventListener(type, fn) { winListeners.push({ type, fn }); },
        removeEventListener(type, fn) {
            const i = winListeners.findIndex((l) => l.type === type && l.fn === fn);
            if (i >= 0) winListeners.splice(i, 1);
        },
    };

    globalThis.window = win;
    globalThis.document = doc;
    globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
    globalThis.requestAnimationFrame = (fn) => {
        rafSeq += 1;
        rafPending.set(rafSeq, fn);
        return rafSeq;
    };
    globalThis.cancelAnimationFrame = (id) => { rafPending.delete(id); };

    return {
        rafPending, winListeners, docListeners, mqlListeners, body, doc, win, mql,
        /**
         * Run every currently-pending rAF callback once at timestamp `now`.
         * @param {number} now Timestamp handed to the callbacks
         * @returns {void}
         */
        flush(now) {
            const due = [...rafPending.entries()];
            for (const [id, fn] of due) {
                rafPending.delete(id);
                fn(now);
            }
        },
        /**
         * The canvas the effect mounted, if any.
         * @returns {object|undefined} The canvas stub
         */
        canvas() { return body.children[0]; },
    };
}


/** Theme ids that ship an effects module, discovered from disk. */
export const effectThemes = fs.readdirSync(themesDir)
    .filter((d) => fs.existsSync(path.join(themesDir, d, 'theme.json')))
    .filter((d) => {
        const m = JSON.parse(fs.readFileSync(path.join(themesDir, d, 'theme.json'), 'utf8'));
        return !!m.effects;
    })
    .sort();

// The client is served as plain static files and the browser loads these as ES
// modules, but node resolves a bare `.js` as CommonJS without a package.json
// declaring otherwise. Rather than adding one to every theme directory purely
// for the tests, mirror the sources into a temp tree as `.mjs` with the one
// relative specifier rewritten. The mirror is byte-identical apart from that
// extension, so nothing about the shipped files is special-cased for tests.
const mirrorDir = fs.mkdtempSync(path.join(os.tmpdir(), 'theme-effects-test-'));
process.on('exit', () => {
    try { fs.rmSync(mirrorDir, { recursive: true, force: true }); } catch (_) {}
});

/**
 * Copy one source file into the mirror tree, rewriting the harness import.
 * @param {string} relDir Directory under client/css/themes
 * @param {string} name Source file name, e.g. "effects.js"
 * @returns {string} Absolute path to the mirrored .mjs file
 */
function mirror(relDir, name) {
    const src = fs.readFileSync(path.join(themesDir, relDir, name), 'utf8');
    const outDir = path.join(mirrorDir, relDir);
    fs.mkdirSync(outDir, { recursive: true });
    const out = path.join(outDir, name.replace(/\.js$/, '.mjs'));
    fs.writeFileSync(out, src.replace(
        '../_shared/effects-base.js',
        '../_shared/effects-base.mjs',
    ));
    return out;
}

mirror('_shared', 'effects-base.js');

/**
 * Import a theme's effects module fresh, defeating the ESM cache so each test
 * gets its own module-level state.
 * @param {string} themeId Theme directory name
 * @returns {Promise<object>} The module namespace
 */
export function loadEffect(themeId) {
    const file = mirror(themeId, 'effects.js');
    const url = pathToFileURL(file).href + '?v=' + Math.random();
    return import(url);
}



/**
 * Import the shared harness module (effects-base) from the mirror tree.
 * Callers need it to build ad-hoc effects for harness-level assertions
 * without knowing where the mirror lives.
 * @returns {Promise<object>} The effects-base module namespace
 */
export function loadHarness() {
    return import(pathToFileURL(path.join(mirrorDir, '_shared', 'effects-base.mjs')).href);
}
