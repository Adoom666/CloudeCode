/**
 * ThemeAudioStatus - turns a ThemeAudio.getStatus() snapshot into the one
 * sentence the music control shows the user.
 *
 * WHY THIS FILE EXISTS. Five separate causes have made this app silent, and
 * every one of them failed the same way: no error, no message, and a
 * control that painted itself as ON. The manifest audio block was stripped
 * by the API response model, the master and per-theme gains multiplied to
 * -45 LUFS, .m4a was served as an unrecognised mime type under nosniff, the
 * element volume zeroed the whole Web Audio graph, and a detached session
 * kept vetoing the master switch. In all five the user tapped "play music",
 * the row said "stop music", and nothing came out. The sixth was the master
 * switch itself, a persisted app-scoped gate defaulting to OFF that sat in
 * front of everything here; it has been deleted, so no reason names it.
 *
 * A control that reports success while producing no sound is the defect,
 * not the symptom. So the rule here is: if the snapshot does not positively
 * show audio reaching the speaker, describe() returns a REASON, and the
 * caller is expected to show it. Silence with no explanation is never a
 * valid outcome of this function.
 *
 * The three-outcome shape applies: playing, a named not-playing reason, or
 * "cannot tell yet" while a track is still opening. The third is a distinct
 * value, not a quiet pass - `settling` says the answer is not in yet and the
 * caller should ask again shortly.
 *
 * Pure and side-effect free: it takes a snapshot object and returns a
 * verdict, so it is unit-testable without a DOM, an AudioContext or a
 * network. Copy is lowercase to match the rest of the UI.
 *
 * Exposed as window.ThemeAudioStatus. Loaded AFTER themeAudio.js.
 */
(function () {
    'use strict';

    if (window.ThemeAudioStatus) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    /**
     * Gain below which the signal is treated as inaudible rather than quiet.
     * A fade-in passes through here legitimately, which is why `settling`
     * exists: a zero gain on a track whose currentTime is still 0 means the
     * fade has not started, not that something is broken.
     */
    var AUDIBLE_GAIN_EPSILON = 0.0001;

    /**
     * Diagnose a ThemeAudio snapshot.
     *
     * Order matters: the checks run from the most upstream cause to the most
     * downstream, so the reason names the thing the user has to fix first
     * rather than the last symptom in the chain.
     *
     * @param {object|null} status - a ThemeAudio.getStatus() snapshot.
     * @returns {{playing: boolean, settling: boolean, reason: string|null}}
     *          `playing` true only when audio is positively reaching the
     *          speaker. `settling` true when the answer is not yet knowable
     *          and the caller should re-check. `reason` is a lowercase
     *          sentence whenever `playing` is false.
     */
    function describe(status) {
        if (!status || typeof status !== 'object') {
            return {
                playing: false,
                settling: false,
                reason: 'audio is unavailable in this build'
            };
        }

        // Upstream of everything: is there a track at all? This is the case
        // that was true for all 23 themes while the API stripped the block.
        if (!status.hasTrack) {
            return {
                playing: false,
                settling: false,
                reason: 'this theme has no music track'
            };
        }

        // A dead load outranks the gates: turning a gate on will not help.
        if (status.loadError) {
            return {
                playing: false,
                settling: false,
                reason: 'this theme\'s music failed to load, so it cannot play'
            };
        }

        // Audio is session-only. On the home screen there is nothing for a
        // track to belong to, so say that rather than blaming a control the
        // user cannot see from here. This branch replaced "app sound is off
        // for all sessions", whose master switch no longer exists.
        if (!status.sessionName) {
            return {
                playing: false,
                settling: false,
                reason: 'music only plays inside a session'
            };
        }

        if (!status.sessionOn) {
            return {
                playing: false,
                settling: false,
                reason: 'music is off for this session'
            };
        }

        if (status.masterVolume <= AUDIBLE_GAIN_EPSILON) {
            return {
                playing: false,
                settling: false,
                reason: 'the master volume is set to zero'
            };
        }

        if (status.hidden) {
            return {
                playing: false,
                settling: false,
                reason: 'music is paused while this tab is in the background'
            };
        }

        // Both gates open and a track declared, but no node: it was built
        // and dropped, or never built. Either way there is nothing to play.
        if (!status.node) {
            return {
                playing: false,
                settling: false,
                reason: 'no audio track is loaded for this theme'
            };
        }

        if (status.playError === 'NotAllowedError') {
            return {
                playing: false,
                settling: false,
                reason: 'the browser blocked playback, tap play music again'
            };
        }
        if (status.playError && status.playError !== 'AbortError') {
            return {
                playing: false,
                settling: false,
                reason: 'playback was rejected by the browser (' +
                    status.playError + ')'
            };
        }

        if (status.node.paused) {
            // A track that has never advanced is still opening; one that has
            // played and then stopped is a real fault.
            if (status.node.currentTime <= 0) {
                return {
                    playing: false,
                    settling: true,
                    reason: 'the track is still opening'
                };
            }
            return {
                playing: false,
                settling: false,
                reason: 'the track is loaded but paused'
            };
        }

        if (status.node.effectiveGain <= AUDIBLE_GAIN_EPSILON) {
            // Mid fade-in is normal; a running track stuck at zero gain is
            // exactly the element-volume bug, and must not read as playing.
            if (status.node.currentTime <= 0) {
                return {
                    playing: false,
                    settling: true,
                    reason: 'the track is still opening'
                };
            }
            return {
                playing: false,
                settling: false,
                reason: 'the track is running but its volume is zero'
            };
        }

        return { playing: true, settling: false, reason: null };
    }

    /**
     * Diagnose the live ThemeAudio singleton.
     *
     * @returns {{playing: boolean, settling: boolean, reason: string|null}}
     */
    function current() {
        if (!window.ThemeAudio ||
            typeof window.ThemeAudio.getStatus !== 'function') {
            return describe(null);
        }
        try {
            return describe(window.ThemeAudio.getStatus());
        } catch (err) {
            return {
                playing: false,
                settling: false,
                reason: 'audio state could not be read'
            };
        }
    }

    window.ThemeAudioStatus = {
        describe: describe,
        current: current,
        AUDIBLE_GAIN_EPSILON: AUDIBLE_GAIN_EPSILON
    };
})();
