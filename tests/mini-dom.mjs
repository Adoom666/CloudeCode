// Minimal DOM for the vm-sandboxed client-JS tests.
//
// WHY THIS EXISTS AND NOT JSDOM: this repo has no package.json and a
// standing "no new runtime dependency without justification" rule. The
// established pattern (tests/test_session_row_actions.node.mjs,
// tests/test_deeplink_resolver.node.mjs) is a hand-rolled stub sized to
// the module under test. Two modules now need a real element tree with
// event bubbling, so the stub is shared rather than copied twice.
//
// SCOPE: exactly the DOM surface client/js/dismiss-guard.js,
// client/js/header-menu.js and client/js/terminal-tools-menu.js touch -
// tree mutation (including ChildNode.remove()), classList, attributes
// (id and title reflect, as they do in a real browser),
// closest/contains/matches, and click/keydown/pointerdown dispatch that
// bubbles to document. It is NOT a browser. Anything beyond that surface should be
// verified in a real browser instead, which is where the visual pass on
// this feature happened.
//
// Not a test file: the suites are `tests/*.node.mjs`, this is `.mjs`.

/**
 * One compound selector, e.g. `a[href]` or `[tabindex]:not([tabindex="-1"])`.
 * Supported: tag, #id, .class, [attr], [attr="value"], :not(<simple>).
 */
class Selector {
    /**
     * Inputs: text (string) - a single compound selector, no combinators.
     */
    constructor(text) {
        this.tag = null;
        this.id = null;
        this.classes = [];
        this.attrs = [];      // [{name, value|null}]
        this.nots = [];       // [Selector]
        this._parse(text.trim());
    }

    _parse(text) {
        // Pull :not(...) out first so its inner brackets are not mistaken
        // for the host selector's own attribute clauses.
        text = text.replace(/:not\(([^)]*)\)/g, (_, inner) => {
            this.nots.push(new Selector(inner));
            return '';
        });
        const token = /([#.]?[A-Za-z0-9_-]+)|(\[[^\]]*\])/g;
        let m;
        while ((m = token.exec(text)) !== null) {
            const raw = m[0];
            if (raw[0] === '#') this.id = raw.slice(1);
            else if (raw[0] === '.') this.classes.push(raw.slice(1));
            else if (raw[0] === '[') {
                const body = raw.slice(1, -1);
                const eq = body.indexOf('=');
                if (eq === -1) this.attrs.push({ name: body, value: null });
                else {
                    this.attrs.push({
                        name: body.slice(0, eq),
                        value: body.slice(eq + 1).replace(/^["']|["']$/g, ''),
                    });
                }
            } else this.tag = raw.toLowerCase();
        }
    }

    /**
     * Inputs: el (MiniElement).
     * Output: boolean.
     */
    matches(el) {
        if (this.tag && el.tagName.toLowerCase() !== this.tag) return false;
        if (this.id && el.getAttribute('id') !== this.id) return false;
        for (const c of this.classes) if (!el.classList.contains(c)) return false;
        for (const a of this.attrs) {
            const got = el.getAttribute(a.name);
            if (got === null || got === undefined) return false;
            if (a.value !== null && String(got) !== a.value) return false;
        }
        for (const n of this.nots) if (n.matches(el)) return false;
        return true;
    }
}

/**
 * A descendant chain, e.g. `.header .controls`: the last compound must
 * match the element, and each earlier compound must match some ancestor,
 * in order. Only the descendant combinator (whitespace) is supported -
 * `>`, `+` and `~` are not used by the modules under test.
 */
class ChainSelector {
    /** Inputs: text (string) - one selector, possibly with descendants. */
    constructor(text) {
        this.parts = String(text).trim().split(/\s+/)
            .filter(Boolean)
            .map(p => new Selector(p));
    }

    /** Inputs: el (MiniElement). Output: boolean. */
    matches(el) {
        if (this.parts.length === 0) return false;
        const last = this.parts[this.parts.length - 1];
        if (!last.matches(el)) return false;
        let i = this.parts.length - 2;
        let node = el.parentNode;
        while (i >= 0 && node) {
            if (this.parts[i].matches(node)) i--;
            node = node.parentNode;
        }
        return i < 0;
    }
}

/** Parse a comma-separated selector list into descendant chains. */
function parseSelectorList(text) {
    return String(text).split(',')
        .map(s => s.trim())
        .filter(Boolean)
        .map(s => new ChainSelector(s));
}

/** classList shim backed by the element's class attribute. */
class MiniTokenList {
    constructor(el) { this.el = el; }
    _tokens() {
        const v = this.el._attrs.get('class') || '';
        return v.split(/\s+/).filter(Boolean);
    }
    _write(tokens) { this.el._attrs.set('class', tokens.join(' ')); }
    contains(t) { return this._tokens().includes(t); }
    add(...ts) {
        const cur = this._tokens();
        for (const t of ts) if (!cur.includes(t)) cur.push(t);
        this._write(cur);
    }
    remove(...ts) { this._write(this._tokens().filter(t => !ts.includes(t))); }
    toggle(t, force) {
        const has = this.contains(t);
        const want = force === undefined ? !has : !!force;
        want ? this.add(t) : this.remove(t);
        return want;
    }
}

class MiniElement {
    /**
     * Inputs: tagName (string), doc (MiniDocument).
     */
    constructor(tagName, doc) {
        this.tagName = tagName.toUpperCase();
        this.ownerDocument = doc;
        this.parentNode = null;
        this.childNodes = [];
        this._attrs = new Map();
        this._listeners = new Map();
        this.classList = new MiniTokenList(this);
        this.innerHTML = '';
        this.style = {};
    }

    // ---- attributes ----
    setAttribute(name, value) { this._attrs.set(name, String(value)); }
    getAttribute(name) {
        const v = this._attrs.get(name);
        return v === undefined ? null : v;
    }
    removeAttribute(name) { this._attrs.delete(name); }
    hasAttribute(name) { return this._attrs.has(name); }

    get id() { return this.getAttribute('id') || ''; }
    set id(v) { this.setAttribute('id', v); }
    get className() { return this.getAttribute('class') || ''; }
    set className(v) { this.setAttribute('class', v); }
    get type() { return this.getAttribute('type') || ''; }
    set type(v) { this.setAttribute('type', v); }
    get hidden() { return this.hasAttribute('hidden'); }
    set hidden(v) { v ? this.setAttribute('hidden', '') : this.removeAttribute('hidden'); }
    get title() { return this.getAttribute('title') || ''; }
    set title(v) { this.setAttribute('title', v); }
    get id() { return this.getAttribute('id') || ''; }
    set id(v) { this.setAttribute('id', v); }

    // ---- tree ----
    appendChild(node) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }

    insertBefore(node, ref) {
        if (!ref) return this.appendChild(node);
        if (node.parentNode) node.parentNode.removeChild(node);
        const i = this.childNodes.indexOf(ref);
        node.parentNode = this;
        this.childNodes.splice(i === -1 ? this.childNodes.length : i, 0, node);
        return node;
    }

    removeChild(node) {
        const i = this.childNodes.indexOf(node);
        if (i !== -1) this.childNodes.splice(i, 1);
        node.parentNode = null;
        return node;
    }

    /** ChildNode.remove(). Detach self from the parent; a no-op when
     *  already detached, exactly like the real thing. */
    remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
        return this;
    }

    get children() { return this.childNodes.slice(); }

    contains(node) {
        for (let n = node; n; n = n.parentNode) if (n === this) return true;
        return false;
    }

    matches(selectorText) {
        return parseSelectorList(selectorText).some(s => s.matches(this));
    }

    closest(selectorText) {
        const list = parseSelectorList(selectorText);
        for (let n = this; n && n.tagName; n = n.parentNode) {
            if (list.some(s => s.matches(n))) return n;
        }
        return null;
    }

    _walk(out) {
        for (const c of this.childNodes) { out.push(c); c._walk(out); }
        return out;
    }

    querySelectorAll(selectorText) {
        const list = parseSelectorList(selectorText);
        return this._walk([]).filter(el => list.some(s => s.matches(el)));
    }

    querySelector(selectorText) {
        return this.querySelectorAll(selectorText)[0] || null;
    }

    // ---- events ----
    addEventListener(type, fn) {
        if (!this._listeners.has(type)) this._listeners.set(type, []);
        this._listeners.get(type).push(fn);
    }

    removeEventListener(type, fn) {
        const arr = this._listeners.get(type) || [];
        const i = arr.indexOf(fn);
        if (i !== -1) arr.splice(i, 1);
    }

    _fire(type, event) {
        for (const fn of (this._listeners.get(type) || []).slice()) fn(event);
    }

    focus() { this.ownerDocument.activeElement = this; }

    /**
     * Description: dispatch an event that bubbles from this node up
     *   through its ancestors to the document, the way a real click does.
     *   The bubbling is the entire point: the focus bug under test only
     *   reproduces because a click on a child reaches an ancestor's
     *   listener.
     * Inputs: type (string), extra (object) - merged onto the event.
     * Output: object - the dispatched event.
     */
    dispatchEvent(type, extra = {}) {
        const event = Object.assign({
            type,
            target: this,
            defaultPrevented: false,
            _stopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this._stopped = true; },
        }, extra);
        // The document is the root of the parent chain for any attached
        // node, so walking to null covers document-level listeners too.
        // Firing it separately would double-deliver to them.
        for (let n = this; n; n = n.parentNode) {
            n._fire(type, event);
            if (event._stopped) return event;
        }
        return event;
    }
}

