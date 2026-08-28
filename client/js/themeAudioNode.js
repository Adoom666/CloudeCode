/**
 * ThemeAudioNode - the mechanics of one playback node.
 *
 * Split out of themeAudio.js, which owns the POLICY (is the session opted
 * in, is the tab visible, which theme is active) while this file owns the
 * MECHANICS (build the element, ramp its gain, start it, tear it down).
 * The two were one file and it grew past the 500-line limit; the seam is
 * real rather than arbitrary, because everything here is stateless with
 * respect to mute and visibility and takes the node as an argument.
 *
 * A "node" is a plain object owned by themeAudio.js:
 *   {
 *     audio: HTMLAudioElement,   // always present
 *     sources: string[],         // candidate URLs, best format first
 *     sourceIndex: number,       // index into sources currently loaded
 *     src: string,               // track IDENTITY (manifest primary)
 *     loadedSrc: string,         // candidate actually on the element
 *     targetVolume: number,      // 0..1 from the manifest
 *     fadeMs: number,
 *     gain: GainNode|null,       // null in element mode
 *     sourceNode: MediaElementAudioSourceNode|null,
 *     rafHandle: number|null     // element-mode ramp handle
 *   }
 *
 * Exposed as window.ThemeAudioNode. Loaded BEFORE themeAudio.js.
 */
