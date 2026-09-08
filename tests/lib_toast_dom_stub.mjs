// Shared DOM stub for the client/js/toast.js node suites.
// ----------------------------------------------------------------------
// Two suites drive the shipped toast module head-on -
// test_toast_stacking.node.mjs (coalesce / cap / tier) and
// test_toast_dismiss.node.mjs (dismiss-on-input and dismiss-all). They
// need the identical fake browser, so it lives here once rather than
// being copy-pasted and drifting: a stub that disagrees between two
// suites means one of them is asserting against a browser the other
// does not have.
//
// WHAT THIS IS NOT. There is no layout, no stylesheet and no compositor
// here, so a card present in this tree could still be painting zero
// pixels. Pixels are measured in scripts/verify_toast_stacking.py and
// scripts/verify_toast_dismiss.py against a real Chromium.
//
// The stub THROWS on a selector shape it does not understand rather
// than returning no match: a stub that silently fails to match
// manufactures a passing test out of its own gap.

import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), '..');
const SRC = fs.readFileSync(path.join(ROOT, 'client/js/toast.js'), 'utf8');

// ---------------------------------------------------------------- stub DOM

/**
 * Description: the DOM surface client/js/toast.js actually touches, and
 *   nothing else - createElement, textContent, dataset, classList, the
 *   attribute reflection the module reads back, tree order via
 *   insertBefore/firstChild/nextSibling, and attribute-value
 *   querySelector. Deliberately small; anything past this surface is a
 *   browser question and belongs in the Playwright verifier.
 * Inputs: tag (string).
 */
