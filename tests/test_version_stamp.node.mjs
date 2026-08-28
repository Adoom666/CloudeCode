/**
 * The VERSION stamp bootstrap writes into Application Support.
 *
 * WHY THIS SUITE EXISTS. In production the Python server runs from a copy of
 * src/ + client/ with no .git and no macOS/package.json. Three of the five
 * sources src/core/version.py can consult are absent there by construction.
 * The env var covers the Electron spawn; the VERSION file is the only thing
 * that survives on disk. If the header this writes ever stops matching the
 * header that file reads, the resolver silently returns the comment line
 * instead of the version, so the two are asserted against each other here
 * rather than trusted to stay in sync.
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
 * Make a throwaway serverDir.
 * @returns {string} the directory path.
 */
function tmpServerDir() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'cc-version-stamp-'));
}

test('the stamp writes a VERSION file the python resolver can read', () => {
    const dir = tmpServerDir();
    assert.equal(bootstrap.writeVersionStamp(dir, '0.8.1'), true);
    const text = fs.readFileSync(path.join(dir, 'VERSION'), 'utf8');
    assert.ok(text.startsWith(bootstrap.VERSION_FILE_HEADER));
    // The resolver skips comment lines and blank lines, then takes the first
    // real line. That line must be the bare version.
    const value = text.split('\n')
        .map((l) => l.trim())
        .filter((l) => l && !l.startsWith('#'))[0];
    assert.equal(value, '0.8.1');
});

test('the js header is byte-identical to the python one', () => {
    const py = fs.readFileSync(
        path.join(repoRoot, 'src', 'core', 'version.py'), 'utf8'
    );
    // The two halves of VERSION_FILE_HEADER as python string literals.
    for (const line of bootstrap.VERSION_FILE_HEADER.split('\n')) {
        if (!line) continue;
        assert.ok(
            py.includes(line),
            `src/core/version.py must contain the header line: ${line}`
        );
    }
});

test('an upgrade overwrites the previous release stamp', () => {
    const dir = tmpServerDir();
    bootstrap.writeVersionStamp(dir, '0.8.1');
    bootstrap.writeVersionStamp(dir, '0.9.0');
    const text = fs.readFileSync(path.join(dir, 'VERSION'), 'utf8');
    assert.match(text, /0\.9\.0/);
    assert.doesNotMatch(text, /0\.8\.1/);
});

test('an empty version writes nothing rather than an empty stamp', () => {
    // A VERSION file containing only the header would resolve to "", which
    // is the same as no file at all but looks deliberate. Do not create it.
    const dir = tmpServerDir();
    assert.equal(bootstrap.writeVersionStamp(dir, ''), false);
    assert.equal(bootstrap.writeVersionStamp(dir, undefined), false);
    assert.equal(fs.existsSync(path.join(dir, 'VERSION')), false);
});

test('an unwritable target is reported, not thrown', () => {
    // A stamp failure must never take down bootstrap: the env var still
    // covers the spawn path, so this degrades rather than fails.
    assert.equal(
        bootstrap.writeVersionStamp('/nonexistent-dir-for-this-test', '1.0.0'),
        false
    );
});
