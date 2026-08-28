/**
 * ThemeAudioVolume - the master gain: the one number every theme's
 * manifest volume is multiplied by, shared across sessions.
 *
 * Split out of themeAudio.js when the settings slider arrived: that file
 * sits at the repo's 500-line budget, and the master gain is a coherent
 * concern of its own - a value, a floor, a persistence contract and a
 * live-apply hook - rather than part of the playback engine.
 *
 * IT IS AN ATTENUATOR, NEVER A SWITCH. set() clamps into
 * [MIN_MASTER_VOLUME, 1] and can never return zero. A master of zero is
 * silence with no error and no control that visibly caused it, which is
 * how this feature failed six consecutive times; the sixth cause was a
 * global audio switch that has since been deleted. The per-session "play
 * music" control is the only on/off in this app, and nothing here may
 * become a second one. The floor and the storage shape live in
 * themeAudioSettings.js, which also explains why a deliberate value is
 * written to its own key.
 *
 * LIVE BY CONTRACT. set() takes an apply callback and calls it with the
 * gain it actually applied, so a volume change reaches the speaker
 * immediately instead of at the next session restart. The callback is
 * how this module stays ignorant of nodes, ramps and AudioContexts.
 *
 * Exposed as window.ThemeAudioVolume. Loads AFTER themeAudioSettings.js
 * and BEFORE themeAudio.js.
 */
(function () {
    'use strict';

    if (window.ThemeAudioVolume) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    var Settings = window.ThemeAudioSettings;

    /** The master gain, MIN_MASTER_VOLUME..1. */
    var gain = Settings.DEFAULT_MASTER_VOLUME;

    /**
     * Load the persisted gain. Call once, AFTER the settings migration.
     *
     * @param {object} storage - a localStorage-like object.
     * @returns {number} the gain now in force.
     */
    function init(storage) {
        gain = Settings.readVolume(storage);
        return gain;
    }

    /**
     * The current master gain.
     *
     * @returns {number} MIN_MASTER_VOLUME..1.
     */
    function get() { return gain; }

    /**
     * The lowest gain set() will apply. The settings slider renders its
     * minimum from this rather than carrying a second copy of the number.
     *
     * @returns {number} 0..1.
     */
    function min() { return Settings.MIN_MASTER_VOLUME; }

    /**
     * Set, persist and apply the master gain.
     *
     * @param {number} v - requested gain; clamped into the usable band,
     *   never to zero.
     * @param {object} storage - a localStorage-like object.
     * @param {Function} [onApplied] - called with the gain actually
     *   applied, so playback can follow it without a restart.
     * @returns {number} the gain actually applied.
     */
    function set(v, storage, onApplied) {
        gain = Settings.clampVolume(parseFloat(v));
        Settings.writeVolume(storage, gain);
        if (typeof onApplied === 'function') onApplied(gain);
        return gain;
    }

    window.ThemeAudioVolume = {
        init: init,
        get: get,
        min: min,
        set: set
    };
})();
