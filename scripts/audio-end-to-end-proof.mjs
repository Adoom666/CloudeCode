/**
 * End-to-end proof that theme music reaches the speaker.
 *
 * WHAT THIS EXISTS TO CATCH. Four audio fixes shipped while the actual
 * cause sat one layer above all of them: GET /api/v1/themes declared a
 * response_model with no `audio` field, so every theme.json on disk carried
 * a correct block and the client received none of them. Both halves looked
 * fine in isolation - the file had audio, the player worked when handed a
 * config - and nothing tested the JOIN between them.
 *
 * So this script deliberately does NOT hand the player a hand-written
 * config. It takes the manifest the SERVER actually serialises, feeds it to
 * the REAL client modules, opens both gates the way a user tap does, and
 * reports what the playback node ends up holding: resolved src, paused,
 * currentTime, and the final effective gain after the fade.
 *
 * WHAT IT CANNOT PROVE. There is no real decoder and no speaker here, so it
 * cannot prove the bytes are audible - only that every layer this codebase
 * controls hands the next one a live, non-zero-gain, playing node. Decode
 * and audibility are the user's ear.
 *
 * Usage:
 *   python3 scripts/dump_themes_payload.py > /tmp/themes.json
 *   node scripts/audio-end-to-end-proof.mjs /tmp/themes.json
 */

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');

/**
 * Read a client JS file.
 *
 * @param {string} name - file name under client/js/.
 * @returns {string} file contents.
 */
function clientJs(name) {
    return fs.readFileSync(path.join(repoRoot, 'client', 'js', name), 'utf8');
}

/**
 * A stand-in HTMLAudioElement that advances currentTime once played, so
 * "is it actually progressing" is a measurable rather than an assumption.
 */
class FakeAudio {
    constructor() {
        this.handlers = {};
        this.volume = 1;
        this.paused = true;
        this.loop = false;
        this.preload = '';
        this.crossOrigin = null;
        this.error = null;
        this._src = '';
        this._t = 0;
    }

    get src() { return this._src; }

    set src(v) { this._src = v; }

    get currentTime() { return this._t; }

    addEventListener(type, fn) { this.handlers[type] = fn; }

    load() {}

    pause() { this.paused = true; }

    play() { this.paused = false; return Promise.resolve(); }

    removeAttribute() { this._src = ''; }

    /**
     * Advance playback, as a decoding element would.
     *
     * @param {number} seconds - how far to advance.
     * @returns {void}
     */
    tick(seconds) { if (!this.paused) this._t += seconds; }
}

/**
 * Build a sandbox holding the real audio modules on a Web Audio graph.
 *
 * @returns {{win: object, els: FakeAudio[], gains: object[]}}
 */
function loadReal() {
    const els = [];
    const gains = [];
    const store = {};
    const win = {
        console: { log() {}, warn(...a) { console.warn('  [module]', ...a); }, error() {} },
        localStorage: {
            getItem: (k) => (k in store ? store[k] : null),
            setItem: (k, v) => { store[k] = String(v); },
            removeItem: (k) => { delete store[k]; }
        },
        CustomEvent: class {
            constructor(type, init) { this.type = type; Object.assign(this, init); }
        },
        document: { hidden: false, addEventListener() {}, dispatchEvent() {} },
        requestAnimationFrame: () => 1,
        cancelAnimationFrame() {},
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        performance: { now: () => 0 },
        AudioContext: class {
            constructor() { this.state = 'suspended'; this.currentTime = 0; }
            createMediaElementSource() { return { connect: (n) => n, disconnect() {} }; }
            createGain() {
                const g = {
                    gain: {
                        value: 0,
                        cancelScheduledValues() {},
                        setValueAtTime() {},
                        // The real node interpolates; the endpoint is what
                        // matters here, so the ramp lands immediately.
                        linearRampToValueAtTime(v) { g.gain.value = v; }
                    },
                    connect: (n) => n,
                    disconnect() {}
                };
                gains.push(g);
                return g;
            }
            resume() { this.state = 'running'; return Promise.resolve(); }
        },
        webkitAudioContext: null
    };
    win.window = win;
    win.Audio = function () { const a = new FakeAudio(); els.push(a); return a; };

    const ctx = vm.createContext(win);
    vm.runInContext(clientJs('themeAudioNode.js'), ctx);
    vm.runInContext(clientJs('themeAudioSettings.js'), ctx);
    vm.runInContext(clientJs('themeAudio.js'), ctx);
    vm.runInContext(clientJs('themeAudioStatus.js'), ctx);
    return { win, els, gains };
}

const payloadPath = process.argv[2];
if (!payloadPath) {
    console.error('usage: node scripts/audio-end-to-end-proof.mjs <themes.json>');
    process.exit(2);
}

const themes = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const withAudio = themes.filter((t) => t.audio && t.audio.src);

console.log('SERVER PAYLOAD');
console.log(`  themes served:            ${themes.length}`);
console.log(`  carrying an audio block:  ${withAudio.length}`);
if (withAudio.length === 0) {
    console.error('\nFAIL: the API served no audio at all. This is the outage.');
    process.exit(1);
}

let bad = 0;
for (const theme of themes) {
    const cfg = theme.audio;
    if (!cfg) {
        console.log(`\n  ${theme.id}: no audio block (silent theme)`);
        continue;
    }

    const { win, els, gains } = loadReal();
    win.ThemeAudio.init();
    // A user tap: master switch on, then the per-session opt-in.
    win.ThemeAudio.setAppSound(true);
    win.ThemeAudio.setSessionEnabled(true);
    win.ThemeAudio.setTheme(cfg);

    const el = els[0];
    if (el) el.tick(2.5);

    const status = win.ThemeAudio.getStatus();
    const verdict = win.ThemeAudioStatus.describe(status);
    const gain = gains.length ? gains[0].gain.value : null;

    const ok = verdict.playing && gain > 0 && !status.node.paused &&
        status.node.currentTime > 0;
    if (!ok) bad++;

    if (theme.id === 'claude' || !ok) {
        console.log(`\nTHEME ${theme.id}  ${ok ? 'PLAYING' : 'NOT PLAYING'}`);
        console.log(`  manifest volume:      ${cfg.volume}`);
        console.log(`  master volume:        ${win.ThemeAudio.getVolume()}`);
        console.log(`  engine:               ${status.node && status.node.engine}`);
        console.log(`  element created:      ${!!el}`);
        console.log(`  resolved src:         ${el && el.src}`);
        console.log(`  element volume:       ${el && el.volume}`);
        console.log(`  paused:               ${status.node && status.node.paused}`);
        console.log(`  currentTime after 2.5s of playback: ${status.node && status.node.currentTime}`);
        console.log(`  effective gain after fade: ${gain}`);
        console.log(`  status verdict:       playing=${verdict.playing} reason=${verdict.reason}`);
    }
}

console.log(`\n${withAudio.length - bad}/${withAudio.length} themes reach a playing, non-zero-gain node.`);
if (bad > 0) {
    console.error('FAIL');
    process.exit(1);
}
console.log('PASS');
