// The menu bar's half of feat/settings-gui.
//
// Two kinds of assertion, and the difference matters.
//
// BEHAVIOURAL, and it is the important half: ServerManager's bind-host
// resolution is loaded into a vm sandbox with a stubbed `electron` and
// exercised against real temp files. The bind preference now has TWO
// possible homes - config.json, where the settings screen writes, and
// the legacy menubar-settings.json - and a resolver that got the
// precedence wrong would make the tray radio disagree with the screen
// that set it, silently and permanently.
//
// STRUCTURAL, and it says so: main.js cannot be run outside Electron, so
// the menu items are checked as source text. That catches the specific
// regressions (the `open -R` coming back, the Settings item losing its
// deep link) and nothing else.
//
// Run with: node tests/test_settings_menubar.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import Module from 'node:module';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const macDir = path.join(__dirname, '..', 'macOS');

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

// ---- behavioural: bind preference resolution -------------------------

/**
 * Load ServerManager with `electron` stubbed, pointed at a temp dir.
 *
 * The real class reaches for app.getPath and spawns processes; nothing
 * exercised here does either. Stubbing rather than mocking the methods
 * under test keeps the code path the real one.
 *
 * @param {string} baseDir  Directory that stands in for the project root.
 * @returns {object} a ServerManager instance.
 */
function loadManager(baseDir) {
    const source = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-menubar-ud-'));
    const electronStub = {
        app: {
            getPath: () => userData,
            getAppPath: () => baseDir,
            isPackaged: false,
            getVersion: () => '0.0.0-test',
        },
    };
    // `electron` and `axios` are Electron-bundle dependencies that are
    // not installed at the repo root. Neither is reached by anything this
    // file exercises, so both are stubbed rather than made a test-time
    // dependency of the whole suite.
    const nodeRequire = Module.createRequire(path.join(macDir, 'server-manager.js'));
    const requireShim = (name) => {
        if (name === 'electron') return electronStub;
        if (name === 'axios') return { get: async () => ({ data: {} }) };
        return nodeRequire(name);
    };

    const sandbox = {
        require: requireShim,
        module: { exports: {} },
        exports: {},
        __dirname: macDir,
        __filename: path.join(macDir, 'server-manager.js'),
        console,
        process,
        Buffer,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'server-manager.js' });

    const ServerManager = sandbox.module.exports;
    const manager = new ServerManager();
    // The constructor resolves baseDir from the Electron app path; point
    // it at the fixture so config.json is the one this test wrote.
    manager.baseDir = baseDir;
    return manager;
}

/**
 * Make a temp project root, optionally holding a config.json.
 * @param {?object} config  Parsed config to write, or null for no file.
 * @returns {string} the directory path.
 */
function fixture(config) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-menubar-'));
    if (config !== null) {
        fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(config, null, 2));
    }
    return dir;
}

test('the tray reads the bind preference the settings screen wrote', () => {
    const dir = fixture({ server_prefs: { bind_host: '0.0.0.0' } });
    assert.equal(loadManager(dir).getBindHost(), '0.0.0.0');
});

test('a config with no server_prefs block expresses no preference', () => {
    const dir = fixture({ agents: {} });
    assert.equal(loadManager(dir).readConfigBindHost(), null);
});

test('an unreadable config is a could-not-read, not an answer', () => {
    const dir = fixture(null);
    fs.writeFileSync(path.join(dir, 'config.json'), '{ not json');
    assert.equal(loadManager(dir).readConfigBindHost(), null);
});

test('an empty stored value is not a preference either', () => {
    const dir = fixture({ server_prefs: { bind_host: '   ' } });
    assert.equal(loadManager(dir).readConfigBindHost(), null);
});

test('with no config preference the legacy menubar setting still wins', () => {
    // The whole point of keeping the fallback: an install that predates
    // the settings screen must not have its choice silently reset.
    const dir = fixture({ agents: {} });
    const manager = loadManager(dir);
    manager._settings.bind_host = '192.168.1.40';
    assert.equal(manager.getBindHost(), '192.168.1.40');
});

test('config.json beats the legacy setting when both are present', () => {
    const dir = fixture({ server_prefs: { bind_host: '127.0.0.1' } });
    const manager = loadManager(dir);
    manager._settings.bind_host = '0.0.0.0';
    assert.equal(manager.getBindHost(), '127.0.0.1');
});

test('writing the preference MERGES into config.json rather than replacing it', () => {
    const dir = fixture({
        agents: { codex_command: 'codex' },
        server_prefs: { tls_preferred: true },
    });
    const manager = loadManager(dir);
    assert.equal(manager.writeConfigBindHost('0.0.0.0'), true);

    const after = JSON.parse(fs.readFileSync(path.join(dir, 'config.json'), 'utf8'));
    assert.equal(after.server_prefs.bind_host, '0.0.0.0');
    assert.equal(after.server_prefs.tls_preferred, true, 'the TLS preference was lost');
    assert.equal(after.agents.codex_command, 'codex', 'an unrelated block was lost');
});

