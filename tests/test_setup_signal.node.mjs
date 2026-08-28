// Node test for the setup signal in macOS/tray-status.js and the menu-bar
// plumbing that reads it.
//
// WHY THIS FILE EXISTS
//
// Two claims are being made by the menu bar once the setup wizard exists, and
// both are the kind that look right while being wrong:
//
//   1. "this instance needs setting up"  - which must reuse the existing
//      attention signal rather than invent a parallel one, because two
//      notions of "needs work" drift and the one nobody watches is the one
//      that rots.
//   2. "the server is at this address"   - which during the setup lockdown is
//      NOT the configured address. A menu that reads configuration and prints
//      it is printing an aspiration.
//
// The assertions that carry weight are the ones about the third outcome. A
// setup status of null means the server has not been asked; treating that as
// 'complete' would render an unmeasured instance as healthy, which is this
// project's signature failure mode.
//
// Run with: node tests/test_setup_signal.node.mjs

import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const macDir = path.join(repoRoot, 'macOS');

const trayStatus = require(path.join(macDir, 'tray-status.js'));

let passed = 0;
let failed = 0;

/**
 * Run one named assertion, reporting rather than throwing.
 *
 * @param {string} name - What is being asserted.
 * @param {Function} body - The assertion body.
 * @returns {void}
 */
function test(name, body) {
  try {
    body();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`  FAIL ${name}`);
    console.log(`       ${err.message}`);
  }
}

/**
 * A healthy running instance, which each test then perturbs one field of.
 *
 * @param {object} [overrides] - Fields to replace.
 * @returns {object} A deriveTrayState input.
 */
function runningInput(overrides) {
  return Object.assign(
    {
      serverState: 'running',
      sessions: [],
      sessionsReachable: true,
      updateStatus: 'current',
      setupStatus: 'complete',
    },
    overrides || {}
  );
}

console.log('setup signal');

// --- the signal reuses the existing attention state ------------------------

test('an unfinished setup raises the SAME attention state the icon uses', () => {
  const derived = trayStatus.deriveTrayState(runningInput({ setupStatus: 'incomplete' }));
  assert.equal(derived.state, 'attention');
  assert.match(derived.reason, /setup is not finished/);
});

test('the attention state is one of the states the icon table already covers', () => {
  // If setup had invented a new state name, no icon asset would back it and
  // the tray would silently keep the previous picture.
  assert.ok(trayStatus.TRAY_STATES.includes('attention'));
});

test('an undetermined setup is attention, and says it could not be determined', () => {
  const derived = trayStatus.deriveTrayState(
    runningInput({ setupStatus: 'undetermined' })
  );
  assert.equal(derived.state, 'attention');
  assert.match(derived.reason, /could not be determined/);
});

test('a complete setup does not raise attention on its own', () => {
  assert.equal(trayStatus.deriveTrayState(runningInput()).state, 'ok');
});

test('setup outranks a session needing attention', () => {
  const derived = trayStatus.deriveTrayState(
    runningInput({
      setupStatus: 'incomplete',
      sessions: [{ activity_status: 'question' }],
    })
  );
  assert.equal(derived.state, 'attention');
  assert.match(derived.reason, /setup/);
});

test('setup does not override a stopped or crashed server', () => {
  // A server that is not running cannot have been asked about its setup, so
  // claiming a setup verdict over a dead server would be inventing one.
  const stopped = trayStatus.deriveTrayState({
    serverState: 'stopped',
    setupStatus: 'incomplete',
  });
  assert.equal(stopped.state, 'stopped');

  const crashed = trayStatus.deriveTrayState({
    serverState: 'stopped',
    lastExitUnexpected: true,
    setupStatus: 'incomplete',
  });
  assert.equal(crashed.state, 'crashed');
});

// --- the third outcome -----------------------------------------------------

