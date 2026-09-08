// Node tests for the "needs a keypress" row indicator - punchlist 19.
//
// WHAT THE FEATURE IS. A claude parked on its folder-trust dialog is a
// live pane running a real process that has fired no lifecycle hook, so
// the sidebar row and the launchpad card painted a green Connected dot
// and a pid over a session that was waiting for somebody to press a key.
// The server now measures that and ships `startup_gate` at the WRAPPER
// level of SessionInfo; these tests cover what the client does with it.
//
// THE THREE THINGS THAT COULD GO WRONG HERE, AND ALL THREE ARE TESTED:
//
//   1. 'unknown' painted as either measured answer. The whole point of a
//      three-outcome field is that "could not determine" is not "fine"
//      and is not "stuck". It must paint NOTHING, exactly as 'ready'
//      does, but for a different reason - and it must reach that outcome
//      without ever being coerced into 'ready'.
//   2. The badge emitted but never spliced into a row. Every assertion
//      in a file like this reads source text, so a builder that returns
//      perfect markup nobody calls would pass a naive suite. Both render
//      sites are checked for the splice, not just for the call.
//   3. The badge going stale. Both the sidebar list and the launchpad
//      running-sessions list skip the innerHTML rewrite when their row
//      signature is unchanged. This badge appears and disappears with NO
//      other field on the row changing - a session parked on its trust
//      prompt has the same name, label, status and ownership before and
//      after somebody answers it - so a signature that does not
//      fingerprint it would leave "needs a keypress" on screen forever.
//      That is the same trap the theme field and the wrapper pill both
//      hit, and it is why the last two tests exist.
//
// Run with: node tests/test_session_startup_gate.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
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
 * Evaluate client/js/session-startup-gate.js in a bare sandbox.
 * @returns {object} The module's exported window.SessionStartupGate.
 */
function loadGate() {
    const sandbox = { console, window: {} };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(
            path.join(ROOT, 'client', 'js', 'session-startup-gate.js'), 'utf8'),
        sandbox,
    );
    return sandbox.window.SessionStartupGate;
}

const gate = loadGate();

// ---------------------------------------------------------------------
// The normalizer. Three values in, three values out, never a fourth.
// ---------------------------------------------------------------------

test('the three server values pass through unchanged', () => {
    assert.equal(gate.normalize('ready'), 'ready');
    assert.equal(gate.normalize('awaiting_startup_prompt'),
        'awaiting_startup_prompt');
    assert.equal(gate.normalize('unknown'), 'unknown');
});

test('anything the server did not send normalizes to unknown', () => {
    for (const bad of [undefined, null, '', 'READY', 'awaiting', 0, {}, []]) {
        assert.equal(gate.normalize(bad), 'unknown',
            `${JSON.stringify(bad)} must not be read as a measured answer`);
    }
});

test('an older payload with no field degrades to painting nothing', () => {
    // The realistic shape: a browser tab loaded against a server that
    // predates this field, or a cached response.
    const row = { name: 'cloude_a', status: 'idle' };
    assert.equal(gate.indicatorHtml(row.startup_gate), '');
});

// ---------------------------------------------------------------------
// The indicator. Only a MEASURED block paints.
// ---------------------------------------------------------------------

test('a blocked session renders the badge', () => {
    const html = gate.indicatorHtml('awaiting_startup_prompt');
    assert.match(html, /class="session-startup-gate"/,
        'the badge must carry the class its stylesheet targets');
    assert.match(html, /needs a keypress/,
        'the badge must say what the user has to do');
});

test('the badge is never color-alone', () => {
    const html = gate.indicatorHtml('awaiting_startup_prompt');
    assert.match(html, /role="img"/,
        'it is a meaningful glyph, not decoration');
    assert.match(html, /title="[^"]+"/,
        'a desktop hover must explain it');
    assert.match(html, /aria-label="[^"]+"/,
        'a screen reader must get the same sentence');
    // The accessible name has to be the SENTENCE, not the two-word
    // badge - the badge alone does not say which session or why.
    const label = html.match(/aria-label="([^"]+)"/)[1];
    assert.ok(label.length > 'needs a keypress'.length,
        'the accessible name must be the full explanation, not the badge text');
});

test('ready paints nothing', () => {
    assert.equal(gate.indicatorHtml('ready'), '');
});

test('unknown paints nothing, and is not routed through ready', () => {
    assert.equal(gate.indicatorHtml('unknown'), '',
        'a row nobody could measure must not be badged');
    // The distinction that matters: both paint nothing, but they are not
    // the same value, and a caller asking which one it is gets a
    // truthful answer rather than a collapsed one.
    assert.equal(gate.normalize('unknown'), 'unknown');
    assert.notEqual(gate.normalize('unknown'), 'ready');
});

test('isAwaiting is strict, not truthy', () => {
    // `!!row.startup_gate` would be true for BOTH other values, which is
    // exactly the bug this predicate exists to make impossible.
    assert.equal(gate.isAwaiting('awaiting_startup_prompt'), true);
    assert.equal(gate.isAwaiting('ready'), false);
    assert.equal(gate.isAwaiting('unknown'), false);
    assert.equal(gate.isAwaiting(undefined), false);
});

test('labelFor says nothing when nothing should be said', () => {
    assert.equal(gate.labelFor('awaiting_startup_prompt'), 'needs a keypress');
    assert.equal(gate.labelFor('ready'), null);
    assert.equal(gate.labelFor('unknown'), null);
});

