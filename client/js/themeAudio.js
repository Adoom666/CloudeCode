/**
 * ThemeAudio — per-theme background music plumbing.
 *
 * v0.7.0+. Ships dormant: no audio plays until a theme manifest contains
 * an optional `audio` block AND the user has clicked the header 🔊 button
 * to opt in (default state is muted).
 *
 * Manifest shape (optional, per theme.json):
 *   "audio": {
 *     "src":     "https://github.com/.../releases/download/theme-music-v1/<id>.mp3",
 *     "volume":  0.3,      // 0..1 target gain after fade-in
 *     "fadeMs":  1500      // crossfade duration in ms
 *   }
 *
 * Public surface (singleton on window.ThemeAudio):
 *   init()                       — call once on app load
 *   setTheme(audioConfig|null)   — called by themes registry on every applyTheme()
 *   toggleMute()                 — header button click handler; returns new muted state
 *   isMuted()                    — boolean for UI
 *   getVolume() / setVolume(v)   — 0..1; persisted to localStorage; no UI yet
 *
 * Persistence:
 *   localStorage['cloude.audio.muted']  → 'true' | 'false'  (default 'true')
 *   localStorage['cloude.audio.volume'] → '0.3' (string of float 0..1)
 *
 * Engine strategy (judgment call documented for future maintainers):
 *   Primary path uses Web Audio via MediaElementAudioSourceNode → GainNode → ctx.destination.
 *   This gives clean linearRampToValueAtTime crossfades between themes.
 *
 *   PROBLEM: <audio crossorigin="anonymous" src="<github-release-url>"> piped
 *   through createMediaElementSource() can taint or CORS-fail because GitHub's
 *   release-download URL is a 302 to a non-CORS-friendly origin. When that
 *   happens we silently fall back to bare HTMLAudioElement mode and drive
 *   volume ramps with requestAnimationFrame instead of GainNode automation.
 *   The user-facing behavior is identical; only the precision of the fade
 *   curve differs (still sub-perceptible).
 *
 *   GAPLESS LOOP CAVEAT: HTMLAudioElement `loop = true` still has audible gaps
 *   on Chromium/WebKit/Gecko in 2026. For v0.7.0 the gap is acceptable for
 *   ambient bg music. Future upgrade path: switch to AudioBufferSourceNode
 *   (fetch → arrayBuffer → ctx.decodeAudioData) — this also sidesteps the
 *   createMediaElementSource CORS taint because decodeAudioData uses the
 *   CORS-friendly fetch model. Trade-off: full-file memory load + no streaming.
 *
 * Page Visibility:
 *   visibilitychange → pause when document.hidden, resume when visible (iff
 *   not muted and a track is loaded). Explicit pause is NOT redundant —
 *   browsers do not universally auto-pause backgrounded tabs.
 *
 * Autoplay policy:
 *   AudioContext is created lazily on the FIRST user-gesture call to
 *   toggleMute(). Calling setTheme() before unmute only preloads metadata —
 *   never invokes .play() until the user-gesture grant exists.
 */