test('an unpolled setup status is NOT treated as complete', () => {
  // The one that matters. If null read as complete, a menu bar that had never
  // successfully reached the server would show a calm icon.
  const derived = trayStatus.deriveTrayState(runningInput({ setupStatus: null }));
  const signals = trayStatus.describeSignals(runningInput({ setupStatus: null }));
  assert.notEqual(signals.setup, 'complete');
  assert.match(signals.setup, /cannot determine/);
  // It does not raise attention either - an unmeasured thing is not an alarm.
  assert.equal(derived.state, 'ok');
});

test('every setup verdict has its own distinct phrase', () => {
  const phrases = ['complete', 'incomplete', 'undetermined', null].map(
    (status) => trayStatus.describeSignals(runningInput({ setupStatus: status })).setup
  );
  assert.equal(new Set(phrases).size, 4, `phrases collapsed: ${phrases}`);
});

test('not-polled and undetermined are distinguishable, not both "unknown"', () => {
  const notPolled = trayStatus.describeSignals(runningInput({ setupStatus: null })).setup;
  const undetermined = trayStatus.describeSignals(
    runningInput({ setupStatus: 'undetermined' })
  ).setup;
  assert.notEqual(notPolled, undetermined);
});

test('the tooltip names the setup signal', () => {
  const tooltip = trayStatus.buildTooltip(runningInput({ setupStatus: 'incomplete' }));
  assert.match(tooltip, /Setup: not finished/);
});

// --- the menu-bar source, read as text -------------------------------------
//
// These are structural assertions on main.js and server-manager.js. They
// cannot run the Electron app, so they check the specific mistakes that would
// reintroduce a stale address or a duplicated attention notion, and they say
// plainly that that is all they check.

import fs from 'node:fs';

const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
const managerSrc = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');

console.log('menu-bar wiring (source-level)');

test('the old config dialog is gone', () => {
  assert.ok(
    !/label:\s*'Check Config for New Defaults\.\.\.'/.test(mainSrc),
    'the dialog menu item is still registered'
  );
});

test('the wizard is opened through shell.openExternal, like Open in Browser', () => {
  assert.match(mainSrc, /shell\.openExternal\(`\$\{base\}\/setup`\)/);
});

test('the menu label derives its marker from deriveTrayState, not its own check', () => {
  const fn = mainSrc.slice(
    mainSrc.indexOf('function setupMenuLabel()'),
    mainSrc.indexOf('function openSetupWizard()')
  );
  assert.match(fn, /trayStatus\.deriveTrayState\(currentTrayInput\(\)\)/);
  assert.match(fn, /\(!\)/);
});

test('the tray input carries the setup status through to the icon', () => {
  const fn = mainSrc.slice(
    mainSrc.indexOf('function currentTrayInput()'),
    mainSrc.indexOf('function currentTrayInput()') + 1200
  );
  // Via getSetupVerdict(), which prefers the SERVER's answer and falls back
  // to a local reading of the same facts only when there is no server to
  // ask. This used to call getSetupStatus() directly, which was correct
  // until a second, private notion of "configured" turned out to be driving
  // the row the user actually saw - see macOS/setup-verdict.js.
  assert.match(fn, /setupStatus:\s*serverManager\s*\?\s*serverManager\.getSetupVerdict\(\)\.status/);
});

test('the icon and the setup ROW read the same verdict, never two', () => {
  // The whole defect was two answers to one question, with the wrong one on
  // screen. Neither path may reach getSetupStatus() directly any more.
  const label = mainSrc.slice(
    mainSrc.indexOf('function setupMenuLabel()'),
    mainSrc.indexOf('function openSetupWizard()')
  );
  assert.match(label, /getSetupVerdict\(\)\.status/);
  const menu = mainSrc.slice(
    mainSrc.indexOf('function currentTrayInput()'),
    mainSrc.indexOf('function openSetupWizard()')
  );
  assert.ok(
    !/serverManager\.getSetupStatus\(\)/.test(menu),
    'a menu path still reads the raw server status instead of the verdict'
  );
});

