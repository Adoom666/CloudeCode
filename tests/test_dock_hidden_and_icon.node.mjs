// fix/dock-hide-and-icon - the app is a menu bar app, so it must not occupy
// the Dock, and the icon it does ship must be the current artwork.
//
// WHY THIS FILE EXISTS: both halves of "no Dock tile" are easy to half-do.
// `app.dock.hide()` alone still lets a packaged build register a Dock tile at
// launch and drop it a moment later, which the user sees as a bounce.
// `LSUIElement` alone does nothing for a dev run (`electron .`), because the
// plist that applies there is Electron's, not ours. So both are asserted.
//
// Hiding the Dock tile also turns the process into an accessory app, which is
// never activated for you. Any window or dialog it opens can appear behind the
// app the user was actually using - the same defect already reported once for
// "Open Terminal Logs". The activation assertions below guard that.
//
// What this file can and cannot cover:
//   * It CANNOT observe a real Dock tile or a real window ordering. That needs
//     a running Electron process and a human or a window-server query, and is
//     recorded in the branch's report, not here.
//   * It CAN prove the declarations exist, that the icns is a structurally
//     valid multi-resolution icon, and that the committed icns is byte-for-byte
//     what generate-app-icon.sh produces from the committed artwork - which is
//     the only thing that distinguishes a current icon from a stale build.
//
// Run with: node tests/test_dock_hidden_and_icon.node.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');
const macDir = path.join(repoRoot, 'macOS');
const assetsDir = path.join(macDir, 'assets');
const mainJs = fs.readFileSync(path.join(macDir, 'main.js'), 'utf8');
const pkg = JSON.parse(fs.readFileSync(path.join(macDir, 'package.json'), 'utf8'));
const launcherJs = fs.readFileSync(path.join(macDir, 'terminal-launcher.js'), 'utf8');
const icnsPath = path.join(assetsDir, 'icon.icns');

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

/**
 * Parse an .icns container into its type/length table.
 *
 * Inputs: buf (Buffer) - raw contents of an .icns file
 * Outputs: { declaredLength: number, entries: Array<{type: string, length: number, data: Buffer}> }
 * Throws if the magic or the length table is malformed.
 * Example: parseIcns(fs.readFileSync('icon.icns')).entries.map(e => e.type)
 */
function parseIcns(buf) {
    assert.equal(buf.subarray(0, 4).toString('latin1'), 'icns', 'missing icns magic');
    const declaredLength = buf.readUInt32BE(4);
    const entries = [];
    let off = 8;
    while (off < declaredLength) {
        const type = buf.subarray(off, off + 4).toString('latin1');
        const length = buf.readUInt32BE(off + 4);
        assert.ok(length >= 8, `entry ${type} has an impossible length ${length}`);
        entries.push({ type, length, data: buf.subarray(off + 8, off + length) });
        off += length;
    }
    assert.equal(off, declaredLength, 'icns entry table does not land on the declared end');
    return { declaredLength, entries };
}

/**
 * Read width and height out of a PNG IHDR chunk.
 *
 * Inputs: buf (Buffer) - a buffer that starts with a PNG signature
 * Outputs: { width: number, height: number } or null if not a PNG
 */