(function () {
    'use strict';

    if (window.ThemeAudio) {
        // Idempotent — never re-init on hot reload or double-script-tag.
        return;
    }

    var LS_MUTED = 'cloude.audio.muted';
    var LS_VOLUME = 'cloude.audio.volume';
    var DEFAULT_VOLUME = 0.3;

    // ---- State ----
    var initialized = false;
    var muted = true;                  // default muted — overridden in init()
    var globalVolume = DEFAULT_VOLUME; // 0..1; multiplied with per-theme volume
    var currentConfig = null;          // last audioConfig passed to setTheme()

    // Current playback node (one of two engines — see init/_engineKind).
    // Both engines share the same node shape so the rest of the code is uniform.
    //   { audio: HTMLAudioElement, src: string, volume: number, fadeMs: number,
    //     gain?: GainNode, sourceNode?: MediaElementAudioSourceNode,
    //     rafHandle?: number }
    var currentNode = null;
    var outgoingNode = null; // held during crossfade only

    // Web Audio (lazy-init on first user gesture)
    var audioCtx = null;
    var engineKind = null; // 'webaudio' | 'element' | null (decided on first play)

    // ---- LocalStorage helpers (defensive — never throw) ----
    function _readMuted() {
        try {
            var v = localStorage.getItem(LS_MUTED);
            // Default to muted; only an explicit 'false' unmutes.
            return v === null ? true : v !== 'false';
        } catch (_) { return true; }
    }
    function _writeMuted(b) {
        try { localStorage.setItem(LS_MUTED, b ? 'true' : 'false'); } catch (_) { /* ignore */ }
    }
    function _readVolume() {
        try {
            var v = localStorage.getItem(LS_VOLUME);
            if (v === null) return DEFAULT_VOLUME;
            var n = parseFloat(v);
            if (isFinite(n) && n >= 0 && n <= 1) return n;
            return DEFAULT_VOLUME;
        } catch (_) { return DEFAULT_VOLUME; }
    }
    function _writeVolume(v) {
        try { localStorage.setItem(LS_VOLUME, String(v)); } catch (_) { /* ignore */ }
    }

    // ---- AudioContext lifecycle ----
    function _ensureAudioCtx() {
        if (audioCtx) return audioCtx;
        try {
            var Ctor = window.AudioContext || window.webkitAudioContext;
            if (!Ctor) return null;
            audioCtx = new Ctor();
            return audioCtx;
        } catch (e) {
            console.warn('ThemeAudio: AudioContext construction failed', e);
            return null;
        }
    }

    function _resumeCtxIfSuspended() {
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(function (e) {
                console.warn('ThemeAudio: ctx.resume() rejected', e);
            });
        }
    }

    // ---- Node construction ----
    /**
     * Build a playback node for the given audio config. Returns null if the
     * config is missing or src is empty. Does NOT call .play() — caller decides.
     *
     * The node holds an <audio> element (always) plus optional Web Audio graph
     * (GainNode + MediaElementAudioSourceNode) if the engine has been settled
     * on 'webaudio'. If 'element' mode, gain is null and volume is driven by
     * the element's .volume property directly via _rampVolume().
     */
    function _makeNode(cfg) {
        if (!cfg || typeof cfg !== 'object' || !cfg.src || typeof cfg.src !== 'string') {
            return null;
        }
        var el = new Audio();
        // crossorigin BEFORE src per spec — Safari/WebKit caches the request
        // mode at src-set time.
        el.crossOrigin = 'anonymous';
        el.loop = true;
        el.preload = 'auto';
        el.volume = 0; // start silent; fade-in handles the ramp
        el.src = cfg.src;

        var node = {
            audio: el,
            src: cfg.src,
            targetVolume: typeof cfg.volume === 'number' ? cfg.volume : DEFAULT_VOLUME,
            fadeMs: typeof cfg.fadeMs === 'number' && cfg.fadeMs >= 0 ? cfg.fadeMs : 1500,
            gain: null,
            sourceNode: null,
            rafHandle: null
        };

        // Silent failure on load errors — no toast, no console spam beyond a
        // single warn. The whole point of this plumbing is to no-op gracefully
        // when a theme references audio that isn't there yet.
        el.addEventListener('error', function () {
            console.warn('ThemeAudio: audio load error for', cfg.src, el.error && el.error.code);
            _teardownNode(node);
            if (currentNode === node) currentNode = null;
            if (outgoingNode === node) outgoingNode = null;
        });

        // Try to wire Web Audio graph. If createMediaElementSource fails
        // (CORS taint, codec issues, etc.), fall back to 'element' mode for
        // the rest of the session.
        if (engineKind !== 'element') {
            var ctx = _ensureAudioCtx();
            if (ctx) {
                try {
                    var src = ctx.createMediaElementSource(el);
                    var gain = ctx.createGain();
                    gain.gain.value = 0;
                    src.connect(gain).connect(ctx.destination);
                    node.gain = gain;
                    node.sourceNode = src;
                    engineKind = 'webaudio';
                } catch (e) {
                    // CORS taint or other graph-construction failure.
                    // Drop to element mode permanently for this session.
                    console.warn('ThemeAudio: WebAudio graph failed, falling back to element mode', e);
                    engineKind = 'element';
                }
            } else {
                engineKind = 'element';
            }
        }

        return node;
    }

    /**
     * Tear down a node — stop playback, disconnect Web Audio graph, null refs
     * so GC can collect the buffer.
     */
    function _teardownNode(node) {
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
        // Detach src so the browser stops decoding/buffering.
        try {
            node.audio.removeAttribute('src');
            node.audio.load();
        } catch (_) { /* ignore */ }
    }

    // ---- Volume ramp (engine-agnostic) ----
    /**
     * Ramp a node from its current effective volume to `target` over `durationMs`.
     * In 'webaudio' mode this uses GainNode automation. In 'element' mode this
     * uses a requestAnimationFrame loop driving audio.volume directly.
     *
     * Anchors current value first (setValueAtTime / read .volume) so back-to-back
     * ramps never click.
     */
    function _rampVolume(node, target, durationMs) {
        if (!node || !node.audio) return;
        var clamped = Math.max(0, Math.min(1, target));
        var dur = Math.max(0, durationMs);

        if (node.gain && audioCtx) {
            var now = audioCtx.currentTime;
            try {
                // Anchor current value to avoid step discontinuity, then ramp.
                node.gain.gain.cancelScheduledValues(now);
                node.gain.gain.setValueAtTime(node.gain.gain.value, now);
                node.gain.gain.linearRampToValueAtTime(clamped, now + dur / 1000);
            } catch (e) {
                console.warn('ThemeAudio: gain ramp threw', e);
            }
            return;
        }

        // Element mode — RAF-driven linear ramp on audio.volume.
        if (node.rafHandle != null) {
            try { cancelAnimationFrame(node.rafHandle); } catch (_) { /* ignore */ }
            node.rafHandle = null;
        }
        var startVol = node.audio.volume;
        var startT = (typeof performance !== 'undefined' ? performance.now() : Date.now());

        function step(t) {
            if (!node.audio) return; // torn down mid-ramp
            var elapsed = t - startT;
            var k = dur === 0 ? 1 : Math.min(1, elapsed / dur);
            try {
                node.audio.volume = startVol + (clamped - startVol) * k;
            } catch (_) { /* ignore */ }
            if (k < 1) {
                node.rafHandle = requestAnimationFrame(step);
            } else {
                node.rafHandle = null;
            }
        }
        node.rafHandle = requestAnimationFrame(step);
    }

    // ---- Effective volume helpers ----
    function _effectiveTarget(node) {
        if (!node) return 0;
        return node.targetVolume * globalVolume;
    }

    // ---- Play / pause primitives (respect muted + autoplay grant) ----
    function _tryPlay(node) {
        if (!node || !node.audio) return;
        var p;
        try {
            p = node.audio.play();
        } catch (e) {
            console.warn('ThemeAudio: play() threw', e);
            return;
        }
        if (p && typeof p.then === 'function') {
            p.catch(function (err) {
                // NotAllowedError when no user-gesture has happened yet — expected.
                // AbortError fires on rapid pause/play during theme swaps — benign.
                if (err && err.name !== 'NotAllowedError' && err.name !== 'AbortError') {
                    console.warn('ThemeAudio: play() rejected', err.name, err.message);
                }
            });
        }
    }

    // ---- Public API ----

    /**
     * Apply a new audio configuration. Pass null to fade out the current track
     * without starting a replacement (e.g. when the active theme has no audio).
     *
     * Called by Themes.applyTheme() on every theme switch — must be cheap and
     * idempotent when the same config is passed twice.
     */
    function setTheme(audioConfig) {
        // Same-src no-op (avoid teardown+rebuild when applyTheme fires for an
        // unrelated reason like cssVars repaint).
        var newSrc = audioConfig && audioConfig.src ? audioConfig.src : null;
        var curSrc = currentNode ? currentNode.src : null;
        if (newSrc === curSrc) {
            currentConfig = audioConfig;
            // Update target gain in case theme changed volume/fadeMs only.
            if (currentNode && audioConfig) {
                currentNode.targetVolume = typeof audioConfig.volume === 'number'
                    ? audioConfig.volume : currentNode.targetVolume;
                currentNode.fadeMs = typeof audioConfig.fadeMs === 'number' && audioConfig.fadeMs >= 0
                    ? audioConfig.fadeMs : currentNode.fadeMs;
                if (!muted && !document.hidden) {
                    _rampVolume(currentNode, _effectiveTarget(currentNode), currentNode.fadeMs);
                }
            }
            return;
        }

        currentConfig = audioConfig;

        // Fade out + tear down any outgoing track.
        if (outgoingNode) {
            _teardownNode(outgoingNode);
            outgoingNode = null;
        }
        if (currentNode) {
            outgoingNode = currentNode;
            var outFade = outgoingNode.fadeMs;
            _rampVolume(outgoingNode, 0, outFade);
            (function (n, ms) {
                setTimeout(function () {
                    if (n === outgoingNode) {
                        _teardownNode(n);
                        outgoingNode = null;
                    }
                }, ms + 50);
            })(outgoingNode, outFade);
            currentNode = null;
        }

        // No new track requested — just silence.
        if (!audioConfig) return;

        var node = _makeNode(audioConfig);
        if (!node) return;
        currentNode = node;

        // If unmuted and visible, fade in immediately. Otherwise the track is
        // preloaded but silent; first unmute or visibility-return will play it.
        if (!muted && !document.hidden) {
            _resumeCtxIfSuspended();
            _tryPlay(node);
            _rampVolume(node, _effectiveTarget(node), node.fadeMs);
        }
    }

    /**
     * Toggle mute. MUST be called from a user-gesture handler (header button
     * click) for the FIRST unmute — that's the only way the autoplay grant
     * gets issued to the AudioContext.
     *
     * Returns the new muted state (boolean).
     */
    function toggleMute() {
        muted = !muted;
        _writeMuted(muted);

        if (muted) {
            // Mute: ramp current track to 0, then pause (don't tear down —
            // user may unmute again and we want the buffer warm).
            if (currentNode) {
                _rampVolume(currentNode, 0, Math.min(currentNode.fadeMs, 400));
                setTimeout(function () {
                    if (currentNode && muted) {
                        try { currentNode.audio.pause(); } catch (_) { /* ignore */ }
                    }
                }, Math.min(currentNode.fadeMs, 400) + 20);
            }
        } else {
            // Unmute: this is (likely) the user-gesture moment. Resume ctx
            // and play.
            _resumeCtxIfSuspended();
            if (currentNode && !document.hidden) {
                _tryPlay(currentNode);
                _rampVolume(currentNode, _effectiveTarget(currentNode), currentNode.fadeMs);
            }
        }
        return muted;
    }

    function isMuted() { return muted; }
    function getVolume() { return globalVolume; }
    function setVolume(v) {
        var clamped = Math.max(0, Math.min(1, v));
        globalVolume = clamped;
        _writeVolume(clamped);
        if (currentNode && !muted && !document.hidden) {
            _rampVolume(currentNode, _effectiveTarget(currentNode), 200);
        }
    }

    // ---- Page Visibility ----
    function _onVisibilityChange() {
        if (document.hidden) {
            if (currentNode) {
                _rampVolume(currentNode, 0, 200);
                setTimeout(function () {
                    if (currentNode && document.hidden) {
                        try { currentNode.audio.pause(); } catch (_) { /* ignore */ }
                    }
                }, 220);
            }
        } else {
            if (!muted && currentNode) {
                _resumeCtxIfSuspended();
                _tryPlay(currentNode);
                _rampVolume(currentNode, _effectiveTarget(currentNode), currentNode.fadeMs);
            }
        }
    }

    function init() {
        if (initialized) return;
        initialized = true;

        muted = _readMuted();
        globalVolume = _readVolume();

        document.addEventListener('visibilitychange', _onVisibilityChange);
        console.log('ThemeAudio: initialized — muted=' + muted + ' volume=' + globalVolume);
    }

    window.ThemeAudio = {
        init: init,
        setTheme: setTheme,
        toggleMute: toggleMute,
        isMuted: isMuted,
        getVolume: getVolume,
        setVolume: setVolume
    };
})();
