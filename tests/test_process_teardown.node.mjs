// Node test for macOS/process-teardown.js - does a process we asked to die
// ACTUALLY die.
//
// WHY THIS FILE EXISTS
//
// On 2026-08-25 a v1.0.2 Cloude Code was quit and its Python server was still
// on port 8000 four hours later, pid 5491, parent pid 1. It had been
// reparented to launchd, which is what happens to any child that outlives its
// parent. The next version of the app then found that healthy listener and
// adopted it, so a v1.0.3 bundle spent four hours running v1.0.2 code.
//
// The precondition for all of that is a quit that does not reliably kill the
// server. macOS/server-manager.js::stop() looked like it escalated:
//
//     this.process.kill('SIGTERM');
//     await sleep(3000);
//     if (this.process && !this.process.killed) {
//       this.process.kill('SIGKILL');
//     }
//
// `child.killed` in Node does NOT mean "the child is dead". It means "a
// signal was successfully DELIVERED to it". It is set true by the SIGTERM
// call two lines above, so the SIGKILL branch is unreachable dead code. A
// uvicorn that declines to exit on SIGTERM - one holding a websocket, one
// mid-request, one waiting on a tmux child - was never escalated against and
// simply outlived the app.
//
// THE ASSERTIONS HERE ARE ABOUT OBSERVED PROCESS STATE, NOT ABOUT CALLS.
// A test that asserted "kill was called with SIGKILL" would have passed
// against the shipped code on the day it orphaned the server, because the
// question is not whether a signal was sent. It is whether the pid is gone.
// So every verdict below is taken from process.kill(pid, 0) - the kernel's
// answer - after the routine returns.
//
// The SIGTERM-ignoring child is the decisive case. It is a real process that
// installs a real no-op SIGTERM handler, exactly like a server that has
// decided it is busy. Against the shipped escalation guard it survives.
//
// Run with: node tests/test_process_teardown.node.mjs

import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const macDir = path.join(here, '..', 'macOS');

const { terminateProcess, isAlive } = require(path.join(macDir, 'process-teardown.js'));

let passed = 0;
let failed = 0;
const pending = [];

