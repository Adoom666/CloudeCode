// Node-based tests for client/js/session-status-key.js - the foldable key
// to the status lights at the foot of the session sidebar.
//
// WHY THIS FILE EXISTS. Since the five-colour pass the LED is the ONLY
// thing on a row saying what a session is doing, and its meaning lives in
// a `title` nobody hovers on a phone. The key is where that vocabulary is
// written down in words, so it has two properties worth guarding and they
// are both about DRIFT:
//
//   1. IT DRAWS REAL LEDS. Every swatch is StatusLed.ledHtml with the same
//      (inner, outer) pair the rows resolve to. A hand-drawn legend is a
//      second implementation of the component, free to show a colour the
//      app does not paint - and this repo has already paid for two
//      stylesheets drawing one dot.
//   2. IT COVERS EVERY LIGHT. A colour added to status-led.css and not to
//      the key is a light on screen that the legend claims does not exist,
//      which is worse than having no legend.
//
// THAT SECOND PROPERTY IS ABOUT LIGHTS, NOT STATES, since 2026-09-09. The
// key used to carry one row per inner state and now carries one per
// colour, at the owner's request, because two rows showing the same
// yellow send a reader hunting for a difference the light cannot show
// them. So coverage is checked by resolving each state's `--led-ink`
// through the STYLESHEET to the value it really paints - the two yellows
// and the two reds each count once, and a genuinely new hue still fails.
// The states that lost their own row did not lose their distinction: it
// moved entirely into the dot's `title` and `aria-label`, which is why
// those are now asserted here too.
//
// Run with: node tests/test_status_key.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;
const queue = [];

/** Queue one named assertion block. Inputs: name, fn. Output: void. */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Run every queued test in order. Inputs: none. Output: Promise<void>. */
async function runQueue() {
    for (const [name, fn] of queue) {
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
}

/** Read one repo file as text. Inputs: ...parts (string). Output: string. */
function repoFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
}

/**
 * A localStorage stand-in that records what it was asked to keep.
 *
 * Inputs: none. Output: object - the Storage-shaped double.
 */
function fakeStorage() {
    const map = new Map();
    return {
        map,
        getItem: (k) => (map.has(k) ? map.get(k) : null),
        setItem: (k, v) => map.set(k, String(v)),
    };
}

/**
 * Load status-led.js and session-status-key.js into ONE sandbox.
 *
 * They share it for the same reason the summary tests do: the key reads
 * the LED builder off the global, and loading them apart would test a
 * legend drawing against a different copy of the component than the rows
 * do - the exact drift this file exists to prevent.
 * Inputs: storage (object|null) - a localStorage double, or null for a
 *   sandbox where localStorage throws (a private window).
 * Output: object - {Led, Key, storage}.
 */
function loadModules(storage) {
    const store = storage === undefined ? fakeStorage() : storage;
    const context = { console };
    if (store) {
        context.localStorage = store;
    } else {
        // A browser that refuses storage THROWS on access rather than
        // returning null, and the key must survive that.
        Object.defineProperty(context, 'localStorage', {
            get() { throw new Error('storage is blocked'); },
        });
    }
    vm.createContext(context);
    for (const file of ['status-led.js', 'session-status-key.js']) {
        vm.runInContext(repoFile('client', 'js', file), context);
    }
    return { Led: context.StatusLed, Key: context.SessionStatusKey, storage: store };
}

/**
 * The smallest element tree onToggleClick needs: a button inside a root
 * that also holds a body. Deliberately hand-rolled rather than a DOM
 * library - the function touches five methods and one property, and a
 * double that implements exactly those cannot pass by accident.
 * Inputs: none. Output: object - {button, body}.
 */
function fakeTree() {
    const body = { hidden: true, className: 'session-status-key__body' };
    const root = {
        querySelector: (sel) => (sel === '.session-status-key__body' ? body : null),
    };
    const attrs = new Map([['aria-expanded', 'false']]);
    const button = {
        closest: (sel) => (sel === '[data-status-key]' ? root : null),
        getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
        setAttribute: (k, v) => attrs.set(k, String(v)),
        attrs,
    };
    return { button, body };
}

const { Led, Key } = loadModules();

const CSS = repoFile('client', 'css', 'status-led.css');