class MiniDocument extends MiniElement {
    constructor() {
        super('#document', null);
        this.ownerDocument = this;
        this.readyState = 'complete';
        this.activeElement = null;
        this.documentElement = this.createElement('html');
        this.body = this.createElement('body');
        this.documentElement.appendChild(this.body);
        this.appendChild(this.documentElement);
    }

    createElement(tag) { return new MiniElement(tag, this); }

    getElementById(id) {
        return this._walk([]).find(el => el.getAttribute('id') === id) || null;
    }
}

/**
 * Description: build a window/document pair good enough to run the
 *   header-menu and dismiss-guard modules, with a controllable
 *   matchMedia so a test can flip between phone and desktop width
 *   without a real viewport.
 * Inputs: options (object) - {matches: boolean} initial media state.
 * Output: {window, document, setMediaMatches(boolean)}.
 */
export function createEnvironment(options = {}) {
    const document = new MiniDocument();
    const mql = {
        matches: !!options.matches,
        _handlers: [],
        addEventListener(type, fn) { if (type === 'change') this._handlers.push(fn); },
        removeEventListener(type, fn) {
            const i = this._handlers.indexOf(fn);
            if (i !== -1) this._handlers.splice(i, 1);
        },
    };
    const window = {
        document,
        matchMedia() { return mql; },
        console,
        addEventListener() {},
    };
    window.window = window;

    return {
        window,
        document,
        mql,
        /** Flip the media query and notify listeners, like a resize. */
        setMediaMatches(v) {
            mql.matches = !!v;
            for (const fn of mql._handlers.slice()) fn(mql);
        },
    };
}

export { MiniElement, MiniDocument, Selector };
