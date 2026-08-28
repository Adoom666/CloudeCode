// No shipped HTML may carry an inline event-handler attribute, because
// src/main.py forbids them and the browser's refusal is SILENT.
//
// THE BUG THIS EXISTS TO CATCH. #logoutBtn shipped
// `onclick="App.logout()"` from the initial commit. src/main.py started
// stamping `Content-Security-Policy: ... script-src 'self' ...` on every
// response in a2a4fa2 (2026-04-23), and `script-src 'self'` forbids
// inline event handlers. From that commit the logout button did nothing
// at all - and nothing anywhere reported it:
//
//   - no exception was thrown, so `pageerror` was empty
//   - no promise rejected, so no `unhandledrejection`
//   - the element stayed present, 186x44, display:flex,
//     visibility:visible, opacity:1, and `document.elementFromPoint()`
//     at its own centre returned the button itself, so it was provably
//     not covered
//
// Every DOM assertion, every geometry assertion and every "is it
// visible" pixel assertion passed for four months on a completely dead
// control. The only witness in the browser was a console message.
//
// WHY A TEXT ASSERTION IS THE RIGHT SHAPE HERE, in a repo whose standing
// rule is to test the pixel and not the markup. The rule exists because
// markup presence is a poor proxy for what RENDERS. This is not that: an
// inline handler attribute is not a proxy for a CSP violation, it IS the
// CSP violation, in the same file, statically decidable. What the pixel
// test cannot do is enumerate - it can only ever measure the controls
// someone remembered to drive. This covers every element in every
// shipped HTML file, including ones added tomorrow, for the cost of a
// regex.
//
// It is the cheap half of a pair, not a replacement for the other half.
// scripts/verify_logout_chrome.py drives the real flow against a real
// uvicorn (the CSP header is produced by the application, so a static
// file server cannot reproduce the condition at all - it sends no CSP
// and the inline handler runs fine there, which is exactly how a harness
// false-greens this). Keep both.
//
// If a control genuinely needs an inline attribute, the answer is to
// wire it with addEventListener in its module, the way #settingsBtn and
// #configEditorBtn beside it already are. Relaxing the CSP to
// 'unsafe-inline' is not the answer and would need its own argument.
//
// Run with: node tests/test_csp_no_inline_handlers.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

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

// Every `on*` attribute the HTML spec defines as an event handler would
// be a mouthful; matching the shape is both shorter and future-proof
// against handlers added to the platform later. Requiring a value
// (`= "..."`) keeps this off ordinary attributes that merely begin with
// "on", and the leading whitespace requirement keeps it off substrings
// inside longer attribute names.
const INLINE_HANDLER = /\son[a-z]{2,24}\s*=\s*["'][^"']*["']/gi;

/**
 * Every .html file the server actually ships to a browser. The manual
 * harnesses under tests/manual/ are deliberately excluded: they are
 * loaded from a plain static server with no CSP, they are never served
 * by src/main.py, and several of them use inline handlers on purpose.
 * @returns {string[]} Absolute paths.
 */
function shippedHtmlFiles() {
    const out = [];
    const walk = (dir) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) walk(full);
            else if (entry.name.endsWith('.html')) out.push(full);
        }
    };
    walk(path.join(ROOT, 'client'));
    return out;
}

test('client/index.html exists and is non-trivial', () => {
    // Guards the whole file against the empty-glob false green: a
    // scanner that finds nothing to scan reports exactly the same "0
    // violations" as a clean tree.
    const idx = path.join(ROOT, 'client', 'index.html');
    assert.ok(fs.existsSync(idx), 'client/index.html is missing');
    assert.ok(fs.readFileSync(idx, 'utf8').length > 5000,
        'client/index.html is suspiciously small - is this the real file?');
});

test('the CSP that makes this matter is still in force', () => {
    // If someone relaxes script-src, this test is measuring a rule that
    // no longer exists and should be reconsidered rather than silently
    // kept passing.
    //
    // The policy moved out of src/main.py into src/security_headers.py so
    // the static test servers under scripts/ can serve the SAME headers the
    // app serves - a harness with no CSP cannot represent a CSP-dependent
    // defect at all, which is how this bug survived every harness ever
    // pointed at it. Both halves are asserted here: the policy still says
    // what it must, AND main.py still applies it. A policy nothing stamps
    // is not a policy.
    const hdrs = fs.readFileSync(
        path.join(ROOT, 'src', 'security_headers.py'), 'utf8');
    assert.ok(/\("script-src",\s*"'self'"\)/.test(hdrs),
        "src/security_headers.py no longer declares script-src 'self' - "
        + "this test's premise is gone");
    // Scoped to the DIRECTIVE TABLE, not the whole file. These modules
    // carry a lot of prose ABOUT 'unsafe-inline' and 'unsafe-hashes' -
    // explaining why they are absent - and a whole-file grep reads that
    // prose as the policy. Comments are not code, in either direction.
    const table = (hdrs.match(/CSP_DIRECTIVES[\s\S]*?^\)/m) || [''])[0];
    assert.ok(table.includes('script-src'),
        'could not locate the CSP_DIRECTIVES table to check');
    assert.ok(!/unsafe-inline/.test(table.split('style-src')[0]),
        "script-src now allows 'unsafe-inline' - that needs its own argument");
    assert.ok(!/unsafe-hashes/.test(table),
        "'unsafe-hashes' would make inline event handlers legal again");

    const main = fs.readFileSync(path.join(ROOT, 'src', 'main.py'), 'utf8');
    assert.ok(/from\s+src\.security_headers\s+import\s+SECURITY_HEADERS/
        .test(main), 'src/main.py no longer imports SECURITY_HEADERS');
    assert.ok(/SECURITY_HEADERS\.items\(\)/.test(main),
        'src/main.py no longer stamps SECURITY_HEADERS onto responses');
});

