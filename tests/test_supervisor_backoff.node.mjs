// Node test for macOS/supervisor-policy.js - what happens when the server
// dies and nobody asked it to.
//
// WHY THIS FILE EXISTS
//
// While diagnosing the 2026-08-25 adoption incident, the orphaned server was
// killed. The app did not respawn one for at least 22 seconds; it stayed down.
// There is no supervisor loop anywhere in the app, and adoption/spawn only
// happens inside start(), which nothing calls again after an unexpected exit.
// So ANY server death is permanent until the user notices the app has gone
// quiet and clicks Start Server. Nothing announces it.
//
// THE CALL, ARGUED. v1 auto-recovers, bounded.
//
// The case against is real and is why this is a policy module with an
// explicit budget rather than a `while (true)` in the exit handler: a restart
// loop thrashes against a server that cannot start (bad .env, occupied port,
// broken venv) and it can MASK a real fault, turning a hard failure into an
// intermittent one that is far harder to diagnose. Those are the failure
// modes to design against, not reasons to do nothing.
//
// The case for is that doing nothing is measurably worse. The current
// behaviour is a dead app with a menu bar that does not say so, and the
// recovery action - click Start - is one the app could have taken instantly
// and correctly. A user cannot act on a failure they have not been told
// about.
//
// So: at most 3 attempts, growing delays, and every attempt SURFACED rather
// than silent. When the budget is spent it gives up, says it gave up, and
// leaves Start Server to the user. Giving up is a real outcome here, not a
// failure of nerve: three failed starts in ninety seconds is a fault a human
// needs to look at, and continuing to retry would only bury it.
//
// THE SUBTLE ONE, and the reason recordUp() takes a duration rather than just
// resetting: a crash loop that gets far enough to look healthy for a moment
// would otherwise reset the budget on every cycle and retry forever. That is
// an unbounded loop wearing a bounded loop's clothes. The budget only resets
// after the server has stayed up for a real window.
//
// THESE ARE RUNTIME ASSERTIONS driven against an injected clock, so the
// backoff is measured rather than read. The checks that server-manager and
// main.js actually USE the policy are labelled SOURCE-LEVEL.
//
// Run with: node tests/test_supervisor_backoff.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const macDir = path.join(here, '..', 'macOS');
const managerSrc = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');
const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');

const {
  createSupervisor,
  SUPERVISOR_RESTART,
  SUPERVISOR_GAVE_UP,
  SUPERVISOR_NOT_SUPERVISED,
} = require(path.join(macDir, 'supervisor-policy.js'));

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
    passed += 1;
  } catch (err) {
    console.log(`  FAIL ${name}`);
    console.log(`       ${err.message}`);
    failed += 1;
  }
}

/** A supervisor with a controllable clock. */
function makeSupervisor(overrides = {}) {
  let now = 1000;
  const sup = createSupervisor({
    maxAttempts: 3,
    delaysMs: [5000, 15000, 45000],
    healthyResetMs: 60000,
    now: () => now,
    ...overrides,
  });
  return { sup, advance: (ms) => { now += ms; }, at: () => now };
}

console.log('supervisor: a dead server should not stay dead in silence');

// --- THE CORE BEHAVIOUR ---------------------------------------------------

test('an unexpected death schedules a restart rather than staying down', () => {
  const { sup } = makeSupervisor();
  const d = sup.recordDown({ expected: false });
  assert.equal(
    d.action,
    SUPERVISOR_RESTART,
    'the server died and nothing was scheduled, so it stays down until the ' +
    'user notices - which is the current behaviour and is what this fixes'
  );
  assert.equal(d.attempt, 1);
  assert.equal(d.delayMs, 5000);
});

test('the delays actually grow, and are the ones declared', () => {
  const { sup } = makeSupervisor();
  assert.equal(sup.recordDown({ expected: false }).delayMs, 5000);
  assert.equal(sup.recordDown({ expected: false }).delayMs, 15000);
  assert.equal(sup.recordDown({ expected: false }).delayMs, 45000);
});

test('THE BUDGET IS REAL: the fourth death gives up instead of retrying', () => {
  const { sup } = makeSupervisor();
  sup.recordDown({ expected: false });
  sup.recordDown({ expected: false });
  sup.recordDown({ expected: false });
  const d = sup.recordDown({ expected: false });
  assert.equal(
    d.action,
    SUPERVISOR_GAVE_UP,
    'the supervisor kept retrying past its budget. A server that cannot start ' +
    'would be restarted forever, which buries the real fault instead of ' +
    'surfacing it.'
  );
  assert.equal(d.attempt, 3);
  assert.equal(d.maxAttempts, 3);
});

test('giving up is SAID, not just done', () => {
  const { sup } = makeSupervisor();
  for (let i = 0; i < 3; i += 1) sup.recordDown({ expected: false });
  const d = sup.recordDown({ expected: false });
  assert.ok(
    /gave up|giving up|no longer/i.test(d.message),
    'the give-up message does not say it gave up, so a permanently dead ' +
    'server looks the same as a healthy quiet one'
  );
  assert.equal(sup.status().gaveUp, true);
});

