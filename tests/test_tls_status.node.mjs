// Node test for macOS/tls-status.js - the secure/insecure menu indicator.
//
// WHY THIS FILE EXISTS
//
// The naive way to answer "am I on a secure connection" is to show a padlock
// when the URL starts with https. That reports the SCHEME, which the app
// typed itself, not the CONNECTION, which has to be measured. A padlock
// nobody verified is worse than no padlock, because it is acted on.
//
// The specific defect being guarded here is a certificate for the WRONG
// NAME. A check that only parses notAfter reports a healthy number of days
// remaining while every browser rejects the connection outright, so the
// monitor says fine forever on a site that is broken today. Name matching
// therefore runs BEFORE expiry, SAN is authoritative, CN is a fallback only
// when no SAN exists, and wildcards match exactly one label.
//
// Three outcomes: secure, insecure, and cannot-determine. The third is never
// a padlock and never an all-clear.
//
// Run with: node tests/test_tls_status.node.mjs

import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const tls = require(path.join(repoRoot, 'macOS', 'tls-status.js'));

let passed = 0;
const failures = [];

/**
 * Run one named assertion block.
 *
 * @param {string} name - Description of the behaviour.
 * @param {() => void} fn - Assertions.
 * @returns {void}
 */
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    failures.push({ name, error });
    console.log('  FAIL ' + name + ': ' + error.message);
  }
}

const NOW = Date.UTC(2026, 7, 20);
const DAY = 86400000;

/**
 * Build a certificate description.
 *
 * @param {Array<string>} sans - Subject alternative names.
 * @param {number} daysLeft - Days until expiry; negative for expired.
 * @param {string} [commonName] - Optional CN.
 * @returns {object} Certificate identity plus expiry.
 */
function cert(sans, daysLeft, commonName) {
  return {
    subjectAltNames: sans,
    commonName: commonName || null,
    notAfter: NOW + daysLeft * DAY,
  };
}

console.log('tls-status: plaintext is never secure');

test('a plain HTTP binding is insecure, not unknown', () => {
  const r = tls.evaluateBinding({ url: 'http://10.0.1.86:8000', now: NOW });
  assert.equal(r.level, tls.LEVEL_INSECURE);
  assert.ok(!/secure \(HTTPS\)/.test(r.label), r.label);
});

test('the insecure detail names what is actually at risk', () => {
  const r = tls.evaluateBinding({ url: 'http://10.0.1.86:8000', now: NOW });
  assert.ok(/unencrypted/i.test(r.detail), r.detail);
});

test('an https URL alone does NOT earn a padlock', () => {
  const r = tls.evaluateBinding({ url: 'https://example.ts.net', now: NOW });
  assert.equal(
    r.level,
    tls.LEVEL_UNKNOWN,
    'the scheme is a claim the app made; with no certificate observed the ' +
      'verdict must be cannot-determine, never secure'
  );
});

console.log('tls-status: identity is checked BEFORE expiry');

test('a perfectly in-date certificate for the WRONG name is insecure', () => {
  const r = tls.evaluateBinding({
    url: 'https://joe-mbp-m1.taild90287.ts.net',
    cert: cert(['nix2.infinitemediacorp.com'], 80),
    now: NOW,
  });
  assert.equal(
    r.level,
    tls.LEVEL_INSECURE,
    'an expiry-only check would call this healthy for 80 more days'
  );
  assert.ok(/wrong certificate/i.test(r.label), r.label);
});

test('the wrong-name detail names the certificate it actually got', () => {
  const r = tls.evaluateBinding({
    url: 'https://a.example.com',
    cert: cert(['b.example.com'], 40),
    now: NOW,
  });
  assert.ok(r.detail.includes('b.example.com'), r.detail);
});

test('a matching name and a live date is secure', () => {
  const r = tls.evaluateBinding({
    url: 'https://joe-mbp-m1.taild90287.ts.net',
    cert: cert(['joe-mbp-m1.taild90287.ts.net'], 60),
    now: NOW,
  });
  assert.equal(r.level, tls.LEVEL_SECURE);
  assert.ok(/60 days/.test(r.detail), r.detail);
});