test('the static test server serves the app policy, not its own copy', () => {
    // The harness half of the same premise. If scripts/lib_csp_static_server
    // ever stops importing the app's headers - or starts granting
    // 'unsafe-hashes' so inline handlers compile there - every pixel
    // verifier silently returns to being blind to this whole class.
    const lib = fs.readFileSync(
        path.join(ROOT, 'scripts', 'lib_csp_static_server.py'), 'utf8');
    assert.ok(/from\s+src\.security_headers\s+import/.test(lib),
        'the static test server no longer imports the app security headers');
    // Behavioural proof that inline handlers still fail in the harness is
    // the positive control inside scripts/verify_login_chrome.py, which
    // re-introduces one and watches the run go red. What is checked here is
    // only the wiring: that the harness renders the app's policy through
    // build_csp() rather than assembling a policy of its own.
    assert.ok(/build_csp\(/.test(lib),
        'the static test server no longer renders the policy via build_csp()');
});

test('the scanner finds files to scan', () => {
    const files = shippedHtmlFiles();
    assert.ok(files.length > 0, 'no shipped .html files found - the scan is vacuous');
});

test('the scanner actually detects an inline handler (positive control)', () => {
    // Without this, a broken regex and a clean tree are the same result.
    // This is the exact string that shipped the four-month-dead button.
    const sample = '<button onclick="App.logout()" id="logoutBtn"></button>';
    assert.ok(new RegExp(INLINE_HANDLER.source, 'i').test(sample),
        'the detector does not match a known-bad inline handler');
    const clean = '<button id="logoutBtn" data-tooltip="Logout"></button>';
    assert.ok(!new RegExp(INLINE_HANDLER.source, 'i').test(clean),
        'the detector fires on markup that carries no handler');
});

test('no shipped HTML carries an inline event handler', () => {
    const offenders = [];
    for (const file of shippedHtmlFiles()) {
        const text = fs.readFileSync(file, 'utf8');
        text.split('\n').forEach((line, i) => {
            // Comments talk ABOUT inline handlers in this repo (see the
            // note on #logoutBtn), and prose is not code.
            const code = line.replace(/<!--[\s\S]*?-->/g, '');
            const hits = code.match(new RegExp(INLINE_HANDLER.source, 'gi'));
            if (hits) {
                offenders.push(
                    `${path.relative(ROOT, file)}:${i + 1}  ${hits.join(' ')}`);
            }
        });
    }
    assert.deepEqual(offenders, [],
        "inline event handlers are dead under `script-src 'self'` - the "
        + 'browser refuses to run them and reports nothing. Wire these with '
        + 'addEventListener in the owning module instead:\n  '
        + offenders.join('\n  '));
});

test('#logoutBtn is wired in app.js, not in markup', () => {
    // The specific control the user reported. The class-wide scan above
    // proves the attribute is gone; this proves something replaced it,
    // so "fixed" cannot mean "the button was quietly unwired."
    const app = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'app.js'), 'utf8');
    assert.ok(/this\.logoutBtn\s*\.addEventListener\(\s*['"]click['"]/.test(app),
        'app.js does not attach a click listener to this.logoutBtn');
    assert.ok(/this\.logoutBtn\s*=\s*document\.getElementById\(\s*['"]logoutBtn['"]/
        .test(app), 'app.js does not resolve #logoutBtn');
});

/**
 * Every .js file under client/, which is markup's other author. An
 * innerHTML template that writes `onclick="..."` produces exactly the same
 * dead control as a literal in index.html, and the HTML scan above cannot
 * see it because the attribute never appears in a .html file.
 * @returns {string[]} Absolute paths.
 */
function clientJsFiles() {
    const out = [];
    const walk = (dir) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            if (entry.name === 'node_modules' || entry.name === 'vendor'
                || entry.name.startsWith('.')) continue;
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) walk(full);
            else if (entry.name.endsWith('.js')) out.push(full);
        }
    };
    walk(path.join(ROOT, 'client'));
    return out;
}

/**
 * Strip line and block comments so prose about inline handlers - of which
 * this repo now has a fair amount - is not read as code.
 * @param {string} text  Source.
 * @returns {string} Source with comments blanked.
 */
function stripJsComments(text) {
    return text
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
}

test('the client JS scanner finds files to scan', () => {
    assert.ok(clientJsFiles().length > 0,
        'no client .js files found - the scan is vacuous');
});

test('the client JS scanner detects an emitted handler (positive control)', () => {
    const sample = 'el.innerHTML = `<button onclick="App.logout()">x</button>`;';
    assert.ok(new RegExp(INLINE_HANDLER.source, 'i').test(sample),
        'the detector does not match a handler emitted from JS');
});

test('no client JS emits markup carrying an inline event handler', () => {
    const offenders = [];
    for (const file of clientJsFiles()) {
        const code = stripJsComments(fs.readFileSync(file, 'utf8'));
        code.split('\n').forEach((line, i) => {
            const hits = line.match(new RegExp(INLINE_HANDLER.source, 'gi'));
            if (hits) {
                offenders.push(
                    `${path.relative(ROOT, file)}:${i + 1}  ${hits.join(' ')}`);
            }
        });
    }
    assert.deepEqual(offenders, [],
        'markup written from JS is subject to the same CSP as markup in a '
        + '.html file - an inline handler here is just as dead, and the HTML '
        + 'scan cannot see it:\n  ' + offenders.join('\n  '));
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
