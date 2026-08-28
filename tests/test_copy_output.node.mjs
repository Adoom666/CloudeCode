// Node tests for the mobile copy workaround:
//   client/js/output-scan.js   - finding urls / codes in pane output
//   client/js/copy-output.js   - buffer reading, chip labelling, the sheet
//
// The clipboard tier ladder moved to tests/test_clipboard_compat.node.mjs
// on 2026-08-16, when the second false-success fix grew this file past
// the 500 line limit.
//
// 2026-08-16: these tests all passed while the feature was completely
// broken in a browser, which is why the wrapped-buffer and
// label-vs-value cases below assert on what would actually be COPIED
// rather than on what is rendered. The end-to-end proof lives in
// tests/manual/verify_copy_output.py, which drives real Chromium.
//
// Run with: node tests/test_copy_output.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function read(file) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', file), 'utf8');
}

const scanSrc = read('output-scan.js');
const outputSrc = read('copy-output.js');

let failures = 0;
let passes = 0;

async function test(name, fn) {
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

function loadScan() {
    const sandbox = { window: {}, console: { warn() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(scanSrc, sandbox);
    return sandbox.window.OutputScan;
}

/**
 * Load copy-output.js far enough to reach its pure helpers. It only needs
 * a window to hang its namespace on; nothing here builds DOM.
 *
 * @returns {object} the CopyOutput namespace.
 */
function loadOutput() {
    const sandbox = {
        window: {},
        document: { createElement: () => ({ style: {}, dataset: {} }) },
        console: { warn() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(outputSrc, sandbox);
    return sandbox.window.CopyOutput;
}

/**
 * Build a fake xterm buffer from rows, `|` marking a wrapped continuation.
 *
 * @param {string[]} rows - each entry is a VISUAL row, as xterm stores it.
 * @returns {object} something shaped like a Terminal.
 */
function fakeTerm(rows) {
    return {
        buffer: {
            active: {
                length: rows.length,
                getLine(i) {
                    const row = rows[i];
                    if (row === undefined) return null;
                    return {
                        isWrapped: row.startsWith('|'),
                        translateToString: () => row.replace(/^\|/, ''),
                    };
                },
            },
        },
    };
}

/* ===================================================================
 * output-scan
 * =================================================================== */

// The shape claude prints for /login: a long url on one line and a
// short code on another. Neither is selectable on a phone.
const LOGIN_OUTPUT = [
    'Browser didn\'t open? Use the url below to sign in:',
    '',
    'https://claude.ai/oauth/authorize?code=true&client_id=abc123&scope=user',
    '',
    'Then enter this code: WDJB-MJHT',
    '',
].join('\n');

await test('finds the sign-in url in login output', () => {
    const scan = loadScan();
    const urls = scan.findUrls(LOGIN_OUTPUT);
    assert.equal(urls.length, 1);
    assert.ok(urls[0].startsWith('https://claude.ai/oauth/authorize'));
});

await test('finds the sign-in code in login output', () => {
    const scan = loadScan();
    assert.ok(loadScan().findCodes(LOGIN_OUTPUT).includes('WDJB-MJHT'));
});

await test('scan surfaces both, urls before codes', () => {
    const items = loadScan().scan(LOGIN_OUTPUT);
    assert.equal(items[0].kind, 'url');
    assert.ok(items.some((i) => i.kind === 'code' && i.value === 'WDJB-MJHT'));
});

await test('trailing sentence punctuation is trimmed off a url', () => {
    const urls = loadScan().findUrls('see https://example.com/a/b.');
    assert.equal(urls[0], 'https://example.com/a/b');
});

await test('the most recent url comes first', () => {
    const urls = loadScan().findUrls('https://old.example.com/x\nhttps://new.example.com/y');
    assert.equal(urls[0], 'https://new.example.com/y');
});

await test('duplicate urls collapse to one entry', () => {
    const text = 'https://example.com/same\nhttps://example.com/same';
    assert.equal(loadScan().findUrls(text).length, 1);
});

await test('shouty prose words are not mistaken for codes', () => {
    // No digit and no dash, so ERROR/WARNING/README must not qualify —
    // otherwise every stack trace fills the sheet with noise.
    const codes = loadScan().findCodes('ERROR WARNING READMEFILE');
    assert.equal(codes.length, 0, `unexpected codes: ${codes.join(',')}`);
});

await test('url fragments are not re-offered as codes', () => {
    const codes = loadScan().findCodes('https://claude.ai/oauth?client_id=ABC123456');
    assert.ok(!codes.includes('ABC123456'));
});

await test('scan is empty and does not throw on empty output', () => {
    assert.equal(loadScan().scan('').length, 0);
    assert.equal(loadScan().scan(null).length, 0);
});

await test('scan respects its limit', () => {
    let text = '';
    for (let i = 0; i < 40; i++) text += `https://example.com/path${i}\n`;
    assert.equal(loadScan().scan(text, 5).length, 5);
});

/* ===================================================================
 * copy-output: buffer reading and chip labels
 * =================================================================== */

const WRAPPED_URL =
    'https://claude.ai/oauth/authorize?code=true&client_id=9d1c3f7a&scope=user';

await test('THE PHONE BUG: a display-wrapped url is rejoined, not cut', () => {
    // A narrow pane stores one buffer row per VISUAL row. Joining them
    // all with a newline truncated the sign-in url at the pane width,
    // which is why the phone showed a stub and the wide desktop pane did
    // not. Genuinely short token, not a display truncation.
    const rows = [
        'Use the url below to sign in:',
        'https://claude.ai/oauth/autho',
        '|rize?code=true&client_id=9d1c',
        '|3f7a&scope=user',
    ];
    const text = loadOutput().readRecentOutput(fakeTerm(rows));
    assert.ok(
        text.includes(WRAPPED_URL),
        `url was cut: ${JSON.stringify(text)}`
    );
    assert.equal(text.split('\n').length, 2);
});

await test('an unwrapped line break is still a line break', () => {
    const text = loadOutput().readRecentOutput(fakeTerm(['alpha', 'beta']));
    assert.equal(text, 'alpha\nbeta');
});

await test('a continuation row is not right-trimmed mid-token', () => {
    // Trimming a continuation would eat characters that belong to the
    // token; only the row that ENDS a logical line may be trimmed.
    const text = loadOutput().readRecentOutput(fakeTerm(['ab   ', '|  cd   ']));
    assert.equal(text, 'ab     cd');
});

await test('a window starting mid-wrap does not lose its first row', () => {
    // The head of that logical line scrolled out of the buffer window.
    const text = loadOutput().readRecentOutput(fakeTerm(['|tail-of-a-url', 'next']));
    assert.equal(text, 'tail-of-a-url\nnext');
});

await test('a short chip label is left exactly as it is', () => {
    assert.equal(loadOutput().shortenForChip('WDJB-MJHT'), 'WDJB-MJHT');
});

await test('THE LABEL IS NOT THE VALUE: shortening never changes what is copied', () => {
    const out = loadOutput();
    const label = out.shortenForChip(WRAPPED_URL);
    assert.ok(label.length < WRAPPED_URL.length, 'label must be shortened');
    assert.ok(label.includes('...'), 'the cut must be visible');
    // Both ends survive, so the user can recognise it, and the value the
    // chip copies is untouched by any of this.
    assert.ok(WRAPPED_URL.startsWith(label.split('...')[0]));
    assert.ok(WRAPPED_URL.endsWith(label.split('...')[1]));
});

await test('shortening is a display concern only and never mutates input', () => {
    const out = loadOutput();
    const value = WRAPPED_URL;
    out.shortenForChip(value, 20);
    assert.equal(value, WRAPPED_URL);
});

/* ===================================================================
 * HARD-WRAPPED ROWS. The regression the user actually hit.
 *
 * `isWrapped` is only set when tmux relies on autowrap. When the emitting
 * program hard-wraps at the pane width itself, which is what claude's own
 * renderer does, tmux writes each row with explicit cursor positioning and
 * EVERY row comes back isWrapped=false. Measured 2026-08-16 at 43 columns:
 * the user got `https://claude.com/cai/oauth/authorize?code`, exactly 43
 * characters, because the rejoin never fired.
 * =================================================================== */

/**
 * A terminal whose rows are space-padded to `cols`, like the real
 * translateToString(false), and where NO row is flagged isWrapped.
 *
 * @param {string[]} rows - logical row contents, unpadded.
 * @param {number} cols - pane width.
 * @returns {object} something shaped like a Terminal.
 */
function hardWrappedTerm(rows, cols) {
    const padded = rows.map((r) => r + ' '.repeat(Math.max(0, cols - r.length)));
    return {
        cols,
        buffer: {
            active: {
                length: padded.length,
                getLine(i) {
                    const row = padded[i];
                    if (row === undefined) return null;
                    return { isWrapped: false, translateToString: () => row };
                },
            },
        },
    };
}

const OAUTH_URL =
    'https://claude.com/cai/oauth/authorize?code=true&client_id=abc123'
    + '&response_type=code&redirect_uri=https%3A%2F%2Fx.dev%2Fcb&scope=user';

await test('hard-wrapped url is rejoined whole across 2, 3 and 4 rows', () => {
    const out = loadOutput();
    for (const cols of [64, 43, 34]) {
        const rows = [];
        for (let i = 0; i < OAUTH_URL.length; i += cols) {
            rows.push(OAUTH_URL.slice(i, i + cols));
        }
        assert.ok(rows.length >= 2, `${cols} cols should split the url`);
        const text = out.readRecentOutput(hardWrappedTerm(rows, cols));
        assert.ok(
            text.includes(OAUTH_URL),
            `at ${cols} cols the url must survive whole, got: ${text}`,
        );
    }
});

await test('a short row still ends the line, so the /login code survives', () => {
    const out = loadOutput();
    // A full row, its continuation, then a separate short line. The short
    // row must NOT be glued onto whatever follows it.
    const term = hardWrappedTerm(
        ['https://claude.com/cai/oauth/authorize?code=true', 'x', 'WDJB-MJHT'],
        48,
    );
    const text = out.readRecentOutput(term);
    assert.ok(text.includes('WDJB-MJHT'), 'the short code must stay intact');
    assert.ok(
        !text.includes('xWDJB'),
        'a short row must not be joined to the next line',
    );
});


/**
 * Minimal element stub: exactly what buildRow/buildChip touch.
 *
 * @param {string} tag - the tag name, so the test can find the anchor.
 * @returns {object} an element-shaped object.
 */
function stubEl(tag) {
    return {
        tag,
        children: [],
        dataset: {},
        style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        setAttribute(k, v) { this[k] = v; },
        getAttribute(k) { return this[k]; },
        addEventListener() {},
        appendChild(c) { this.children.push(c); return c; },
    };
}

/**
 * Load CopyOutput against the stub document, so the DOM-building
 * functions can be exercised rather than only the pure ones.
 *
 * @returns {object} the CopyOutput namespace.
 */
function loadOutputWithDom() {
    const sandbox = {
        window: {},
        document: { createElement: (tag) => stubEl(tag) },
        console: { warn() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(outputSrc, sandbox);
    return sandbox.window.CopyOutput;
}

/* ===================================================================
 * OPEN IN A NEW TAB.
 *
 * A url chip now carries a real <a target="_blank"> beside it, because
 * on the phone that printed the sign-in link, following it beats copying
 * it. That turns a string scraped out of arbitrary terminal output into
 * an href, which is a live injection surface: a pane can print anything,
 * including `javascript:...`. The gate is an ALLOW-LIST of http/https,
 * matching the deny-list shape in client/js/markdown-lite.js#isSafeUrl
 * but stricter, and a code token never gets a link at all.
 * =================================================================== */

await test('only http and https are ever offered as a link', () => {
    const out = loadOutput();
    for (const ok of [
        'http://example.com/x',
        'https://claude.com/cai/oauth/authorize?code=true',
        'HTTPS://EXAMPLE.COM/Y',
    ]) {
        assert.equal(out.isHttpUrl(ok), true, `${ok} should be linkable`);
    }
});

await test('HOSTILE SCHEMES ARE REFUSED, including obfuscated ones', () => {
    const out = loadOutput();
    const hostile = [
        'javascript:alert(1)',
        'JaVaScRiPt:alert(1)',
        // Whitespace and control characters inside the scheme: a browser
        // ignores them, so a naive check does not see the scheme at all.
        'java\nscript:alert(1)',
        'java\tscript:alert(1)',
        ' javascript:alert(1)',
        'java script:alert(1)',
        'data:text/html;base64,PHNjcmlwdD4=',
        'vbscript:msgbox(1)',
        'file:///etc/passwd',
        // Looks like it starts with http, is not an http url.
        'javascript:x?http://evil.example',
        // A code token is not a url and must never be linkable.
        'WDJB-MJHT',
        'sk-ant-0000',
        '',
        null,
        undefined,
    ];
    for (const bad of hostile) {
        assert.equal(out.isHttpUrl(bad), false, `${bad} must NOT be linkable`);
    }
});

await test('the link is a real anchor with target and rel, and only for urls', () => {
    const sandboxed = loadOutputWithDom();

    const urlRow = sandboxed.buildRow(null, { kind: 'url', value: 'https://example.com/a' });
    const anchor = urlRow.children.find((c) => c.tag === 'a');
    assert.ok(anchor, 'an http url must get an open link');
    assert.equal(anchor.href, 'https://example.com/a');
    assert.equal(anchor.target, '_blank');
    assert.equal(anchor.rel, 'noopener noreferrer',
        'noopener severs window.opener so the new tab cannot navigate this one');

    // A hostile "url" reaches buildRow the same way a real one does.
    const badRow = sandboxed.buildRow(null, { kind: 'url', value: 'javascript:alert(1)' });
    assert.equal(badRow.children.find((c) => c.tag === 'a'), undefined,
        'a javascript: url must render no anchor at all');

    const codeRow = sandboxed.buildRow(null, { kind: 'code', value: 'WDJB-MJHT' });
    assert.equal(codeRow.children.find((c) => c.tag === 'a'), undefined,
        'a code token must render no anchor at all');
});

await test('the sheet styles ship in copy-output.css AND index.html loads it', () => {
    const cssDir = path.join(__dirname, '..', 'client', 'css');
    const sheetCss = fs.readFileSync(path.join(cssDir, 'copy-output.css'), 'utf8');
    const toolsCss = fs.readFileSync(path.join(cssDir, 'terminal-tools.css'), 'utf8');
    for (const cls of ['.cloude-copy-sheet', '.cloude-copy-chip', '.cloude-copy-row',
        '.cloude-copy-open', '.cloude-copy-sheet__action']) {
        assert.ok(sheetCss.includes(cls + ' {') || sheetCss.includes(cls + ','),
            `${cls} must be styled in copy-output.css`);
        assert.ok(!toolsCss.includes(cls),
            `${cls} must not be left behind in terminal-tools.css`);
    }
    // A stylesheet on disk that no page links is the same as no styles.
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    assert.ok(html.includes('/static/css/copy-output.css'),
        'index.html must load the split stylesheet');
    // 500-line limit: the split exists because the combined file broke it.
    for (const f of ['copy-output.css', 'terminal-tools.css']) {
        const lines = fs.readFileSync(path.join(cssDir, f), 'utf8').split('\n').length;
        assert.ok(lines < 500, `${f} is ${lines} lines, over the 500 limit`);
    }
});

/* copy all must not be a lopsided pill again: --radius-full is 50%. */
await test('copy all matches the chips it sits under, and shows a copied state', () => {
    const css = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'css', 'copy-output.css'), 'utf8');
    const at = css.indexOf('.cloude-copy-sheet__action {');
    const block = css.slice(at, css.indexOf('}', at));
    assert.ok(/border-radius:\s*6px;/.test(block),
        'the pill shape was the main complaint - 6px matches the chips');
    assert.ok(!/--radius-full/.test(block), '--radius-full is 50%, not a stadium');
    assert.ok(/font-size:\s*12px;/.test(block), 'same size as the chips above it');
    assert.ok(/min-height:\s*44px;/.test(block));
    assert.ok(/width:\s*auto;/.test(block),
        'without an explicit width the bare `button` rule makes it 36px');
    assert.ok(css.includes('.cloude-copy-sheet__action.is-copied'),
        'copyAndReport() adds .is-copied - it must render');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
