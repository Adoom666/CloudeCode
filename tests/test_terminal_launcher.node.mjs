// Node test for macOS/terminal-launcher.js - the "Open Terminal Logs" path.
//
// WHY THIS FILE EXISTS
//
// The tray item used to run this, as one shell string:
//
//   osascript -e 'tell application "Terminal" to do script "tail -f ..."'
//
// It failed in two ways at once, and BOTH were silent.
//
// First, `do script` creates a window but does not raise the application, so
// the window was created behind everything and the user saw nothing happen.
// The tail really was running. Clicking again started another. Two orphaned
// `tail -f` processes with no window were found on the development machine,
// which is the exact signature. Nothing errored, nothing logged, and the
// process exited 0 every time.
//
// Second, it hardcoded Terminal.app while the sibling "Open in Browser" item
// correctly honours the default browser via shell.openExternal.
//
// So the assertions below are about the ARTIFACT and the OBSERVABLE CALLS:
// the text of the script that gets executed, the path and mode of the file
// that gets written, and which dependency actually got invoked. A test that
// only asserted "the click handler ran" would have passed against the broken
// version, because the broken version ran fine. It just ran invisibly.
//
// The THREE-OUTCOME cases are covered explicitly: handler resolved, handler
// resolution failed, and handler could not be determined at all. The third is
// not folded into either of the other two.
//
// Run with: node tests/test_terminal_launcher.node.mjs

import assert from 'node:assert/strict';
import fsReal from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile as execFileReal } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const launcher = require(path.join(repoRoot, 'macOS', 'terminal-launcher.js'));

let passed = 0;
let skipped = 0;
const failures = [];

/**
 * Run one named assertion block, recording pass or failure without aborting
 * the remaining checks.
 *
 * @param {string} name - Human readable description of the behaviour.
 * @param {() => void} fn - Body containing the assertions.
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

/**
 * Invoke openServerLogInDefaultTerminal and resolve with its result.
 *
 * This MUST be awaited rather than read synchronously: when a handler
 * resolves, the module goes through shell.openPath, which returns a Promise,
 * so the callback lands on a microtask. An earlier version of this file read
 * the result synchronously and saw null on exactly those paths.
 *
 * @param {string} logPath - Log file to tail.
 * @param {string} scriptPath - Stable .command path.
 * @param {object} deps - Injected dependencies.
 * @returns {Promise<object>} The result object handed to the callback.
 */
function openAndWait(logPath, scriptPath, deps) {
  return new Promise((resolve) => {
    launcher.openServerLogInDefaultTerminal(logPath, scriptPath, resolve, deps);
  });
}

/**
 * Build a set of stub dependencies that record every side effect, so the
 * assertions can inspect what the module actually did rather than trusting a
 * return value.
 *
 * @param {{handler?: (string|null), handlerError?: (Error|null),
 *   openPathResult?: (string|Promise<string>), writeThrows?: (Error|null)}}
 *   options - Behaviour to simulate.
 * @returns {{deps: object, calls: object}} Injectable deps plus a record of
 *   observed calls.
 */
function makeDeps(options) {
  const opts = options || {};
  const calls = { writes: [], chmods: [], execFile: [], openPath: [] };

  const deps = {
    fs: {
      writeFileSync(target, contents, fileOptions) {
        calls.writes.push({ target, contents, fileOptions });
        if (opts.writeThrows) throw opts.writeThrows;
      },
      chmodSync(target, mode) {
        calls.chmods.push({ target, mode });
      },
    },
    shell: {
      openPath(target) {
        calls.openPath.push(target);
        return Promise.resolve(
          opts.openPathResult === undefined ? '' : opts.openPathResult
        );
      },
    },
    execFile(bin, args, callback) {
      calls.execFile.push({ bin, args });
      const isDetect = args[0] === '-l';
      if (isDetect) {
        if (opts.handlerError) return callback(opts.handlerError, '');
        return callback(null, opts.handler === null ? '' : opts.handler + '\n');
      }
      return callback(null, '');
    },
  };

  return { deps, calls };
}