test('every restart attempt is announced with its number and the budget', () => {
  const { sup } = makeSupervisor();
  const d = sup.recordDown({ expected: false });
  assert.match(d.message, /1/);
  assert.match(d.message, /3/);
  assert.ok(
    d.message.length > 20,
    'an attempt message too thin to render is a silent retry, which is the ' +
    'masking failure mode this policy is supposed to avoid'
  );
});

// --- WHAT MUST NOT BE SUPERVISED ------------------------------------------

test('a DELIBERATE stop is never restarted', () => {
  const { sup } = makeSupervisor();
  const d = sup.recordDown({ expected: true });
  assert.equal(
    d.action,
    SUPERVISOR_NOT_SUPERVISED,
    'the user chose Stop Server and the supervisor started it again. There ' +
    'is no way for the user to turn the server off.'
  );
  assert.equal(sup.status().attempts, 0, 'a deliberate stop spent budget');
});

test('a quit is never restarted', () => {
  const { sup } = makeSupervisor();
  const d = sup.recordDown({ expected: false, quitting: true });
  assert.equal(
    d.action,
    SUPERVISOR_NOT_SUPERVISED,
    'the supervisor respawned a server while the app was quitting, which ' +
    'creates exactly the orphan this whole change exists to prevent'
  );
});

// --- THE BUDGET RESET, AND THE TRAP IN IT ---------------------------------

test('a genuinely healthy run resets the budget', () => {
  const { sup, advance } = makeSupervisor();
  sup.recordDown({ expected: false });
  sup.recordDown({ expected: false });
  sup.recordUp();
  advance(60001);
  sup.noteStillUp();
  assert.equal(sup.status().attempts, 0, 'a long healthy run did not clear the budget');
  assert.equal(sup.recordDown({ expected: false }).delayMs, 5000);
});

test('THE TRAP: a crash loop that briefly looks healthy does NOT reset the budget', () => {
  // Without a duration requirement this is an unbounded retry loop dressed as
  // a bounded one: every cycle comes up, resets, dies, and retries forever.
  const { sup, advance } = makeSupervisor();
  for (let i = 0; i < 3; i += 1) {
    sup.recordDown({ expected: false });
    sup.recordUp();
    advance(2000); // up for two seconds, then dies again
    sup.noteStillUp();
  }
  const d = sup.recordDown({ expected: false });
  assert.equal(
    d.action,
    SUPERVISOR_GAVE_UP,
    'a server that comes up for two seconds and dies reset the budget every ' +
    'cycle, so the supervisor would restart it forever'
  );
});

test('noteStillUp before the window has elapsed leaves the budget alone', () => {
  const { sup, advance } = makeSupervisor();
  sup.recordDown({ expected: false });
  sup.recordUp();
  advance(59999);
  sup.noteStillUp();
  assert.equal(sup.status().attempts, 1);
});

// --- STATUS IS PUBLISHED --------------------------------------------------

test('status is always renderable, including before anything has happened', () => {
  const { sup } = makeSupervisor();
  const s = sup.status();
  assert.equal(s.attempts, 0);
  assert.equal(s.gaveUp, false);
  assert.equal(typeof s.message, 'string');
});

test('a manual start clears a give-up so the user is not locked out', () => {
  const { sup } = makeSupervisor();
  for (let i = 0; i < 4; i += 1) sup.recordDown({ expected: false });
  assert.equal(sup.status().gaveUp, true);
  sup.reset();
  assert.equal(sup.status().gaveUp, false);
  assert.equal(sup.recordDown({ expected: false }).action, SUPERVISOR_RESTART);
});

// --- wiring (SOURCE-LEVEL) ------------------------------------------------

test('wiring (SOURCE-LEVEL): server-manager runs the supervisor on unexpected exit', () => {
  assert.ok(
    /require\('\.\/supervisor-policy'\)/.test(managerSrc),
    'server-manager does not use the supervisor at all, so a dead server ' +
    'still stays dead'
  );
  assert.ok(
    /superviseDown\(|recordDown\(/.test(managerSrc),
    'nothing ever tells the supervisor the server went down'
  );
});

test('wiring (SOURCE-LEVEL): the supervisor is surfaced, not silent', () => {
  assert.ok(
    /getSupervisorStatus\(/.test(managerSrc),
    'there is no way to read what the supervisor is doing, so an automatic ' +
    'restart is indistinguishable from nothing happening'
  );
  assert.ok(
    /getSupervisorStatus\(/.test(mainSrc),
    'the menu never reads the supervisor status, so the user is not told the ' +
    'server is down or that it is being restarted'
  );
});

console.log(`\nsupervisor: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log('ALL PASS');
