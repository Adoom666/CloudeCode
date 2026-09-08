// THE ARCHIVE IS A HEADER ICON, NOT A MENU ENTRY AND NOT A BODY ROW.
//
// WHAT THIS EXISTS TO CATCH. #archiveBtn has been in index.html the
// whole time - so every "is the button there" assertion passed while the
// owner still could not see it, because header-menu.js's _fold() moved
// the node into the overflow panel at init. The markup and the rendered
// header disagreed, and only the rendered header is what anybody uses.
// So this suite runs the REAL fold over a real mini-DOM and asks where
// the node ENDED UP, rather than whether it exists.
//
// The compensating launchpad body row is gone in the same change: it was
// a full-width card with a title and a description under its own section
// heading, spending vertical space on every visit to buy back a
// destination that was buried in a kebab. One icon costs neither.
//
// Run with: node tests/test_archive_header_icon.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, awaiting async bodies so a rejected
 * promise cannot be reported as a pass.
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
 * @returns {Promise<void>}
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
 * Read a file under the repo root.
 * @param {...string} parts - Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

const HTML = read('client', 'index.html');
const LAUNCHPAD = read('client', 'js', 'launchpad.js');
const CSS = read('client', 'css', 'styles.css');

/**
 * Build a real header, run the REAL header-menu.js over it, and report
 * where each control ended up.
 *
 * @param {object|null} feature - the message_archive block the stubbed
 *   /api/v1/features answers with; null means no API client at all.
 * @returns {object} {btn, controls, panel, flush}.
 */
function mountHeader(feature) {
    const env = createEnvironment({ matches: false });
    const header = env.document.createElement('div');
    header.className = 'header';
    const controls = env.document.createElement('div');
    controls.className = 'controls';
    // Declaration order as index.html has it: the two overflow controls,
    // then the archive icon, then the file editor it sits beside.
    for (const id of ['logoutBtn', 'settingsBtn', 'archiveBtn', 'configEditorBtn']) {
        const b = env.document.createElement('button');
        b.setAttribute('type', 'button');
        b.setAttribute('id', id);
        controls.appendChild(b);
    }
    header.appendChild(controls);
    env.document.body.appendChild(header);

    env.window.API = feature === null ? undefined : {
        call() { return Promise.resolve({ message_archive: feature }); },
    };
    const sandbox = {
        window: env.window,
        document: env.document,
        Promise,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const file of ['kebab-icon.js', 'archive-entry.js', 'dismiss-guard.js', 'header-menu.js']) {
        vm.runInContext(read('client', 'js', file), sandbox, { filename: file });
    }
    sandbox.window.HeaderMenu.init();
    return {
        btn: env.document.getElementById('archiveBtn'),
        controls,
        panel: env.document.getElementById('header-menu-panel'),
        /** Let the availability probe settle. @returns {Promise<void>} */
        flush: () => new Promise((resolve) => setTimeout(resolve, 0)),
    };
}

// ---- POSITIVE CONTROL --------------------------------------------------
// The mount must be shown capable of putting a control in the PANEL, or
// "the archive button is inline" proves nothing - a fold that silently
// did not run would satisfy it just as well.

await test('POSITIVE CONTROL: the fold really runs and really moves nodes', async () => {
    const { controls, panel } = mountHeader({ state: 'enabled', reason: '' });
    assert.ok(panel, 'header-menu.js built no panel, so nothing was folded');
    const inPanel = panel.querySelectorAll('button')
        .map((b) => b.getAttribute('id')).filter(Boolean);
    assert.ok(inPanel.includes('logoutBtn') && inPanel.includes('settingsBtn'),
        'the overflow is empty - the fold did not run, so every "it is not ' +
        'in the panel" assertion below would pass vacuously');
    assert.ok(controls.querySelectorAll('button')
        .some((b) => b.getAttribute('id') === 'configEditorBtn'),
        'the inline row is empty; the mount is not representative');
});

// ---- 1. IT IS AN ICON IN THE HEADER ------------------------------------

await test('the archive control stays INLINE in the header after the fold', async () => {
    const { btn, controls, panel } = mountHeader({ state: 'enabled', reason: '' });
    assert.ok(btn, '#archiveBtn is gone');
    assert.equal(btn.parentNode, controls,
        'the archive control was folded into the overflow panel; the owner ' +
        'asked for an icon in the header, not a menu entry');
    const inPanel = panel.querySelectorAll('button')
        .map((b) => b.getAttribute('id'));
    assert.ok(!inPanel.includes('archiveBtn'),
        '#archiveBtn is in the overflow panel');
});

await test('the icon sits next to the file-editor icon', async () => {
    // "an icon next to files up on top". Adjacency is the whole reason a
    // glyph-only control reads as a browser without a text label.
    const { controls } = mountHeader({ state: 'enabled', reason: '' });
    const ids = controls.querySelectorAll('button')
        .map((b) => b.getAttribute('id'))
        .filter((id) => id === 'archiveBtn' || id === 'configEditorBtn');
    assert.deepEqual(ids, ['archiveBtn', 'configEditorBtn'],
        'the archive icon and the file-editor icon are not adjacent in the ' +
        'inline row');
});