const LOG = '/Users/someone/Library/Application Support/cloude-code-menubar/logs/server.log';
const SCRIPT = '/Users/someone/Library/Application Support/cloude-code-menubar/logs/open-server-log.command';

console.log('terminal-launcher: pure builders');

test('the AppleScript fallback contains activate at all', () => {
  const script = launcher.buildTerminalAppleScript(LOG);
  assert.ok(
    script.includes('activate'),
    'the missing activate is the whole original bug; script was:\n' + script
  );
});

test('activate is issued AFTER do script so the new window is raised', () => {
  const script = launcher.buildTerminalAppleScript(LOG);
  const doScriptAt = script.indexOf('do script');
  const activateAt = script.indexOf('activate');
  assert.ok(doScriptAt >= 0, 'no do script found');
  assert.ok(activateAt >= 0, 'no activate found');
  assert.ok(
    activateAt > doScriptAt,
    'activate must follow do script, otherwise the raised window is the ' +
      'previously frontmost one rather than the one just created'
  );
});

test('the AppleScript quotes the log path so spaces survive', () => {
  const script = launcher.buildTerminalAppleScript(LOG);
  assert.ok(
    script.includes("tail -f '" + LOG + "'"),
    'log path is not shell quoted inside the AppleScript literal:\n' + script
  );
});

test('a double quote in the path cannot break out of the shell word', () => {
  const nasty = '/tmp/we"ird/server.log';
  const script = launcher.buildTerminalAppleScript(nasty);
  // Single quoted for the shell, then backslash escaped for AppleScript, so
  // AppleScript hands the shell  tail -f '/tmp/we"ird/server.log'  intact.
  assert.ok(
    script.includes("tail -f '/tmp/we\\\"ird/server.log'"),
    'double quote is not contained by the shell word:\n' + script
  );
});

test('a backtick in the path is inert in the AppleScript fallback too', () => {
  const script = launcher.buildTerminalAppleScript('/tmp/`whoami`/server.log');
  assert.ok(
    script.includes("tail -f '/tmp/`whoami`/server.log'"),
    'backtick path not single quoted:\n' + script
  );
});

test('the .command script single quotes the path and execs tail -f', () => {
  const script = launcher.buildTailCommandScript(LOG);
  assert.ok(script.startsWith('#!/bin/bash\n'), 'missing shebang');
  assert.ok(
    script.includes("exec tail -f '" + LOG + "'"),
    'path is not single quoted in the exec line:\n' + script
  );
  assert.ok(script.endsWith('\n'), 'script must end with a newline');
});

test('single quoting makes a backtick inert rather than command substitution', () => {
  const quoted = launcher.shellSingleQuote('/tmp/`whoami`/x.log');
  assert.equal(quoted, "'/tmp/`whoami`/x.log'");
  assert.ok(!quoted.startsWith('"'), 'must not be double quoted');
});

test('single quoting closes and reopens around an embedded single quote', () => {
  assert.equal(launcher.shellSingleQuote("it's"), "'it'\\''s'");
});

console.log('terminal-launcher: default-handler route');

{
  const { deps, calls } = makeDeps({ handler: '/Applications/iTerm.app' });
  const result = await openAndWait(LOG, SCRIPT, deps);
  test('a resolved handler opens the .command via shell.openPath', () => {
  assert.deepEqual(calls.openPath, [SCRIPT], 'shell.openPath not called with the script');
  assert.equal(result.opened, true);
  assert.equal(result.usedFallback, false, 'must not fall back when a handler exists');
  assert.equal(result.handlerPath, '/Applications/iTerm.app');
  const osascriptRuns = calls.execFile.filter((c) => c.args[0] === '-e');
  assert.equal(osascriptRuns.length, 0, 'Terminal.app must not be driven when a handler resolved');
  });
}

test('the .command file is written executable (mode 0755)', () => {
  const { deps, calls } = makeDeps({ handler: '/Applications/Warp.app' });
  launcher.openServerLogInDefaultTerminal(LOG, SCRIPT, () => {}, deps);

  assert.equal(calls.writes.length, 1);
  assert.equal(calls.writes[0].target, SCRIPT);
  assert.equal(calls.writes[0].fileOptions.mode, 0o755, 'file written non-executable');
  assert.deepEqual(calls.chmods, [{ target: SCRIPT, mode: 0o755 }], 'chmod not re-asserted');
});

