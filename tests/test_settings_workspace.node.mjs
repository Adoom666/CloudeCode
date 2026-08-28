// The workspace settings tab's markup contract and its stylesheet.
//
// SCOPE, stated so this file is not mistaken for the whole check. What
// is asserted here is what a STRING can honestly answer: that the
// renderer emits the controls, that it distinguishes the three bind
// states instead of two, and that the stylesheet names tokens rather
// than literals. Whether any of it is on screen, reachable at 390px, or
// actually follows the theme is measured in a real Chromium by
// scripts/verify_settings_gui.py, because a string cannot answer that
// and this repo has shipped three features that were in the DOM and
// visibly broken.
//
// Run with: node tests/test_settings_workspace.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
 * Read one file from the repo root.
 * @param {...string} parts  Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
}

/**
 * Load the shipped module in a sandbox with just enough of a document
 * for its escapeHtml helper, and return its public surface.
 *
 * The helper is the real one from settings-sections.js rather than a
 * stub, so an escaping bug in the shipped code cannot be papered over by
 * a friendlier fake here.
 *
 * @returns {object} window.SettingsWorkspace.
 */
function loadModule() {
    const sandbox = {
        window: {},
        document: {
            createElement() {
                return {
                    _text: '',
                    set textContent(v) { this._text = v == null ? '' : String(v); },
                    get innerHTML() {
                        return this._text
                            .replace(/&/g, '&amp;')
                            .replace(/</g, '&lt;')
                            .replace(/>/g, '&gt;')
                            .replace(/"/g, '&quot;');
                    },
                };
            },
        },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(read('client', 'js', 'settings-sections.js'), sandbox);
    vm.runInContext(read('client', 'js', 'settings-workspace.js'), sandbox);
    return sandbox.window.SettingsWorkspace;
}

const WS = loadModule();

const SUMMARY = {
    workspace: {
        development_root: '/dev',
        default_shell: '/bin/zsh',
        default_editor: 'code -w',
        env: { EDITOR: 'code -w' },
    },
    server_prefs: {
        bind_host: '0.0.0.0',
        tls_preferred: false,
        effective_bind_host: '127.0.0.1',
        tls_available: false,
    },
};

// ---- the controls exist and carry their current values ---------------

test('every one of the four settings has an input carrying its stored value', () => {
    const html = WS.render(SUMMARY);
    assert.match(html, /id="settings-ws-root"[^>]*value="\/dev"/);
    assert.match(html, /id="settings-ws-shell"[^>]*value="\/bin\/zsh"/);
    assert.match(html, /id="settings-ws-editor"[^>]*value="code -w"/);
    assert.match(html, /id="settings-ws-bind"[^>]*value="0\.0\.0\.0"/);
    assert.match(html, /data-env-name[^>]*value="EDITOR"/);
});

test('a label is associated with each input by for/id, not by proximity', () => {
    const html = WS.render(SUMMARY);
    for (const id of ['settings-ws-root', 'settings-ws-shell',
                      'settings-ws-editor', 'settings-ws-bind']) {
        assert.ok(html.includes(`for="${id}"`), `no label for ${id}`);
    }
});

test('an empty config still renders one env row to type into', () => {
    const html = WS.render({ workspace: {}, server_prefs: {} });
    assert.equal((html.match(/data-env-row/g) || []).length, 1);
});

// ---- three bind states, not two --------------------------------------

test('a saved bind that differs from the one in force renders a WARNING', () => {
    const html = WS.render(SUMMARY);
    assert.match(html, /data-bind-state="pending"/);
    assert.match(html, /not in force yet/);
    // It must name the address actually running, not only the saved one.
    assert.match(html, /127\.0\.0\.1/);
});

test('a saved bind that matches the one in force says so, and does not warn', () => {
    const html = WS.render({
        workspace: {},
        server_prefs: { bind_host: '127.0.0.1', effective_bind_host: '127.0.0.1' },
    });
    assert.match(html, /data-bind-state="in-force"/);
    assert.ok(!/data-bind-state="pending"/.test(html));
});

test('no saved bind is its own state, not "matches" and not "differs"', () => {
    const html = WS.render({
        workspace: {},
        server_prefs: { bind_host: '', effective_bind_host: '0.0.0.0' },
    });
    assert.match(html, /data-bind-state="unset"/);
});

test('TLS is rendered as recorded-and-not-in-force, never as a plain on/off', () => {
    const html = WS.render(SUMMARY);
    assert.match(html, /data-tls-state="unavailable"/);
    assert.match(html, /not in force/);
});

test('the environment list says a change reaches only NEW terminals', () => {
    const html = WS.render(SUMMARY);
    assert.match(html, /sessions already open keep the/i);
});

test('the reserved prefix is named in the UI, not only enforced server-side', () => {
    assert.match(WS.render(SUMMARY), /CLOUDECODE_/);
});

// ---- escaping ---------------------------------------------------------

test('a value containing markup is escaped, not injected', () => {
    const html = WS.render({
        workspace: { development_root: '"><script>x</script>', env: {} },
        server_prefs: {},
    });
    assert.ok(!html.includes('<script>x</script>'));
    assert.match(html, /&lt;script&gt;/);
});

// ---- collection -------------------------------------------------------

test('collectEnv drops a blank NAME so the always-present empty row is invisible', () => {
    const rows = [
        { name: 'A', value: '1' },
        { name: '   ', value: 'ignored' },
        { name: 'B', value: '' },
    ];
    const fakeRoot = {
        querySelectorAll() {
            return rows.map((r) => ({
                querySelector(sel) {
                    if (sel === '[data-env-name]') return { value: r.name };
                    if (sel === '[data-env-value]') return { value: r.value };
                    return null;
                },
            }));
        },
    };
    // Compared through JSON because the module runs in a vm context, so
    // the object it returns has that realm's Object prototype and a
    // strict deep-equal fails on identity rather than on contents.
    assert.equal(
        JSON.stringify(WS.collectEnv(fakeRoot)),
        JSON.stringify({ A: '1', B: '' })
    );
});

// ---- the stylesheet ---------------------------------------------------

/**
 * Split a stylesheet into flat {selector, body} records, comments first
 * stripped so a selector named in prose is never taken for a live rule.
 * @param {string} source  CSS text.
 * @returns {Array<{selector: string, body: string}>} One entry per rule.
 */
function rules(source) {
    const clean = source.replace(/\/\*[\s\S]*?\*\//g, '');
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(clean)) !== null) {
        const selector = m[1].trim().replace(/\s+/g, ' ');
        if (!selector || selector.startsWith('@')) continue;
        out.push({ selector, body: m[2] });
    }
    return out;
}

const CSS = read('client', 'css', 'styles.css');
const WORKSPACE_RULES = rules(CSS).filter((r) => /settings-env/.test(r.selector));

test('the workspace rules exist at all', () => {
    assert.ok(WORKSPACE_RULES.length >= 5,
        `only ${WORKSPACE_RULES.length} .settings-env rules found`);
});

test('no workspace rule hardcodes a colour', () => {
    for (const rule of WORKSPACE_RULES) {
        const hex = rule.body.match(/#[0-9a-fA-F]{3,8}\b/g);
        assert.equal(hex, null,
            `${rule.selector} hardcodes ${hex && hex.join(', ')}`);
        assert.ok(!/\brgba?\(/.test(rule.body),
            `${rule.selector} hardcodes an rgb() colour`);
    }
});

test('no workspace rule hardcodes a corner radius', () => {
    for (const rule of WORKSPACE_RULES) {
        const radius = rule.body.match(/border-radius:\s*([^;]+)/);
        if (!radius) continue;
        assert.match(radius[1], /var\(--radius-/,
            `${rule.selector} sets border-radius: ${radius[1].trim()} rather than a token`);
    }
});

test('the env row is a grid so the NAME column stays a column', () => {
    const row = WORKSPACE_RULES.find((r) => r.selector === '.settings-env-row');
    assert.ok(row, 'no .settings-env-row rule');
    assert.match(row.body, /display:\s*grid/);
    assert.match(row.body, /grid-template-columns/);
});

test('the row collapses on a phone so the remove control stays on screen', () => {
    // The media query's contents are stripped by `rules()`, so this one
    // reads the raw source. A three-across grid at 390px put the remove
    // button off the right edge - the control was unreachable, which is
    // why this is asserted rather than left to taste.
    const media = CSS.match(/@media[^{]*max-width:\s*600px[^{]*\{([\s\S]*?)\n\}/g) || [];
    assert.ok(media.some((block) => /settings-env-row/.test(block)),
        'no narrow-viewport rule for .settings-env-row');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
