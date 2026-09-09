// Node test for client/js/kebab-icon.js - the vertical three-dot mark
// shared by the app header's overflow menu and every conversation row.
//
// WHY THIS FILE EXISTS. The owner asked for the sidebar row's menu to be
// "like in main sites top right", which is this app's own header kebab.
// Making that literally true meant lifting the glyph out of
// client/js/header-menu.js into its own module - and lifting a glyph is
// exactly the kind of edit that moves a pixel without anyone noticing.
//
// scripts/verify_login_chrome.py measures the header kebab's INK against
// a floor, because the mark shipped once at r=1.5 and the owner reported
// the control as "an empty button". That script needs a live server and
// a login, so it is not something this refactor could re-run. What CAN
// be proven here, cheaply and without a browser, is stronger anyway:
// that the shared builder at the header's size emits the SAME BYTES the
// header used to hold inline. If the bytes are identical the rendering
// is identical, and the ink floor is untouched by construction.
//
// Run with: node tests/test_kebab_icon_shared.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. Inputs: name. Output: string. */
function clientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;

/** Run one named assertion block. Inputs: name, fn. Output: void. */
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
 * The glyph exactly as client/js/header-menu.js held it before the
 * extraction, copied out of git history. This constant is the CONTRACT;
 * it must not be regenerated from the module under test, because a test
 * that derives its expectation from the thing it is testing can only
 * ever pass.
 */
const HEADER_GLYPH_BEFORE_EXTRACTION =
    '<svg width="20" height="20" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<circle cx="8" cy="3" r="2" fill="currentColor"/>'
    + '<circle cx="8" cy="8" r="2" fill="currentColor"/>'
    + '<circle cx="8" cy="13" r="2" fill="currentColor"/>'
    + '</svg>';

const context = { window: {}, console: { log() {} } };
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(clientJs('kebab-icon.js'), context);
const KebabIcon = context.window.KebabIcon;

test('the shared glyph is byte-identical to the header s own, at the header s size', () => {
    assert.equal(KebabIcon.DEFAULT_SIZE, 20, 'the header renders at 20px');
    assert.equal(KebabIcon.svg(), HEADER_GLYPH_BEFORE_EXTRACTION);
    assert.equal(KebabIcon.svg(20), HEADER_GLYPH_BEFORE_EXTRACTION);
});

test('a smaller rendering scales the SAME dots, it does not thin them', () => {
    const small = KebabIcon.svg(16);
    assert.ok(small.includes('width="16" height="16"'));
    // The viewBox and the radii are what decide how much of the box is
    // ink. Both must survive a size change, or a small rendering becomes
    // the faint mark the header already had to be rescued from.
    assert.ok(small.includes('viewBox="0 0 16 16"'), 'the 16-unit viewBox is fixed');
    assert.equal((small.match(/r="2"/g) || []).length, 3, 'three r=2 dots, always');
});

test('a bad size falls back to the header size rather than rendering nothing', () => {
    for (const bad of [undefined, null, 0, -4, '20', NaN]) {
        assert.equal(KebabIcon.svg(bad), HEADER_GLYPH_BEFORE_EXTRACTION,
            `size ${String(bad)} must fall back, not emit a 0px icon`);
    }
});

test('there is exactly ONE definition of the mark in the client', () => {
    // The whole point of the extraction. A second inline copy is how the
    // header and the row would drift apart again.
    const dir = path.join(__dirname, '..', 'client', 'js');
    const offenders = [];
    for (const name of fs.readdirSync(dir)) {
        if (!name.endsWith('.js') || name === 'kebab-icon.js') continue;
        const src = fs.readFileSync(path.join(dir, name), 'utf8');
        if (src.includes('<circle cx="8" cy="13"')) offenders.push(name);
    }
    assert.deepEqual(offenders, [],
        `these files draw their own kebab instead of calling KebabIcon: ${offenders}`);
});

test('the one remaining consumer actually calls the shared builder', () => {
    // THERE USED TO BE TWO. The conversation rows drew this mark for
    // their own overflow menu until 2026-09-08, when the owner asked for
    // the three dots to go and pin/close to come back as inline icons.
    // The extraction is kept for the header alone: the ink measurement in
    // the module's docblock is the reason this mark is not a literal, and
    // that reason has nothing to do with how many callers there are.
    assert.ok(clientJs('header-menu.js').includes('window.KebabIcon.svg('),
        'the header overflow must render the shared mark');
});

test('NO CONVERSATION ROW DRAWS A KEBAB ANY MORE', () => {
    // The removal, asserted rather than assumed. A row builder that
    // started emitting a three-dot control again would be re-opening a
    // menu whose items were deleted, so it would paint an empty panel.
    for (const name of ['session-sidebar-rows.js', 'launchpad.js']) {
        const src = clientJs(name);
        assert.ok(!src.includes('KebabIcon'),
            `${name} must not draw a kebab on a session row`);
        assert.ok(!src.includes('SessionRowMenu'),
            `${name} must not reach for the deleted row overflow menu`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