function test(name, fn) {
  pending.push([name, fn]);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Spawn a real child process and wait until IT says it is ready.
 *
 * Waiting on isAlive() alone is not enough and the first draft of this file
 * got it wrong in a way worth recording: a pid is alive from the instant
 * spawn() returns, which is BEFORE node has booted and evaluated -e. A
 * SIGTERM sent in that window hits a process that has not installed its
 * handler yet, so the deliberately-stubborn child died on SIGTERM like any
 * other and the decisive assertion passed for the wrong reason. The child
 * now announces readiness on stdout AFTER its handler is installed, so
 * "stubborn" means stubborn.
 *
 * @param {string} body - JS evaluated by `node -e` in the child. Must print
 *   "ready" on stdout once it is genuinely in its steady state.
 * @returns {Promise<import('node:child_process').ChildProcess>}
 */
async function spawnChild(body) {
  const child = spawn(process.execPath, ['-e', body], {
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  let ready = false;
  child.stdout.on('data', (buf) => {
    if (String(buf).includes('ready')) ready = true;
  });
  for (let i = 0; i < 250 && !ready; i += 1) {
    await sleep(20);
  }
  assert.ok(ready, 'the helper child never announced itself ready');
  assert.ok(isAlive(child.pid), 'the helper child never came up');
  return child;
}

// A child that exits promptly on SIGTERM (the well-behaved case).
const OBEDIENT = "console.log('ready'); setInterval(() => {}, 1000);";

// A child that installs a no-op SIGTERM handler and therefore survives it,
// exactly like a uvicorn that has decided it is busy. This is the case the
// shipped code could not kill. Readiness is printed AFTER the handler is
// installed - see spawnChild.
const STUBBORN =
  "process.on('SIGTERM', () => {}); console.log('ready'); " +
  'setInterval(() => {}, 1000);';

test('a process that ignores SIGTERM is still dead when the routine returns', async () => {
  const child = await spawnChild(STUBBORN);
  const result = await terminateProcess(child.pid, { graceMs: 600, pollMs: 50 });

  // The verdict comes from the kernel, not from the routine's own opinion.
  assert.equal(
    isAlive(child.pid),
    false,
    'the pid is STILL ALIVE after terminateProcess returned. This is the ' +
    'orphan that gets adopted by the next version of the app.'
  );
  assert.equal(result.terminated, true);
  assert.equal(
    result.outcome,
    'sigkill',
    'a SIGTERM-ignoring process must be reported as having needed SIGKILL, ' +
    'so the escalation is visible rather than assumed'
  );
  assert.equal(result.escalated, true);
});

test('a well-behaved process dies on SIGTERM and is never escalated against', async () => {
  const child = await spawnChild(OBEDIENT);
  const result = await terminateProcess(child.pid, { graceMs: 3000, pollMs: 50 });

  assert.equal(isAlive(child.pid), false, 'the obedient child survived');
  assert.equal(result.terminated, true);
  assert.equal(result.outcome, 'sigterm');
  assert.equal(
    result.escalated,
    false,
    'SIGKILL was used on a process that would have exited on SIGTERM, which ' +
    'denies the server its chance to flush connections'
  );
});

test('a pid that is already gone reports already-gone, not a fake kill', async () => {
  const child = await spawnChild(OBEDIENT);
  const pid = child.pid;
  process.kill(pid, 'SIGKILL');
  for (let i = 0; i < 100 && isAlive(pid); i += 1) {
    await sleep(20);
  }
  assert.equal(isAlive(pid), false, 'could not pre-kill the helper child');

  const result = await terminateProcess(pid, { graceMs: 500, pollMs: 50 });
  assert.equal(result.terminated, true);
  assert.equal(
    result.outcome,
    'already-gone',
    'a pid that was already dead must say so; reporting "sigterm" would ' +
    'claim a kill that never happened'
  );
  assert.equal(result.escalated, false);
});

test('a pid that cannot be determined is a third outcome, never a success', async () => {
  // pid 1 is launchd. We are not root, so signalling it is EPERM: the
  // routine can neither kill it nor prove it dead. That is CANNOT DETERMINE,
  // and it must not be laundered into terminated:true.
  const result = await terminateProcess(1, { graceMs: 200, pollMs: 50 });
  assert.equal(
    result.terminated,
    false,
    'a process we are not permitted to signal was reported as terminated'
  );
  assert.equal(result.outcome, 'not-permitted');
  assert.equal(isAlive(1), true, 'launchd should still be running');
});

test('a null or nonsense pid is refused rather than signalled', async () => {
  for (const bad of [null, undefined, 0, -1, NaN, 'x']) {
    const result = await terminateProcess(bad, { graceMs: 100, pollMs: 50 });
    assert.equal(result.terminated, false, `pid ${String(bad)} was accepted`);
    assert.equal(result.outcome, 'no-pid');
  }
  // Negative pids are process GROUPS. Signalling one by accident would take
  // down every process in the group, which on macOS can include the caller.
  assert.equal((await terminateProcess(-1, {})).outcome, 'no-pid');
});

test('isAlive answers from the kernel and distinguishes gone from unknowable', async () => {
  assert.equal(isAlive(process.pid), true, 'this very process reads as dead');
  // An EPERM pid is alive - we got a permission error, which only a live
  // process can produce. Treating EPERM as dead is how a running server
  // gets reported as stopped.
  assert.equal(isAlive(1), true, 'launchd reads as dead because EPERM was ' +
    'mistaken for ESRCH');
});

(async () => {
  console.log('process-teardown: killing things and asking the kernel whether they died');
  for (const [name, fn] of pending) {
    try {
      await fn();
      console.log(`  ok   ${name}`);
      passed += 1;
    } catch (err) {
      console.log(`  FAIL ${name}`);
      console.log(`       ${err.message}`);
      failed += 1;
    }
  }
  console.log(`\nprocess-teardown: ${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
  console.log('ALL PASS');
})();
