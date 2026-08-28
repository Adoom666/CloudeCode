// Node test for ServerManager.getPublishedUrl() under the setup lockdown.
//
// WHY THIS FILE EXISTS
//
// A menu item opened a page and the browser showed a connection error, on a
// server that was perfectly healthy. The mechanism:
//
//   - A FRESH install has not finished setup, so src/core/setup_state.py
//     pins the bind to 127.0.0.1 no matter what HOST says. That is the
//     lockdown, and it is the state every new user is in.
//   - getPublishedUrl() preferred the MEASURED bind and fell back to the
//     CONFIGURED one. Configured is 0.0.0.0, and the 0.0.0.0 branch returned
//     the primary LAN address.
//   - So whenever the bind had not been measured yet - reportedBind is null
//     because the server was adopted rather than spawned, or its ready line
//     had not been parsed - the menu handed the browser a LAN URL while the
//     server was listening only on loopback.
//
// Measured against a server in the lockdown shape:
//     http://127.0.0.1:PORT/setup  -> 200
//     http://<lan-ip>:PORT/setup   -> connection refused
//
// THE THREE OUTCOMES. Measured-as-0.0.0.0 is a real fact and the LAN address
// is genuinely right (it is what the user shares with a phone). Unmeasured is
// NOT that fact - it is the absence of it, and collapsing the two is how an
// aspiration got printed as a measurement. Loopback is correct under both,
// so the unmeasured case resolves there rather than to a guess.
//
// Run with: node tests/test_published_url_lockdown.node.mjs

import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');

let passed = 0;
let failed = 0;

/**
 * Run one named assertion, reporting rather than throwing.
 *
 * @param {string} name - What is being asserted.
 * @param {Function} fn - The assertion body.
 * @returns {void}
 */
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`  FAIL ${name}`);
    console.log(`       ${err.message}`);
  }
}

const { resolvePublishedUrl } = require(path.join(repoRoot, 'macOS', 'published-url.js'));

/**
 * Build the input object resolvePublishedUrl consults.
 *
 * @param {object} opts - {port, configured, measured, lan}
 * @returns {object} Arguments for resolvePublishedUrl.
 */
function stub({ port = 8000, configured = '0.0.0.0', measured = null, lan = '10.0.1.86' }) {
  return { port, configuredHost: configured, measuredHost: measured, lanIp: lan };
}

console.log('getPublishedUrl under the setup lockdown');

test('UNMEASURED bind resolves to loopback, not a LAN address nothing may be listening on', () => {
  const url = resolvePublishedUrl(stub({ measured: null, configured: '0.0.0.0' }));
  assert.equal(
    url,
    'http://127.0.0.1:8000',
    `an unmeasured bind produced ${url}. During the setup lockdown - the ` +
    `state every fresh install is in - the server listens ONLY on loopback, ` +
    `so this URL is connection-refused and the menu item opens a browser ` +
    `error page on a healthy server.`
  );
});

test('MEASURED 0.0.0.0 still yields the LAN address, which is the point of measuring', () => {
  const url = resolvePublishedUrl(stub({ measured: '0.0.0.0', configured: '0.0.0.0' }));
  assert.equal(
    url,
    'http://10.0.1.86:8000',
    `a measured 0.0.0.0 bind produced ${url}; the LAN address is real here ` +
    `and is what the user shares with another device`
  );
});

test('MEASURED loopback (the lockdown, once reported) yields loopback', () => {
  const url = resolvePublishedUrl(stub({ measured: '127.0.0.1', configured: '0.0.0.0' }));
  assert.equal(url, 'http://127.0.0.1:8000');
});

test('a measured specific LAN bind is used verbatim', () => {
  const url = resolvePublishedUrl(stub({ measured: '192.168.1.250', configured: '192.168.1.250' }));
  assert.equal(url, 'http://192.168.1.250:8000');
});

test('no LAN interface plus a measured 0.0.0.0 still yields loopback', () => {
  const url = resolvePublishedUrl(stub({ measured: '0.0.0.0', lan: null }));
  assert.equal(url, 'http://127.0.0.1:8000');
});

test('an undeterminable port yields null rather than a guessed-port URL', () => {
  const s = stub({});
  s.port = null;
  assert.equal(resolvePublishedUrl(s), null);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