/**
 * The colour VALUE an inner state paints, read out of the stylesheet.
 *
 * Description: resolves the state's `--led-ink` to its `--led-color-*`
 *   token and then that token to its declared value, so two states with
 *   different token names but one hue (`--led-color-permission` and
 *   `--led-color-waiting` both resolve to `var(--color-warning, #fbbf24)`)
 *   compare EQUAL. That is the whole point: the key now carries one row
 *   per light, and "which lights are the same light" is a fact about the
 *   stylesheet, not something this file may hardcode a table of.
 * Inputs: inner (string) - a member of Led.INNER_STATES.
 * Output: string - the declared colour value.
 * Example: hueOf('dead') -> 'var(--color-danger, #ff4444)'
 */
function hueOf(inner) {
    const block = CSS.split(`.status-led[data-inner='${inner}'] {`)[1].split('}')[0];
    const token = block.match(/--led-ink:\s*var\((--led-color-[a-z-]+)\)/)[1];
    return CSS.match(new RegExp(`${token}:\\s*([^;]+);`))[1].trim();
}

// ---- one row per light, and nothing invented ----------------------------

test('THE KEY CARRIES EXACTLY SEVEN ROWS', () => {
    // The owner's 2026-09-09 count, and it is a count of LIGHTS rather
    // than of states: "there should only be one entry per colour". Nine
    // rows showed the same yellow twice and the same red twice, which
    // sends a reader looking up a dot hunting for a difference the light
    // cannot show them. Pinned as a number because the failure mode is a
    // future edit re-expanding a collapsed pair one row at a time.
    assert.equal(Key.ENTRIES.length, 7, 'seven lights, seven rows');
});

test('EVERY LIGHT THE COMPONENT CAN PAINT HAS A ROW OF ITS OWN COLOUR', () => {
    // The anti-drift assertion, restated for a key that no longer has one
    // row per state. A hue added to status-led.css and not to the key is
    // a light on screen the legend says does not exist. Resolved through
    // the stylesheet, so the two yellows and the two reds each count once
    // and a genuinely NEW colour still fails this.
    const covered = new Set(Key.ENTRIES.map((e) => hueOf(e.inner)));
    for (const state of Led.INNER_STATES) {
        assert.ok(
            covered.has(hueOf(state)),
            `the key never explains the colour "${state}" paints`,
        );
    }
});

test('AND NO TWO ROWS DRAW THE SAME LIGHT', () => {
    // The other half of "one entry per colour". Two rows may share a hue
    // ONLY when they differ in shape - a solid green dot beside a green
    // ring, a solid grey dot beside a grey outline - because those are
    // four distinct things on screen. Two rows with the same hue AND the
    // same halo treatment would be the nine-row problem coming back.
    const seen = new Set();
    for (const e of Key.ENTRIES) {
        const light = `${hueOf(e.inner)}|${e.outer}`;
        assert.ok(!seen.has(light), `two rows draw the same light: ${e.text}`);
        seen.add(light);
    }
});

test('THE COLLAPSED PAIRS ARE STILL SEPARATED IN WORDS', () => {
    // COLLAPSING THE ROWS DID NOT MERGE THE STATES. Four states now share
    // two rows, so the legend can no longer tell a user whether a yellow
    // dot is a permission prompt or a startup prompt, or whether a red
    // one is a dead process or a dead socket. The dot's own tooltip and
    // accessible name are the only place left that can, which makes them
    // load-bearing rather than decorative.
    for (const [a, b] of [
        ['waiting-permission', 'waiting-input'],
        ['dead', 'disconnected'],
    ]) {
        assert.equal(hueOf(a), hueOf(b), `${a} and ${b} really are one colour`);
        const la = Led.INNER_LABELS[a];
        const lb = Led.INNER_LABELS[b];
        assert.ok(la && lb, 'both states are labelled');
        assert.notEqual(la, lb, `${a} and ${b} must not share words too`);
    }
    // And the row that stands in for each pair says something true of
    // BOTH members, not just of the one it happens to draw.
    const yellow = Key.ENTRIES.find((e) => e.inner === 'waiting-permission');
    assert.ok(/waiting on you/.test(yellow.text), yellow.text);
    const red = Key.ENTRIES.find((e) => e.inner === 'dead');
    assert.ok(/dead/.test(red.text) && /disconnected/.test(red.text), red.text);
});