test('a write to a missing config reports failure instead of creating one', () => {
    // Creating a config.json here would produce a file with no secrets
    // and no projects that the server would then treat as a real one.
    const dir = fixture(null);
    assert.equal(loadManager(dir).writeConfigBindHost('0.0.0.0'), false);
});

test('no temp file is left behind by a successful write', () => {
    const dir = fixture({ server_prefs: {} });
    loadManager(dir).writeConfigBindHost('127.0.0.1');
    const stray = fs.readdirSync(dir).filter((f) => f.endsWith('.tmp'));
    assert.deepEqual([...stray], []);
});

// ---- structural: the menu items --------------------------------------

const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');

/**
 * main.js with every comment removed.
 *
 * Asserting the ABSENCE of a string over raw source finds it inside the
 * comment that documents its own removal - a false FAIL manufactured in
 * the verification step. The prose below deliberately names `open -R`,
 * so the check has to read code only.
 *
 * @type {string}
 */
const mainCode = mainSrc
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !/^\s*\/\//.test(line))
    .join('\n');

test('Edit Config no longer reveals the file in Finder', () => {
    // `open -R` selects the file rather than opening it, and an edit made
    // that way is invisible to the running server, which caches its
    // parsed config until it writes the file itself.
    assert.ok(!/open -R/.test(mainCode), 'main.js still shells out to `open -R`');
});

test('Edit Config goes through the configured-editor helper', () => {
    assert.match(mainSrc, /label:\s*'Edit Config',\s*click:\s*\(\)\s*=>\s*openInConfiguredEditor\(/);
});

test('the editor helper reads workspace.default_editor', () => {
    const fn = mainSrc.slice(
        mainSrc.indexOf('function openInConfiguredEditor('),
        mainSrc.indexOf('function updateMenu(')
    );
    assert.match(fn, /workspace\.default_editor|workspace && parsed\.workspace/);
});

test('the editor helper falls back rather than becoming a dead menu item', () => {
    const fn = mainSrc.slice(
        mainSrc.indexOf('function openInConfiguredEditor('),
        mainSrc.indexOf('function updateMenu(')
    );
    assert.match(fn, /shell\.openPath/);
});

test('the editor helper says a hand edit needs a restart', () => {
    const fn = mainSrc.slice(
        mainSrc.indexOf('function openInConfiguredEditor('),
        mainSrc.indexOf('function updateMenu(')
    );
    assert.match(fn, /Restart the server after you save/);
});

test('the menu bar has an item that opens the settings screen', () => {
    assert.match(mainSrc, /label:\s*'Settings\.\.\.',\s*\n\s*click:\s*\(\)\s*=>\s*openWebSettings\(\)/);
});

test('it opens the WEB app at the settings deep link', () => {
    assert.match(mainSrc, /shell\.openExternal\(`\$\{base\}\/#settings`\)/);
});

test('it uses the published URL, not the configured one', () => {
    const fn = mainSrc.slice(
        mainSrc.indexOf('function openWebSettings()'),
        mainSrc.indexOf('function openInConfiguredEditor(')
    );
    assert.match(fn, /getPublishedUrl\(\)/);
    assert.ok(!/getBindHost\(\)/.test(fn),
        'openWebSettings reads the configured bind, which during the setup '
        + 'lockdown is not where the server is listening');
});

// ---- the client honours the deep link --------------------------------

const appSrc = fs.readFileSync(path.join(__dirname, '..', 'client', 'js', 'app.js'), 'utf8');

test('the web app honours #settings and clears it afterwards', () => {
    const fn = appSrc.slice(
        appSrc.indexOf('_openSettingsIfDeepLinked() {'),
        appSrc.indexOf('setupEventListeners() {')
    );
    assert.match(fn, /#settings/);
    assert.match(fn, /history\.replaceState/);
    assert.match(fn, /SettingsPanel\.open/);
});

test('the deep link is honoured from showLaunchpad, which BOTH login paths reach', () => {
    // The `authenticated` event fires on only one of the two paths -
    // init()'s existing-token branch calls showLaunchpad() directly - so
    // hooking the event would work for a fresh login and silently do
    // nothing for a returning one.
    const fn = appSrc.slice(appSrc.indexOf('showLaunchpad() {'));
    // WINDOW WIDENED 2026-08-31. A fixed byte slice over a function
    // body is brittle: adding a comment inside showLaunchpad() pushes
    // the thing being asserted past the end and this reports a
    // FAILURE about the wrong subject entirely - the hook is still
    // there, the window just stopped covering it. Widened rather than
    // trimmed at the source, because shrinking a comment to fit a
    // test's magic number is fitting the code to the harness.
    assert.match(fn.slice(0, 8000), /_openSettingsIfDeepLinked\(\)/);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
