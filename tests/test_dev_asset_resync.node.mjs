/**
 * A CHANGED file must reach the served copy on a dev launch.
 *
 * WHY THIS SUITE EXISTS. The Electron app never serves the checkout. It
 * serves a copy under Application Support, and in dev the only thing that
 * ever landed files there was a first-run `copyRecursive` that SKIPS any
 * path already present at the destination. A brand new file appeared on
 * the next launch; an EDITED file never did, for as long as the install
 * lived.
 *
 * Nothing errored, which is the whole problem. The server happily served
 * a client/ that was part one build and part another, the checkout on
 * disk was correct, every test was green, and the result reached the user
 * as what looked like a rendering defect. Measured 2026-08-20: the served
 * index.html predated the merge and carried none of the new screen-chrome
 * assets while the checkout beside it had both.
 *
 * So the assertion here is deliberately about the CHANGED case. A test
 * that only checks a new file arrives would have passed against the
 * defect.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, '..');
const bootstrap = require(path.join(repoRoot, 'macOS', 'bootstrap.js'));

/**
 * Build a throwaway source tree and an already-provisioned server dir.
 *
 * The server dir starts with an OLD copy of the same file, which is the
 * state a second launch always finds.
 *
 * @returns {{src: string, dst: string}} the two directory paths.
 */
function stage() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-dev-resync-'));
  const src = path.join(root, 'checkout');
  const dst = path.join(root, 'serverDir');
  fs.mkdirSync(path.join(src, 'client'), { recursive: true });
  fs.mkdirSync(path.join(src, 'src'), { recursive: true });
  fs.mkdirSync(path.join(dst, 'client'), { recursive: true });

  // Deliberately DIFFERENT LENGTHS. rsync's quick check compares size and
  // mtime; two same-size files written in the same second look identical to
  // it, which would make this test pass or fail on a coincidence rather
  // than on the behaviour under test.
  fs.writeFileSync(path.join(src, 'client', 'index.html'),
                   'NEW BUILD with the merged client in it');
  fs.writeFileSync(path.join(src, 'client', 'brand-new.css'), 'added');
  fs.writeFileSync(path.join(src, 'src', 'main.py'), 'new server');
  fs.writeFileSync(path.join(dst, 'client', 'index.html'), 'OLD BUILD');
  fs.writeFileSync(path.join(dst, 'client', 'orphan.js'), 'removed upstream');

  // User-owned state that the allowlist must never touch.
  fs.writeFileSync(path.join(dst, '.env'), 'SECRET=keepme');
  fs.writeFileSync(path.join(dst, 'config.json'), '{"mine":true}');
  return { src, dst };
}

test('a changed file lands in the served copy, not just a new one', () => {
  const { src, dst } = stage();
  const res = bootstrap.syncBundledAssets({
    serverDir: dst,
    bundleResourcesDir: src,
    isPackaged: true,
  });
  assert.equal(res.ok, true, res.details || 'resync reported not ok');

  const served = path.join(dst, 'client', 'index.html');
  assert.equal(
    fs.readFileSync(served, 'utf8'), 'NEW BUILD with the merged client in it',
    'the EDITED file still holds its old contents - this is the defect: a '
    + 'skip-if-exists copy makes a changed file invisible forever'
  );
  assert.equal(
    fs.readFileSync(path.join(dst, 'client', 'brand-new.css'), 'utf8'),
    'added', 'a newly added file did not land'
  );
  assert.equal(
    fs.readFileSync(path.join(dst, 'src', 'main.py'), 'utf8'),
    'new server', 'src/ did not sync'
  );
});

test('an upstream deletion is removed from the served copy', () => {
  const { src, dst } = stage();
  bootstrap.syncBundledAssets({
    serverDir: dst, bundleResourcesDir: src, isPackaged: true,
  });
  assert.equal(
    fs.existsSync(path.join(dst, 'client', 'orphan.js')), false,
    'a file deleted upstream survived in the served copy, so the app would '
    + 'keep serving it'
  );
});

test('user-owned state outside the allowlist is untouched', () => {
  const { src, dst } = stage();
  bootstrap.syncBundledAssets({
    serverDir: dst, bundleResourcesDir: src, isPackaged: true,
  });
  assert.equal(fs.readFileSync(path.join(dst, '.env'), 'utf8'),
               'SECRET=keepme', '.env was clobbered by the resync');
  assert.equal(fs.readFileSync(path.join(dst, 'config.json'), 'utf8'),
               '{"mine":true}', 'config.json was clobbered by the resync');
});