test('THE NOT-MEASURED ROW SAYS IT IS NOT IDLE, in so many words', () => {
    // The row the owner had to ask about ("what does this even mean").
    // Grey and grey-outline are one hue told apart by shape alone, so
    // this sentence is the only thing that stops a reader filing the
    // outline under "idle" - which is this project's recurring false-green
    // failure wearing its other face.
    const unknown = Key.ENTRIES.find((e) => e.inner === 'unknown');
    const idle = Key.ENTRIES.find((e) => e.inner === 'done' && e.outer === 'steady');
    assert.ok(unknown && idle, 'both grey rows exist');
    assert.equal(hueOf('unknown'), hueOf('done'), 'they really share a hue');
    assert.ok(/measured/.test(unknown.text), unknown.text);
    assert.ok(/idle/.test(unknown.text), 'it must name the state it is not');
    assert.notEqual(unknown.text, idle.text);
});

test('AND NOTHING IN THE KEY IS INVENTED', () => {
    // The other direction. An entry naming a state the component cannot
    // produce would be a legend for a light nobody will ever see.
    for (const entry of Key.ENTRIES) {
        assert.ok(
            Led.INNER_STATES.indexOf(entry.inner) >= 0,
            `"${entry.inner}" is not an inner state`,
        );
        assert.ok(
            Led.OUTER_STATES.indexOf(entry.outer) >= 0,
            `"${entry.outer}" is not an outer state`,
        );
        assert.ok(entry.text && entry.text.trim(), 'every light gets words');
        assert.equal(entry.text, entry.text.toLowerCase(),
            'UI copy in this app is lowercase');
    }
});

test('THE FINISHED-TURN RING IS IN THERE, because it is the one nobody guesses', () => {
    // Grey dot, green ring. It replaced the unread envelope on both
    // surfaces, so it is the entry the key most needs to carry.
    const ring = Key.ENTRIES.filter((e) => e.outer === 'unread');
    assert.equal(ring.length, 1, 'exactly one entry explains the ring');
    assert.equal(ring[0].inner, 'done', 'and it is the grey centre it really has');
    assert.ok(/read/.test(ring[0].text), `got "${ring[0].text}"`);
});

test('THE KEY EXPLAINS THE GROUP HEADER TOO, in one line', () => {
    // A dot appears in two places. A reader who has just learned the
    // vocabulary must be told the header reuses it rather than left to
    // assume a header dot means something else.
    assert.ok(/group header/.test(Key.HEADER_NOTE), Key.HEADER_NOTE);
    assert.ok(Key.keyHtml().includes(Key.HEADER_NOTE));
});

// ---- the swatches are the shipped component -----------------------------

test('EVERY SWATCH IS A REAL LED, byte for byte', () => {
    // Not "looks like an LED": the exact string ledHtml returns for that
    // pair. A legend that built its own span could show a colour, a size
    // or a shape the app does not paint.
    for (const entry of Key.ENTRIES) {
        const expected = Led.ledHtml({ inner: entry.inner, outer: entry.outer });
        assert.ok(
            Key.itemHtml(entry).includes(expected),
            `the swatch for ${entry.inner}/${entry.outer} is not the component`,
        );
    }
});

test('a swatch keeps the component\'s own words, so colour is never the only signal', () => {
    const html = Key.keyHtml();
    for (const entry of Key.ENTRIES) {
        const label = Led.ledLabel(entry.inner, entry.outer);
        assert.ok(html.includes(`aria-label="${label}"`), `missing label: ${label}`);
    }
});