export class El {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.parentNode = null;
        this.childNodes = [];
        this._attrs = new Map();
        this._text = '';
        this._listeners = new Map();
        this.dataset = new Proxy({}, {
            set: (t, k, v) => {
                t[k] = String(v);
                this._attrs.set(dashed(k), String(v));
                return true;
            },
            get: (t, k) => t[k],
        });
        this.style = { setProperty: (k, v) => { this._css = this._css || {}; this._css[k] = v; } };
        const self = this;
        this.classList = {
            add(...c) { c.forEach((x) => self._classes().add(x)); self._syncClass(); },
            remove(...c) { c.forEach((x) => self._classes().delete(x)); self._syncClass(); },
            contains(c) { return self._classes().has(c); },
        };
    }
    _classes() {
        if (!this.__cls) this.__cls = new Set(
            (this._attrs.get('class') || '').split(/\s+/).filter(Boolean));
        return this.__cls;
    }
    _syncClass() { this._attrs.set('class', [...this._classes()].join(' ')); }
    get className() { return this._attrs.get('class') || ''; }
    set className(v) { this.__cls = new Set(String(v).split(/\s+/).filter(Boolean)); this._syncClass(); }
    setAttribute(n, v) { this._attrs.set(n, String(v)); }
    getAttribute(n) { const v = this._attrs.get(n); return v === undefined ? null : v; }
    set type(v) { this.setAttribute('type', v); }
    get type() { return this.getAttribute('type') || ''; }

    get textContent() {
        if (this.childNodes.length === 0) return this._text;
        return this.childNodes.map((c) => c.textContent).join('');
    }
    set textContent(v) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; this._text = String(v); }

    appendChild(n) {
        if (n.parentNode) n.parentNode.removeChild(n);
        n.parentNode = this; this._text = ''; this.childNodes.push(n); return n;
    }
    insertBefore(n, ref) {
        if (!ref) return this.appendChild(n);
        if (n.parentNode) n.parentNode.removeChild(n);
        const i = this.childNodes.indexOf(ref);
        n.parentNode = this; this._text = '';
        this.childNodes.splice(i === -1 ? this.childNodes.length : i, 0, n);
        return n;
    }
    removeChild(n) {
        const i = this.childNodes.indexOf(n);
        if (i !== -1) this.childNodes.splice(i, 1);
        n.parentNode = null; return n;
    }
    remove() { if (this.parentNode) this.parentNode.removeChild(this); }
    get firstChild() { return this.childNodes[0] || null; }
    get nextSibling() {
        if (!this.parentNode) return null;
        const s = this.parentNode.childNodes;
        return s[s.indexOf(this) + 1] || null;
    }
    _all(out) { for (const c of this.childNodes) { out.push(c); c._all(out); } return out; }
    _matches(sel) {
        // Supports `.class`, `.class[attr="value"]` and a trailing
        // `:not(.class)` - the three shapes the module builds selectors
        // in. Anything else THROWS rather than silently not matching: a
        // stub that quietly returns false for a selector it does not
        // understand manufactures a passing test out of its own gap.
        const not = /:not\(\.([A-Za-z0-9_-]+)\)$/.exec(sel);
        if (not) {
            if (this._classes().has(not[1])) return false;
            sel = sel.slice(0, not.index);
        }
        const m = /^\.([A-Za-z0-9_-]+)(?:\[([a-z-]+)="((?:[^"\\]|\\.)*)"\])?$/.exec(sel);
        if (!m) throw new Error(`stub selector unsupported: ${sel}`);
        if (!this._classes().has(m[1])) return false;
        if (!m[2]) return true;
        return this.getAttribute(m[2]) === m[3].replace(/\\(.)/g, '$1');
    }
    querySelectorAll(sel) { return this._all([]).filter((e) => e._matches(sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    addEventListener(t, fn) {
        if (!this._listeners.has(t)) this._listeners.set(t, []);
        this._listeners.get(t).push(fn);
    }
    click() { for (const fn of this._listeners.get('click') || []) fn({}); }
}

/** Description: dataset key -> attribute name. Inputs/Output: string. */
function dashed(k) { return 'data-' + String(k).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }

/**
 * Description: build a window/document with one #toast-container and a
 *   controllable narrow-viewport media query.
 * Inputs: narrow (boolean) - does (max-width: 640px) match.
 * Output: {sandbox, container, mql, acked} - acked is every toast id the
 *   module sent to the server, in order.
 */
export function makeEnv(narrow = false) {
    const container = new El('div');
    container.setAttribute('id', 'toast-container');
    const mql = { matches: !!narrow, _h: [], addEventListener(t, fn) { if (t === 'change') this._h.push(fn); } };
    const acked = [];
    const document = {
        createElement: (t) => new El(t),
        getElementById: (id) => (id === 'toast-container' ? container : null),
    };
    const window = {
        document,
        matchMedia: () => mql,
        API: { ackToast: (id) => { acked.push(id); return Promise.resolve(); } },
        requestAnimationFrame: (fn) => { fn(); return 1; },
    };
    window.window = window;
    const sandbox = {
        window, document, console,
        requestAnimationFrame: window.requestAnimationFrame,
        setTimeout, clearTimeout, Promise, CSS: undefined,
        // terminal.js guards every send with `readyState !== WebSocket.OPEN`,
        // so the constant has to exist in this context or its real methods
        // throw before reaching anything worth asserting.
        WebSocket: { OPEN: 1 },
        TextEncoder,
    };
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox, { filename: 'toast.js' });
    return { sandbox, container, mql, acked, mgr: window.ToastManager };
}

let seq = 0;
/**
 * Description: a server-shape toast.
 * Inputs: kind (string), title (string), body (string|null),
 *   session (string).
 * Output: object.
 */
export function toast(kind, title, body = null, session = 's1') {
    seq += 1;
    return { id: `t${seq}`, session_id: session, kind, title, body, color: '#ff8800', acknowledged: false };
}

/** Description: the cards actually in the container, top to bottom. */
export function cards(container) {
    return container.childNodes.filter((e) => e._classes().has('toast'));
}
/** Description: the overflow row, or null. */
export function overflow(container) {
    return container.childNodes.find((e) => e._classes().has('toast-overflow')) || null;
}


/**
 * Description: the "Dismiss all" control, or null when absent.
 * Inputs: container (El). Output: El|null.
 */
export function dismissAllRow(container) {
    return container.childNodes.find((e) => e._classes().has('toast-dismiss-all')) || null;
}

/**
 * Description: flush the 220ms fade-out timers `dismiss()` schedules, so
 *   an assertion reads the settled tree rather than the mid-animation
 *   one. Inputs: None. Output: Promise.
 */
export function settle() {
    return new Promise((resolve) => setTimeout(resolve, 300));
}

const TERM_SRC = fs.readFileSync(path.join(ROOT, 'client/js/terminal.js'), 'utf8');
const TERM_SINGLETON = 'window.TerminalController = new Terminal();';

/**
 * Description: the SHIPPED client/js/terminal.js class, without its
 *   trailing singleton construction. That one line builds a live xterm
 *   against a pty and cannot run outside a browser; everything above it
 *   - including every method under test - is the real file, unmodified.
 *   Loading it this way rather than re-implementing the method is the
 *   point: a re-implementation would pass while the shipped code broke.
 * Inputs: sandbox (object) - a context from makeEnv().sandbox, so the
 *   class sees the same `window` (and therefore the same ToastManager)
 *   the toast module was loaded into.
 * Output: the Terminal class.
 */
export function loadTerminalClass(sandbox) {
    const cut = TERM_SRC.indexOf(TERM_SINGLETON);
    if (cut === -1) {
        throw new Error(
            'terminal.js no longer ends with its singleton line, so this '
            + 'loader is slicing something it does not understand - refusing '
            + 'to guess');
    }
    vm.runInContext(
        TERM_SRC.slice(0, cut) + '\nwindow.__TerminalClass = Terminal;',
        sandbox, { filename: 'terminal.js' });
    return sandbox.window.__TerminalClass;
}

/**
 * Description: a `this` carrying exactly what the methods under test
 *   read - the attached session and an open websocket - plus a recorder
 *   for the bytes that would have gone to the pty, so nothing this suite
 *   does can reach a real session.
 * Inputs: Klass (class) from loadTerminalClass; sessionId (string|null).
 * Output: object.
 */
export function fakeTerminal(Klass, sessionId) {
    const P = Klass.prototype;
    const self = {
        _currentSession: sessionId ? { id: sessionId } : null,
        sent: [],
        ws: { readyState: 1, send: (b) => { self.sent.push(b); } },
    };
    for (const name of ['_unwrapSession', '_sessionId', '_noteUserInputToSession',
        'sendKeyToTerminal', 'insertText', '_writeSynthetic']) {
        if (typeof P[name] !== 'function') {
            throw new Error(`terminal.js has no ${name}() any more`);
        }
        self[name] = P[name];
    }
    return self;
}
