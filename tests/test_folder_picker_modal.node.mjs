// THE EXTRACTED FOLDER PICKER STILL BUILDS THE SAME MODAL.
//
// client/js/folder-picker-modal.js came out of launchpad.js on
// 2026-08-26. Before that move the modal had NO behavioural coverage at
// all: the only tests naming `folder-picker-*` classes exercise
// `_showChoiceModal`, which merely reuses the same CSS. So an extraction
// verified by "it still parses" would have been verified by nothing, and
// a silently broken picker would have shipped through a green suite -
// which this repo has done three times for exactly this reason.
//
// THIS FILE IS THE STRUCTURAL HALF ONLY. tests/mini-dom.mjs does not
// parse innerHTML into a tree, and the picker builds its body as an
// innerHTML string and then querySelector()s into it, so every
// behavioural assertion here would fail on the harness rather than on
// the code. mini-dom's own header says as much: anything past its
// surface belongs in a real browser. The behaviour - that it renders the
// listing, escapes a hostile directory name, uses the injected escaper
// and resolves the chosen path - is measured in a real Chromium by
// scripts/verify_folder_picker.py, which carries a --control run so the
// measurement is shown capable of failing.
//
// What is left here is what node can honestly answer: the module's shape,
// and that launchpad.js delegates rather than keeping a second copy.
//
// Run with: node tests/test_folder_picker_modal.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const MODULE = path.join(ROOT, 'client', 'js', 'folder-picker-modal.js');

let passes = 0;
let failures = 0;

/**
 * Description: run one named assertion block, recording rather than
 *   throwing so one failure does not hide the rest.
 * Inputs: name (string), fn (function). Output: Promise<void>.
 */
async function test(name, fn) {
    try {
        await fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Description: load the extracted module over the shared mini-DOM with a
 *   scripted directory listing behind window.API.
 * Inputs: listing (object) - what browseDirectory resolves with.
 * Output: object - {sandbox, document, calls}.
 */
function load(listing) {
    const env = createEnvironment();
    const calls = [];
    const sandbox = env.window;
    sandbox.console = { log() {}, warn() {}, error() {} };
    sandbox.setTimeout = setTimeout;
    sandbox.clearTimeout = clearTimeout;
    sandbox.API = {
        browseDirectory(p) { calls.push(['browseDirectory', p]); return Promise.resolve(listing); },
        makeDirectory(p) { calls.push(['makeDirectory', p]); return Promise.resolve({ path: p }); },
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(MODULE, 'utf8'), sandbox, { filename: 'folder-picker-modal.js' });
    return { sandbox, document: env.document, calls };
}

const LISTING = {
    path: '/Users/demo',
    parent: '/Users',
    entries: [
        { name: 'projects', path: '/Users/demo/projects' },
        { name: 'notes', path: '/Users/demo/notes' },
    ],
};

/**
 * Description: let queued promise callbacks and the module's timers run.
 * Inputs: none. Output: Promise<void>.
 */
const settle = () => new Promise((r) => setTimeout(r, 30));

await test('the module publishes exactly the one entry point it promises', () => {
    const { sandbox } = load(LISTING);
    assert.equal(typeof sandbox.FolderPickerModal, 'object');
    assert.equal(typeof sandbox.FolderPickerModal.open, 'function');
});

await test('launchpad.js delegates instead of keeping its own copy', () => {
    const src = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    // Assert on structure, not on a raw substring: a comment explaining
    // the extraction legitimately mentions the modal, and matching that
    // would be a false result in either direction.
    const code = src.split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('//')
            && !l.trim().startsWith('/*'))
        .join('\n');
    assert.ok(code.includes('window.FolderPickerModal.open('),
        'launchpad.js does not call the extracted module');
    assert.ok(!code.includes('folder-picker-toolbar'),
        'launchpad.js still builds the picker markup itself - the extraction '
        + 'left a second copy behind');
});

await test('the module reaches for no controller state', () => {
    const src = fs.readFileSync(MODULE, 'utf8');
    const code = src.split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('//')
            && !l.trim().startsWith('/*'))
        .join('\n');
    assert.ok(!/\bthis\./.test(code),
        'the extracted module references `this`. It is a plain function by '
        + 'design; a `this` here means it has grown a hidden dependency on '
        + 'whatever calls it, which is the coupling the extraction removed.');
});

await test('the index page loads the module BEFORE launchpad.js', () => {
    const html = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');
    const picker = html.indexOf('/static/js/folder-picker-modal.js');
    const launchpad = html.indexOf('/static/js/launchpad.js');
    assert.ok(picker !== -1, 'folder-picker-modal.js is never loaded at all');
    assert.ok(launchpad !== -1, 'launchpad.js is never loaded at all');
    assert.ok(picker < launchpad,
        'the picker must load first; launchpad.js calls into it');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
