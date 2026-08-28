// Node test for the home bar's server-status panel.
//
// TWO KINDS OF ASSERTION, both earned by real defects in this repo:
//
//   BEHAVIOUR. server-status-format.js is a pure function of the payload
//   and touches no DOM, so it is evaluated here for real and driven with
//   crafted snapshots. What is being guarded is the THIRD OUTCOME: every
//   section must be able to say "cannot determine" and that must never
//   look like a healthy reading. A memory row printing 0 bytes because
//   vm_stat was missing, or a version check printing "up to date" because
//   it could not reach the network, is the false-green class this project
//   has paid for more than any other.
//
//   SOURCE AND CSS. The rest are declaration defects, which no screenshot
//   catches: a button class that forgets `border-radius` silently inherits
//   the round bare-`button` reset from styles.css; a panel that calls
//   API.destroySession() directly silently becomes a SECOND way to destroy
//   a session and drifts from the shared confirmation copy.
//
// Measured live before committing at 1280px desktop and 390px, against
// 127.0.0.1 only.
//
// Run with: node tests/test_server_status_panel.node.mjs

import fs from 'node:fs';
import path from 'node:path';
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
 * Read one file from the repo root.
 * @param {...string} parts  Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

/**
 * Split a stylesheet into flat {selector, body} records, comments stripped
 * first so a selector quoted inside a comment cannot be mistaken for a
 * live rule. Deliberately not a real parser - these are flat sheets.
 *
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

/**
 * The body of the first rule whose selector list contains `selector`.
 * @param {Array<{selector: string, body: string}>} list  Parsed rules.
 * @param {string} selector  Exact selector to look for.
 * @returns {string} The rule body, or '' when absent.
 */
function ruleBody(list, selector) {
    for (const r of list) {
        const parts = r.selector.split(',').map((s) => s.trim());
        if (parts.includes(selector)) return r.body;
    }
    return '';
}

/**
 * Evaluate an IIFE client module that only touches `window` and `console`,
 * and hand back the object it exported.
 *
 * @param {string} file  Filename under client/js.
 * @param {string} exportName  Key it assigns on window.
 * @returns {object} The exported module.
 */
function loadModule(file, exportName) {
    const win = {};
    // eslint-disable-next-line no-new-func
    new Function('window', 'console', read('client', 'js', file))(
        win, { log() {}, error() {}, warn() {} }
    );
    assert.ok(win[exportName], `${file} must export window.${exportName}`);
    return win[exportName];
}

const F = loadModule('server-status-format.js', 'ServerStatusFormat');
const panelSrc = read('client', 'js', 'server-status-panel.js');
const apiSrc = read('client', 'js', 'api.js');
const menuSrc = read('client', 'js', 'server-controls-menu.js');
const actionsSrc = read('client', 'js', 'session-row-actions.js');
const launchpadSrc = read('client', 'js', 'launchpad.js');
const indexHtml = read('client', 'index.html');
const css = read('client', 'css', 'server-status.css');
const cssRules = rules(css);

// ---------------------------------------------------------------------
// formatters
// ---------------------------------------------------------------------

test('esc neutralises every character that could break out of markup', () => {
    assert.equal(F.esc('<img src=x onerror="a">'),
        '&lt;img src=x onerror=&quot;a&quot;&gt;');
    assert.equal(F.esc("it's & so"), 'it&#39;s &amp; so');
    assert.equal(F.esc(null), '');
});

test('bytes renders a real figure and UNKNOWN for a non-number', () => {
    assert.equal(F.bytes(0), '0 b');
    assert.equal(F.bytes(1024), '1.0 kb');
    assert.equal(F.bytes(68719476736), '64.0 gb');
    assert.equal(F.bytes(null), F.UNKNOWN);
    assert.equal(F.bytes(undefined), F.UNKNOWN);
    assert.equal(F.bytes(-1), F.UNKNOWN);
});

test('duration never renders an unknown value as 0s', () => {
    assert.equal(F.duration(45), '45s');
    assert.equal(F.duration(3723), '1h 2m');
    assert.equal(F.duration(183845), '2d 3h');
    assert.equal(F.duration(null), F.UNKNOWN);
    assert.equal(F.duration(undefined), F.UNKNOWN);
});

test('since is relative to an injectable clock and refuses the future', () => {
    assert.equal(F.since(1000, 4600), '1h 0m ago');
    assert.equal(F.since(5000, 4600), F.UNKNOWN);
    assert.equal(F.since(0, 4600), F.UNKNOWN);
});

