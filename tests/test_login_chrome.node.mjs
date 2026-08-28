// fix/login-chrome - the MECHANISM behind the login screen's clean chrome.
//
// THIS FILE DELIBERATELY PROVES NOTHING ABOUT PIXELS. It cannot: there is
// no layout engine here, and an assertion that an element carries a class
// is exactly the kind of evidence that let three separate visibly broken
// features ship green in this repo. The pixel verdict - does the slash
// button occupy a 45x45 box on the login screen or not - lives in
// scripts/verify_login_chrome.py, which measures
// getBoundingClientRect()/getComputedStyle() in a real Chromium and
// exits 0/1/2.
//
// What IS worth pinning here is the policy, because the policy is what
// makes the next control safe:
//
//   1. THE DEFAULT IS HIDDEN. `body.is-authenticated` is written only for
//      the authenticated screens. An unknown screen name, or no call at
//      all, leaves it off. Fail-closed, not fail-open.
//   2. THE CSS GATES BY CONTAINER, NOT BY NAME. The rule that hides the
//      header's right-hand cluster addresses `.controls > *`, so a
//      control added to that row tomorrow is covered with no code
//      written. If someone rewrites it as a list of ids, this fails.
//   3. app.js NO LONGER CARRIES A PER-BUTTON HIDE LIST in showAuth().
//      That list existing is the defect; its absence is the fix.
//   4. THE RUNTIME-INJECTED FLOATING CONTROLS CARRY THE OPT-IN TOKEN.
//      They have no shared ancestor to gate on, so `data-auth-only` on
//      the element is the declaration.
//
// Run with: node tests/test_login_chrome.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT = path.join(__dirname, '..', 'client');

let failures = 0;
let passes = 0;

/**
 * Description: run one named assertion block, recording pass/fail.
 * Inputs: name (string), fn (function).
 * Output: void.
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
 * Description: read one file under client/.
 * Inputs: rel (string) - path relative to client/.
 * Output: string file contents.
 */
function client(rel) {
    return fs.readFileSync(path.join(CLIENT, rel), 'utf8');
}

/**
 * Description: evaluate client/js/screen-chrome.js against a minimal
 *   stand-in for <body>, and return the module plus that stand-in.
 *   Deliberately hand-rolled rather than mini-dom: the only DOM surface
 *   the module touches is dataset + classList, and a fake that
 *   implements exactly those two makes it obvious what is being pinned.
 * Inputs: none.
 * Output: {ScreenChrome, body}.
 */
function loadScreenChrome() {
    const classes = new Set();
    const body = {
        dataset: {},
        classList: {
            toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
            contains(name) { return classes.has(name); }
        }
    };
    const sandbox = {
        window: {},
        document: { body },
        console: { log() {} }
    };
    sandbox.window.document = sandbox.document;
    vm.createContext(sandbox);
    vm.runInContext(client('js/screen-chrome.js'), sandbox);
    return { ScreenChrome: sandbox.window.ScreenChrome, body };
}

// ---------------------------------------------------------------- policy

test('the authenticated marker is ABSENT by default - fail closed', () => {
    const { body } = loadScreenChrome();
    assert.equal(body.classList.contains('is-authenticated'), false,
        'merely loading the module must not assert authentication');
});

test('only launchpad and terminal are authenticated screens', () => {
    const { ScreenChrome } = loadScreenChrome();
    // Compared as a joined string, not deepEqual: the module is
    // evaluated in a vm realm, so its Array is a different intrinsic and
    // a strict structural compare fails on identity, not on content.
    assert.equal(
        Array.prototype.slice.call(ScreenChrome.AUTHENTICATED_SCREENS)
            .sort().join(','),
        'launchpad,terminal');
});

test('apply() marks the authenticated screens and clears auth', () => {
    const { ScreenChrome, body } = loadScreenChrome();
    ScreenChrome.apply('launchpad');
    assert.equal(body.classList.contains('is-authenticated'), true);
    assert.equal(body.dataset.screen, 'launchpad');

    ScreenChrome.apply('terminal');
    assert.equal(body.classList.contains('is-authenticated'), true);

    ScreenChrome.apply('auth');
    assert.equal(body.classList.contains('is-authenticated'), false,
        'the login screen must clear the marker, not merely not set it');
    assert.equal(body.dataset.screen, 'auth');
});