test('the copy is lowercase and plain, with no dashes or emoji', () => {
    for (const s of [gate.LABEL, gate.REASON]) {
        assert.equal(s, s.toLowerCase(), `${s} must be lowercase`);
        // Escaped, not literal: this repo forbids these characters in its own
        // source, so the test that guards the rule must not carry them.
        assert.ok(!/[\u2013\u2014]/.test(s), `${s} must carry no en/em dash`);
        assert.ok(!/[\u{1F300}-\u{1FAFF}]/u.test(s), `${s} must carry no emoji`);
    }
});

test('attribute values are escaped', () => {
    // Nothing user-controlled reaches this builder today - the label and
    // reason are frozen constants - but the escaper is what stops a
    // future copy edit from reintroducing raw interpolation into an
    // attribute, which this repo has shipped once already.
    assert.equal(gate.escapeAttr('a"b\'c<d>&e'),
        'a&quot;b&#39;c&lt;d&gt;&amp;e');
});

// ---------------------------------------------------------------------
// The row indicator, at both render sites. A builder nobody calls is a
// feature that does not exist.
// ---------------------------------------------------------------------

test('the sidebar row emits the badge', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-sidebar-rows.js'), 'utf8');
    assert.match(src,
        /window\.SessionStartupGate\s*\?\s*window\.SessionStartupGate\.indicatorHtml\(/,
        'the sidebar row must ask the shared module for the badge, guarded '
        + 'so a load-order slip degrades to no badge rather than a crash');
    const body = src.slice(src.indexOf('function rowHtml'));
    assert.match(body.slice(0, body.indexOf('\n    }')), /startupGate \+/,
        'building the badge and never splicing it into the row is a feature '
        + 'that renders nothing while passing every call-site assertion');
});

test('the launchpad card emits the badge', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    assert.match(src,
        /window\.SessionStartupGate\s*\?\s*window\.SessionStartupGate\.indicatorHtml\(/,
        'the launchpad card must use the same shared module, or the two '
        + 'surfaces will drift on when the badge appears');
    assert.match(src, /\$\{startupGate\}/,
        'the launchpad must splice the badge into the card markup');
});

test('both merges carry the field off the wire', () => {
    for (const file of ['session-sidebar-fetch.js', 'launchpad.js']) {
        const src = fs.readFileSync(
            path.join(ROOT, 'client', 'js', file), 'utf8');
        assert.match(src, /startup_gate/,
            `${file} builds its rows field by field, so a field it does not `
            + 'copy simply does not exist on the row it hands the renderer');
    }
});

test('the field is read at the WRAPPER level, never inside .session', () => {
    // The single most repeated bug in this project (CLAUDE.md): reading
    // `info.session.startup_gate` returns undefined silently and looks
    // exactly like the backend not sending it.
    for (const file of ['session-sidebar-fetch.js', 'launchpad.js']) {
        const src = fs.readFileSync(
            path.join(ROOT, 'client', 'js', file), 'utf8');
        assert.ok(!/\.session\.startup_gate/.test(src),
            `${file} must read startup_gate off the SessionInfo wrapper`);
    }
});

test('the stylesheet is actually loaded', () => {
    const html = fs.readFileSync(
        path.join(ROOT, 'client', 'index.html'), 'utf8');
    assert.match(html, /session-startup-gate\.css/,
        'an unlinked stylesheet renders an unstyled badge');
    assert.match(html, /session-startup-gate\.js/,
        'an unloaded module means window.SessionStartupGate is undefined and '
        + 'every guarded call site silently paints nothing');
    // Load order: the module has to be defined before the two files that
    // call it, both of which are script tags further down the same file.
    const gateAt = html.indexOf('session-startup-gate.js');
    const rowsAt = html.indexOf('session-sidebar-rows.js"');
    const padAt = html.indexOf('launchpad.js"');
    assert.ok(gateAt > 0 && rowsAt > gateAt,
        'session-startup-gate.js must load before session-sidebar-rows.js');
    assert.ok(padAt > gateAt,
        'session-startup-gate.js must load before launchpad.js');
});

// ---------------------------------------------------------------------
// Staleness. The badge changes with nothing else on the row changing.
// ---------------------------------------------------------------------

test('answering the prompt repaints the sidebar list', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-sidebar-rows.js'), 'utf8');
    const sig = src.slice(src.indexOf('function signature'));
    assert.match(sig.slice(0, sig.indexOf('\n    }')), /startup:/,
        'the sidebar skips the DOM rewrite when its row signature is '
        + 'unchanged, and no OTHER field on the row changes when a trust '
        + 'prompt is answered - so a signature without this leaves "needs a '
        + 'keypress" on screen after the session has started');
});

test('answering the prompt repaints the launchpad card', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    assert.match(src, /startup: window\.SessionStartupGate/,
        'the running-sessions list has the same signature shortcut and the '
        + 'same staleness trap as the sidebar');
});

test('the toast kind is registered as blocking', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'toast.js'), 'utf8');
    assert.match(src, /StartupPrompt: 3/,
        'a startup prompt is blocking and the user cannot see it by glancing '
        + 'at the terminal, so it must be HIGH - which is also what makes it '
        + 'cap-exempt and unable to hide behind "+3 more"');
    assert.match(src, /StartupPrompt: \(t\) =>/,
        'it must declare a coalesce key, or two of them would stack');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