// ---------------------------------------------------------------------
// ownership - the field that has already shipped wrong twice
// ---------------------------------------------------------------------

test('ownership is three-valued and unknown is not "external"', () => {
    assert.equal(F.ownershipWord(true), 'cloudecode');
    assert.equal(F.ownershipWord(false), 'external');
    assert.equal(F.ownershipWord(null), 'owner unknown');
    assert.equal(F.ownershipWord(undefined), 'owner unknown');
});

test('the panel never re-derives ownership from the adopted: id prefix', () => {
    // The live bug: after a restart the app re-adopts its OWN sessions, so
    // an app-created session carries an adopted: id while still sitting in
    // owned_tmux_sessions. Only the server's created_by_cloude may decide.
    const html = F.renderSessionRow({
        name: 'cloude_ses_ec5bf2a3',
        session_id: 'adopted:cloude_ses_ec5bf2a3',
        created_by_cloude: true,
        open_in_app: true,
        pane_cols: 120, pane_rows: 40, created_at_epoch: 1, working_dir: '/tmp',
    }, 100);
    assert.match(html, /cloudecode/);
    assert.doesNotMatch(html, /external/);
    assert.doesNotMatch(
        [panelSrc, read('client', 'js', 'server-status-format.js')].join('\n'),
        /startsWith\(\s*['"]adopted:/,
        'ownership must come from the server field, never from the id prefix'
    );
});

// ---------------------------------------------------------------------
// the third outcome
// ---------------------------------------------------------------------

test('an unavailable memory probe says why instead of showing zero', () => {
    const html = F.renderHost({
        host: { available: true, hostname: 'h', os: 'os', uptime_seconds: 1 },
        memory: { available: false, error: 'vm_stat unavailable' },
        disk: { available: true, path: '/', free_bytes: 1, total_bytes: 2, used_percent: 50 },
        load: { available: true, load_1: 1, load_5: 1, load_15: 1, cpu_count: 8 },
    });
    assert.match(html, /cannot determine: vm_stat unavailable/);
    assert.match(html, /server-status-value--unknown/);
    assert.doesNotMatch(html, /0 b of 0 b/);
});

test('an unavailable disk probe does not silently vanish', () => {
    const html = F.renderHost({
        host: { available: true, hostname: 'h', os: 'o', uptime_seconds: 1 },
        memory: { available: true, used_bytes: 1, total_bytes: 2, available_bytes: 1, used_percent: 50 },
        disk: { available: false, error: 'unreadable' },
        load: { available: false, error: 'no loadavg' },
    });
    assert.match(html, /cannot determine: unreadable/);
    assert.match(html, /cannot determine: no loadavg/);
});

test('tmux with no server running is a fact, not an error', () => {
    const html = F.renderTmux({
        available: true, socket: 'cloude', server_running: false,
        history_limit: 2000, sessions: [],
    });
    assert.match(html, /not running/);
    // The section is fully evaluated: nothing here is a could-not-measure.
    assert.doesNotMatch(html, /cannot determine/);
    assert.doesNotMatch(html, /server-status-value--unknown/);
});

test('tmux that could not be reached is NOT reported as "not running"', () => {
    const html = F.renderTmux({
        available: false, socket: 'cloude', server_running: null,
        error: 'tmux is not installed', sessions: [],
    });
    assert.match(html, /cannot determine/);
    assert.match(html, /tmux is not installed/);
    assert.doesNotMatch(html, /open sessions/);
});

test('an unreadable history-limit is unknown, never tmux\'s 2000 default', () => {
    const html = F.renderTmux({
        available: true, socket: 'cloude', server_running: true,
        history_limit: null, sessions: [],
    });
    assert.match(html, /cannot determine: tmux did not report history-limit/);
    assert.doesNotMatch(html, /2000/);
});

test('a real history-limit is printed as lines', () => {
    const html = F.renderTmux({
        available: true, socket: 'cloude', server_running: true,
        history_limit: 10000, sessions: [],
    });
    assert.match(html, /10000 lines/);
});

// ---------------------------------------------------------------------
// release / self check - three outcomes, never two
// ---------------------------------------------------------------------

test('release: an unreachable check is never rendered as up to date', () => {
    const html = F.renderRelease({
        version: '0.7.1',
        update: {
            status: 'unknown', current_version: '0.7.1', latest_version: '',
            checked_at: 100, reason: 'github unreachable',
        },
    }, 3700);
    assert.match(html, /could not check: github unreachable/);
    assert.doesNotMatch(html, /up to date/);
});

test('release: a transport failure is could-not-check, not up to date', () => {
    // The panel synthesises this marker when the call never landed.
    const html = F.renderRelease({
        available: false, error: 'this server has no version check yet',
    });
    assert.match(html, /cannot determine: this server has no version check yet/);
    assert.doesNotMatch(html, /up to date/);
});

test('release: a missing payload is could-not-check, not up to date', () => {
    const html = F.renderRelease(null);
    assert.match(html, /cannot determine/);
    assert.doesNotMatch(html, /up to date/);
});

test('release: an unrecognised status falls to could-not-check', () => {
    // A status string this client has never heard of is precisely the case
    // where it does not know the answer. It must not resolve to up to date.
    const html = F.renderRelease({
        version: '0.8.0',
        update: { status: 'brand_new_thing', checked_at: 100, reason: '' },
    }, 3700);
    assert.match(html, /could not check/);
    assert.doesNotMatch(html, /up to date/);
});

test('release: an available update names the newer tag', () => {
    const html = F.renderRelease({
        version: '0.7.1',
        update: {
            status: 'update_available', current_version: '0.7.1',
            latest_version: '0.8.0', checked_at: 100,
            upgrade_command: 'open https://example.test/releases/latest',
        },
    }, 3700);
    assert.match(html, /update available: 0\.8\.0/);
    assert.match(html, /open https:\/\/example\.test\/releases\/latest/);
    assert.doesNotMatch(html, /up to date/);
});

test('release: up to date is only claimed on a real comparison', () => {
    const html = F.renderRelease({
        version: '0.8.0',
        update: {
            status: 'current', current_version: '0.8.0',
            latest_version: '0.8.0', checked_at: 100,
        },
    }, 3700);
    assert.match(html, /up to date/);
    assert.match(html, /1h 0m ago/);
    assert.doesNotMatch(html, /could not check/);
});

test('release: a check with no timestamp says so rather than looking fresh', () => {
    const html = F.renderRelease({
        version: '0.8.0',
        update: { status: 'current', checked_at: null },
    });
    assert.match(html, /cannot determine: no check has been recorded/);
});

test('release: the running line shows the resolved version', () => {
    const html = F.renderRelease({
        version: '0.9.2',
        update: { status: 'current', checked_at: 1 },
    }, 2);
    assert.match(html, /0\.9\.2/);
});

test('a failed release fetch is reworded into this app\'s lowercase voice', () => {
    // The raw message is the fetch layer's wording - "Not Found" is
    // capitalised and, on its own, tells the user nothing about what was
    // not found.
    assert.match(panelSrc, /function releaseFailureReason/);
    assert.match(panelSrc, /this server has no version check yet/);
    assert.doesNotMatch(panelSrc, /error: \(err && err\.message\) \|\| 'the version check did not answer'/);
});

test('release offers a copyable command, never a one-click upgrade', () => {
    const html = F.renderRelease({
        version: '1.0.0',
        update: {
            status: 'current', checked_at: 1,
            upgrade_command: 'open https://example.test/releases/latest',
        },
    }, 2);
    assert.match(html, /<code class="server-status-command">/);
    assert.doesNotMatch(panelSrc, /upgradeNow|runUpgrade|performUpgrade/);
});

test('the release call targets the one endpoint the server actually serves', () => {
    // /server/release never existed. src/api/version_routes.py serves
    // /api/v1/version and nothing else answers this question.
    assert.match(apiSrc, /getReleaseStatus\(\)[\s\S]{0,200}this\.call\('\/version'\)/);
    assert.doesNotMatch(apiSrc, /'\/server\/release'/);
});

// ---------------------------------------------------------------------
// session rows and the kill control
// ---------------------------------------------------------------------

test('a hostile session name cannot break out of the row markup', () => {
    const html = F.renderSessionRow({
        name: '"><img src=x onerror=alert(1)>',
        working_dir: '/tmp/<script>', created_at_epoch: 1,
        pane_cols: 80, pane_rows: 24, created_by_cloude: false,
    }, 2);
    assert.doesNotMatch(html, /<img/);
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /&lt;img src=x/);
});

test('the row carries the literal name and id back on data attributes', () => {
    const html = F.renderSessionRow({
        name: 'test pause', session_id: 'adopted:test pause',
        attached_clients: 2, open_in_app: true, created_by_cloude: true,
        pane_cols: 80, pane_rows: 24, created_at_epoch: 1, working_dir: '/x',
    }, 2);
    assert.match(html, /data-kill-name="test pause"/);
    assert.match(html, /data-kill-id="adopted:test pause"/);
    assert.match(html, /data-kill-attached="2"/);
    assert.match(html, /data-kill-open="1"/);
    assert.match(html, /2 clients attached/);
    assert.match(html, /open in cloudecode/);
});

test('a session name never reaches anything executable', () => {
    // The precedent is terminal quick-commands: send an id, never text.
    for (const src of [panelSrc, read('client', 'js', 'server-status-format.js')]) {
        assert.doesNotMatch(src, /\beval\s*\(/);
        assert.doesNotMatch(src, /new Function\s*\(/);
        assert.doesNotMatch(src, /\.innerHTML\s*\+?=\s*[^;]*\$\{/);
        assert.doesNotMatch(src, /child_process|\.exec\(|spawnSync|execSync/);
    }
});

test('the panel reuses the shared destruction path, it does not add one', () => {
    assert.match(panelSrc, /actions\.perform\(/);
    assert.doesNotMatch(panelSrc, /API\.destroySession/);
    assert.doesNotMatch(panelSrc, /API\.destroyExternalSession/);
    assert.match(panelSrc, /SessionRowActions/);
});

test('the shared module still owns the two-endpoint branch', () => {
    assert.match(actionsSrc, /function perform\(tmuxName, sessionId\)/);
    assert.match(actionsSrc, /window\.API\.destroySession\(sessionId\)/);
    assert.match(actionsSrc, /window\.API\.destroyExternalSession\(tmuxName\)/);
});

test('the panel confirms before killing, and names the session', () => {
    assert.match(panelSrc, /actions\.confirm\(actions\.ACTION_CLOSE, name/);
    // The confirm must happen BEFORE the destructive call.
    assert.ok(panelSrc.indexOf('actions.confirm(') < panelSrc.indexOf('actions.perform('),
        'confirm must precede perform');
    assert.match(panelSrc, /if \(!confirmed\) return;/);
});

test('the confirmation says the transcript survives and names the uploads', () => {
    // Copy that misstates consequences is worse than no copy. This is the
    // shared table, unchanged, and it is true on both endpoint paths.
    // Comments are stripped first: the docblock quotes the forbidden
    // phrase as the example of what NOT to write.
    const code = actionsSrc.replace(/\/\*[\s\S]*?\*\//g, '');
    assert.match(code, /the transcript is not deleted and stays under/);
    assert.match(code, /\.cloude_uploads/);
    assert.doesNotMatch(code, /nothing on disk is touched/);
});

test('attachment facts are stated only when true, and lead the copy', () => {
    const win = {};
    // eslint-disable-next-line no-new-func
    new Function('window', 'console', actionsSrc)(win, { log() {} });
    const A = win.SessionRowActions;
    assert.equal(A.attachmentPreamble(null), '');
    assert.equal(A.attachmentPreamble({ openInApp: false, attachedClients: 0 }), '');
    assert.match(A.attachmentPreamble({ openInApp: true, attachedClients: 0 }),
        /open in cloudecode right now/);
    assert.match(A.attachmentPreamble({ openInApp: false, attachedClients: 1 }),
        /^1 tmux client is attached/);
    assert.match(A.attachmentPreamble({ openInApp: false, attachedClients: 3 }),
        /^3 tmux clients are attached/);
});

test('an existing call site passing no context gets its old copy back', () => {
    // launchpad.js and session-sidebar.js call confirm() with two args.
    // The preamble must be additive or this change silently rewrites the
    // dialog every other surface in the app shows.
    assert.match(actionsSrc, /function confirm\(action, displayName, context\)/);
    assert.match(actionsSrc, /if \(!context\) return '';/);
    assert.match(launchpadSrc, /SessionRowActions\.confirm\(resolved, display\)/);
});

// ---------------------------------------------------------------------
// menu wiring
// ---------------------------------------------------------------------

test('server status is an ENTRY_ID, not a rewrite of the menu', () => {
    assert.match(menuSrc, /'serverStatusRow'/);
    assert.match(menuSrc, /return \[statusRow\];/);
});

// The "restart server" row was WITHDRAWN together with
// POST /api/v1/server/reset: that endpoint spawned a reset.sh the packaged
// app has never shipped, so the row returned a 500 on every packaged
// install. See the removal note in src/api/routes.py for why it was
// withdrawn rather than shipped. Asserted as an absence here so the row
// cannot reappear without the supervisor-owned action it needs.
test('the menu carries no server-restart row', () => {
    assert.ok(!/restartServer/.test(menuSrc));
    assert.ok(!/'serverRestartRow'/.test(menuSrc));
    assert.ok(!/restart server, sessions keep running/.test(menuSrc));
    assert.ok(!/async restartServer\(\)/.test(launchpadSrc));
});

test('the panel and its stylesheet are both loaded, css after styles.css', () => {
    const base = indexHtml.indexOf('/static/css/styles.css');
    const own = indexHtml.indexOf('/static/css/server-status.css');
    assert.ok(base > -1 && own > -1, 'both stylesheets must be linked');
    assert.ok(base < own, 'server-status.css narrows rules styles.css owns');
    assert.ok(indexHtml.includes('/static/js/server-status-format.js'));
    assert.ok(indexHtml.includes('/static/js/server-status-panel.js'));
});

test('format loads before the panel that renders through it', () => {
    const a = indexHtml.indexOf('/static/js/server-status-format.js');
    const b = indexHtml.indexOf('/static/js/server-status-panel.js');
    assert.ok(a > -1 && b > -1 && a < b);
});

// ---------------------------------------------------------------------
// the home bar must not regress
// ---------------------------------------------------------------------

test('the home bar is still rendered exactly once, into the home screen', () => {
    const hits = launchpadSrc.match(/<div class="home-bar"/g) || [];
    assert.equal(hits.length, 1, 'exactly one .home-bar markup site');
    const terminalScreen = indexHtml.indexOf('id="terminal-screen"');
    assert.ok(!indexHtml.slice(terminalScreen).includes('class="home-bar"'),
        '.home-bar must never appear inside #terminal-screen');
});

test('#statusText and the version chip each exist exactly once', () => {
    const statusIds = (indexHtml.match(/id="statusText"/g) || []).length
        + (launchpadSrc.match(/id="statusText"/g) || []).length;
    assert.equal(statusIds, 1, 'exactly one #statusText node in the app');
    const chips = (launchpadSrc.match(/id="home-bar-version"/g) || []).length;
    assert.equal(chips, 1, 'exactly one version chip, in the bar');
});

// ---------------------------------------------------------------------
// flat shapes - no pills, and every button escapes the round reset
// ---------------------------------------------------------------------

test('no rule in this panel is a pill, a capsule or a circle', () => {
    for (const r of cssRules) {
        assert.doesNotMatch(r.body, /border-radius\s*:\s*(9999px|999px|50%)/,
            `${r.selector} must not be fully rounded`);
        assert.doesNotMatch(r.body, /radius-full|radius-pill/,
            `${r.selector} must not use a pill radius token`);
    }
});

test('every button class declares width, height AND border-radius', () => {
    // styles.css has a bare `button` rule setting all three. A class only
    // beats an element selector for the properties it actually declares.
    for (const selector of ['.server-status-kill', '.server-status-refresh']) {
        const body = ruleBody(cssRules, selector);
        assert.ok(body, `${selector} must have a rule`);
        for (const prop of ['width', 'height', 'border-radius']) {
            assert.match(body, new RegExp(`(^|;|\\s)${prop}\\s*:`),
                `${selector} must declare ${prop}`);
        }
    }
});

test('the hover guard outranks button:hover:not(:disabled)', () => {
    // button:hover:not(:disabled) is (0,2,1) and applies a scale plus an
    // outset glow. A bare .class:hover at (0,2,0) loses to it.
    for (const selector of ['.server-status-kill:hover:not(:disabled)',
        '.server-status-refresh:hover:not(:disabled)']) {
        const body = ruleBody(cssRules, selector);
        assert.ok(body, `${selector} must exist to beat the button reset`);
        assert.match(body, /transform\s*:\s*none/);
        assert.match(body, /box-shadow\s*:\s*none/);
    }
});

test('the phone breakpoint gives the kill control a full tap target', () => {
    const phone = css.slice(css.indexOf('@media (max-width: 520px)'));
    assert.ok(phone.length > 0, 'a narrow breakpoint must exist');
    assert.match(phone, /\.server-status-kill\s*\{[^}]*height:\s*44px/);
    assert.match(phone, /\.server-status-line\s*\{[^}]*grid-template-columns:\s*1fr/);
});

test('every user-facing string in the panel is lowercase', () => {
    const strings = [
        ...panelSrc.matchAll(/notify\(\s*'([^']+)'/g),
        ...menuSrc.matchAll(/'(server status|restart server)'/g),
    ].map((m) => m[1]);
    assert.ok(strings.length > 0, 'expected some UI copy to check');
    for (const s of strings) {
        assert.equal(s, s.toLowerCase(), `"${s}" must be lowercase`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
