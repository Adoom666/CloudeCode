// The Cloudflare tunnel system went away in plan v3.2. Two leftovers of it
// were still executing on 2026-08-26.
//
// setup.sh opened by requiring the `cloudflared` binary and calling `exit 1`
// without it, so a fresh install refused to proceed on any machine that had
// no use for a feature this app no longer has, then ran `cloudflared login`
// - an interactive browser flow - to authorise a tunnel nothing would ever
// create. Further down it prompted for a domain, an API token, a zone ID and
// a tunnel name, and stored none of them: .env.example carries no
// CLOUDFLARE_* keys, so every `sed` that was supposed to write them matched
// nothing. It collected an API token from the user and discarded it.
//
// The tray's `Tunnels: N` row read `health.tunnel_count`, a field
// HealthResponse does not declare. FastAPI's response_model is a FILTER, not
// a passthrough, so that field cannot reach the client even if something
// upstream set it - the row was pinned at 0 permanently while describing a
// subsystem the product does not have.
//
// WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
// Not "the string cloudflare is absent". Both files carry comments recording
// exactly what was removed and why, and that history is worth more than a
// clean grep. Comments are stripped before every absence check below. This
// repo has produced false FAILs five separate times from an absence check
// matching the comment that documented its own removal.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const setupSrc = readFileSync(join(ROOT, 'setup.sh'), 'utf8');
const mainSrc = readFileSync(join(ROOT, 'macOS', 'main.js'), 'utf8');

let passed = 0;
const failures = [];

/**
 * Description: run one named assertion, recording pass or failure.
 * Inputs: name (string), fn (function) - throws to fail.
 * Output: void.
 */
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (err) {
    failures.push(name);
    console.log('  FAIL ' + name + ': ' + err.message);
  }
}

/**
 * Description: strip shell comments so an absence check cannot match the
 *   note explaining the removal.
 * Inputs: src (string) - shell source.
 * Output: string with every whole-line # comment removed.
 */
function stripShellComments(src) {
  return src.replace(/^\s*#.*$/gm, '');
}

/**
 * Description: strip JS block and line comments, same reasoning.
 * Inputs: src (string) - JavaScript source.
 * Output: string with comments removed.
 */
function stripJsComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
}

test('setup.sh executes nothing that mentions cloudflare', () => {
  const code = stripShellComments(setupSrc);
  const offending = code
    .split('\n')
    .filter((line) => /cloudflare|cloudflared/i.test(line));
  assert.deepEqual(
    offending,
    [],
    'setup.sh still runs Cloudflare code:\n' + offending.join('\n')
  );
});

test('setup.sh does not gate installation on the cloudflared binary', () => {
  const code = stripShellComments(setupSrc);
  assert.ok(
    !/command\s+-v\s+cloudflared/.test(code),
    'the cloudflared presence gate is back; it exits 1 on machines that ' +
      'have no reason to install it'
  );
});

test('setup.sh collects no credential it cannot store', () => {
  const code = stripShellComments(setupSrc);
  // Assert on the VARIABLES the prompts bound, not on prose. A reworded
  // prompt asking for the same token would slip past a phrase match.
  for (const name of ['CF_TOKEN', 'CF_ZONE', 'CF_DOMAIN', 'CF_TUNNEL_NAME']) {
    assert.ok(
      !new RegExp('\\b' + name + '\\b').test(code),
      'setup.sh still binds ' + name + ', a value nothing reads back'
    );
  }
});

test('the setup.sh comments explaining the removal are still there', () => {
  // The counterpart to the absence checks: without this, the cheapest way to
  // make them pass is to delete the reasoning.
  assert.match(setupSrc, /Plan v3\.2/);
  assert.match(setupSrc, /CLOUDFLARE_\*/);
});

test('the tray reads no field the health response cannot carry', () => {
  const code = stripJsComments(mainSrc);
  assert.ok(
    !/tunnel_count|tunnelCount/.test(code),
    'macOS/main.js still reads tunnel_count, which HealthResponse does not ' +
      'declare and the response_model therefore strips'
  );
  assert.ok(
    !/Tunnels:/.test(code),
    'the tray still renders a Tunnels row for a demolished subsystem'
  );
});

test('the tray row it was replaced with reads a field that exists', () => {
  const code = stripJsComments(mainSrc);
  assert.match(
    code,
    /local_server_count/,
    'the row now shows nothing at all; HealthResponse.local_server_count is ' +
      'the field that replaced tunnel_count and it should be surfaced'
  );
  assert.match(
    code,
    /Local servers: \$\{/,
    'local_server_count is read but never rendered'
  );
  // Three outcomes: a server that has not answered is not a server with
  // zero local servers.
  assert.match(
    code,
    /'unknown'/,
    'the row collapses an unanswered server into the number 0'
  );
});

console.log('');
console.log(
  'no-dead-cloudflare-surface: ' + passed + ' passed, ' + failures.length + ' failed'
);
process.exit(failures.length > 0 ? 1 : 0);
