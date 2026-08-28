// Node test for macOS/adoption-decision.js - may this bundle adopt the server
// that is already on the port?
//
// WHY THIS FILE EXISTS
//
// Measured on the user's machine, 2026-08-25:
//
//   * Electron (v1.0.3) started            14:33:52
//   * the Python server on :8000 was pid   5491
//   * that pid started                     10:45:14
//   * its parent pid was                   1
//
// ppid 1 is launchd. A process only gets there by outliving its parent. So a
// v1.0.2 app had orphaned its server on quit, and when the freshly installed
// v1.0.3 started, start() found a healthy listener on 8000 and adopted it. The
// user then ran v1.0.2 SERVER code under a v1.0.3 bundle for four hours. Its
// in-memory config cache was four hours stale too, which manufactured a false
// divergence report that cost a separate investigation.
//
// Adoption itself is not the bug. It is correct and wanted when Electron
// crashed and left its own healthy server behind - double-spawning there would
// collide on the port and kill live tmux sessions for nothing. The bug is that
// an adopted server carries NO GUARANTEE it is running this bundle's code, and
// the shipped code never asked.
//
// So the question is now asked, and there are FOUR answers, not two. The two
// unknowns are the point of this file: an upgrade that cannot prove a match is
// not a licence to adopt. "I could not tell" resolving to "yes" is how every
// false green in this codebase has worked.
//
// THESE ARE RUNTIME ASSERTIONS on real inputs and real outputs. The decision
// lives in its own dependency-free module precisely so it can be driven here;
// macOS/server-manager.js requires electron and axios at module scope and can
// only be read as text. The source-level checks that server-manager actually
// CONSULTS this module are labelled as such at the bottom.
//
// Run with: node tests/test_adoption_is_version_gated.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const macDir = path.join(here, '..', 'macOS');
const managerSrc = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');

const {
  decideAdoption,
  ADOPT_MATCH,
  ADOPT_MISMATCH,
  ADOPT_RUNNING_UNKNOWN,
  ADOPT_BUNDLE_UNKNOWN,
} = require(path.join(macDir, 'adoption-decision.js'));

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

console.log('adoption: may this bundle adopt the server already on the port');

// --- THE DECISIVE CASE ----------------------------------------------------

test('THE INCIDENT: a 1.0.2 server is NOT adopted by a 1.0.3 bundle', () => {
  const d = decideAdoption({ runningVersion: '1.0.2', bundleVersion: '1.0.3' });
  assert.equal(
    d.adopt,
    false,
    'the upgrade adopted the old version\'s server. This is the defect: four ' +
    'hours of v1.0.2 code running under a v1.0.3 bundle.'
  );
  assert.equal(d.outcome, ADOPT_MISMATCH);
  // The reason has to name BOTH numbers, because the user has to be told what
  // is on the port before being asked what to do about it.
  assert.match(d.reason, /1\.0\.2/);
  assert.match(d.reason, /1\.0\.3/);
});

test('a DOWNGRADE is refused too, not just an upgrade', () => {
  // Rolling back the app is a supported operation here, and a newer server
  // under an older bundle is if anything worse: old code reading newer state.
  const d = decideAdoption({ runningVersion: '1.0.3', bundleVersion: '1.0.2' });
  assert.equal(d.adopt, false);
  assert.equal(d.outcome, ADOPT_MISMATCH);
});

test('a MATCHING version IS adopted - the crash-recovery case still works', () => {
  const d = decideAdoption({ runningVersion: '1.0.3', bundleVersion: '1.0.3' });
  assert.equal(
    d.adopt,
    true,
    'adoption was refused for a server running exactly this bundle\'s code. ' +
    'That breaks Electron crash recovery and would double-spawn onto a held ' +
    'port, or kill live sessions for no reason.'
  );
  assert.equal(d.outcome, ADOPT_MATCH);
});

// --- CANNOT DETERMINE: the third and fourth states ------------------------

test('a running server that does NOT report a version is NOT adopted', () => {
  // Any server predating this change, or one whose version did not resolve,
  // answers with nothing. That is CANNOT DETERMINE. It is precisely the state
  // the orphan was in, and treating it as adoptable restores the bug.
  for (const missing of [undefined, null, '', '   ']) {
    const d = decideAdoption({ runningVersion: missing, bundleVersion: '1.0.3' });
    assert.equal(
      d.adopt,
      false,
      `a server reporting ${JSON.stringify(missing)} as its version was ` +
      'adopted. "I could not tell" is not "yes".'
    );
    assert.equal(d.outcome, ADOPT_RUNNING_UNKNOWN);
  }
});

test('a bundle that cannot resolve its OWN version does not adopt either', () => {
  // Comparing against an unknown is not a match. Symmetry matters: the
  // temptation is to say "well, we do not know ours, so anything goes".
  const d = decideAdoption({ runningVersion: '1.0.3', bundleVersion: '' });
  assert.equal(d.adopt, false);
  assert.equal(d.outcome, ADOPT_BUNDLE_UNKNOWN);
});

test('both unknown is still not a match', () => {
  const d = decideAdoption({ runningVersion: '', bundleVersion: '' });
  assert.equal(
    d.adopt,
    false,
    'two empty strings compared equal and were treated as a match, which is ' +
    'the single most likely way this gate gets accidentally disabled'
  );
});

test('every outcome carries a reason a human can act on', () => {
  const cases = [
    { runningVersion: '1.0.2', bundleVersion: '1.0.3' },
    { runningVersion: '1.0.3', bundleVersion: '1.0.3' },
    { runningVersion: '', bundleVersion: '1.0.3' },
    { runningVersion: '1.0.3', bundleVersion: '' },
  ];
  for (const c of cases) {
    const d = decideAdoption(c);
    assert.equal(typeof d.reason, 'string');
    assert.ok(d.reason.length > 20, `thin reason for ${JSON.stringify(c)}`);
  }
});

