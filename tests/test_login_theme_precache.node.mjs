// THE LOGIN SCREEN MUST PAINT THE USER'S PALETTE, NOT JUST NAME IT.
//
// `Themes.applyStoredThemeIdSync()` runs before auth. It stamped
// `<html data-theme="...">` and stopped there. No stylesheet in this app
// keys off that attribute - a theme's palette is `cssVars` painted inline
// on :root from a manifest fetched from `GET /api/v1/themes`, which is
// behind require_auth. So pre-auth the attribute was right and the colours
// were Claude's, under every theme.
//
// MEASURED in a real Chromium over HTTP before the fix: with
// `cloude.theme` set to `terminal` and then `claude`, the login screen
// resolved --color-bg #1e1e1e, --color-accent #d77757 and --color-fg
// #d4d4d4 in BOTH runs. Only the data-theme attribute differed. The pixel
// verdict lives in scripts/verify_login_theme.py, which carries a
// --control run proving the measurement can fail. This file is the fast
// unit half: it pins the cache rules that make the paint possible.
//
// Themes chosen deliberately: terminal and claude differ on every token
// touched here. gameboy and matrix set several tokens to the same value,
// so a test written against them can pass for the wrong reason.
//
// Run with: node tests/test_login_theme_precache.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const THEME_KEY = 'cloude.theme';
const VARS_CACHE_KEY = 'cloude.theme.vars';

let passes = 0;
let failures = 0;

/**
 * Description: run one named assertion block, recording rather than
 *   throwing so one failure does not hide the rest.
 * Inputs: name (string), fn (function). Output: Promise<void>.
 */
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

/**
 * Description: load themes/registry.js over a DOM shim whose :root style
 *   object records every property set on it, so the test can read back
 *   exactly what would have been painted.
 * Inputs: store (object) - initial localStorage contents.
 * Output: object - {sandbox, painted, dataset}.
 */
function load(store) {
    const painted = new Map();
    const backing = { ...store };
    const rootStyle = {
        setProperty(name, value) { painted.set(name, value); },
        removeProperty(name) { painted.delete(name); },
    };
    const documentElement = { style: rootStyle, dataset: {} };
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: {
            documentElement,
            createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
            head: { appendChild() {} },
            addEventListener() {}, removeEventListener() {},
            getElementById: () => null, querySelector: () => null,
            querySelectorAll: () => [],
        },
        localStorage: {
            getItem: (k) => (k in backing ? backing[k] : null),
            setItem: (k, v) => { backing[k] = String(v); },
            removeItem: (k) => { delete backing[k]; },
        },
        setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
        fetch: () => Promise.reject(new Error('no network pre-auth')),
        Set, Map, JSON, Object, Array, Promise, Error,
        location: { href: '' }, navigator: { userAgent: '' },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'themes', 'registry.js'), 'utf8'),
        sandbox, { filename: 'registry.js' });
    return { sandbox, painted, dataset: documentElement.dataset, backing };
}

/**
 * Description: read a bundled theme manifest off disk.
 * Inputs: id (string). Output: object.
 */
function manifest(id) {
    return JSON.parse(fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'themes', id, 'theme.json'), 'utf8'));
}

const TERMINAL = manifest('terminal');
const CLAUDE = manifest('claude');

await test('the two themes actually differ, or nothing below discriminates', () => {
    for (const token of ['--color-bg', '--color-accent', '--color-fg']) {
        assert.notEqual(TERMINAL.cssVars[token], CLAUDE.cssVars[token],
            `terminal and claude agree on ${token}; it cannot tell them apart`);
    }
});