test('the published URL prefers the MEASURED bind over the configured one', () => {
  // THE RULE MOVED, AND THIS ASSERTION GOT STRONGER FOR IT. It used to
  // regex the SOURCE TEXT of getPublishedUrl for
  // `this.getEffectiveBindHost() || this.getBindHost()`. That is a markup
  // assertion about a behaviour - it passes on any source that contains
  // the string and fails on any refactor that preserves the behaviour,
  // which is exactly what happened.
  //
  // The rule now lives in macOS/published-url.js, a pure module with no
  // imports (server-manager.js requires axios and electron, so the rule
  // was untestable where it was). So assert the BEHAVIOUR instead: the
  // measured host wins over the configured one whenever it exists.
  const { resolvePublishedUrl } = require(path.join(macDir, 'published-url.js'));
  assert.equal(
    resolvePublishedUrl({
      port: 8000,
      configuredHost: '0.0.0.0',
      measuredHost: '127.0.0.1',
      lanIp: '10.0.1.86',
    }),
    'http://127.0.0.1:8000',
    'the configured host beat the measured one, so the menu would print an ' +
    'aspiration rather than where the server actually is'
  );
  // And server-manager must still be delegating to that module rather than
  // keeping a second copy of the rule that could drift from it.
  assert.match(managerSrc, /resolvePublishedUrl\(\{/);
});

test('the effective bind never falls back to the configured value', () => {
  // Anchor on the METHOD DEFINITION, not the first textual occurrence of the
  // name: the name also appears inside a comment in getPublishedUrl, and
  // slicing from there measured the wrong function entirely. A source-reading
  // assertion that quietly reads a different region than intended is the same
  // class of defect the rest of this suite is about.
  const marker = '\n  getEffectiveBindHost() {';
  const start = managerSrc.indexOf(marker);
  assert.notEqual(start, -1, 'getEffectiveBindHost is not defined as a method');
  const fn = managerSrc.slice(start, start + 300);
  assert.ok(
    !/getBindHost\(\)/.test(fn),
    'getEffectiveBindHost falls back to configuration, which turns an ' +
      'aspiration into a reported measurement'
  );
});

test('the health probe also tries loopback, so the lockdown cannot look dead', () => {
  assert.match(managerSrc, /probeHostCandidates\(\)/);
  const fn = managerSrc.slice(
    managerSrc.indexOf('probeHostCandidates()'),
    managerSrc.indexOf('probeHostCandidates()') + 700
  );
  assert.match(fn, /127\.0\.0\.1/);
});

test('a failed exposure read clears the cached bind rather than keeping it', () => {
  const start = managerSrc.indexOf('async getHealth()');
  const end = managerSrc.indexOf('\n  getEffectiveBindHost() {');
  assert.ok(start !== -1 && end > start, 'could not locate getHealth');
  const fn = managerSrc.slice(start, end);
  assert.match(fn, /this\.reportedBind = null;/);
  assert.match(fn, /this\.reportedSetupStatus = null;/);
});

// --- the wizard's own client, pinned against the app it shares a session with

const setupJs = fs.readFileSync(path.join(repoRoot, 'client', 'setup.js'), 'utf8');
const apiJs = fs.readFileSync(path.join(repoRoot, 'client', 'js', 'api.js'), 'utf8');

console.log('wizard client');

test('the wizard reads the SAME token key the main web client writes', () => {
  // Caught in review: the wizard originally invented 'access_token'. Nothing
  // errors when this drifts - the wizard simply never finds the session the
  // user already has, demands a TOTP code from somebody already logged in,
  // and stores its own token where nothing else looks. A silent, plausible,
  // entirely wrong login prompt.
  const mine = setupJs.match(/const TOKEN_KEY = '([^']+)'/);
  assert.ok(mine, 'the wizard does not declare a TOKEN_KEY');
  const theirs = apiJs.match(/localStorage\.getItem\('([^']+)'\)/);
  assert.ok(theirs, 'could not find the main client token key in api.js');
  assert.equal(mine[1], theirs[1], `wizard uses ${mine[1]}, app uses ${theirs[1]}`);
});

test('the wizard posts to the auth endpoint that actually exists', () => {
  assert.ok(apiJs.includes('/auth/verify'));
  assert.match(setupJs, /fetch\('\/api\/v1\/auth\/verify'/);
});

console.log('');
console.log(`setup signal: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
