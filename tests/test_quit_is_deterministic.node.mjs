// Node test for the QUIT path: macOS/quit-sequence.js, plus the source-level
// checks that macOS/main.js and macOS/server-manager.js actually use it.
//
// WHY THIS FILE EXISTS
//
// On 2026-08-25 quitting Cloude Code took the Python server down sometimes and
// left it running other times, measured BOTH ways across different restarts on
// the same machine and the same build. The surviving server was pid 5491 with
// PARENT PID 1 - reparented to launchd, the signature of an orphan - and the
// next version of the app adopted it and ran its code for four hours.
//
// There were TWO independent mechanisms, either one sufficient on its own:
//
//  1. `app.on('before-quit', async () => { await serverManager.stop(); })`.
//     Electron does not await an async listener. It calls the function, gets a
//     pending promise, discards it, and carries on quitting. Everything after
//     the first `await` inside stop() was racing the main process teardown.
//     Which one won was the whole difference between a clean exit and an
//     orphan. That sequencing now lives in macOS/quit-sequence.js and is
//     DRIVEN below, not grepped.
//
//  2. stop()'s escalation to SIGKILL was unreachable dead code. Covered by
//     tests/test_process_teardown.node.mjs against real spawned processes.
//
// WHICH ASSERTIONS ARE WHICH
//
// The `quit-sequence:` group is RUNTIME. It builds the real handler with fake
// collaborators and observes the ORDER things happen in - that the quit is
// deferred, that it is re-issued only after the teardown resolves, that it is
// re-issued exactly once, and that a concurrent quit does not start a second
// teardown. These would fail against the shipped code.
//
// The `wiring:` group is SOURCE-LEVEL and says so. macOS/main.js requires
// `electron` at module scope and registers against a live `app`; it cannot be
// loaded outside a running Electron. Reading its text can prove it delegates
// to the module that IS tested. It cannot prove Electron honours
// preventDefault. That limit is real and is not dressed up as more.
//
// Run with: node tests/test_quit_is_deterministic.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const macDir = path.join(here, '..', 'macOS');
const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
const managerSrc = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');

const { createQuitHandler } = require(path.join(macDir, 'quit-sequence.js'));

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
 * A fake Electron quit event that records whether it was deferred.
 * @returns {{preventDefault: () => void, defers: number}}
 */
function fakeEvent() {
  const ev = { defers: 0, preventDefault: () => { ev.defers += 1; } };
  return ev;
}

// ---------------------------------------------------------------- runtime

test('runtime: the quit is deferred and NOT issued until the teardown resolves', async () => {
  const order = [];
  let releaseTeardown;
  const handler = createQuitHandler({
    teardown: async () => {
      order.push('teardown:start');
      await new Promise((r) => { releaseTeardown = r; });
      order.push('teardown:end');
    },
    quit: () => order.push('quit'),
  });

  const ev = fakeEvent();
  const inFlight = handler(ev);
  await sleep(20);

  assert.equal(ev.defers, 1, 'the quit was never deferred');
  assert.deepEqual(
    order,
    ['teardown:start'],
    'the app quit while the server teardown was still in flight. This is the ' +
    'race that orphaned pid 5491.'
  );

  releaseTeardown();
  await inFlight;
  assert.deepEqual(order, ['teardown:start', 'teardown:end', 'quit']);
});

test('runtime: the re-issued quit passes straight through, it does not defer again', async () => {
  const order = [];
  let teardowns = 0;
  const handler = createQuitHandler({
    teardown: async () => { teardowns += 1; order.push('teardown'); },
    quit: () => order.push('quit'),
  });

  await handler(fakeEvent());
  // Electron delivers before-quit again for the quit we just issued.
  const second = fakeEvent();
  await handler(second);

  assert.equal(
    second.defers,
    0,
    'the handler deferred its own re-issued quit, so the app can never quit ' +
    'at all - an infinite quit loop, worse than the leak being fixed'
  );
  assert.equal(teardowns, 1, 'the server was torn down twice');
});

