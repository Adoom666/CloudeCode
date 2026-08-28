// Node test for macOS/scripts/adhoc-sign.js and the signing wiring around it.
//
// WHY THIS FILE EXISTS: a release rehearsal produced a DMG that macOS refused
// to open at all. electron-builder could not find a signing identity, logged
// "skipped macOS application code signing", and exited 0. The bundle it left
// had no Contents/_CodeSignature directory, so the only signature was the
// ad-hoc one the linker puts on the arm64 executable. Both `codesign --verify`
// and `spctl --assess` failed with "code has no resources but signature
// indicates they must be present", and once a browser download marked the app
// quarantined, macOS showed "Cloude Code is damaged and can't be opened".
// Right click > Open does NOT clear that dialog. The user's only escape was
// `xattr -dr com.apple.quarantine` in Terminal.
//
// Every signal in that build was green. The build exited 0, the DMG was the
// right size, and the app ran fine on the machine that built it because a
// locally-built app is never quarantined. So the assertions here are about the
// ARTIFACT, never about an exit code.
//
// What this file can and cannot cover:
//   * It CANNOT build a real DMG. That is a ~4 minute, 117 MB electron-builder
//     run, which does not belong in a unit test. The published DMG is instead
//     verified end-to-end by the "Verify the DMG is properly signed" step in
//     .github/workflows/release.yml, which mounts the real artifact. The last
//     test below asserts that step still exists, because it is the only check
//     that sees what the user downloads.
//   * It CAN prove the hook itself does what it claims, by signing a real
//     minimal bundle. That runs on macOS only; on other platforms codesign
//     does not exist and those assertions are reported as skipped, never as
//     passed. A check that silently passes when it could not run is the exact
//     defect this file exists to prevent.
//
// Run with: node tests/test_adhoc_sign.node.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');
const macDir = path.join(repoRoot, 'macOS');
const hookPath = path.join(macDir, 'scripts', 'adhoc-sign.js');
const require = createRequire(import.meta.url);

let failures = 0;
let passes = 0;
let skips = 0;
const queue = [];

function test(name, fn) {
    queue.push([name, fn]);
}

async function runQueue() {
    for (const [name, fn] of queue) {
        try {
            await fn();
            passes += 1;
        } catch (err) {
            if (err && err.skip) {
                skips += 1;
                console.log(`SKIP ${name}: ${err.message}`);
                continue;
            }
            failures += 1;
            console.error(`FAIL ${name}: ${err && err.message}`);
        }
    }
}

/** Mark the current test as skipped rather than passed. */
function skip(reason) {
    const e = new Error(reason);
    e.skip = true;
    throw e;
}

// --- wiring: the hook must actually be connected -------------------------

test('package.json wires the afterPack hook to a file that exists', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(macDir, 'package.json'), 'utf8'));
    assert.equal(
        pkg.build.afterPack,
        'scripts/adhoc-sign.js',
        'build.afterPack must point at the ad-hoc signing hook. Without it ' +
            'electron-builder skips signing and ships a bundle macOS calls damaged.'
    );
    assert.ok(fs.existsSync(hookPath), `${hookPath} must exist`);
});

test('the hook exports a function and its signing primitive', () => {
    const mod = require(hookPath);
    assert.equal(typeof mod, 'function', 'afterPack hooks are called as functions');
    assert.equal(typeof mod.adhocSign, 'function', 'adhocSign must be exported for this test');
});

test('the hook is not shipped inside the app bundle', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(macDir, 'package.json'), 'utf8'));
    assert.ok(
        pkg.build.files.includes('!scripts/**'),
        'build tooling must be excluded from the packaged asar'
    );
});

// --- version: one source, so the four surfaces cannot disagree -----------

test('package.json version is not the upstream 0.8.1', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(macDir, 'package.json'), 'utf8'));
    assert.notEqual(
        pkg.version,
        '0.8.1',
        'our DMG must not be named identically to upstream\'s published 0.8.1 asset'
    );
    assert.match(pkg.version, /^\d+\.\d+\.\d+$/, 'version must be a plain semver triple');
});

