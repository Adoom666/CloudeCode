// Node tests for client/js/themeAudioStatus.js - the module that turns a
// ThemeAudio snapshot into the sentence the music control shows.
//
// WHY THIS SUITE EXISTS. Five separate causes have made this app silent,
// and every one of them failed the same way: no error, no message, and a
// control that painted itself ON. The API response model dropped the
// manifest audio block, the master and per-theme gains multiplied to about
// -45 LUFS, .m4a was served as an unrecognised mime type under nosniff, the
// element's own volume zeroed the whole Web Audio graph, and a detached
// session kept vetoing the master switch. In all five the user tapped "play
// music", the row said "stop music", and nothing came out.
//
// So the property under test is not "does describe() return nice strings".
// It is: NO reachable snapshot that produces silence may come back as
// playing. The exhaustive sweep at the bottom is the real test; the named
// cases exist so a failure says which cause regressed.
//
// Run with: node tests/test_theme_audio_status.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');

/**
 * Load themeAudioStatus.js into a bare sandbox and return its exports.
 *
 * The module is deliberately pure, so it needs no DOM, no AudioContext and
 * no network - which is the whole reason the diagnosis lives in its own
 * file rather than inside the playback engine.
 *
 * @returns {{describe: Function, current: Function}}
 */
function loadStatus() {
    const win = { console: { log() {}, warn() {}, error() {} } };
    win.window = win;
    const ctx = vm.createContext(win);
    vm.runInContext(
        fs.readFileSync(
            path.join(repoRoot, 'client', 'js', 'themeAudioStatus.js'), 'utf8'
        ),
        ctx
    );
    return win.ThemeAudioStatus;
}

const Status = loadStatus();

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
 * A snapshot of a healthy, audible playing track. Individual tests break
 * exactly one field, so a failure names one cause rather than a soup.
 *
 * @param {object} [over] - fields to override.
 * @returns {object} a ThemeAudio.getStatus()-shaped object.
 */
function healthy(over) {
    return Object.assign({
        sessionName: 'alpha',
        sessionOn: true,
        muted: false,
        masterVolume: 1,
        hidden: false,
        hasTrack: true,
        playError: null,
        loadError: null,
        node: {
            src: '/static/assets/audio/dead-ship.m4a',
            loadedSrc: '/static/assets/audio/dead-ship.m4a',
            paused: false,
            currentTime: 3.2,
            engine: 'webaudio',
            effectiveGain: 0.5
        }
    }, over || {});
}

// ---------------------------------------------------------------------
// The happy path has to be reachable, or every other assertion is vacuous.
// ---------------------------------------------------------------------

test('an audible track reports playing with no reason', () => {
    const v = Status.describe(healthy());
    assert.equal(v.playing, true);
    assert.equal(v.settling, false);
    assert.equal(v.reason, null);
});

// ---------------------------------------------------------------------
// One named case per cause that has actually shipped.
// ---------------------------------------------------------------------

