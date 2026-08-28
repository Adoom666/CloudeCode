// Shared harness for the sidebar-sessions suite (feat/sidebar-sessions).
//
// WHY THIS EXISTS AND NOT JSDOM: same reason tests/mini-dom.mjs and
// tests/lib-home-mechanics.mjs exist - this repo has no package.json and
// a standing "no new runtime dependency without justification" rule, so
// the established pattern is a stub sized to the modules under test.
// mini-dom.mjs is scoped to the dismiss-guard / header-menu surface and
// has no `dataset` and no `getBoundingClientRect`; the sidebar reorder
// path needs both, so this is that stub.
//
// It is NOT a browser and computes NO layout. Real pixels for this
// feature - row heights per density, the docked offset on the home
// screen, and the keyboard reorder driven by REAL key events at a REALLY
// focused row - are measured by scripts/verify_sidebar_sessions.py in a
// real headless Chromium at 1280x900.
//
// Not a test file: the suites are `tests/*.node.mjs`, this is `.mjs`.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.join(__dirname, '..');

const counts = { passes: 0, failures: 0 };

/**
 * Description: tally of results so far, for the suite's summary line.
 * Inputs: none.
 * Output: {passes: number, failures: number}.
 */
export function results() { return counts; }

/**
 * Description: run one named assertion block, recording pass/fail rather
 *   than throwing, so one bad assertion does not hide the rest.
 * Inputs: name (string), fn (function|async function).
 * Output: Promise<void>.
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
 * Description: read one repo file as text.
 * Inputs: parts (...string) - path segments below the repo root.
 * Output: string.
 */
