// Node tests for client/js/settings-audio.js - the global music volume
// row in the settings panel's general tab.
//
// WHAT THESE EXIST TO PREVENT. This app's background music failed six
// times in a row, silently every time, and the sixth cause was a global
// audio SWITCH that defaulted off and sat in front of every per-session
// control. It was deleted. A global volume is a legitimate replacement
// only for as long as it stays an ATTENUATOR: the moment it can reach
// zero it is the same trap in a new costume, because a user at zero gets
// silence with no error and reports the feature as broken.
//
// So the properties locked down here are:
//  1. The row contains no checkbox, no switch, no mute - nothing that can
//     silence audio as a boolean.
//  2. The slider's minimum is the ENGINE's floor, not zero, and it is
//     read from the engine rather than hardcoded a second time.
//  3. The readout shows the gain the engine ACTUALLY applied, not the
//     number the input happened to hold, so it can never claim a value
//     that was clamped away.
//  4. Dragging applies live, through ThemeAudio.setVolume, rather than
//     waiting for the panel's Save.
//  5. The touch target clears 44px at 390px.
//
// No jsdom in this repo, so render() is asserted as markup (it is a pure
// string builder) and wire() runs against a minimal element stand-in,
// same approach as test_session_theme_menu.node.mjs.
//
// Run with: node tests/test_settings_audio.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');

/**
 * Read a file from the repo.
 *
 * @param {...string} parts - path segments below the repo root.
 * @returns {string} File contents.
 */
function repoFile(...parts) {
    return fs.readFileSync(path.join(repoRoot, ...parts), 'utf8');
}

let failures = 0;
let passes = 0;

/**
 * Run one named assertion, recording the outcome.
 *
 * @param {string} name - Test name.
 * @param {Function} fn - Body; throwing marks a failure.
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * A stand-in element: records attributes, handlers and text, and can
 * fire an event at its own handler.
 */
class FakeEl {
    /**
     * @param {object} attrs - initial attributes.
     */
    constructor(attrs) {
        this.attrs = Object.assign({}, attrs || {});
        this.handlers = {};
        this.textContent = '';
        this.value = this.attrs.value;
    }

    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }

    setAttribute(k, v) { this.attrs[k] = String(v); }

    addEventListener(type, fn) {
        this.handlers[type] = (this.handlers[type] || []).concat(fn);
    }

    /**
     * Fire every handler registered for a type.
     *
     * @param {string} type - event name.
     * @returns {number} how many handlers ran.
     */
    fire(type) {
        (this.handlers[type] || []).forEach((fn) => fn());
        return (this.handlers[type] || []).length;
    }
}

/**
 * Load settings-audio.js against a stub ThemeAudio, and return the
 * sandbox window plus the calls the stub recorded.
 *
 * @param {object} opts - {volume, min, clampTo} for the stub engine.
 * @returns {{win: object, calls: number[]}}
 */
function loadModule(opts) {
    const o = Object.assign({ volume: 1, min: 0.35, clampTo: null }, opts || {});
    const calls = [];
    const win = {
        console: { log() {}, warn() {}, error() {} },
        ThemeAudioSettings: { DEFAULT_MASTER_VOLUME: 1, MIN_MASTER_VOLUME: o.min },
        ThemeAudio: {
            getVolume: () => o.volume,
            getMinVolume: () => o.min,
            setVolume: (v) => {
                calls.push(v);
                const applied = o.clampTo === null
                    ? Math.max(o.min, Math.min(1, v)) : o.clampTo;
                o.volume = applied;
                return applied;
            }
        }
    };
    win.window = win;
    const ctx = vm.createContext(win);
    vm.runInContext(repoFile('client', 'js', 'settings-audio.js'), ctx);
    return { win, calls };
}

/**
 * Build a root stand-in whose querySelector serves the slider and the
 * readout by the selectors settings-audio.js uses.
 *
 * @param {FakeEl} slider - the range input stand-in.
 * @param {FakeEl} readout - the output stand-in.
 * @returns {object} a root with querySelector.
 */
function fakeRoot(slider, readout) {
    return {
        querySelector(sel) {
            if (sel === '[data-settings-volume="master"]') return slider;
            if (sel === '#settings-master-volume-value') return readout;
            return null;
        }
    };
}

// ---------------------------------------------------------------------
// Markup: an attenuator, and nothing that can act as a switch.
// ---------------------------------------------------------------------