test('CAUSE 1: a theme with no track says so, and that outranks everything', () => {
    // The API response model dropped the manifest audio block, so this was
    // the state of all 23 themes while four unrelated fixes were shipped.
    const v = Status.describe(healthy({ hasTrack: false, node: null }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /no music track/);
});

test('CAUSE 2: a zero master volume is named, not treated as quiet', () => {
    const v = Status.describe(healthy({ masterVolume: 0 }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /master volume/);
});

test('CAUSE 3: an exhausted load outranks the gates', () => {
    // A decode failure cannot be fixed by opening a gate, so naming a gate
    // here would send the user to the wrong control.
    const v = Status.describe(healthy({
        loadError: 'no playable source (a.m4a, a.ogg), last media error code 4',
        sessionOn: false,
        node: null
    }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /failed to load/);
});

test('CAUSE 4: a running track at zero gain is a fault, not playback', () => {
    // The element-volume bug exactly: currentTime advances, play() never
    // rejects, the graph is multiplied by zero. Everything that looks at
    // "is it playing" said yes.
    const v = Status.describe(healthy({
        node: Object.assign(healthy().node, { effectiveGain: 0, currentTime: 4 })
    }));
    assert.equal(v.playing, false);
    assert.equal(v.settling, false);
    assert.match(v.reason, /volume is zero/);
});

test('CAUSE 5: a closed session gate is named as such', () => {
    const v = Status.describe(healthy({ sessionOn: false }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /off for this session/);
});

test('the home screen is named as such, not blamed on a control', () => {
    // Audio is session-only. With no session in scope there is nothing a
    // music track can belong to, and there is no app-level switch left to
    // point the user at, so the reason has to say where sound lives.
    const v = Status.describe(healthy({ sessionName: null }));
    assert.equal(v.playing, false);
    assert.equal(v.settling, false);
    assert.match(v.reason, /only plays inside a session/);
});

test('the retired app sound master switch is named by NO reason', () => {
    // A status branch that can never fire is worse than no branch: it
    // sends whoever reads it looking for a control that does not exist.
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'themeAudioStatus.js'),
        'utf8');
    assert.ok(!/reason:\s*'app sound is off for all sessions'/.test(src));
    assert.ok(!/status\.appSoundOn/.test(src),
        'nothing may read a field ThemeAudio.getStatus() no longer emits');
});

test('a blocked autoplay tells the user to tap again', () => {
    const v = Status.describe(healthy({
        playError: 'NotAllowedError',
        node: Object.assign(healthy().node, { paused: true, currentTime: 0 })
    }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /tap play music again/);
});

test('a declared track with no node is reported, not passed over', () => {
    const v = Status.describe(healthy({ node: null }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /no audio track is loaded/);
});

test('a backgrounded tab explains the pause instead of looking broken', () => {
    const v = Status.describe(healthy({ hidden: true }));
    assert.equal(v.playing, false);
    assert.match(v.reason, /background/);
});

// ---------------------------------------------------------------------
// The third outcome. Collapsing it into either of the other two is the
// defect this whole feature keeps re-committing.
// ---------------------------------------------------------------------

test('a track that has not started yet is settling, never playing', () => {
    const v = Status.describe(healthy({
        node: Object.assign(healthy().node, { paused: true, currentTime: 0 })
    }));
    assert.equal(v.playing, false, 'settling must never read as success');
    assert.equal(v.settling, true, 'nor as a hard failure');
    assert.ok(v.reason);
});

test('a fade-in at zero gain is settling, not a zero-volume fault', () => {
    const v = Status.describe(healthy({
        node: Object.assign(healthy().node, { effectiveGain: 0, currentTime: 0 })
    }));
    assert.equal(v.playing, false);
    assert.equal(v.settling, true);
});

test('a track that played and then stopped is a fault, not settling', () => {
    const v = Status.describe(healthy({
        node: Object.assign(healthy().node, { paused: true, currentTime: 12 })
    }));
    assert.equal(v.playing, false);
    assert.equal(v.settling, false);
    assert.match(v.reason, /paused/);
});

test('a missing snapshot is reported rather than assumed healthy', () => {
    for (const bad of [null, undefined, 'nope', 42]) {
        const v = Status.describe(bad);
        assert.equal(v.playing, false, `describe(${String(bad)}) must not pass`);
        assert.ok(v.reason);
    }
});

test('AbortError is benign and does not mask a real verdict', () => {
    // A rapid pause/play during a theme swap. Reporting it would make the
    // control cry wolf on every theme change.
    const v = Status.describe(healthy({ playError: 'AbortError' }));
    assert.equal(v.playing, true);
});

// ---------------------------------------------------------------------
// THE REAL TEST. Sweep every combination of the fields that can cause
// silence and assert the invariant directly: if the snapshot is not the
// fully-healthy one, describe() must not claim it is playing, and it must
// always hand back a reason when it says so.
// ---------------------------------------------------------------------

test('no silent snapshot is ever reported as playing, and every failure names itself', () => {
    const axes = {
        sessionName: ['alpha', null],
        sessionOn: [true, false],
        hasTrack: [true, false],
        hidden: [true, false],
        masterVolume: [1, 0],
        loadError: [null, 'no playable source (a.m4a), last media error code 4'],
        playError: [null, 'NotAllowedError', 'NotSupportedError'],
        nodeState: ['playing', 'paused-started', 'zero-gain-started', 'absent']
    };

    let checked = 0;
    let sawPlaying = 0;
    for (const sessionName of axes.sessionName) {
        for (const sessionOn of axes.sessionOn) {
            for (const hasTrack of axes.hasTrack) {
                for (const hidden of axes.hidden) {
                    for (const masterVolume of axes.masterVolume) {
                        for (const loadError of axes.loadError) {
                            for (const playError of axes.playError) {
                                for (const nodeState of axes.nodeState) {
                                    const base = healthy().node;
                                    const node = nodeState === 'absent' ? null
                                        : nodeState === 'paused-started'
                                            ? Object.assign({}, base, { paused: true, currentTime: 9 })
                                            : nodeState === 'zero-gain-started'
                                                ? Object.assign({}, base, { effectiveGain: 0, currentTime: 9 })
                                                : Object.assign({}, base);
                                    const snap = {
                                        sessionName, sessionOn, hasTrack, hidden,
                                        masterVolume, loadError, playError, node,
                                        muted: !(sessionName && sessionOn)
                                    };
                                    const v = Status.describe(snap);
                                    checked++;

                                    // The one and only combination that is
                                    // genuinely audible.
                                    const audible = !!sessionName && sessionOn && hasTrack &&
                                        !hidden && masterVolume > 0 && !loadError &&
                                        (!playError || playError === 'AbortError') &&
                                        nodeState === 'playing';

                                    if (v.playing) {
                                        sawPlaying++;
                                        assert.ok(
                                            audible,
                                            'claimed playing on a silent snapshot: ' +
                                                JSON.stringify(snap)
                                        );
                                        assert.equal(v.reason, null);
                                    } else {
                                        assert.ok(
                                            typeof v.reason === 'string' && v.reason.length > 0,
                                            'not playing with no reason given: ' +
                                                JSON.stringify(snap)
                                        );
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    assert.equal(checked, 2 * 2 * 2 * 2 * 2 * 2 * 3 * 4);
    assert.ok(sawPlaying > 0, 'the sweep never reached the audible case');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