await test('the icon is a real button, keyboard reachable, with a label and a tooltip', async () => {
    const at = HTML.indexOf('id="archiveBtn"');
    assert.ok(at > -1, 'index.html has no #archiveBtn');
    const tag = HTML.slice(at - 60, at + 260);
    // A real <button> is focusable and Enter/Space-activatable for free -
    // which is exactly what the launchpad's div-with-role="button" row
    // had to hand-write. Nothing here should be re-adding tabindex.
    assert.ok(/<button[^>]*type="button"/.test(HTML.slice(at - 60, at + 40)),
        'the archive control is not a real <button>, so it loses keyboard ' +
        'activation unless somebody hand-writes it back');
    assert.ok(/aria-label="Message archive"/.test(tag),
        'the icon has no accessible name; a glyph-only control with no ' +
        'label is unusable with a screen reader');
    assert.ok(/title="message archive"/.test(tag),
        'the icon has no native tooltip');
    assert.ok(/data-tooltip="Message archive"/.test(tag),
        'the icon does not use the app tooltip treatment its neighbours do');
    assert.ok(/<svg/.test(tag),
        'the icon renders no glyph');
});

await test('the icon does not depend on rounded corners or colour alone', async () => {
    // Three themes deliberately zero every radius token, so a control
    // whose only affordance is its radius becomes a flat rectangle there.
    const at = HTML.indexOf('id="archiveBtn"');
    assert.ok(/class="btn-icon"/.test(HTML.slice(at - 60, at + 160)),
        '#archiveBtn does not carry .btn-icon, so it falls through to the ' +
        'user-agent stylesheet as an unstyled square beside its sibling');
    const bat = CSS.indexOf('.btn-icon {');
    const body = CSS.slice(bat, CSS.indexOf('}', bat));
    assert.ok(/border:\s*1px solid/.test(body),
        '.btn-icon has no border, so on a zero-radius theme the control has ' +
        'no shape of its own at all');
    // And the glyph itself is a distinct silhouette, not a tinted square.
    assert.ok(/<rect[^>]*\/>[\s\S]*<path/.test(HTML.slice(at, at + 900)),
        'the archive glyph is not a drawn shape');
});

// ---- 2. THE BODY ROW IS GONE -------------------------------------------

await test('the launchpad body no longer renders an archive row or section', async () => {
    assert.ok(LAUNCHPAD.length > 1000, 'launchpad.js did not load; vacuous');
    assert.ok(!LAUNCHPAD.includes('id="launchpad-archive-entry"'),
        'the archive row is back in the launchpad body');
    assert.ok(!LAUNCHPAD.includes('id="archive-section"'),
        'the archive section heading is back in the launchpad body');
    assert.ok(!/setupArchiveEntry/.test(LAUNCHPAD),
        'setupArchiveEntry is back');
    assert.ok(!/browse ingested transcripts/.test(LAUNCHPAD),
        'the row description is still being rendered into the body');
});

// ---- 3. THE FEATURE FLAG STILL GOVERNS IT ------------------------------
// Moving a control out of a menu changes WHERE it lives, not WHETHER it
// exists. `unknown` is not a flavour of `enabled`: a door drawn on a
// failed probe leads onto a 302 and a wall of 404s.

await test('the icon is ABSENT when the archive is switched off', async () => {
    const { btn, flush } = mountHeader({ state: 'disabled', reason: 'off' });
    await flush();
    assert.equal(btn.style.display, 'none',
        'the archive icon is visible on a server with the archive disabled');
    assert.equal(btn.hidden, true, 'the icon is not hidden from the a11y tree');
});

await test('the icon appears only once the server is MEASURED as enabled', async () => {
    const { btn, flush } = mountHeader({ state: 'enabled', reason: '' });
    await flush();
    assert.equal(btn.style.display, '', 'the icon was never revealed');
    assert.equal(btn.hidden, false);
});

await test('an unmeasurable probe leaves the icon hidden, never shown', async () => {
    for (const feature of [{ state: 'cannot_determine', reason: 'x' },
                           { state: 'nonsense-this-build-does-not-know' },
                           null]) {
        const { btn, flush } = mountHeader(feature);
        await flush();
        assert.equal(btn.style.display, 'none',
            `state ${JSON.stringify(feature)} drew the door on a guess`);
        assert.equal(btn.hidden, true);
    }
});

await test('the icon starts hidden BEFORE the probe answers', async () => {
    // Not merely hidden eventually - hidden from the first frame, or the
    // control flashes onto a screen it may have no business being on.
    const { btn } = mountHeader({ state: 'enabled', reason: '' });
    assert.equal(btn.style.display, 'none',
        'the icon is visible before anything has been measured');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