test('the row is a slider and ONLY a slider - no mute in any costume', () => {
    const html = loadModule().win.SettingsAudio.render();
    assert.ok(/type="range"/.test(html), 'no range input rendered');
    assert.ok(!/type="checkbox"/.test(html), 'a checkbox here is a mute');
    assert.ok(!/\brole="switch"/.test(html), 'a switch here is a mute');
    assert.ok(!/\bmute|\bsilence audio|turn (music|sound) off/i.test(html),
        'the row must not offer an off');
    assert.ok(!/<button/.test(html), 'no toggle button belongs in this row');
});

test('THE FLOOR: the slider minimum is the engine floor, never zero', () => {
    const html = loadModule({ min: 0.35 }).win.SettingsAudio.render();
    const min = /min="(\d+)"/.exec(html);
    assert.ok(min, 'no min attribute - the slider would bottom out at 0');
    assert.equal(min[1], '35');
    assert.notEqual(min[1], '0');
});

test('the floor comes FROM the engine, not a second hardcoded copy', () => {
    // Move the engine's floor and the markup must follow it. A literal 35
    // in the renderer passes the test above and fails this one.
    const html = loadModule({ min: 0.5, volume: 1 }).win.SettingsAudio.render();
    assert.ok(/min="50"/.test(html), 'the rendered floor ignored the engine');
});

test('the current value is shown, so it is not a mystery slider', () => {
    const html = loadModule({ volume: 0.6 }).win.SettingsAudio.render();
    assert.ok(/value="60"/.test(html), 'the slider is not seeded from the engine');
    assert.ok(/>60%</.test(html), 'the current value is not displayed');
});

test('a stored value under the floor still renders inside the track', () => {
    // An out-of-band value must not produce a slider whose value is below
    // its own min, which browsers silently snap and which would then read
    // back as a change the user never made.
    const html = loadModule({ volume: 0.1, min: 0.35 }).win.SettingsAudio.render();
    assert.ok(/value="35"/.test(html));
});

test('the hint names the floor and points at the real on/off', () => {
    const html = loadModule().win.SettingsAudio.render();
    assert.ok(/35%/.test(html), 'the floor is not explained');
    assert.ok(/play music/.test(html),
        'the hint must send the user to the per-session control for on/off');
});

// ---------------------------------------------------------------------
// Behaviour: live, clamped, and honest about what was applied.
// ---------------------------------------------------------------------

test('LIVE: an input event applies the gain immediately', () => {
    const { win, calls } = loadModule({ volume: 1 });
    const slider = new FakeEl({ value: '100' });
    const readout = new FakeEl({});
    win.SettingsAudio.wire(fakeRoot(slider, readout));

    slider.value = '70';
    slider.fire('input');
    assert.deepEqual(calls, [0.7], 'the engine was not driven on input');
    assert.equal(readout.textContent, '70%');
});

test('the readout reports what the ENGINE applied, not what was asked', () => {
    // The clamp lives in the engine. A readout built from the input value
    // would claim 10% while the engine plays 35%, which is exactly the
    // class of lie this feature keeps shipping.
    const { win } = loadModule({ clampTo: 0.35 });
    const slider = new FakeEl({ value: '100' });
    const readout = new FakeEl({});
    win.SettingsAudio.wire(fakeRoot(slider, readout));

    slider.value = '10';
    slider.fire('input');
    assert.equal(readout.textContent, '35%');
});

test('wiring twice does not double-apply', () => {
    const { win, calls } = loadModule();
    const slider = new FakeEl({ value: '80' });
    const readout = new FakeEl({});
    const root = fakeRoot(slider, readout);
    win.SettingsAudio.wire(root);
    win.SettingsAudio.wire(root);

    slider.fire('input');
    assert.equal(calls.length, 1, 'the panel remounts on every save - one handler only');
});

test('wire() is safe when the section is absent', () => {
    const { win } = loadModule();
    assert.equal(win.SettingsAudio.wire(fakeRoot(null, null)), null);
    assert.equal(win.SettingsAudio.wire(null), null);
});

test('render() survives with no audio engine on the page', () => {
    // The settings panel must still open if themeAudio.js failed to load.
    const win = { console: { log() {} } };
    win.window = win;
    const ctx = vm.createContext(win);
    vm.runInContext(repoFile('client', 'js', 'settings-audio.js'), ctx);
    const html = win.SettingsAudio.render();
    assert.ok(/type="range"/.test(html));
    assert.ok(/min="35"/.test(html), 'the floor must hold without the engine');
});

// ---------------------------------------------------------------------
// Placement and touch target.
// ---------------------------------------------------------------------

