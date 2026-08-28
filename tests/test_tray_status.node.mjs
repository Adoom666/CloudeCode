// Node test for macOS/tray-status.js and macOS/tray-api.js - the menu-bar
// status light.
//
// WHY THIS FILE EXISTS
//
// The tray icon is the only thing this app shows without being clicked, so it
// is the only place a problem can announce itself. It used to be one fixed
// glyph, identical whether the server was healthy, dead, or unreachable.
//
// The defect class being guarded here is NOT "the icon is the wrong picture".
// It is "the icon says everything is fine when nothing was actually
// measured". So the sharpest assertions below are the ones that pin the
// difference between:
//
//   * an empty session list  - the server WAS asked, and has nothing running
//   * an unreachable server  - the server was NOT asked, and we know nothing
//
// Both produce zero sessions needing attention. Exactly one of them is
// allowed to render as healthy. A test suite that only checked "no sessions
// need attention implies ok" would pass against the false green.
//
// The icon assets are asserted to EXIST on disk per state, because a state
// that resolves to a missing file is a state that silently renders as the
// previous icon, which is the same failure wearing a different hat.
//
// Run with: node tests/test_tray_status.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const macDir = path.join(repoRoot, 'macOS');
const assetsDir = path.join(macDir, 'assets');

const trayStatus = require(path.join(macDir, 'tray-status.js'));
const { TrayApiClient } = require(path.join(macDir, 'tray-api.js'));

let passed = 0;
const failures = [];

/**
 * Run one named assertion block, recording the outcome without aborting the
 * rest of the suite.
 *
 * @param {string} name - Description of the behaviour under test.
 * @param {() => (void|Promise<void>)} fn - Assertions to run.
 * @returns {Promise<void>} Resolves once the block has been scored.
 */
async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    failures.push({ name, error });
    console.log('  FAIL ' + name + ': ' + error.message);
  }
}

/**
 * Build a session row.
 *
 * @param {string} status - activity_status value.
 * @returns {{session_name: string, activity_status: string}} A row.
 */
function session(status) {
  return { session_name: 'sess-' + status, activity_status: status };
}

const RUNNING = { serverState: 'running', sessionsReachable: true, updateStatus: 'current' };

console.log('tray-status: the false green this feature exists to kill');

await test('an UNREACHABLE session list is unknown, never ok', () => {
  const derived = trayStatus.deriveTrayState({
    serverState: 'running',
    sessions: null,
    sessionsReachable: false,
    updateStatus: 'current',
  });
  assert.equal(
    derived.state,
    'unknown',
    'a server that could not be polled must NOT render as healthy'
  );
  assert.notEqual(derived.state, 'ok');
});

await test('an EMPTY session list is ok, never unknown', () => {
  const derived = trayStatus.deriveTrayState({ ...RUNNING, sessions: [] });
  assert.equal(
    derived.state,
    'ok',
    'the server was asked and genuinely has nothing running; that is a real ' +
      'measurement and must not be downgraded to cannot-determine'
  );
});

await test('empty-and-reachable and unreachable do NOT collapse together', () => {
  const measured = trayStatus.deriveTrayState({ ...RUNNING, sessions: [] });
  const unmeasured = trayStatus.deriveTrayState({
    serverState: 'running',
    sessions: null,
    sessionsReachable: false,
    updateStatus: 'current',
  });
  assert.notEqual(
    measured.state,
    unmeasured.state,
    'both have zero sessions needing attention; they must still differ'
  );
});

await test('a session reporting unknown status is unknown, not ok', () => {
  const derived = trayStatus.deriveTrayState({
    ...RUNNING,
    sessions: [session('idle'), session('unknown')],
  });
  assert.equal(derived.state, 'unknown');
  assert.equal(derived.unknownCount, 1);
});

console.log('tray-status: attention detection');

for (const status of ['question', 'finished_unread', 'dead']) {
  await test(`a "${status}" session raises the attention state`, () => {
    const derived = trayStatus.deriveTrayState({
      ...RUNNING,
      sessions: [session('working'), session(status)],
    });
    assert.equal(derived.state, 'attention');
    assert.equal(derived.attentionCount, 1);
  });
}

await test('working and idle sessions alone do not raise attention', () => {
  const derived = trayStatus.deriveTrayState({
    ...RUNNING,
    sessions: [session('working'), session('working_subagent'), session('idle')],
  });
  assert.equal(derived.state, 'ok');
  assert.equal(derived.attentionCount, 0);
});

await test('attention outranks an available update', () => {
  const derived = trayStatus.deriveTrayState({
    serverState: 'running',
    sessionsReachable: true,
    sessions: [session('question')],
    updateStatus: 'update_available',
  });
  assert.equal(derived.state, 'attention');
});

await test('an available update surfaces when nothing needs attention', () => {
  const derived = trayStatus.deriveTrayState({
    serverState: 'running',
    sessionsReachable: true,
    sessions: [session('idle')],
    updateStatus: 'update_available',
  });
  assert.equal(derived.state, 'update');
});