export function repoFile(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

/**
 * Minimal element: enough tree, attributes, dataset, classList, focus and
 * a settable box for the sidebar modules. Everything it does NOT do is
 * done by the Chromium verifier instead.
 */
export class El {
    /**
     * Inputs: tag (string), doc (Doc).
     */
    constructor(tag, doc) {
        this.tagName = String(tag).toUpperCase();
        this.ownerDocument = doc;
        this.parentNode = null;
        this.childNodes = [];
        this._attrs = new Map();
        this._listeners = new Map();
        this._box = { top: 0, height: 20 };
        this.innerHTML = '';
        this._text = '';
        this.style = {};
        const self = this;
        this.dataset = new Proxy({}, {
            get(_, key) {
                return self.getAttribute(`data-${dashed(String(key))}`) ?? undefined;
            },
            set(_, key, value) {
                self.setAttribute(`data-${dashed(String(key))}`, value);
                return true;
            },
        });
        this.classList = {
            add: (c) => self._classes().add(c) && self._writeClasses(),
            remove: (c) => { const s = self._classes(); s.delete(c); self._writeClasses(s); },
            contains: (c) => self._classes().has(c),
        };
    }

    _classes() { return new Set((this.getAttribute('class') || '').split(/\s+/).filter(Boolean)); }

    _writeClasses(set) {
        if (set) this.setAttribute('class', Array.from(set).join(' '));
        return true;
    }

    setAttribute(name, value) { this._attrs.set(name, String(value)); }
    getAttribute(name) { const v = this._attrs.get(name); return v === undefined ? null : v; }
    hasAttribute(name) { return this._attrs.has(name); }
    removeAttribute(name) { this._attrs.delete(name); }

    get id() { return this.getAttribute('id') || ''; }
    set id(v) { this.setAttribute('id', v); }

    /**
     * `className` is the SAME STORE as the `class` attribute, because in a
     * browser it is. Without this it was a plain JS property: a module
     * that built an element with `el.className = 'x'` - which the inline
     * rename editor does for both the input and its wrapper - produced a
     * node that `querySelector('.x')` could never find, and the resulting
     * null read as "the editor refused to open" rather than "the harness
     * cannot see it". Both of the app's ways of setting a class now land
     * in one place, so a selector written against either one works.
     */
    get className() { return this.getAttribute('class') || ''; }
    set className(v) { this.setAttribute('class', v == null ? '' : String(v)); }

    /**
     * textContent must ESCAPE into innerHTML, because that round trip IS
     * the app's HTML escaper (`esc()` in session-sidebar-rows.js sets
     * textContent on a scratch div and reads innerHTML back). A stub that
     * left innerHTML alone would make every escaped value render as the
     * empty string and every injection assertion pass for free.
     */
    get textContent() { return this._text; }

    set textContent(v) {
        this._text = v == null ? '' : String(v);
        this.innerHTML = this._text
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    appendChild(node) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }

    removeChild(node) {
        const i = this.childNodes.indexOf(node);
        if (i !== -1) this.childNodes.splice(i, 1);
        node.parentNode = null;
        return node;
    }

    /**
     * Description: insert a sibling next to this node, the way the inline
     *   rename editor mounts its input beside the row's name span.
     *
     *   ONLY THE POSITIONS THE APP ACTUALLY USES ARE IMPLEMENTED, and an
     *   unimplemented one THROWS rather than returning quietly. A stub
     *   that silently no-opped on a position it did not understand would
     *   let a caller believe it had mounted an element that is not in the
     *   tree, and every later `querySelector` for it would come back null
     *   - which reads exactly like "the editor refused to open" rather
     *   than "the harness dropped it on the floor".
     * Inputs: where (string) - 'afterend' or 'beforebegin'; node (El).
     * Output: El - the inserted node.
     * Raises: Error - any other position.
     */
    insertAdjacentElement(where, node) {
        const parent = this.parentNode;
        if (!parent) throw new Error('insertAdjacentElement on a detached node');
        if (node.parentNode) node.parentNode.removeChild(node);
        const at = parent.childNodes.indexOf(this);
        if (where === 'afterend') parent.childNodes.splice(at + 1, 0, node);
        else if (where === 'beforebegin') parent.childNodes.splice(at, 0, node);
        else throw new Error(`insertAdjacentElement: unsupported position ${where}`);
        node.parentNode = parent;
        return node;
    }

    _walk(out) {
        for (const c of this.childNodes) { out.push(c); c._walk(out); }
        return out;
    }

    /**
     * Description: match one simple selector - `.class`, `[attr]`,
     *   `[attr="v"]`, or a tag. Enough for the selectors these modules
     *   actually use, and deliberately no more.
     * Inputs: sel (string). Output: boolean.
     */
    matches(sel) {
        return sel.split(',').map((s) => s.trim()).some((one) => {
            if (one.startsWith('.')) return this._classes().has(one.slice(1));
            if (one.startsWith('[')) {
                const body = one.slice(1, -1);
                const eq = body.indexOf('=');
                if (eq === -1) return this.hasAttribute(body);
                const n = body.slice(0, eq);
                const v = body.slice(eq + 1).replace(/^["']|["']$/g, '');
                return this.getAttribute(n) === v;
            }
            return this.tagName === one.toUpperCase();
        });
    }

    closest(sel) {
        for (let n = this; n && n.matches; n = n.parentNode) if (n.matches(sel)) return n;
        return null;
    }

    querySelectorAll(sel) { return this._walk([]).filter((e) => e.matches(sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }

    addEventListener(type, fn) {
        if (!this._listeners.has(type)) this._listeners.set(type, []);
        this._listeners.get(type).push(fn);
    }

    focus() { this.ownerDocument.activeElement = this; }

    /**
     * Description: select all of an input's text. RECORDED rather than
     *   ignored, because "the editor opens with the text selected" is a
     *   real behaviour a caller may want to assert - an empty method
     *   would make that assertion impossible to write and equally
     *   impossible to notice was missing.
     * Inputs: none. Output: void.
     */
    select() { this.selectedAll = true; }

    getBoundingClientRect() {
        return { top: this._box.top, height: this._box.height, bottom: this._box.top + this._box.height };
    }

    /**
     * Description: dispatch an event that BUBBLES to the ancestors, the
     *   way a real key press does. The bubbling is the point: the row
     *   handlers are bound to the list container, not to the rows.
     * Inputs: type (string), extra (object).
     * Output: object - the dispatched event.
     */
    dispatchEvent(type, extra = {}) {
        const event = Object.assign({
            type, target: this, defaultPrevented: false, _stopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this._stopped = true; },
        }, extra);
        for (let n = this; n; n = n.parentNode) {
            for (const fn of (n._listeners.get(type) || []).slice()) fn(event);
            if (event._stopped) break;
        }
        return event;
    }
}

/** Minimal document holding a body and an activeElement. */
export class Doc extends El {
    constructor() {
        super('#document', null);
        this.ownerDocument = this;
        this.activeElement = null;
        this.body = this.createElement('body');
        this.appendChild(this.body);
    }

    createElement(tag) { return new El(tag, this); }

    getElementById(id) { return this._walk([]).find((e) => e.getAttribute('id') === id) || null; }
}

/**
 * Description: turn a camelCase dataset key into its data-* attribute
 *   suffix, so `row.dataset.sessionId` reads `data-session-id` exactly as
 *   a browser does.
 * Inputs: key (string). Output: string.
 */
function dashed(key) {
    return key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

/**
 * Description: copy a value out of the vm sandbox's realm into this one.
 *   An array built inside `vm.createContext` has that realm's Array as
 *   its prototype, so `assert.deepEqual` reports "same structure but not
 *   reference-equal" and every ordering assertion fails for a reason that
 *   has nothing to do with the ordering. This is the fix, and it is
 *   applied at the assertion boundary rather than by loosening deepEqual
 *   to a non-strict compare.
 * Inputs: value (any) - anything JSON-representable.
 * Output: any - the same data, in this realm.
 */
export function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

/**
 * Description: the `name` of every row in a list, as a host-realm array.
 * Inputs: rows (Array<object>).
 * Output: Array<string>.
 */
export function names(rows) {
    return plain(Array.from(rows).map((r) => r.name));
}

/**
 * Description: a localStorage stand-in, optionally one that throws on
 *   every access so the storage-denied branch is exercised rather than
 *   assumed.
 * Inputs: seed (object) - initial key/value pairs. throws (boolean).
 * Output: object - a Storage-shaped object plus a `map` for inspection.
 */
export function fakeStorage(seed = {}, throws = false) {
    const map = new Map(Object.entries(seed));
    return {
        map,
        getItem(k) { if (throws) throw new Error('denied'); return map.has(k) ? map.get(k) : null; },
        setItem(k, v) { if (throws) throw new Error('denied'); map.set(k, String(v)); },
        removeItem(k) { map.delete(k); },
    };
}

/**
 * Description: load a set of client modules into one vm sandbox with a
 *   document, a localStorage and a console, and hand back the sandbox's
 *   window. The modules run as-is - no shimming of the code under test.
 * Inputs: names (Array<string>) - client/js file names, in load order.
 *   opts (object) - {storage (object), document (Doc)}.
 * Output: object - {window, document, storage}.
 */
export function loadModules(names, opts = {}) {
    const document = opts.document || new Doc();
    const storage = opts.storage || fakeStorage();
    const context = {
        console: { log() {}, warn() {}, error() {} },
        document,
        localStorage: storage,
        Date,
        JSON,
        Math,
        Set,
        Map,
        Array,
        Object,
        String,
        Number,
        Boolean,
        Promise,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
    };
    context.window = context;
    vm.createContext(context);
    for (const name of names) {
        const src = fs.readFileSync(path.join(ROOT, 'client', 'js', name), 'utf8');
        vm.runInContext(src, context, { filename: name });
    }
    return { window: context, document, storage };
}