test('an UNKNOWN screen name is treated as unauthenticated', () => {
    const { ScreenChrome, body } = loadScreenChrome();
    ScreenChrome.apply('launchpad');
    for (const junk of ['', 'setup', undefined, null, 'AUTH']) {
        ScreenChrome.apply(junk);
        assert.equal(body.classList.contains('is-authenticated'), false,
            `apply(${JSON.stringify(junk)}) must not assert authentication`);
    }
});

// ------------------------------------------------------------------- css

test('the header gate addresses a CONTAINER, never a list of ids', () => {
    const css = client('css/screen-chrome.css');
    assert.match(
        css,
        /body:not\(\.is-authenticated\)\s+\.header\s+\.controls\s*>\s*\*:not\(\[data-show-on-auth\]\)\s*\{[^}]*display:\s*none\s*!important/,
        'the whole point is that a NEW control in .controls is covered ' +
        'with no code written. A selector listing ids would restore the ' +
        'exact failure mode this branch removed.'
    );
});

test('the floating-chrome gate is an opt-in token, and is !important', () => {
    const css = client('css/screen-chrome.css');
    assert.match(
        css,
        /body:not\(\.is-authenticated\)\s+\[data-auth-only\]\s*\{[^}]*display:\s*none\s*!important/,
        '#slash-commands-btn is shown by an inline style.display, so a ' +
        'non-important rule would lose to it - and that button is the ' +
        'one the user actually reported.'
    );
});

test('the gate stylesheet is linked from index.html', () => {
    assert.match(client('index.html'),
        /<link rel="stylesheet" href="\/static\/css\/screen-chrome\.css"/,
        'an unlinked stylesheet gates nothing');
});

// -------------------------------------------------------------- app wiring

test('showAuth() no longer carries a per-button hide list', () => {
    const app = client('js/app.js');
    const start = app.indexOf('    showAuth() {');
    assert.ok(start > 0, 'showAuth() not found');
    // Comment lines are stripped first: this method's comment EXPLAINS
    // the deleted hide list by quoting it, and a naive scan would match
    // the explanation and call the defect present.
    const body = app.slice(start, app.indexOf('\n    }', start))
        .split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n');
    assert.doesNotMatch(body, /classList\.add\('hidden'\)/,
        'naming the controls to hide, one line each, IS the defect. ' +
        'Every control not on that list rendered on the login screen.');
    assert.match(body, /ScreenChrome\.apply\('auth'\)/);
});

test('every screen transition stamps the marker', () => {
    const app = client('js/app.js');
    for (const screen of ['auth', 'launchpad', 'terminal']) {
        assert.ok(app.includes(`ScreenChrome.apply('${screen}')`),
            `no ScreenChrome.apply('${screen}') in app.js`);
    }
    // Every currentScreen assignment must have a matching stamp, or a
    // screen can be entered without the gate being re-evaluated.
    const assigns = (app.match(/this\.currentScreen = '/g) || []).length;
    const stamps = (app.match(/ScreenChrome\.apply\('/g) || []).length;
    assert.equal(stamps, assigns,
        `${assigns} screen assignments but ${stamps} marker stamps`);
});

test('screen-chrome.js loads before app.js', () => {
    const html = client('index.html');
    const gate = html.indexOf('/static/js/screen-chrome.js');
    const app = html.indexOf('/static/js/app.js');
    assert.ok(gate > 0 && app > 0, 'both script tags must exist');
    assert.ok(gate < app, 'app.js calls ScreenChrome.apply() during boot');
});

// ------------------------------------------------- runtime-injected chrome

test('the body-level floating controls declare data-auth-only', () => {
    assert.match(client('js/slash-commands.js'),
        /setAttribute\('data-auth-only'/,
        'the slash button is appended to <body>, so it has no ancestor ' +
        'to gate on - the token on the element is the declaration');
    assert.match(client('js/dpad.js'),
        /setAttribute\('data-auth-only'/);
    assert.match(client('index.html'),
        /id="session-sidebar-toggle"[\s\S]{0,120}data-auth-only/);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