// --- normalisation --------------------------------------------------------

test('a leading v and surrounding whitespace do not fake a mismatch', () => {
  // The bundle reads package.json ("1.0.3"); a server may resolve from a git
  // tag ("v1.0.3"). A refusal caused by punctuation would be a false alarm on
  // every launch, and the fix people reach for is disabling the gate.
  const d = decideAdoption({ runningVersion: ' v1.0.3 ', bundleVersion: '1.0.3' });
  assert.equal(d.adopt, true, 'v1.0.3 and 1.0.3 were treated as different');
  assert.equal(d.outcome, ADOPT_MATCH);
});

test('a non-string version is CANNOT DETERMINE, never coerced', () => {
  for (const junk of [1.03, {}, [], true, NaN]) {
    const d = decideAdoption({ runningVersion: junk, bundleVersion: '1.0.3' });
    assert.equal(d.adopt, false, `${JSON.stringify(junk)} was accepted`);
    assert.equal(d.outcome, ADOPT_RUNNING_UNKNOWN);
  }
});

test('a build suffix is a real difference, not noise', () => {
  // "1.0.3" and "1.0.3-2-gabc1234" are different code. describe_git_tag can
  // produce the latter, and it means "not at the release tag".
  const d = decideAdoption({
    runningVersion: '1.0.3-2-gabc1234',
    bundleVersion: '1.0.3',
  });
  assert.equal(d.adopt, false);
  assert.equal(d.outcome, ADOPT_MISMATCH);
});

test('decideAdoption called with nothing at all does not throw and does not adopt', () => {
  assert.equal(decideAdoption().adopt, false);
  assert.equal(decideAdoption({}).adopt, false);
});

// --- wiring (SOURCE-LEVEL) ------------------------------------------------

test('wiring (SOURCE-LEVEL): start() consults the gate before adopting', () => {
  assert.ok(
    /require\('\.\/adoption-decision'\)/.test(managerSrc),
    'server-manager does not import the adoption gate at all, so whatever it ' +
    'decides is untested'
  );
  const at = managerSrc.indexOf('async start(');
  assert.notEqual(at, -1, 'start() is gone');
  const body = managerSrc.slice(at, at + 8000);
  const adoptAt = body.indexOf('adopting it');
  const decideAt = body.indexOf('decideAdoption(');
  assert.notEqual(decideAt, -1, 'start() never calls decideAdoption');
  assert.ok(
    decideAt < adoptAt || adoptAt === -1,
    'start() adopts before it decides whether it may, which makes the gate ' +
    'decorative'
  );
});

test('wiring (SOURCE-LEVEL): the refusal is thrown, not logged and swallowed', () => {
  const at = managerSrc.indexOf('async start(');
  const body = managerSrc.slice(at, at + 8000);
  assert.ok(
    /ADOPTION_REFUSED/.test(body),
    'a refused adoption does not raise anything the menu can show, so the ' +
    'user sees a server that silently did not start'
  );
});

test('wiring (SOURCE-LEVEL): the post-start health resync cannot undo a refusal', () => {
  // start() refuses, and then main.js re-probes health and promotes any
  // healthy answer to state 'running'. That probe cannot tell our server from
  // the stranger we just declined to adopt, so an unguarded resync re-adopts
  // it one line later - the defect surviving its own fix.
  const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
  const at = mainSrc.indexOf('Force immediate health check');
  assert.notEqual(at, -1, 'the post-start resync is gone; re-check this guard');
  const region = mainSrc.slice(at, at + 900);
  assert.ok(
    /startBlockedReason/.test(region),
    'the post-start health resync is not gated on the refusal, so a refused ' +
    'adoption is promoted straight back to "running"'
  );
});

test('wiring (SOURCE-LEVEL): a refusal reaches the user as a choice, not a dead end', () => {
  const mainSrc = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
  assert.ok(
    /ADOPTION_REFUSED/.test(mainSrc),
    'main.js does not distinguish a refused adoption from any other start ' +
    'failure, so the user is told something is wrong and nothing about what ' +
    'they can do'
  );
  assert.ok(
    /takeOverPort\(\)/.test(mainSrc),
    'there is no way for the user to replace the server, so the only ' +
    'remaining move is Activity Monitor'
  );
  // The harmless option must be the default. Killing a process this app did
  // not start is not something to do on a stray Return key.
  const at = mainSrc.indexOf("buttons: ['Leave it running', 'Replace it']");
  assert.notEqual(at, -1, 'the replace prompt does not offer both options');
  assert.match(
    mainSrc.slice(at, at + 200),
    /defaultId:\s*0/,
    'the destructive option is the default button'
  );
});

test('wiring (SOURCE-LEVEL): takeOverPort confirms the kill AND the port', () => {
  const at = managerSrc.indexOf('async takeOverPort(');
  assert.notEqual(at, -1, 'takeOverPort is gone');
  const body = managerSrc.slice(at, at + 2000);
  assert.ok(/terminateProcess\(/.test(body), 'the holder is not terminated');
  assert.ok(
    /result\.terminated/.test(body),
    'the kill result is never checked, so a failed kill falls through to a ' +
    'spawn that will fail on a held port with a confusing bind error'
  );
  assert.ok(
    /waitForPortFree\(/.test(body),
    'a dead pid is not a free port - a released socket lingers - and spawning ' +
    'into that window fails as something else entirely'
  );
});

console.log(`\nadoption: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log('ALL PASS');