await test('an undeterminable UPDATE check does not pin the icon to unknown', () => {
  // Deliberate: the update check routinely cannot reach GitHub offline. A
  // warning that never clears is furniture. It is still reported in text.
  const derived = trayStatus.deriveTrayState({
    serverState: 'running',
    sessionsReachable: true,
    sessions: [session('idle')],
    updateStatus: null,
  });
  assert.equal(derived.state, 'ok');
  const signals = trayStatus.describeSignals({
    serverState: 'running',
    sessionsReachable: true,
    sessions: [session('idle')],
    updateStatus: null,
  });
  assert.equal(
    signals.update,
    'cannot determine',
    'the unknown must still be STATED even though it does not drive the icon'
  );
});

console.log('tray-status: server state precedence');

await test('a stop the user asked for is stopped, not crashed', () => {
  const derived = trayStatus.deriveTrayState({
    serverState: 'stopped',
    lastExitUnexpected: false,
  });
  assert.equal(derived.state, 'stopped');
});

await test('an exit nobody asked for is crashed, not stopped', () => {
  const derived = trayStatus.deriveTrayState({
    serverState: 'stopped',
    lastExitUnexpected: true,
  });
  assert.equal(
    derived.state,
    'crashed',
    'a server that died on its own must not look like one the user stopped'
  );
});

await test('starting is its own state', () => {
  const derived = trayStatus.deriveTrayState({ serverState: 'starting' });
  assert.equal(derived.state, 'starting');
});

await test('a non-running server reports sessions as cannot determine', () => {
  const signals = trayStatus.describeSignals({ serverState: 'stopped' });
  assert.ok(
    signals.sessions.includes('cannot determine'),
    'got: ' + signals.sessions
  );
});

console.log('tray-status: the tooltip states every signal');

await test('the tooltip names server, sessions and update on separate lines', () => {
  const tip = trayStatus.buildTooltip({
    serverState: 'running',
    sessionsReachable: false,
    sessions: null,
    updateStatus: null,
  });
  const lines = tip.split('\n');
  assert.ok(lines.some((l) => l.startsWith('Server:')), tip);
  assert.ok(lines.some((l) => l.startsWith('Sessions:')), tip);
  assert.ok(lines.some((l) => l.startsWith('Update:')), tip);
  assert.ok(
    tip.includes('cannot determine'),
    'an unmeasured signal must SAY so in the tooltip:\n' + tip
  );
});

console.log('tray-status: icon assets');

await test('EVERY state renders through the same non-template path', () => {
  // Measured, not assumed. Leaving "ok" on AppKit's template path while the
  // dotted states used the ordinary image path put the healthy state through
  // a different renderer, and the two disagreed on weight: in a real menu bar
  // the template glyph measured p90 luminance 70 and a full-opacity ordinary
  // glyph measured 166, so "stopped" came out BRIGHTER than "ok". A stopped
  // server looked healthy. One path for all states removes that by
  // construction.
  for (const state of trayStatus.TRAY_STATES) {
    const asset = trayStatus.resolveIconAsset(state, false, assetsDir);
    assert.equal(
      asset.isTemplate,
      false,
      state + ' must not be a template image; AppKit would discard its colour ' +
        'and it would render at a different weight than its siblings'
    );
    assert.ok(
      asset.path.includes(path.sep + 'tray' + path.sep),
      state + ' must resolve into the generated tray asset directory'
    );
  }
});

await test('light and dark menu bars resolve to DIFFERENT files', () => {
  const light = trayStatus.resolveIconAsset('attention', false, assetsDir);
  const dark = trayStatus.resolveIconAsset('attention', true, assetsDir);
  assert.notEqual(light.path, dark.path);
});

await test('every tray state resolves to a file that actually exists', () => {
  const missing = [];
  for (const state of trayStatus.TRAY_STATES) {
    for (const isDark of [false, true]) {
      const asset = trayStatus.resolveIconAsset(state, isDark, assetsDir);
      if (!fs.existsSync(asset.path)) missing.push(asset.path);
      // AppKit resolves the @2x twin by filename convention, so it must be
      // there too or the icon renders soft on every Retina display.
      const retina = asset.path.replace(/\.png$/, '@2x.png');
      if (!fs.existsSync(retina)) missing.push(retina);
    }
  }
  assert.deepEqual(missing, [], 'missing icon assets:\n' + missing.join('\n'));
});

await test('no two states share an identical icon file', () => {
  const seen = new Map();
  for (const state of trayStatus.TRAY_STATES) {
    const asset = trayStatus.resolveIconAsset(state, true, assetsDir);
    const bytes = fs.readFileSync(asset.path).toString('base64');
    if (seen.has(bytes)) {
      throw new Error(
        `${state} renders identically to ${seen.get(bytes)}; the states are ` +
          'nominally different but visually the same'
      );
    }
    seen.set(bytes, state);
  }
});