test('runtime: a second quit ARRIVING DURING the teardown is deferred but does not re-tear-down', async () => {
  let teardowns = 0;
  let release;
  let quits = 0;
  const handler = createQuitHandler({
    teardown: async () => {
      teardowns += 1;
      await new Promise((r) => { release = r; });
    },
    quit: () => { quits += 1; },
  });

  const first = handler(fakeEvent());
  await sleep(10);
  const mid = fakeEvent();
  await handler(mid);

  assert.equal(
    mid.defers,
    1,
    'a quit that arrived mid-teardown was let through, which quits the app ' +
    'out from under the stop it is waiting for - the original bug, one door over'
  );
  assert.equal(teardowns, 1, 'a second teardown was started concurrently');

  release();
  await first;
  assert.equal(quits, 1, 'the app quit more or fewer than exactly once');
});

test('runtime: a teardown that throws still quits, and reports', async () => {
  let quits = 0;
  const errors = [];
  const handler = createQuitHandler({
    teardown: async () => { throw new Error('stop blew up'); },
    quit: () => { quits += 1; },
    onError: (msg, err) => errors.push(err.message),
  });

  await handler(fakeEvent());
  assert.equal(
    quits,
    1,
    'a failing teardown left the app unable to quit. Cmd-Q must always work.'
  );
  assert.deepEqual(errors, ['stop blew up'], 'the failure was swallowed silently');
});

test('runtime: missing collaborators are refused at construction, not at quit time', () => {
  assert.throws(() => createQuitHandler({ quit: () => {} }), /teardown/);
  assert.throws(() => createQuitHandler({ teardown: async () => {} }), /quit/);
});

// ----------------------------------------------------------- source-level

test('wiring (SOURCE-LEVEL): main.js registers the tested handler, not an ad-hoc async listener', () => {
  assert.ok(
    /require\('\.\/quit-sequence'\)/.test(mainSrc),
    'main.js does not use quit-sequence.js, so whatever it does on quit is ' +
    'untested'
  );
  // Anchor on the STRING LITERAL, not on `app.on('before-quit'` as one
  // token. The registration is legitimately spread over several lines, and an
  // anchor that assumes a single-line call reports "no handler is registered
  // at all" about a file that registers one correctly - a false FAIL
  // manufactured inside the check. Skip the prose above it by taking the LAST
  // occurrence, since the comment block names it too.
  const at = mainSrc.lastIndexOf("'before-quit'");
  assert.notEqual(at, -1, 'no before-quit handler is registered at all');
  const registration = mainSrc.slice(at, at + 400);
  assert.ok(
    /createQuitHandler\(/.test(registration),
    'before-quit is still registered with an inline async function. Electron ' +
    'does not await one, so the teardown races the quit.'
  );
});

test('wiring (SOURCE-LEVEL): stop() no longer decides escalation from ChildProcess.killed', () => {
  const at = managerSrc.indexOf('async stop(');
  assert.notEqual(at, -1, 'stop() is gone');
  const body = managerSrc.slice(at, at + 4000);
  // Strip comments first: this file DOCUMENTS the old bug in prose, and a raw
  // substring search would match the explanation and fail on correct code.
  // (Asserting the absence of a string in commented source is its own trap.)
  const code = body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  assert.ok(
    !/\.killed/.test(code),
    'stop() still reads ChildProcess.killed, which means "a signal was sent", ' +
    'not "the process died" - the guard that made SIGKILL unreachable'
  );
  assert.ok(
    /terminateProcess\(/.test(code),
    'stop() does not call terminateProcess, so nothing confirms the exit'
  );
  assert.ok(
    /isAlive\(/.test(code),
    'stop() sets state without asking the kernel whether the process exited, ' +
    'so the tray can report stopped for a server that is still serving'
  );
});

test('wiring (SOURCE-LEVEL): isProcessRunning asks the kernel rather than a stale flag', () => {
  const at = managerSrc.indexOf('  isProcessRunning() {');
  assert.notEqual(at, -1, 'isProcessRunning is gone');
  const body = managerSrc.slice(at, at + 1400);
  const code = body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  assert.ok(
    !/\.killed/.test(code),
    'isProcessRunning short-circuits on ChildProcess.killed, so it answers ' +
    '"running" about a process it never measured'
  );
  assert.ok(/isAlive\(/.test(code), 'no kernel liveness check');
});

(async () => {
  console.log('quit determinism: does the app wait for the server to actually die');
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
  console.log(`\nquit determinism: ${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
  console.log('ALL PASS');
})();