test('repeated opens reuse ONE stable path and do not accumulate files', () => {
  const { deps, calls } = makeDeps({ handler: '/Applications/Ghostty.app' });
  launcher.openServerLogInDefaultTerminal(LOG, SCRIPT, () => {}, deps);
  launcher.openServerLogInDefaultTerminal(LOG, SCRIPT, () => {}, deps);

  const targets = new Set(calls.writes.map((w) => w.target));
  assert.equal(calls.writes.length, 2, 'expected two writes');
  assert.equal(targets.size, 1, 'each click wrote a DIFFERENT file: ' + [...targets].join(', '));
});

console.log('terminal-launcher: three outcomes');

{
  const { deps, calls } = makeDeps({ handler: null });
  const result = await openAndWait(LOG, SCRIPT, deps);
  test('COULD NOT DETERMINE a handler falls back AND reports usedFallback', () => {
  assert.equal(result.opened, true, 'fallback should still open something');
  assert.equal(
    result.usedFallback,
    true,
    'an undetermined handler must be reported, not silently substituted'
  );
  assert.equal(result.handlerPath, null);
  assert.deepEqual(calls.openPath, [], 'must not openPath when no handler resolved');

  const osa = calls.execFile.find((c) => c.args[0] === '-e');
  assert.ok(osa, 'fallback did not drive Terminal.app');
  assert.ok(
    osa.args[1].includes('activate'),
    'the FALLBACK must also activate, or it reproduces the original bug'
  );
  });
}

{
  const { deps, calls } = makeDeps({
    handler: '/Applications/iTerm.app',
    openPathResult: 'LSOpenURLsWithRole() failed',
  });
  const result = await openAndWait(LOG, SCRIPT, deps);
  test('a failed shell.openPath falls back rather than reporting success', () => {
    assert.equal(result.usedFallback, true);
    assert.ok(calls.execFile.some((c) => c.args[0] === '-e'), 'no fallback attempted');
  });
}

{
  const { deps, calls } = makeDeps({
    handler: '/Applications/iTerm.app',
    writeThrows: new Error('EROFS: read-only file system'),
  });
  const result = await openAndWait(LOG, SCRIPT, deps);
  test('an unwritable .command file falls back instead of throwing', () => {
  assert.equal(result.usedFallback, true);
  assert.deepEqual(calls.openPath, []);
  assert.ok(calls.execFile.some((c) => c.args[0] === '-e'), 'no fallback attempted');
  });
}

{
  const { deps } = makeDeps({ handlerError: new Error('osascript exploded') });
  const result = await openAndWait(LOG, SCRIPT, deps);
  test('a failing handler probe is treated as undetermined, not as success', () => {
    assert.equal(result.handlerPath, null);
    assert.equal(result.usedFallback, true);
  });
}

console.log('terminal-launcher: live LaunchServices probe');

if (os.platform() !== 'darwin') {
  skipped += 1;
  console.log('  skip live handler probe: not darwin (reported as SKIPPED, never as passed)');
} else {
  const tmpDir = fsReal.mkdtempSync(path.join(os.tmpdir(), 'cloude-cmd-'));
  const probe = path.join(tmpDir, 'probe.command');
  fsReal.writeFileSync(probe, '#!/bin/bash\nexit 0\n', { mode: 0o755 });

  await new Promise((resolve) => {
    launcher.detectDefaultCommandHandler(
      probe,
      (handlerPath) => {
        test('LaunchServices resolves a real handler for a real .command file', () => {
          assert.ok(
            handlerPath && handlerPath.endsWith('.app'),
            'no application bundle resolved for a .command file; got: ' +
              String(handlerPath)
          );
        });
        resolve();
      },
      { execFile: execFileReal }
    );
  });

  fsReal.rmSync(tmpDir, { recursive: true, force: true });
}

console.log('');
console.log(
  'terminal-launcher: ' + passed + ' passed, ' + failures.length +
    ' failed, ' + skipped + ' skipped'
);
process.exit(failures.length > 0 ? 1 : 0);
