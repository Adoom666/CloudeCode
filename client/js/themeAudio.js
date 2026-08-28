/**
 * ThemeAudio - per-theme background music.
 *
 * Ships dormant: nothing plays until a theme manifest carries an `audio`
 * block AND the global audio control (client/js/globalAudioToggle.js, in
 * the bottom bar) is on, which is also the user gesture browsers require.
 * The manifest block is src, srcFallback, volume and fadeMs; the
 * authoritative definition is ThemeAudioManifest in src/models.py.
 *
 * FORMAT ORDER IS FIXED, m4a first: iOS returns "probably" for Ogg Vorbis
 * from canPlayType and then fails to decode it, so the fallback is driven
 * by a real load ERROR, not a capability probe - see
 * ThemeAudioNode.candidates() in themeAudioNode.js.
 *
 * GAIN BUDGET (read before lowering any of these numbers). The clips are
 * loudnorm'd to about -24 LUFS. The master and the per-theme volume
 * MULTIPLY: the original 0.28 x 0.3 was a linear gain of 0.084, roughly
 * -45 LUFS at the speaker and inaudible on a phone. The master now
 * defaults to 1.0 and manifests carry 0.45..0.60. A THIRD multiplier, the
 * element's own `.volume`, attenuates UPSTREAM of the graph - see
 * ThemeAudioNode.makeNode().
 *
 * `src` MUST be same-origin: src/main.py declares no `media-src`, so media
 * falls back to `default-src 'self'`, and any remote URL is blocked.
 *
 * ONE GATE. Sound requires a tmux session to be in scope AND the `on`
 * half to be true, in a single expression, `_gateOpen()`. `sessionName`
 * is per-session (attach/detach set/clear it, not a user control);
 * `sessionOn` now comes from ONE global on/off (globalAudioToggle.js)
 * instead of a per-session stored choice - setSessionAudio()'s contract
 * did not change, only who calls it, and with what.
 *
 * READ BEFORE CHANGING EITHER FILE AGAIN: this used to warn against
 * reintroducing an app-scoped mute, which is literally what the global
 * toggle is. That warning was never really about scope - the bug was TWO
 * independently stored booleans (an old master switch AND a per-session
 * opt-in) each able to veto the other silently, five silent-audio bugs'
 * worth. Both eras have exactly one stored boolean; a second one
 * anywhere here reintroduces the real bug.
 *
 * NO AUDIO ON THE HOME SCREEN, by construction rather than a mute: with
 * no session in scope `sessionName` is null and `_gateOpen()` is false.
 *
 * Public surface (singleton on window.ThemeAudio): init(), setTheme(cfg|null)
 * from the themes registry, setSessionAudio(name, on) / isSessionEnabled() /
 * getSessionName() for the gate, isMuted(), getLastPlayError(), getStatus()
 * for diagnosis, and getVolume()/setVolume()/getMinVolume() (settings-audio.js
 * drives it, themeAudioVolume.js owns it) - an ATTENUATOR, floored above
 * zero, never an on/off.
 *
 * Persistence and the upgrade migration live in themeAudioSettings.js. A
 * `cloude:audio-state` CustomEvent fires on `document` on any gate change,
 * so globalAudioToggle.js repaints on changes its own click did not cause.
 *
 * Engine: MediaElementAudioSourceNode -> GainNode -> destination, for clean
 * linearRampToValueAtTime crossfades; a requestAnimationFrame ramp is the
 * fallback when AudioContext construction fails. Looping uses `el.loop`,
 * which still gaps audibly in 2026 and is acceptable for ambience.
 * Visibility is handled explicitly (pause on hidden, resume on visible if
 * unmuted) because browsers do not universally pause backgrounded tabs.
 * setTheme() only preloads, never plays; the AudioContext is built by
 * makeNode() outside any gesture so it starts suspended, and the first
 * unmute resumes it from the gesture.
 *
 * WHERE THE MANIFEST COMES FROM, and why this module was dead code until
 * 2026-08-16. setTheme() is fed from GET /api/v1/themes, NOT theme.json on
 * disk, and that endpoint's Pydantic response_model dropped the `audio`
 * block, so this module received null for every theme and correctly played
 * nothing. If sound disappears again, check the API payload first.
 *
 * REPORTING FAILURE. Every audio bug this feature has shipped was silent.
 * getStatus() exposes the raw facts; themeAudioStatus.js turns them into
 * the sentence the audio control shows the user.
 */(function () {
    'use strict';

    if (window.ThemeAudio) {
        // Idempotent - never re-init on hot reload or double-script-tag.
        return;
    }

    var Settings = window.ThemeAudioSettings;
    // The master gain: unity by default, floored, never an on/off.
    var Volume = window.ThemeAudioVolume;
    // ---- State ----
    var initialized = false;
    // The session in scope, or null on the home screen. In-memory, set by
    // app.js's screen transitions via Themes.setActiveSession.
    var sessionName = null;
    // The global audio on/off, applied to the session in scope. In-memory;
    // globalAudioToggle.js owns the one persisted key.
    var sessionOn = false;
    // Derived from the gate below - never assigned directly.
    var muted = true;
    var currentConfig = null;          // last audioConfig passed to setTheme()

    // Current playback node. The node shape and the choice of engine
    // (webaudio vs element) both live in themeAudioNode.js, which also
    // owns the shared AudioContext; this file only holds which node is
    // playing and which is fading out.
    var currentNode = null;
    var outgoingNode = null; // held during crossfade only

    // ---- Gate plumbing ----
    /**
     * The one gate. A session must be in scope AND opted into music.
     *
     * Written as a single expression on purpose: the previous shape kept
     * two independent booleans that could each veto the other, and every
     * silent-audio bug this feature shipped lived in the gap between them.
     *
     * @returns {boolean} true when sound is allowed to reach the speaker.
     */
    function _gateOpen() {
        return !!sessionName && sessionOn;
    }

    /**
     * Tell listeners that the gate moved. Fired for any change, including
     * ones the caller did not cause, so a control never paints from a
     * state something else has since changed.
     *
     * @returns {void}
     */
    function _emitChange() {
        try {
            if (typeof document === 'undefined' ||
                typeof document.dispatchEvent !== 'function' ||
                typeof CustomEvent !== 'function') {
                return;
            }
            document.dispatchEvent(new CustomEvent('cloude:audio-state', {
                detail: {
                    sessionName: sessionName,
                    sessionOn: sessionOn,
                    muted: muted
                }
            }));
        } catch (err) { /* an event is never worth an exception */ }
    }

    /**
     * Start or stop playback to match the current gate state. Idempotent:
     * safe to call when nothing actually changed.
     *
     * @returns {void}
     */
    function _applyGate() {
        if (muted) {
            if (!currentNode) return;
            var outMs = Math.min(currentNode.fadeMs, 400);
            window.ThemeAudioNode.ramp(currentNode, 0, outMs, _ctx());
            setTimeout(function () {
                if (currentNode && muted) {
                    try { currentNode.audio.pause(); } catch (_) { /* ignore */ }
                }
            }, outMs + 20);
            return;
        }
        // Unmuting is normally the user-gesture moment, so resume the
        // context here rather than at construction time.
        _resumeCtxIfSuspended();
        if (currentNode && !document.hidden) {
            _tryPlay(currentNode);
            window.ThemeAudioNode.ramp(
                currentNode, _effectiveTarget(currentNode), currentNode.fadeMs, _ctx());
        }
    }

    /**
     * Recompute the effective mute from the gate and act on it.
     *
     * @returns {boolean} the new effective muted state.
     */
    function _recompute() {
        var next = !_gateOpen();
        var changed = next !== muted;
        muted = next;
        if (changed) _applyGate();
        _emitChange();
        return muted;
    }

    // ---- Node construction ----
    /**
     * Policy callbacks handed to ThemeAudioNode.makeNode(). The mechanics
     * of building and retrying a node live in that module; these four
     * answers are the only things it needs from the policy layer, and
     * keeping them as callbacks is what lets the two files stay separate.
     */
    var nodeHost = {
        /**
         * Is this node the one currently in play?
         *
         * @param {object} node - a playback node.
         * @returns {boolean}
         */
        isCurrent: function (node) { return node === currentNode; },

        /**
         * Should audio be running right now?
         *
         * @returns {boolean}
         */
        shouldPlay: function () { return !muted && !document.hidden; },

        /**
         * The node's gain target once the master is applied.
         *
         * @param {object} node - a playback node.
         * @returns {number} 0..1.
         */
        effectiveTarget: function (node) { return _effectiveTarget(node); },

        /**
         * Forget a node whose sources are all exhausted.
         *
         * @param {object} node - the node being dropped.
         * @returns {void}
         */
        onDrop: function (node) {
            if (currentNode === node) currentNode = null;
            if (outgoingNode === node) outgoingNode = null;
        }
    };

    /**
     * Build a playback node for a manifest `audio` block.
     *
     * @param {object|null} cfg - a manifest `audio` block.
     * @returns {object|null} the node, or null for an unusable cfg.
     */
    function _makeNode(cfg) {
        return window.ThemeAudioNode.makeNode(cfg, nodeHost);
    }

    /**
     * The shared AudioContext, or null. Owned by ThemeAudioNode.
     *
     * @returns {AudioContext|null}
     */
    function _ctx() { return window.ThemeAudioNode.getCtx(); }

    /**
     * Resume the AudioContext if the browser suspended it.
     *
     * @returns {void}
     */
    function _resumeCtxIfSuspended() { window.ThemeAudioNode.resumeCtx(); }

    // ---- Effective volume helpers ----
    function _effectiveTarget(node) {
        if (!node) return 0;
        return node.targetVolume * Volume.get();
    }

    // ---- Play / pause primitives (respect muted + autoplay grant) ----
    /**
     * Start a node. Mechanics and rejection logging live in ThemeAudioNode.
     *
     * @param {object} node - the playback node.
     * @returns {void}
     */
    function _tryPlay(node) {
        window.ThemeAudioNode.play(node);
    }

    // ---- Public API ----

    /**
     * Apply a new audio configuration. Pass null to fade out the current track
     * without starting a replacement (e.g. when the active theme has no audio).
     *
     * Called by Themes.applyTheme() on every theme switch - must be cheap and
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
                    window.ThemeAudioNode.ramp(currentNode, _effectiveTarget(currentNode), currentNode.fadeMs, _ctx());
                }
            }
            return;
        }

        currentConfig = audioConfig;

        // Fade out + tear down any outgoing track.
        if (outgoingNode) {
            window.ThemeAudioNode.teardown(outgoingNode);
            outgoingNode = null;
        }
        if (currentNode) {
            outgoingNode = currentNode;
            var outFade = outgoingNode.fadeMs;
            window.ThemeAudioNode.ramp(outgoingNode, 0, outFade, _ctx());
            (function (n, ms) {
                setTimeout(function () {
                    if (n === outgoingNode) {
                        window.ThemeAudioNode.teardown(n);
                        outgoingNode = null;
                    }
                }, ms + 50);
            })(outgoingNode, outFade);
            currentNode = null;
        }

        // No new track requested - just silence.
        if (!audioConfig) return;

        var node = _makeNode(audioConfig);
        if (!node) return;
        currentNode = node;

        // If unmuted and visible, fade in immediately. Otherwise the track is
        // preloaded but silent; first unmute or visibility-return will play it.
        if (!muted && !document.hidden) {
            _resumeCtxIfSuspended();
            _tryPlay(node);
            window.ThemeAudioNode.ramp(node, _effectiveTarget(node), node.fadeMs, _ctx());
        }
    }

    /**
     * Set the one gate: which session is in scope, and whether audio is
     * on. MUST be called from a user-gesture handler the FIRST time it
     * turns sound on - that is the only way the autoplay grant reaches
     * the AudioContext.
     *
     * Not persisted here: globalAudioToggle.js owns the one stored key
     * (cloude.audio.enabled) and calls this on every session change too,
     * not only on a user tap - see that file's syncForSession(). A null
     * name is how the home screen is expressed, not a mute: nothing is
     * in scope for music to belong to, so the gate stays closed whatever
     * `on` says.
     *
     * @param {string|null} name - the tmux session in scope, or null.
     * @param {boolean} on - the global audio on/off.
     * @returns {boolean} the new EFFECTIVE muted state.
     */
    function setSessionAudio(name, on) {
        sessionName = name ? String(name) : null;
        sessionOn = !!on;
        return _recompute();
    }

    /**
     * Is the gate open - a session in scope, opted into music?
     *
     * @returns {boolean}
     */
    function isSessionEnabled() { return _gateOpen(); }

    /**
     * The session currently in scope, or null on the home screen.
     *
     * @returns {string|null}
     */
    function getSessionName() { return sessionName; }

    /**
     * The effective gate, inverted: silent unless a session is in scope
     * and has opted into music.
     *
     * @returns {boolean}
     */
    function isMuted() { return muted; }

    /**
     * The name of the last play() rejection, or null if the last attempt
     * was clean. A rejected play() produces no error event and no
     * exception, so without this there is nothing to inspect.
     *
     * @returns {string|null}
     */
    function getLastPlayError() { return window.ThemeAudioNode.getLastPlayError(); }

    /**
     * A snapshot of everything that decides whether sound is coming out.
     *
     * Facts only, no interpretation: themeAudioStatus.js owns the mapping
     * from this shape to a sentence. Never throws, because it is called
     * from paint paths.
     *
     * @returns {{sessionName: string|null, sessionOn: boolean, muted: boolean,
     *            masterVolume: number, hidden: boolean, hasTrack: boolean,
     *            playError: string|null, loadError: string|null,
     *            node: null|{src: string, loadedSrc: string, paused: boolean,
     *                        currentTime: number, engine: string|null,
     *                        effectiveGain: number}}}
     */
    function getStatus() {
        var Node = window.ThemeAudioNode;
        return {
            sessionName: sessionName,
            sessionOn: _gateOpen(),
            muted: muted,
            masterVolume: Volume.get(),
            hidden: typeof document !== 'undefined' && !!document.hidden,
            hasTrack: !!(currentConfig && currentConfig.src),
            playError: Node ? Node.getLastPlayError() : null,
            loadError: Node && typeof Node.getLastLoadError === 'function'
                ? Node.getLastLoadError() : null,
            node: Node && typeof Node.snapshot === 'function'
                ? Node.snapshot(currentNode) : null
        };
    }
    /** @returns {number} the master gain, the floor..1. */
    function getVolume() { return Volume.get(); }
    /**
     * Set, persist and immediately APPLY the master gain: a volume that
     * needed a restart to be heard would be this feature's silent-failure
     * shape all over again. Clamped into the usable band, never to zero.
     *
     * @param {number} v - requested gain.
     * @returns {number} the gain actually applied.
     */
    function setVolume(v) {
        return Volume.set(v, localStorage, function () {
            if (currentNode && !muted && !document.hidden) {
                window.ThemeAudioNode.ramp(
                    currentNode, _effectiveTarget(currentNode), 200, _ctx());
            }
        });
    }
    /** @returns {number} the lowest gain setVolume() will apply, 0..1. */
    function getMinVolume() { return Volume.min(); }

    // ---- Page Visibility ----
    function _onVisibilityChange() {
        if (document.hidden) {
            if (currentNode) {
                window.ThemeAudioNode.ramp(currentNode, 0, 200, _ctx());
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
                window.ThemeAudioNode.ramp(currentNode, _effectiveTarget(currentNode), currentNode.fadeMs, _ctx());
            }
        }
    }

    /**
     * Read persisted settings and wire the visibility handler. Call once.
     *
     * The migration runs BEFORE anything is read, so neither a master
     * volume left over from the old attenuating gain budget nor the
     * retired app sound mute flag can override the current defaults. See
     * themeAudioSettings.js for why those values cannot simply be trusted.
     *
     * @returns {void}
     */
    function init() {
        if (initialized) return;
        initialized = true;

        var summary = Settings.migrationSummary(Settings.migrate(localStorage));
        if (summary) console.log(summary);

        Volume.init(localStorage);
        muted = !_gateOpen();

        document.addEventListener('visibilitychange', _onVisibilityChange);
        console.log('ThemeAudio: initialized - session=' + sessionName +
            ' sessionOn=' + sessionOn + ' muted=' + muted +
            ' volume=' + Volume.get());
    }

    window.ThemeAudio = {
        init: init,
        setTheme: setTheme,
        isMuted: isMuted,
        setSessionAudio: setSessionAudio,
        isSessionEnabled: isSessionEnabled,
        getSessionName: getSessionName,
        getLastPlayError: getLastPlayError,
        getStatus: getStatus,
        getVolume: getVolume,
        setVolume: setVolume,
        getMinVolume: getMinVolume
    };
})();