(function () {
    'use strict';

    if (window.ThemeAudioNode) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    /** Name of the most recent play() rejection, or null after a clean one. */
    var lastPlayError = null;

    /**
     * Description of the most recent EXHAUSTED load, or null.
     *
     * Set only when every declared source for a node has failed, which is
     * the point at which the node is dropped and the app goes permanently
     * silent for that theme. Until this existed the only trace was a
     * console.warn, so a decode failure and a theme with no track were
     * indistinguishable from the UI.
     */
    var lastLoadError = null;

    /**
     * The shared AudioContext, built lazily so construction always happens
     * inside a user gesture, and the engine that decision settled on:
     * 'webaudio' | 'element' | null (undecided). Both are per-session and
     * belong here rather than in themeAudio.js, which owns policy only.
     */
    var audioCtx = null;
    var engineKind = null;

    /**
     * Target gain for a theme whose manifest declares audio but omits a
     * volume. Deliberately mid-scale, not unity.
     */
    var DEFAULT_TRACK_VOLUME = 0.5;

    /**
     * The shared AudioContext, constructing it on first use.
     *
     * @returns {AudioContext|null} null when the engine has none.
     */
    function ensureCtx() {
        if (audioCtx) return audioCtx;
        try {
            var Ctor = window.AudioContext || window.webkitAudioContext;
            if (!Ctor) return null;
            audioCtx = new Ctor();
            return audioCtx;
        } catch (err) {
            console.warn('ThemeAudioNode: AudioContext construction failed', err);
            return null;
        }
    }

    /**
     * The shared AudioContext without constructing one.
     *
     * @returns {AudioContext|null}
     */
    function getCtx() { return audioCtx; }

    /**
     * Resume the context if the browser suspended it. Safe to call always.
     *
     * @returns {void}
     */
    function resumeCtx() {
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(function (err) {
                console.warn('ThemeAudioNode: ctx.resume() rejected', err);
            });
        }
    }

    /**
     * The ordered list of URLs to try for one manifest `audio` block.
     *
     * The order is FIXED, not chosen by canPlayType: on iOS, canPlayType
     * returns "probably" for Ogg Vorbis and the decode then fails with
     * MEDIA_ERR_SRC_NOT_SUPPORTED, so a capability probe would pick the
     * unplayable file. See the header of themeAudio.js for the measurement.
     *
     * @param {{src: string, srcFallback?: string}} cfg - a manifest block.
     * @returns {string[]} candidates, primary first, duplicates dropped.
     */
    function candidates(cfg) {
        var out = [cfg.src];
        if (typeof cfg.srcFallback === 'string' && cfg.srcFallback &&
            cfg.srcFallback !== cfg.src) {
            out.push(cfg.srcFallback);
        }
        return out;
    }

    /**
     * Ramp a node to `target` over `durationMs`.
     *
     * In webaudio mode this is GainNode automation; in element mode a
     * requestAnimationFrame loop driving audio.volume. Either way the
     * current value is anchored first, so back-to-back ramps never click.
     *
     * @param {object} node - the playback node.
     * @param {number} target - 0..1 destination gain.
     * @param {number} durationMs - ramp length in ms.
     * @param {AudioContext|null} ctx - the shared context, or null.
     * @returns {void}
     */
    function ramp(node, target, durationMs, ctx) {
        if (!node || !node.audio) return;
        var clamped = Math.max(0, Math.min(1, target));
        var dur = Math.max(0, durationMs);

        if (node.gain && ctx) {
            var now = ctx.currentTime;
            try {
                node.gain.gain.cancelScheduledValues(now);
                node.gain.gain.setValueAtTime(node.gain.gain.value, now);
                node.gain.gain.linearRampToValueAtTime(clamped, now + dur / 1000);
            } catch (e) {
                console.warn('ThemeAudioNode: gain ramp threw', e);
            }
            return;
        }

        if (node.rafHandle != null) {
            try { cancelAnimationFrame(node.rafHandle); } catch (_) { /* ignore */ }
            node.rafHandle = null;
        }
        var startVol = node.audio.volume;
        var startT = (typeof performance !== 'undefined' ? performance.now() : Date.now());

        /**
         * One frame of the element-mode ramp.
         *
         * @param {number} t - the rAF timestamp.
         * @returns {void}
         */
        function step(t) {
            if (!node.audio) return; // torn down mid-ramp
            var k = dur === 0 ? 1 : Math.min(1, (t - startT) / dur);
            try {
                node.audio.volume = startVol + (clamped - startVol) * k;
            } catch (_) { /* ignore */ }
            node.rafHandle = k < 1 ? requestAnimationFrame(step) : null;
        }
        node.rafHandle = requestAnimationFrame(step);
    }

    /**
     * Stop a node, disconnect its Web Audio graph and drop the buffer.
     *
     * @param {object|null} node - the playback node, or null.
     * @returns {void}
     */
    function teardown(node) {
        if (!node) return;
        try { node.audio.pause(); } catch (_) { /* ignore */ }
        if (node.rafHandle != null) {
            try { cancelAnimationFrame(node.rafHandle); } catch (_) { /* ignore */ }
            node.rafHandle = null;
        }
        if (node.sourceNode) {
            try { node.sourceNode.disconnect(); } catch (_) { /* ignore */ }
            node.sourceNode = null;
        }
        if (node.gain) {
            try { node.gain.disconnect(); } catch (_) { /* ignore */ }
            node.gain = null;
        }
        // Detach src so the browser stops decoding and buffering.
        try {
            node.audio.removeAttribute('src');
            node.audio.load();
        } catch (_) { /* ignore */ }
    }

    /**
     * Start a node, recording any rejection.
     *
     * A rejected play() is invisible by construction: no error event, no
     * exception, just silence. It is logged even in the expected case,
     * because "the user tapped and heard nothing" is precisely the bug this
     * module has already shipped once, and the console is the only place
     * that failure is visible.
     *
     * NotAllowedError means the autoplay grant is missing - expected before
     * the first tap, and a REAL problem after one, because it means the
     * play() call lost its user activation. AbortError is a rapid
     * pause/play during a theme swap and is benign.
     *
     * @param {object} node - the playback node.
     * @returns {void}
     */
    function play(node) {
        if (!node || !node.audio) return;
        var p;
        try {
            p = node.audio.play();
        } catch (e) {
            lastPlayError = e && e.name ? e.name : 'Error';
            console.warn('ThemeAudioNode: play() threw', e);
            return;
        }
        lastPlayError = null;
        if (p && typeof p.then === 'function') {
            p.catch(function (err) {
                var name = err && err.name ? err.name : 'Error';
                lastPlayError = name;
                if (name === 'AbortError') return;
                console.warn('ThemeAudioNode: play() rejected', name, err && err.message);
            });
        }
    }

    /**
     * The name of the last play() rejection, or null if it was clean.
     *
     * @returns {string|null}
     */
    function getLastPlayError() {
        return lastPlayError;
    }

    /**
     * Description of the last exhausted load, or null if none has happened
     * since the current node was built.
     *
     * @returns {string|null}
     */
    function getLastLoadError() {
        return lastLoadError;
    }

    /**
     * Handle a media load error: advance to the next declared format if
     * there is one, otherwise tear the node down.
     *
     * Retrying reuses the SAME element, so the Web Audio graph hanging off
     * it (MediaElementAudioSourceNode -> GainNode) survives the swap.
     * createMediaElementSource can only ever be called once per element,
     * so building a fresh element here would throw on the second attempt.
     *
     * @param {object} node - the node whose element errored.
     * @param {object} host - the policy callbacks passed to makeNode().
     * @returns {void}
     */
    function _onNodeError(node, host) {
        var el = node.audio;
        var code = el && el.error ? el.error.code : null;
        var next = node.sourceIndex + 1;

        if (next < node.sources.length) {
            console.warn('ThemeAudioNode: ' + node.sources[node.sourceIndex] +
                ' failed (code ' + code + '), trying ' + node.sources[next]);
            node.sourceIndex = next;
            node.loadedSrc = node.sources[next];
            try {
                el.src = node.sources[next];
                el.load();
            } catch (err) {
                console.warn('ThemeAudioNode: fallback src swap threw', err);
                return;
            }
            // load() resets the element, so re-assert the state the node was
            // in. Volume is re-ramped rather than set, so a mid-fade swap
            // does not click.
            if (host.isCurrent(node) && host.shouldPlay()) {
                resumeCtx();
                play(node);
                ramp(node, host.effectiveTarget(node), node.fadeMs, audioCtx);
            }
            return;
        }

        lastLoadError = 'no playable source (' + node.sources.join(', ') +
            '), last media error code ' + code;
        console.warn('ThemeAudioNode: ' + lastLoadError);
        teardown(node);
        host.onDrop(node);
    }

    /**
     * Build a playback node for a manifest `audio` block. Returns null for
     * an unusable config. Does NOT call play() - the caller decides.
     *
     * The node holds an <audio> element (always) plus, in webaudio mode, a
     * MediaElementAudioSourceNode -> GainNode graph. In element mode the
     * gain is null and ramp() drives the element's own .volume.
     *
     * @param {{src: string, srcFallback?: string, volume?: number,
     *          fadeMs?: number}|null} cfg - a manifest `audio` block.
     * @param {{isCurrent: Function, shouldPlay: Function,
     *          effectiveTarget: Function, onDrop: Function}} host -
     *          policy callbacks, used only when a source fails to load.
     * @returns {object|null} the playback node, or null.
     */
    function makeNode(cfg, host) {
        if (!cfg || typeof cfg !== 'object' || !cfg.src || typeof cfg.src !== 'string') {
            return null;
        }
        // A fresh node is a fresh verdict: a previous theme's dead track
        // must not keep reporting itself as this theme's problem.
        lastLoadError = null;
        var el = new Audio();
        // crossorigin BEFORE src per spec - WebKit caches the request mode
        // at src-set time.
        el.crossOrigin = 'anonymous';
        el.loop = true;
        el.preload = 'auto';
        el.volume = 0; // start silent; the element-mode fade ramps this up

        var node = {
            audio: el,
            sources: candidates(cfg),
            sourceIndex: 0,
            // `src` is the track's IDENTITY (always the manifest's primary),
            // which is what setTheme() compares to decide "same track". It
            // must NOT follow a fallback swap, or re-applying the same theme
            // after a fallback would look like a track change and restart it.
            src: cfg.src,
            // `loadedSrc` is the candidate actually on the element right now.
            loadedSrc: cfg.src,
            targetVolume: typeof cfg.volume === 'number' ? cfg.volume : DEFAULT_TRACK_VOLUME,
            fadeMs: typeof cfg.fadeMs === 'number' && cfg.fadeMs >= 0 ? cfg.fadeMs : 1500,
            gain: null,
            sourceNode: null,
            rafHandle: null
        };

        // A load error is not automatically fatal: if another format is
        // declared, try it before giving up.
        el.addEventListener('error', function () { _onNodeError(node, host); });

        el.src = node.sources[0];

        if (engineKind !== 'element') {
            var ctx = ensureCtx();
            if (ctx) {
                try {
                    var src = ctx.createMediaElementSource(el);
                    var gain = ctx.createGain();
                    gain.gain.value = 0;
                    src.connect(gain).connect(ctx.destination);
                    node.gain = gain;
                    node.sourceNode = src;
                    engineKind = 'webaudio';
                    // THE SILENT BUG, measured in desktop Chrome 150 on
                    // 2026-08-16 with an AnalyserNode on the graph:
                    // HTMLMediaElement.volume attenuates the signal
                    // UPSTREAM of createMediaElementSource. With gain fixed
                    // at 0.6, el.volume=1 gave RMS 0.0342, el.volume=0 gave
                    // RMS exactly 0, and restoring it mid-play recovered to
                    // 0.0377. The element is constructed at volume 0 so the
                    // ELEMENT-mode fade can ramp up from silence, but in
                    // webaudio mode the GainNode owns the fade and a 0 here
                    // multiplies the whole graph by zero - permanently,
                    // because ramp() returns early in webaudio mode and
                    // never touches el.volume again. The symptom is the
                    // worst kind: currentTime advances, gain reads 0.6,
                    // play() never rejects, and there is no sound. In
                    // webaudio mode the element must be wide open and the
                    // GainNode must be the only attenuator.
                    el.volume = 1;
                } catch (err) {
                    // CORS taint or other graph-construction failure. Drop
                    // to element mode permanently for this session.
                    console.warn('ThemeAudioNode: WebAudio graph failed, ' +
                        'falling back to element mode', err);
                    engineKind = 'element';
                }
            } else {
                engineKind = 'element';
            }
        }

        return node;
    }

    /**
     * Which engine this session settled on.
     *
     * @returns {string|null} 'webaudio', 'element', or null if undecided.
     */
    function getEngineKind() { return engineKind; }

    /**
     * The observable facts about one node, for diagnosis.
     *
     * `effectiveGain` is the gain that actually reaches the speaker: the
     * GainNode's value in webaudio mode, the element's own volume in
     * element mode. Reading the wrong one of those two is exactly how a
     * graph multiplied by zero read as healthy, so the choice is made here,
     * next to the code that decides which engine is in use, rather than in
     * the policy layer.
     *
     * @param {object|null} node - a playback node, or null.
     * @returns {null|{src: string, loadedSrc: string, paused: boolean,
     *                 currentTime: number, engine: string|null,
     *                 effectiveGain: number}}
     */
    function snapshot(node) {
        if (!node || !node.audio) return null;
        return {
            src: node.src,
            loadedSrc: node.loadedSrc,
            paused: !!node.audio.paused,
            currentTime: node.audio.currentTime,
            engine: engineKind,
            effectiveGain: node.gain ? node.gain.gain.value : node.audio.volume
        };
    }

    window.ThemeAudioNode = {
        candidates: candidates,
        ramp: ramp,
        teardown: teardown,
        play: play,
        makeNode: makeNode,
        ensureCtx: ensureCtx,
        getCtx: getCtx,
        resumeCtx: resumeCtx,
        getEngineKind: getEngineKind,
        getLastPlayError: getLastPlayError,
        getLastLoadError: getLastLoadError,
        snapshot: snapshot
    };
})();
