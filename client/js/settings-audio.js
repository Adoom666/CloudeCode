/**
 * SettingsAudio - the global music volume row in the settings panel's
 * general tab.
 *
 * WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT. It is an ATTENUATOR for
 * theme background music, applied on top of every theme's manifest
 * volume, shared by every session. It is NOT an on/off. There is no
 * checkbox, no mute, and the slider cannot reach zero: its minimum is
 * ThemeAudio.getMinVolume() (0.35). The per-session "play music" control
 * in the session editor FAB is the only thing in this app that starts or
 * stops sound.
 *
 * That restraint is the whole point. An app-scoped switch in front of the
 * per-session control is exactly what made this feature silent for its
 * sixth consecutive release, and it was deleted days before this file was
 * written (themeAudioSettings.js, v2 -> v3). A slider that bottoms out at
 * zero is the same trap wearing a different hat: the user drags it down,
 * gets silence with no error, and reports the feature as broken. So the
 * floor is enforced in the engine (ThemeAudio.setVolume clamps) and only
 * MIRRORED here, rather than this file owning a second copy of the number.
 *
 * LIVE, NOT ON SAVE. The panel's Save button batches a PATCH of the
 * server-side config; this setting is local to the browser and applies on
 * `input`, so dragging changes what is playing right now. It never joins
 * collectPatch() - same contract as the theme picker in the same tab.
 *
 * Touch: the slider sits in a 44px-tall row and the thumb is 28px, so the
 * hit area clears the Apple HIG / WCAG 2.5.5 minimum at 390px one-handed.
 * See client/css/settings-audio.css.
 *
 * Loads BEFORE settings-panel.js, which calls render() and wire().
 */
(function () {
    'use strict';

    if (window.SettingsAudio) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    /** Slider granularity, in percent. Coarse enough to hit with a thumb. */
    var STEP_PERCENT = 5;

    /**
     * The engine's current master gain, or the default when the audio
     * module is not on the page (the settings panel must still render).
     *
     * @returns {number} 0..1.
     */
    function _currentVolume() {
        if (window.ThemeAudio && typeof window.ThemeAudio.getVolume === 'function') {
            return window.ThemeAudio.getVolume();
        }
        return window.ThemeAudioSettings
            ? window.ThemeAudioSettings.DEFAULT_MASTER_VOLUME : 1;
    }

    /**
     * The lowest gain the engine will apply. Read from the engine so the
     * floor has exactly one definition.
     *
     * @returns {number} 0..1.
     */
    function _minVolume() {
        if (window.ThemeAudio && typeof window.ThemeAudio.getMinVolume === 'function') {
            return window.ThemeAudio.getMinVolume();
        }
        return window.ThemeAudioSettings
            ? window.ThemeAudioSettings.MIN_MASTER_VOLUME : 0.35;
    }

    /**
     * Percent for display and for the slider's integer scale.
     *
     * @param {number} gain - 0..1.
     * @returns {number} 0..100, rounded.
     */
    function toPercent(gain) {
        return Math.round(gain * 100);
    }

    /**
     * Build the section markup. Pure - reads the engine, touches no DOM.
     *
     * @returns {string} HTML for one `.settings-section`.
     */
    function render() {
        var minPct = toPercent(_minVolume());
        var valuePct = Math.max(minPct, toPercent(_currentVolume()));
        return (
            '<section class="settings-section" data-settings-section="audio">' +
            '  <h3 class="settings-section-title">sound</h3>' +
            '  <div class="settings-section-description">how loud theme music plays, in every session. applies immediately - no save needed.</div>' +
            '  <div class="settings-field">' +
            '    <label class="settings-field-label" for="settings-master-volume">music volume</label>' +
            '    <div class="settings-volume-row">' +
            '      <input type="range" id="settings-master-volume" class="settings-volume-slider"' +
            '        data-settings-volume="master"' +
            '        min="' + minPct + '" max="100" step="' + STEP_PERCENT + '"' +
            '        value="' + valuePct + '"' +
            '        aria-describedby="settings-master-volume-hint">' +
            '      <output class="settings-volume-value" id="settings-master-volume-value"' +
            '        for="settings-master-volume">' + valuePct + '%</output>' +
            '    </div>' +
            '    <div class="settings-field-hint" id="settings-master-volume-hint">' +
            'this is a volume, not a switch: it stops at ' + minPct + '% so it can never be silence. ' +
            'to start or stop music, use "play music" in the session you are in.' +
            '</div>' +
            '  </div>' +
            '</section>'
        );
    }

    /**
     * Wire the slider inside an already-rendered panel: live apply on
     * every input event, with the readout kept in step.
     *
     * Safe to call when the section is absent (a tab layout that does not
     * include it) and safe to call twice on the same element.
     *
     * @param {Element} rootEl - element containing the rendered section.
     * @returns {HTMLInputElement|null} the wired slider, or null.
     */
    function wire(rootEl) {
        if (!rootEl || typeof rootEl.querySelector !== 'function') return null;
        var slider = rootEl.querySelector('[data-settings-volume="master"]');
        if (!slider || slider.getAttribute('data-wired') === '1') return slider;
        slider.setAttribute('data-wired', '1');

        var readout = rootEl.querySelector('#settings-master-volume-value');
        slider.addEventListener('input', function () {
            var pct = parseInt(slider.value, 10);
            if (!isFinite(pct)) return;
            var applied = pct / 100;
            if (window.ThemeAudio && typeof window.ThemeAudio.setVolume === 'function') {
                // The engine clamps and returns what it actually applied,
                // so the readout can never claim a gain that was refused.
                applied = window.ThemeAudio.setVolume(applied);
            }
            if (readout) readout.textContent = toPercent(applied) + '%';
        });
        return slider;
    }

    window.SettingsAudio = {
        render: render,
        wire: wire,
        toPercent: toPercent,
        STEP_PERCENT: STEP_PERCENT
    };
})();