console.log('tray-api: authentication and three outcomes');

/**
 * Build a fetch stub that replays scripted responses and records requests.
 *
 * @param {Array<{status: number, body: object}>} script - Responses in order.
 * @returns {{fetchImpl: Function, calls: Array<object>}} Stub plus record.
 */
function makeFetch(script) {
  const calls = [];
  let index = 0;
  const fetchImpl = async (url, options) => {
    const opts = options || {};
    calls.push({
      url,
      method: opts.method || 'GET',
      auth: (opts.headers || {}).Authorization || null,
      body: opts.body ? JSON.parse(opts.body) : null,
    });
    const next = script[Math.min(index, script.length - 1)];
    index += 1;
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      json: async () => next.body,
    };
  };
  return { fetchImpl, calls };
}

await test('the tray authenticates by spending a TOTP code for a token', async () => {
  const { fetchImpl, calls } = makeFetch([
    { status: 200, body: { access_token: 'AT', refresh_token: 'RT', expires_in: 14400 } },
    { status: 200, body: [session('question')] },
  ]);
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => '123456',
    fetchImpl,
  });

  const result = await client.fetchSessions();
  assert.equal(result.reachable, true);
  assert.equal(result.sessions.length, 1);

  assert.ok(calls[0].url.endsWith('/api/v1/auth/verify'), calls[0].url);
  assert.deepEqual(calls[0].body, { code: '123456' });
  assert.equal(calls[1].auth, 'Bearer AT', 'the session call was not authenticated');
});

await test('a cached token is reused instead of spending another TOTP code', async () => {
  const { fetchImpl, calls } = makeFetch([
    { status: 200, body: { access_token: 'AT', refresh_token: 'RT', expires_in: 14400 } },
    { status: 200, body: [] },
    { status: 200, body: [] },
  ]);
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => '123456',
    fetchImpl,
  });

  await client.fetchSessions();
  await client.fetchSessions();

  const verifies = calls.filter((c) => c.url.endsWith('/auth/verify'));
  assert.equal(
    verifies.length,
    1,
    'TOTP codes are single use and shared with his browser login; polling ' +
      'must not burn one per poll'
  );
});

await test('no TOTP secret is CANNOT DETERMINE, not an empty session list', async () => {
  const { fetchImpl } = makeFetch([{ status: 200, body: [] }]);
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => null,
    fetchImpl,
  });

  const result = await client.fetchSessions();
  assert.equal(result.reachable, false);
  assert.equal(result.sessions, null, 'must not fabricate an empty list');
  assert.ok(result.error, 'a failure must carry a reason');
});

await test('a rejected TOTP code reports unreachable rather than healthy', async () => {
  const { fetchImpl } = makeFetch([{ status: 401, body: { detail: 'bad code' } }]);
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => '000000',
    fetchImpl,
  });

  const result = await client.fetchSessions();
  assert.equal(result.reachable, false);
  assert.equal(result.sessions, null);
});

await test('a transport failure is unreachable, never an empty list', async () => {
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => '123456',
    fetchImpl: async () => {
      throw new Error('ECONNREFUSED');
    },
  });

  const result = await client.fetchSessions();
  assert.equal(result.reachable, false);
  assert.equal(result.sessions, null);
  assert.ok(String(result.error).includes('ECONNREFUSED'), result.error);
});

await test('changing the bind address discards the cached token', async () => {
  const { fetchImpl, calls } = makeFetch([
    { status: 200, body: { access_token: 'AT', refresh_token: 'RT', expires_in: 14400 } },
    { status: 200, body: [] },
    { status: 200, body: { access_token: 'AT2', refresh_token: 'RT2', expires_in: 14400 } },
    { status: 200, body: [] },
  ]);
  const client = new TrayApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getOtp: () => '123456',
    fetchImpl,
  });

  await client.fetchSessions();
  client.setBaseUrl('http://10.0.1.86:8000');
  await client.fetchSessions();

  const verifies = calls.filter((c) => c.url.endsWith('/auth/verify'));
  assert.equal(verifies.length, 2, 'a token minted for one origin was replayed at another');
});

await test('the update verdict passes through all three of its states', async () => {
  for (const status of ['current', 'update_available', 'unknown']) {
    const { fetchImpl } = makeFetch([
      { status: 200, body: { access_token: 'AT', refresh_token: 'RT', expires_in: 14400 } },
      { status: 200, body: { version: '0.8.2', update: { status } } },
    ]);
    const client = new TrayApiClient({
      baseUrl: 'http://127.0.0.1:8000',
      getOtp: () => '123456',
      fetchImpl,
    });
    const result = await client.fetchUpdateStatus();
    assert.equal(result.reachable, true);
    assert.equal(result.status, status);
  }
});

console.log('');
console.log('tray-status: ' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length > 0 ? 1 : 0);
