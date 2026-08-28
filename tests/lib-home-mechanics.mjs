// Shared harness for the home-screen mechanics suite.
//
// WHY THIS EXISTS AND NOT JSDOM: same reason tests/mini-dom.mjs exists -
// this repo has no package.json and a standing "no new runtime dependency
// without justification" rule, so the established pattern is a stub sized
// to the module under test. mini-dom.mjs is scoped to the dismiss-guard /
// header-menu / terminal-tools surface; the launchpad render path needs a
// different one (an element tree with `style`, plus a vm sandbox wired
// with the globals launchpad.js reaches for), so this is that stub.
//
// It is NOT a browser and computes NO layout. Real pixels for this
// feature are measured by scripts/verify_home_mechanics.py in a real
// headless Chromium.
//
// Not a test file: the suites are `tests/*.node.mjs`, this is `.mjs`.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.join(__dirname, '..');
export const LAUNCHPAD_SRC = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
export const STYLES = fs.readFileSync(path.join(ROOT, 'client', 'css', 'styles.css'), 'utf8');
export const INDEX = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');

const counts = { passes: 0, failures: 0 };

/**
 * Tally of results so far, for the suite's own summary line.
 * @returns {{passes: number, failures: number}}
 */
export function results() { return counts; }

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => (void|Promise<void>)} fn  Body; throwing marks it failed.
 * @returns {Promise<void>}
 */
export async function test(name, fn) {
    try {
        await fn();
        counts.passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        counts.failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Extract the body of the FIRST rule with this exact selector.
 * @param {string} css  Stylesheet text.
 * @param {string} selector  Exact selector text, e.g. '.project-item'.
 * @returns {string} The declarations between the braces.
 */
export function ruleBody(css, selector) {
    const idx = css.indexOf(`\n${selector} {`);
    assert.ok(idx !== -1, `expected a rule for ${selector}`);
    const open = css.indexOf('{', idx);
    const close = css.indexOf('}', open);
    return css.slice(open + 1, close);
}

/**
 * A stub element good enough for the render + handler paths under test.
 * `children` participates in closest()/querySelectorAll() so the fold
 * handler's real traversal is exercised rather than mocked away.
 * @param {string} tag  Class list as a space-separated string.
 * @param {object} [opts]  {id, attrs, children}
 * @returns {object} Stub element.
 */
export function el(tag, opts = {}) {
    const classes = tag.split(/\s+/).filter(Boolean);
    const node = {
        id: opts.id || '',
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: opts.dataset || {},
        parent: null,
        children: opts.children || [],
        _attrs: Object.assign({}, opts.attrs),
        classes,
        setAttribute(n, v) { this._attrs[n] = String(v); },
        getAttribute(n) {
            return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
        },
        hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); },
        addEventListener() {},
        appendChild(child) { this.children.push(child); child.parent = this; return child; },
        removeChild(child) { this.children = this.children.filter((c) => c !== child); },
        scrollIntoView() {},
        classList: {
            add: (c) => { if (!classes.includes(c)) classes.push(c); },
            remove: (c) => { const i = classes.indexOf(c); if (i >= 0) classes.splice(i, 1); },
            toggle() {},
            contains: (c) => classes.includes(c),
        },
        closest(sel) {
            const want = sel.replace(/^\./, '');
            let cur = this;
            while (cur) {
                if (cur.classes && cur.classes.includes(want)) return cur;
                cur = cur.parent;
            }
            return null;
        },
        querySelectorAll(sel) {
            const want = sel.replace(/^:scope\s*>\s*/, '').replace(/^\./, '');
            const out = [];
            const walk = (n) => {
                (n.children || []).forEach((c) => {
                    if (c.classes && c.classes.includes(want)) out.push(c);
                    walk(c);
                });
            };
            walk(this);
            return out;
        },
        querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    };
    node.children.forEach((c) => { c.parent = node; });
    return node;
}

/**
 * Load launchpad.js in a vm sandbox and return the Launchpad instance
 * plus the fake document, so a test can drive real methods.
 * @param {object} [docOverrides]  Extra ids for getElementById.
 * @returns {{lp: object, doc: object, body: object, win: object}} `win`
 *   is the sandbox's own window, which is where launchpad.js reads
 *   window.API from - stubbing a global here would reach nothing.
 */
export function loadLaunchpad(docOverrides = {}) {
    const body = el('body');
    const byId = Object.assign({}, docOverrides);
    const doc = {
        body,
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        removeEventListener() {},
        createElement() { return el('created'); },
    };
    const win = {
        API: {},
        SessionStatusUI: {
            dotHtml() { return '<span class="status-dot"></span>'; },
            pencilIconSvg() { return '<svg class="pencil"></svg>'; },
            trashIconSvg() { return '<svg class="trash"></svg>'; },
        },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) { this.type = type; this.detail = opts && opts.detail; },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    win.window = win;
    const ctx = {
        window: win,
        document: doc,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: win.localStorage,
        requestAnimationFrame: win.requestAnimationFrame,
        CustomEvent: win.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(ctx);
    vm.runInContext(LAUNCHPAD_SRC, ctx, { filename: 'launchpad.js' });
    return { lp: ctx.window.Launchpad, doc, body, win: ctx.window };
}

/**
 * Render one project list and hand back the container's markup.
 * @param {object} fixture {projects, presence, runningSessions, attribution}
 * @returns {{html: string, lp: object, projectList: object}}
 */
export function renderProjects(fixture) {
    const projectList = el('project-list', { id: 'project-list' });
    const { lp } = loadLaunchpad({ 'project-list': projectList });
    lp.projects = fixture.projects;
    lp.projectPresence = new Map((fixture.presence || []).map((r) => [r.raw_path, r]));
    lp.runningSessions = fixture.runningSessions || [];
    lp.sessionAttribution = new Map((fixture.attribution || []).map((r) => [r.tmux_name, r]));
    lp.sessionAttributionListingOk = true;
    lp.projectAuthority = { mode: 'db', degraded: false, disagreement: null };
    lp.renderProjectList();
    return { html: projectList.innerHTML, lp, projectList };
}