await test('the harness records painted vars (positive control)', () => {
    const { sandbox, painted } = load({});
    sandbox.Themes.applyStoredThemeIdSync();
    // With no cache this legitimately paints nothing, so prove the recorder
    // works by driving the painter through a real apply instead.
    assert.equal(painted.size, 0, 'no cache must paint nothing');
    const seeded = load({
        [THEME_KEY]: 'terminal',
        [VARS_CACHE_KEY]: JSON.stringify({ id: 'terminal', cssVars: TERMINAL.cssVars }),
    });
    seeded.sandbox.Themes.applyStoredThemeIdSync();
    assert.ok(seeded.painted.size > 0,
        'the style recorder captured nothing at all - every assertion below '
        + 'would pass for the wrong reason');
});

await test('a cached palette is painted before auth', () => {
    const { sandbox, painted, dataset } = load({
        [THEME_KEY]: 'terminal',
        [VARS_CACHE_KEY]: JSON.stringify({ id: 'terminal', cssVars: TERMINAL.cssVars }),
    });
    sandbox.Themes.applyStoredThemeIdSync();
    assert.equal(dataset.theme, 'terminal', 'the attribute must still be set');
    assert.equal(painted.get('--color-bg'), TERMINAL.cssVars['--color-bg']);
    assert.equal(painted.get('--color-accent'), TERMINAL.cssVars['--color-accent']);
    assert.notEqual(painted.get('--color-bg'), CLAUDE.cssVars['--color-bg'],
        'the login screen is still showing the default palette');
});

await test('a cache written for ANOTHER theme is never painted', () => {
    const { sandbox, painted } = load({
        [THEME_KEY]: 'terminal',
        [VARS_CACHE_KEY]: JSON.stringify({ id: 'claude', cssVars: CLAUDE.cssVars }),
    });
    sandbox.Themes.applyStoredThemeIdSync();
    assert.equal(painted.size, 0,
        'a palette cached for claude was painted while terminal was stored - '
        + 'the user would see colours they never chose');
});

await test('a corrupt or absent cache falls back to the defaults, silently', () => {
    for (const raw of ['not json at all', '{"id":"terminal"}', '[]', 'null']) {
        const { sandbox, painted } = load({ [THEME_KEY]: 'terminal', [VARS_CACHE_KEY]: raw });
        assert.doesNotThrow(() => sandbox.Themes.applyStoredThemeIdSync(),
            `applyStoredThemeIdSync threw on cache ${raw} - this runs on the '
            + 'pre-auth path where a throw means a blank page`);
        assert.equal(painted.size, 0, `cache ${raw} should paint nothing`);
    }
});

await test('a real init() writes the cache the next load reads', async () => {
    // Drive the whole path the app drives: the manifest endpoint answers,
    // init() loads it and applies the stored theme. Arranging this through
    // the fetch seam rather than by poking `manifests` is deliberate - a
    // test that writes the cache by hand would prove nothing about whether
    // the app ever writes it, which is the half that was missing.
    const { sandbox, backing } = load({ [THEME_KEY]: 'terminal' });
    sandbox.fetch = () => Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve([TERMINAL, CLAUDE]),
    });
    await sandbox.Themes.init();
    const cached = JSON.parse(backing[VARS_CACHE_KEY] || '{}');
    assert.equal(cached.id, 'terminal',
        'init() applied the theme but cached no palette, so the next login '
        + 'screen has nothing to paint');
    assert.equal(cached.cssVars['--color-bg'], TERMINAL.cssVars['--color-bg']);
});

await test('the cache init() writes is the one applyStoredThemeIdSync reads', async () => {
    // The round trip, end to end: no hand-written cache anywhere.
    const first = load({ [THEME_KEY]: 'terminal' });
    first.sandbox.fetch = () => Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve([TERMINAL, CLAUDE]),
    });
    await first.sandbox.Themes.init();

    const second = load({ ...first.backing });   // next page load, no network
    second.sandbox.Themes.applyStoredThemeIdSync();
    assert.equal(second.painted.get('--color-bg'), TERMINAL.cssVars['--color-bg'],
        'the login screen did not paint the palette the last session cached');
    assert.notEqual(second.painted.get('--color-bg'), CLAUDE.cssVars['--color-bg']);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