test('the volume row lives in the general tab, beside appearance', () => {
    const panel = repoFile('client', 'js', 'settings-panel.js');
    const general = /\{ id: 'general'[^}]*\}/.exec(panel);
    assert.ok(general, 'no general tab declared');
    assert.ok(/'audio'/.test(general[0]),
        'the audio slot is not in the general tab');
    // 'agents' is absent from this list because that tab no longer
    // exists: wrappers cover every family, so its four
    // `<family>_command` keys folded into the wrappers screen. The
    // assertion below requires each named tab to be FOUND, so leaving a
    // dead name here would fail for the wrong reason.
    for (const other of ['wrappers', 'terminal', 'notifications']) {
        const tab = new RegExp(`\\{ id: '${other}'[^}]*\\}`).exec(panel);
        assert.ok(tab && !/'audio'/.test(tab[0]),
            `the audio slot leaked into the ${other} tab`);
    }
});

test('the panel renders and wires the section', () => {
    const panel = repoFile('client', 'js', 'settings-panel.js');
    assert.ok(/SettingsAudio\.render\(\)/.test(panel));
    assert.ok(/SettingsAudio\.wire\(/.test(panel));
});

test('the section is not part of the batched PATCH', () => {
    // It is a browser-local preference applied live. A key reaching the
    // server would be a silent no-op the user could not explain.
    const panel = repoFile('client', 'js', 'settings-panel.js');
    const sections = /var SECTIONS = \[[\s\S]*?\n    \];/.exec(panel);
    assert.ok(sections, 'SECTIONS not found');
    assert.ok(!/audio|volume/i.test(sections[0]),
        'the volume must not be a collected field');
});

test('TOUCH: the hit area clears 44px and the thumb is thumb-sized', () => {
    const css = repoFile('client', 'css', 'settings-audio.css');
    const row = /\.settings-volume-row \{[\s\S]*?\}/.exec(css);
    assert.ok(row && /min-height: 44px/.test(row[0]),
        'the row is under the 44px touch minimum');

    const slider = /\.settings-volume-slider \{[\s\S]*?\}/.exec(css);
    assert.ok(slider && /height: 44px/.test(slider[0]),
        'the input must fill the row, or only the 6px track is live');
    assert.ok(/touch-action: pan-y/.test(slider[0]),
        'without pan-y a vertical flick fights the slider');

    // Both vendor thumbs, or one platform gets the 16px default.
    for (const sel of ['::-webkit-slider-thumb', '::-moz-range-thumb']) {
        const thumb = new RegExp(`\\.settings-volume-slider${sel} \\{[\\s\\S]*?\\}`).exec(css);
        assert.ok(thumb, `no ${sel} rule`);
        const px = /width: (\d+)px/.exec(thumb[0]);
        assert.ok(px && Number(px[1]) >= 24, `${sel} thumb is ${px && px[1]}px`);
    }
});

test('the 500-line budget still holds for every file this touched', () => {
    // themeAudio.js sat at 499 lines before this feature. Growing it was
    // what forced the master gain out into themeAudioVolume.js, and the
    // budget is the thing that keeps forcing that choice.
    for (const f of ['themeAudio.js', 'themeAudioSettings.js',
        'themeAudioVolume.js', 'settings-audio.js', 'settings-panel.js']) {
        const lines = repoFile('client', 'js', f).split('\n').length;
        assert.ok(lines <= 500, `${f} is ${lines} lines, over the 500 budget`);
    }
});

test('both assets are served, in an order that works', () => {
    const html = repoFile('client', 'index.html');
    assert.ok(/css\/settings-audio\.css/.test(html), 'the stylesheet is not linked');
    // Match the script TAGS, not any mention: these filenames also appear
    // in the surrounding comments, and indexOf on a bare name would
    // measure the order of the prose.
    const tag = (name) => html.indexOf(`<script src="/static/js/${name}"></script>`);
    const js = tag('settings-audio.js');
    const engine = tag('themeAudio.js');
    const volume = tag('themeAudioVolume.js');
    assert.ok(volume > 0 && volume < engine,
        'themeAudioVolume.js must load before the engine that delegates to it');
    const panel = tag('settings-panel.js');
    assert.ok(js > 0 && engine > 0 && panel > 0, 'a script tag is missing');
    assert.ok(engine < js, 'settings-audio.js reads window.ThemeAudio at render time');
    assert.ok(js < panel, 'settings-panel.js renders the section at open');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