test('THE KEY STYLESHEET NEVER REPAINTS A LIGHT', () => {
    // The legend sizes the ROW around the light and never the light. A
    // rule here that set a colour, a width or a border-radius on a
    // `.status-led` would make the key show something the sidebar does
    // not, which is the whole failure it exists to avoid.
    const css = repoFile('client', 'css', 'session-status-key.css');
    const rules = css.match(/^[^\n{]*\.status-led[^\n{]*\{/gm) || [];
    assert.deepEqual(rules, [], `the key must not style the LED: ${rules.join(', ')}`);
});

// ---- the disclosure ------------------------------------------------------

test('IT SHIPS COLLAPSED, and says so in ARIA rather than by shape alone', () => {
    const { Key: K } = loadModules();
    const html = K.keyHtml();
    assert.ok(html.includes('aria-expanded="false"'), 'collapsed by default');
    assert.ok(html.includes(`aria-controls="${K.BODY_ID}"`), 'points at its body');
    assert.ok(html.includes(`id="${K.BODY_ID}"`), 'which really exists');
    // The body is HIDDEN, not dropped: `aria-controls` must always
    // resolve to a real element, or the button announces a target that
    // is not there.
    assert.ok(/class="session-status-key__body" id="[^"]+" hidden>/.test(html),
        'the closed body is hidden, not absent');
});

test('IT IS A BUTTON, so Tab reaches it and Enter and Space work for free', () => {
    // A div with a click handler is not keyboard operable, and this app
    // ships no key handler for the key. The element type IS the feature.
    const html = Key.keyHtml();
    assert.ok(/<button type="button" class="session-status-key__toggle"/.test(html));
});

test('the fold PERSISTS, on the app\'s own localStorage convention', () => {
    const { Key: K, storage } = loadModules();
    assert.ok(K.STORAGE_KEY.startsWith('cloude.'),
        `preferences live under cloude.*, got ${K.STORAGE_KEY}`);
    assert.equal(K.isOpen(), false, 'nothing stored means closed');
    K.setOpen(true);
    assert.equal(storage.map.get(K.STORAGE_KEY), '1');
    assert.equal(K.isOpen(), true);
    // AND THE MARKUP READS IT AT RENDER TIME, which is what makes the
    // fold survive the list repainting itself from a signature that
    // knows nothing about it.
    assert.ok(K.keyHtml().includes('aria-expanded="true"'));
    assert.ok(!/session-status-key__body" id="[^"]+" hidden/.test(K.keyHtml()));
    K.setOpen(false);
    assert.equal(storage.map.get(K.STORAGE_KEY), '0');
    assert.ok(K.keyHtml().includes('aria-expanded="false"'));
});

test('A BLOCKED localStorage COSTS THE PREFERENCE, NEVER THE KEY', () => {
    // A private window throws on access rather than returning null. The
    // legend must still render; it simply opens closed every time.
    const { Key: K } = loadModules(null);
    assert.equal(K.isOpen(), false);
    K.setOpen(true);
    assert.ok(K.keyHtml().includes('data-status-key'), 'it still renders');
    assert.ok(K.keyHtml().includes('aria-expanded="false"'));
});

test('the toggle folds IN PLACE and moves every attribute together', () => {
    const { Key: K, storage } = loadModules();
    const { button, body } = fakeTree();
    assert.equal(K.onToggleClick(button), true);
    assert.equal(button.getAttribute('aria-expanded'), 'true');
    assert.equal(body.hidden, false);
    assert.equal(storage.map.get(K.STORAGE_KEY), '1');
    // The tooltip and the accessible name say what the NEXT press does,
    // so they have to move with the state or they start lying.
    assert.equal(button.attrs.get('title'), button.attrs.get('aria-label'));
    assert.ok(/Hide/.test(button.attrs.get('title')), button.attrs.get('title'));

    assert.equal(K.onToggleClick(button), false);
    assert.equal(button.getAttribute('aria-expanded'), 'false');
    assert.equal(body.hidden, true);
    assert.equal(storage.map.get(K.STORAGE_KEY), '0');
    assert.ok(/Show/.test(button.attrs.get('title')), button.attrs.get('title'));
});

test('a toggle with no key around it refuses rather than throwing', () => {
    const orphan = {
        closest: () => null,
        getAttribute: () => 'false',
        setAttribute: () => {},
    };
    assert.equal(Key.onToggleClick(orphan), false);
    assert.equal(Key.onToggleClick(null), false);
});

test('THE SIDEBAR ACTUALLY CLAIMS THE CLICK - a control nobody wires is furniture', () => {
    const clicks = repoFile('client', 'js', 'session-sidebar-clicks.js');
    assert.ok(clicks.includes('data-status-key-toggle'), 'the click is routed');
    assert.ok(clicks.includes('SessionStatusKey.onToggleClick'), 'to this module');
    const html = repoFile('client', 'index.html');
    assert.ok(html.includes('/static/js/session-status-key.js'), 'the module is served');
    assert.ok(html.includes('/static/css/session-status-key.css'), 'and so is its CSS');
});

test('nothing here uses an em-dash, an en-dash, or an emoji', () => {
    for (const f of [['js', 'session-status-key.js'], ['css', 'session-status-key.css']]) {
        const src = repoFile('client', ...f);
        assert.ok(!/[—–]/.test(src), `${f[1]} carries a dash`);
        assert.ok(
            !/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u.test(src),
            `${f[1]} carries an emoji`,
        );
    }
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
