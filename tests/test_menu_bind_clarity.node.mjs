// Node test for the menu rows a first-run user reads before anything else:
// what address the server is on, and whether setup is finished.
//
// WHY THIS FILE EXISTS
//
// The owner installed a fresh packaged build and the menu told him he was
// "hosting on 0.0.0.0". He was not. A slow bootstrap meant the server had
// evaluated setup as incomplete at startup, the lockdown pinned uvicorn to
// 127.0.0.1, and uvicorn bound once and never rebound. The menu row was
// reading getBindHost() - CONFIGURATION - and printing it as though it were
// a measurement. Nothing errored, and the address it printed was a perfectly
// plausible one.
//
// THE DEFECT IS NOT "THE ROW SHOWED THE WRONG NUMBER". It is that the row
// could show a number at all when nothing had been measured. That is the
// three-outcome rule's worst collapse: an unmeasurable thing rendered as a
// confident answer. So the assertion that carries the weight below is the
// mechanical one - when the effective bind is unknown, the row's label
// contains NO address whatsoever. A test asserting the label "mentions
// setup" would prove nothing about whether a human understands it; this one
// pins the actual defect.
//
// He also said the existing setup warning was "not self explanitory". A
// status is not an instruction, so the setup row is asserted to name an
// ACTION, in words somebody who has never seen this app can follow.
//
// Run with: node tests/test_menu_bind_clarity.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const macDir = path.join(repoRoot, 'macOS');

const trayStatus = require(path.join(macDir, 'tray-status.js'));

let passed = 0;
const failures = [];
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (err) {
    failures.push(name);
    console.log('  FAIL ' + name + '\n       ' + err.message);
  }
}

// An address in any of the shapes this app can produce. Used to prove a
// label carries none, which is stronger than checking for one known string.
const ANY_ADDRESS = /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/;

console.log('bind row');

test('a measured bind matching configuration is stated plainly', () => {
  const row = trayStatus.describeBind({
    effectiveHost: '0.0.0.0',
    configuredHost: '0.0.0.0',
  });
  assert.equal(row.known, true);
  assert.equal(row.applied, true);
  assert.match(row.label, /0\.0\.0\.0/);
});

test('an UNKNOWN bind puts no address in the row at all', () => {
  // The defect, stated as an invariant. Anything that reintroduces a
  // fallback to configuration fails here regardless of how it is spelled.
  const row = trayStatus.describeBind({
    effectiveHost: null,
    configuredHost: '0.0.0.0',
  });
  assert.equal(row.known, false);
  assert.ok(
    !ANY_ADDRESS.test(row.label),
    'the row displayed an address while the effective bind was unknown: ' +
      JSON.stringify(row.label)
  );
  assert.ok(
    !row.label.includes('0.0.0.0'),
    'the configured value reached the row: ' + JSON.stringify(row.label)
  );
});

test('an unknown bind says it is unknown rather than leaving a blank', () => {
  const row = trayStatus.describeBind({
    effectiveHost: null,
    configuredHost: '192.168.1.50',
  });
  assert.match(row.label, /unknown|not measured/i);
  // The configured value is still worth knowing, but only in the detail and
  // only labelled as a request rather than as the address in force.
  assert.match(row.detail, /192\.168\.1\.50/);
  assert.match(row.detail, /configur/i);
});

test('an empty string is unknown, not an address', () => {
  const row = trayStatus.describeBind({
    effectiveHost: '',
    configuredHost: '0.0.0.0',
  });
  assert.equal(row.known, false);
  assert.ok(!ANY_ADDRESS.test(row.label));
});

test('a bind that differs from configuration names the restart', () => {
  // The owner's exact case: bound to loopback, configured for 0.0.0.0. The
  // menu must lead with what is TRUE and then say what to do about it.
  const row = trayStatus.describeBind({
    effectiveHost: '127.0.0.1',
    configuredHost: '0.0.0.0',
  });
  assert.equal(row.known, true);
  assert.equal(row.applied, false);
  assert.match(row.label, /127\.0\.0\.1/);
  assert.match(row.label, /restart/i);
  assert.ok(
    row.label.indexOf('127.0.0.1') < row.label.indexOf('0.0.0.0'),
    'the configured address is stated before the one in force, which is the ' +
      'reading order that caused the confusion in the first place'
  );
});

console.log('');
console.log('setup row');

test('an unfinished setup names the action, not just the status', () => {
  const label = trayStatus.describeSetupRow('incomplete');
  assert.match(label, /setup/i);
  // "Setup incomplete" is a status. He said the existing warning was not
  // self-explanatory, so the row has to say what to DO and what happens next.
  assert.match(label, /open|finish/i);
  assert.match(label, /restart/i);
});

test('an undetermined setup does not claim to be incomplete', () => {
  const label = trayStatus.describeSetupRow('undetermined');
  assert.match(label, /unknown|could not/i);
  assert.ok(
    !/not finished/i.test(label),
    '"could not determine" was rendered as a definite verdict'
  );
});

test('a finished setup is an ordinary row with no alarm words', () => {
  const label = trayStatus.describeSetupRow('complete');
  assert.ok(!/not finished|unknown/i.test(label), label);
  assert.match(label, /setup/i);
});

test('an unpolled setup is not rendered as finished', () => {
  const label = trayStatus.describeSetupRow(null);
  assert.notEqual(label, trayStatus.describeSetupRow('complete'));
});

console.log('');
console.log('menu construction (SOURCE-LEVEL - main.js cannot run outside');
console.log('Electron, so these read the file rather than execute it)');

const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
const managerSrc = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');

test('the bind row is built from describeBind, not from getBindHost', () => {
  const fn = mainSrc.slice(
    mainSrc.indexOf('function buildBindAndUrlItems()'),
    mainSrc.indexOf('async function handleBindChange(')
  );
  assert.ok(fn.length > 200, 'could not locate buildBindAndUrlItems');
  assert.match(fn, /describeBind\(/);
  assert.ok(
    !/label:\s*`Bind IP:\s*\$\{bindHost\}`/.test(fn),
    'the row still prints the CONFIGURED bind host as though it were the ' +
      'address in force'
  );
});

test('the copy-URL row is built from the MEASURED bind only', () => {
  const fn = mainSrc.slice(
    mainSrc.indexOf('function buildBindAndUrlItems()'),
    mainSrc.indexOf('async function handleBindChange(')
  );
  assert.match(fn, /getMeasuredUrl\(\)/);
  assert.ok(
    !/`Copy URL:\s*\$\{publishedUrl\}`/.test(fn),
    'Copy URL is built from the best-effort navigation URL, which falls ' +
      'back to configuration and can hand him a dead address to paste'
  );
});

test('getMeasuredUrl refuses to fall back to the configured host', () => {
  const marker = '\n  getMeasuredUrl() {';
  const start = managerSrc.indexOf(marker);
  assert.notEqual(start, -1, 'getMeasuredUrl is not defined as a method');
  const fn = managerSrc.slice(start, start + 900);
  assert.ok(
    !/getBindHost\(\)/.test(fn),
    'getMeasuredUrl reaches for configuration, which is the fallback the ' +
      'whole change removes'
  );
  assert.match(fn, /getEffectiveBindHost\(\)/);
});

console.log('');
console.log('menu-bind-clarity: ' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length > 0 ? 1 : 0);