function pngSize(buf) {
    const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    if (buf.length < 24 || !buf.subarray(0, 8).equals(sig)) return null;
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

// --- no Dock tile ---------------------------------------------------------

test('packaged build declares LSUIElement so no Dock tile is ever registered', () => {
    const info = pkg.build && pkg.build.mac && pkg.build.mac.extendInfo;
    assert.ok(info, 'build.mac.extendInfo is missing');
    assert.equal(
        info.LSUIElement, true,
        'LSUIElement must be true, or the packaged app takes a Dock tile at launch'
    );
});

test('main.js hides the Dock tile at runtime for a dev run', () => {
    assert.ok(
        /app\.dock\.hide\(\)/.test(mainJs),
        'main.js must call app.dock.hide(); LSUIElement does not apply to `electron .`'
    );
    assert.ok(
        /process\.platform !== 'darwin'/.test(mainJs),
        'the Dock call must be platform guarded'
    );
    assert.ok(
        /^hideFromDock\(\);$/m.test(mainJs),
        'hideFromDock() must be invoked at module scope, before app ready, so no tile appears first'
    );
});

// --- what hiding the Dock breaks -----------------------------------------

test('every window fronts the app when it is shown', () => {
    assert.ok(
        /app\.on\('browser-window-created'/.test(mainJs),
        'activation must be wired once for all windows, not per call site'
    );
    assert.ok(
        /win\.on\('show'/.test(mainJs),
        'the handler must run on show, which is when the window becomes visible'
    );
    assert.ok(
        /app\.focus\(\{ steal: true \}\)/.test(mainJs),
        'an accessory app is not activated for you; app.focus({steal:true}) is required'
    );
});

test('every dialog fronts the app before it is displayed', () => {
    for (const name of ['showErrorBox', 'showMessageBox', 'showMessageBoxSync']) {
        assert.ok(
            mainJs.includes(`'${name}'`),
            `${name} must be wrapped so it cannot open behind another app`
        );
    }
    assert.ok(
        /installDialogActivation\(\);/.test(mainJs),
        'the dialog wrappers must actually be installed'
    );
});

// UPDATED on the ship/round4 merge. This test used to assert that main.js
// itself ran an osascript that activates Terminal.app. That code path no
// longer exists: feat/menubar-status-and-config replaced it with an
// executable .command document handed to shell.openPath, so Open Terminal
// Logs routes to the user's DEFAULT terminal rather than hardcoding
// Terminal.app. The AppleScript survives only as the fallback for when no
// handler for .command is registered, and it lives in terminal-launcher.js
// now. The requirement is unchanged and still asserted: whichever path runs,
// an accessory app with no Dock tile must raise the window it just opened.
test('Open Terminal Logs routes to the default terminal, not a hardcoded Terminal.app', () => {
    assert.ok(
        /terminalLauncher\.openServerLogInDefaultTerminal\(/.test(mainJs),
        'Open Terminal Logs must go through the .command launcher'
    );
    assert.ok(
        !/osascript[^\n]*Terminal/.test(mainJs),
        'main.js must not hardcode Terminal.app any more; that is the fallback\'s job'
    );
});

test('the Terminal.app fallback still activates, after it opens the window', () => {
    const script = launcherJs.match(/'tell application "Terminal"'[\s\S]*?\]\.join/);
    assert.ok(script, 'the fallback AppleScript builder is gone or was renamed');
    const body = script[0];
    const doScriptAt = body.indexOf('do script');
    const activateAt = body.indexOf('activate');
    assert.ok(doScriptAt !== -1, 'the fallback must still open a window');
    assert.ok(
        activateAt !== -1,
        'without activate the Terminal window opens behind everything - already reported once'
    );
    assert.ok(
        activateAt > doScriptAt,
        'activate must come AFTER do script so the window just created is the one raised'
    );
});

// --- the icon -------------------------------------------------------------

test('build points at the icns', () => {
    assert.equal(pkg.build.mac.icon, 'assets/icon.icns');
    assert.ok(fs.existsSync(icnsPath), 'assets/icon.icns does not exist');
});

test('icns is a structurally valid multi-resolution icon', () => {
    const buf = fs.readFileSync(icnsPath);
    const { declaredLength, entries } = parseIcns(buf);
    assert.equal(declaredLength, buf.length, 'icns declared length does not match the file size');

    const types = entries.map((e) => e.type);
    // ic04/ic05 are the 16 and 32 point ARGB variants; ic07..ic14 are the PNG
    // ladder up to 512@2x. Missing the large end is what makes a Dock or
    // Finder icon render blurry.
    for (const required of ['ic07', 'ic08', 'ic09', 'ic10', 'ic11', 'ic12', 'ic13', 'ic14']) {
        assert.ok(types.includes(required), `icns is missing the ${required} representation`);
    }

    const expected = {
        ic07: 128, ic08: 256, ic09: 512, ic10: 1024,
        ic11: 32, ic12: 64, ic13: 256, ic14: 512,
    };
    for (const e of entries) {
        const want = expected[e.type];
        if (!want) continue;
        const size = pngSize(e.data);
        assert.ok(size, `${e.type} is not a PNG`);
        assert.equal(size.width, want, `${e.type} should be ${want}px wide, got ${size.width}`);
        assert.equal(size.height, want, `${e.type} should be ${want}px tall, got ${size.height}`);
    }
});

test('committed icns is current - it regenerates byte-identically from the committed artwork', () => {
    if (process.platform !== 'darwin') skip('sips and iconutil are macOS only');
    for (const tool of ['sips', 'iconutil']) {
        if (spawnSync('/usr/bin/which', [tool]).status !== 0) skip(`${tool} not available`);
    }
    const gen = path.join(assetsDir, 'generate-app-icon.sh');
    assert.ok(fs.existsSync(gen), 'generate-app-icon.sh is missing');

    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'icns-currency-'));
    try {
        fs.copyFileSync(path.join(assetsDir, 'AppIcon-1024.png'), path.join(tmp, 'AppIcon-1024.png'));
        fs.copyFileSync(gen, path.join(tmp, 'generate-app-icon.sh'));
        execFileSync('/bin/bash', ['generate-app-icon.sh'], { cwd: tmp, stdio: 'ignore' });
        const rebuilt = fs.readFileSync(path.join(tmp, 'icon.icns'));
        const committed = fs.readFileSync(icnsPath);
        assert.ok(
            rebuilt.equals(committed),
            'assets/icon.icns is STALE - it does not match AppIcon-1024.png. Re-run assets/generate-app-icon.sh.'
        );
    } finally {
        fs.rmSync(tmp, { recursive: true, force: true });
    }
});

await runQueue();
console.log(`${passes} passed, ${failures} failed, ${skips} skipped`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