test('an expired certificate for the RIGHT name is insecure', () => {
  const r = tls.evaluateBinding({
    url: 'https://host.ts.net',
    cert: cert(['host.ts.net'], -1),
    now: NOW,
  });
  assert.equal(r.level, tls.LEVEL_INSECURE);
  assert.ok(/expired/i.test(r.label), r.label);
});

console.log('tls-status: SAN beats CN, and wildcards match one label');

test('CN is ignored when a SAN is present, as browsers do', () => {
  const r = tls.evaluateBinding({
    url: 'https://a.example.com',
    cert: cert(['b.example.com'], 30, 'a.example.com'),
    now: NOW,
  });
  assert.equal(
    r.level,
    tls.LEVEL_INSECURE,
    'the CN matches but a SAN exists, so the CN must not be consulted'
  );
});

test('CN is the fallback when the certificate carries NO SAN', () => {
  const r = tls.evaluateBinding({
    url: 'https://a.example.com',
    cert: cert([], 30, 'a.example.com'),
    now: NOW,
  });
  assert.equal(r.level, tls.LEVEL_SECURE);
});

test('a wildcard covers exactly one label', () => {
  assert.equal(tls.nameMatches('a.example.com', '*.example.com'), true);
  assert.equal(tls.nameMatches('a.b.example.com', '*.example.com'), false);
  assert.equal(tls.nameMatches('example.com', '*.example.com'), false);
});

test('name matching is case insensitive and tolerates a trailing dot', () => {
  assert.equal(tls.nameMatches('HOST.TS.NET', 'host.ts.net.'), true);
});

console.log('tls-status: the third outcome');

test('a bare IP binding is cannot-determine, not a mismatch', () => {
  const r = tls.evaluateBinding({
    url: 'https://10.0.1.86:8000',
    cert: cert(['whatever.ts.net'], 30),
    now: NOW,
  });
  assert.equal(
    r.level,
    tls.LEVEL_UNKNOWN,
    'no certificate can carry a private IP identity; false-CRITing on it ' +
      'would be a warning that never clears'
  );
});

test('a certificate probe error is cannot-determine, never secure', () => {
  const r = tls.evaluateBinding({
    url: 'https://host.ts.net',
    certError: 'connection reset',
    now: NOW,
  });
  assert.equal(r.level, tls.LEVEL_UNKNOWN);
  assert.ok(r.detail.includes('connection reset'), r.detail);
});

test('an unreadable expiry is cannot-determine, not secure', () => {
  const r = tls.evaluateBinding({
    url: 'https://host.ts.net',
    cert: { subjectAltNames: ['host.ts.net'], notAfter: NaN },
    now: NOW,
  });
  assert.equal(r.level, tls.LEVEL_UNKNOWN);
});

test('an unparseable URL is cannot-determine, not secure', () => {
  const r = tls.evaluateBinding({ url: 'not a url', now: NOW });
  assert.equal(r.level, tls.LEVEL_UNKNOWN);
});

test('every verdict carries a detail sentence saying what was measured', () => {
  const cases = [
    { url: 'http://10.0.1.86:8000' },
    { url: 'https://host.ts.net' },
    { url: 'https://host.ts.net', cert: cert(['other.ts.net'], 10) },
    { url: 'https://host.ts.net', cert: cert(['host.ts.net'], 10) },
    { url: 'nonsense' },
  ];
  for (const input of cases) {
    const r = tls.evaluateBinding({ ...input, now: NOW });
    assert.ok(r.detail && r.detail.length > 20, JSON.stringify(r));
    assert.ok(
      [tls.LEVEL_SECURE, tls.LEVEL_INSECURE, tls.LEVEL_UNKNOWN].includes(r.level),
      'invented a fourth verdict: ' + r.level
    );
  }
});

console.log('');
console.log('tls-status: ' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length > 0 ? 1 : 0);