test('the app version reaches the web client from package.json alone', () => {
    // macOS/package.json -> app.getVersion() -> CLOUDE_APP_VERSION ->
    // src/core/version.py::resolve_version() (imported by src/main.py) ->
    // {{VERSION}} in client/index.html. If any link is renamed the chip
    // silently renders blank, which looks like a styling bug, not a broken
    // release. Assert the chain by name.
    const sm = fs.readFileSync(path.join(macDir, 'server-manager.js'), 'utf8');
    assert.ok(
        sm.includes('CLOUDE_APP_VERSION') && sm.includes('app.getVersion()'),
        'server-manager.js must inject app.getVersion() as CLOUDE_APP_VERSION'
    );
    // src/main.py no longer reads the env var itself: it delegates to the
    // single resolver in src/core/version.py (env-var-first precedence), so
    // the name is asserted there and main.py is asserted to call it.
    const versionPy = fs.readFileSync(path.join(repoRoot, 'src', 'core', 'version.py'), 'utf8');
    assert.ok(
        versionPy.includes('CLOUDE_APP_VERSION'),
        'src/core/version.py must read CLOUDE_APP_VERSION'
    );
    const mainPy = fs.readFileSync(path.join(repoRoot, 'src', 'main.py'), 'utf8');
    assert.ok(
        mainPy.includes('resolve_version'),
        'src/main.py must delegate to src/core/version.py::resolve_version'
    );
    const html = fs.readFileSync(path.join(repoRoot, 'client', 'index.html'), 'utf8');
    assert.ok(html.includes('{{VERSION}}'), 'client/index.html must keep the {{VERSION}} token');
});

// --- behaviour: the hook produces a genuinely valid signature ------------

test('adhocSign seals a real bundle and the signature verifies', () => {
    if (os.platform() !== 'darwin') {
        skip('codesign only exists on macOS');
    }

    const { adhocSign } = require(hookPath);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'adhoc-sign-test-'));
    const appPath = path.join(tmp, 'Fixture.app');
    const macOSDir = path.join(appPath, 'Contents', 'MacOS');
    fs.mkdirSync(macOSDir, { recursive: true });

    // A real Mach-O is required; codesign will not sign a shell script as a
    // bundle executable. /bin/echo is small, always present, and irrelevant.
    fs.copyFileSync('/bin/echo', path.join(macOSDir, 'Fixture'));
    fs.writeFileSync(
        path.join(appPath, 'Contents', 'Info.plist'),
        `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Fixture</string>
<key>CFBundleIdentifier</key><string>com.cloudecode.signfixture</string>
<key>CFBundleName</key><string>Fixture</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
`
    );

    // Precondition: an unsigned bundle has no sealed resources. If this ever
    // stops being true the test below proves nothing.
    const sealed = path.join(appPath, 'Contents', '_CodeSignature', 'CodeResources');
    assert.ok(!fs.existsSync(sealed), 'fixture must start unsigned');

    adhocSign(appPath);

    // The exact thing that was missing from the broken release build.
    assert.ok(fs.existsSync(sealed), 'Contents/_CodeSignature/CodeResources must exist after signing');

    // And it must actually verify, not merely be present.
    const out = execFileSync('codesign', ['--verify', '--deep', '--strict', '--verbose=2', appPath], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    assert.doesNotThrow(() => out);

    // It is ad-hoc: no identity, no team. Asserting this keeps the test honest
    // about what we ship, so nobody reads a passing test as "properly signed".
    // `codesign -d` writes its report to stderr, hence spawnSync over
    // execFileSync, which would hand back an empty stdout and assert nothing.
    const info = spawnSync('codesign', ['-dvv', appPath], { encoding: 'utf8' });
    const report = `${info.stdout || ''}${info.stderr || ''}`;
    assert.ok(/Signature=adhoc/.test(report), `signature must be ad-hoc, got: ${report.trim()}`);

    fs.rmSync(tmp, { recursive: true, force: true });
});

// --- the only check that sees the real artifact --------------------------

test('release workflow still verifies the published DMG signature', () => {
    const wf = fs.readFileSync(
        path.join(repoRoot, '.github', 'workflows', 'release.yml'),
        'utf8'
    );
    assert.ok(
        wf.includes('Verify the DMG is properly signed'),
        'the workflow must verify the DMG it publishes'
    );
    assert.ok(
        wf.includes('_CodeSignature/CodeResources'),
        'the workflow must assert the sealed resource directory exists'
    );
    assert.ok(
        wf.includes('codesign --verify --deep --strict'),
        'the workflow must strictly verify the signature'
    );
});

await runQueue();
console.log(`${passes} passed, ${failures} failed, ${skips} skipped`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
