/**
 * GlobalAudioToggle - ONE audio on/off control, ONE DOM node, living in
 * whichever screen's bottom bar is showing.
 * ----------------------------------------------------------------------
 * WHY THIS FILE EXISTS. Audio's control surface used to be scattered:
 * a per-session "play music" row buried inside the session editor FAB's
 * dropdown menu (session-editor-menu.js), reachable only once a session
 * was open and only after two taps to get there. The user asked for one
 * simple on/off, in one place, visible everywhere there is a bottom bar
 * to put it in. This module is that control. The per-session opt-in
 * (session-theme-menu.js's old isAudioOn/setAudioOn/toggleAudio) and the
 * session-editor "play music" row are DELETED, not layered under this -
 * see the note below on why a second gate is not what this is.
 *
 * THIS IS NOT THE DELETED APP-SOUND MASTER SWITCH, EVEN THOUGH IT LOOKS
 * LIKE ONE. themeAudio.js carries a hard warning against reintroducing an
 * app-scoped mute, because the old design had TWO independently stored
 * booleans - an app-scoped master switch (defaulting OFF) AND a
 * per-session opt-in - and either one could veto the other silently. That
 * is the bug: two gates, either able to close, neither visible to the
 * user who only touched one of them. This module does not add a second
 * gate on top of the per-session one. It REPLACES the per-session opt-in
 * outright: there is exactly one stored boolean (cloude.audio.enabled)
 * and exactly one control that reads or writes it. themeAudio.js's own
 * internal sessionName/sessionOn pair is unchanged plumbing - a session
 * still has to be in scope for anything to play, which is automatic
 * (attach/detach), not a second user-facing switch - only the SOURCE of
 * the `on` half moved from per-session storage to this one flag.
 *
 * ONE SHARED BUTTON NODE, MOVED BETWEEN BARS. Same pattern app.js already
 * uses for the connection light (_placeStatusLight): build the element
 * once, and re-parent it into whichever bar the current screen owns
 * rather than keeping one instance per screen. Two instances would need
 * two writers to stay in sync and would eventually drift; one instance
 * cannot drift from itself. place(screen) is called by app.js alongside
 * _placeStatusLight() on every screen transition.
 *
 *   - 'launchpad' (home screen): re-parented into `.home-bar`, inserted
 *     right before `#home-bar-status` - `.home-bar` markup is rendered
 *     by launchpad.js, which this module does not own or edit; it only
 *     ever touches the DOM launchpad.js has already produced, exactly
 *     the way `_placeStatusLight` already does for the same element.
 *   - 'terminal': re-parented into `.info`, inserted right before
 *     `#terminal-bar-status`. `.info` is `display: none` below 768px
 *     (styles.css) to give the mobile terminal back its 17 vertical
 *     pixels; this control adds NO markup outside `.info`, so hiding the
 *     bar hides the control with it and reclaims exactly that space -
 *     nothing here fights that rule.
 *   - 'auth' (and anything else): no target bar exists, so the node has
 *     nowhere to go and stays detached - invisible, same as the
 *     connection light on this screen. No bar, no control.
 *
 * STATE MODEL - THREE OUTCOMES, NEVER TWO. isOn() is the user's stored
 * choice: on or off, nothing else. But "on" does not mean "audible", and
 * collapsing those into one painted state is exactly the defect
 * themeAudioStatus.js was written to retire. paint() therefore always
 * asks ThemeAudioStatus for the live verdict and renders FIVE distinct
 * data-audio-state values so a theme with no track, a theme whose file
 * failed to load, and a theme that is genuinely playing never look the
 * same:
 *   off           - the user's stored choice is off. Nothing else matters.
 *   on-playing    - on, and audio is positively reaching the speaker.
 *   on-no-session - on, but the home screen has no session for a track
 *                   to belong to (ThemeAudioStatus's "only plays inside
 *                   a session" reason).
 *   on-no-track   - on, a session is active, but its theme declares no
 *                   audio block. Distinct from on-error: nothing is
 *                   broken, there is simply nothing to play.
 *   on-error      - on, a track is declared, but it failed to load, was
 *                   blocked by the browser, or is stuck at zero gain.
 *                   Distinct from on-no-track: something IS broken.
 *   on-settling   - on, a track just started opening; not yet knowable.
 *   unknown       - ThemeAudioStatus itself is unavailable or threw. This
 *                   is the COULD-NOT-EVALUATE outcome - it must never be
 *                   rendered as either on-playing or off.
 *
 * Exposed as window.GlobalAudioToggle. Loads AFTER themeAudio.js and
 * themeAudioStatus.js (reads both) and BEFORE app.js calls place() or
 * syncForSession() from its screen-transition methods.
 */
(function () {
    'use strict';

    if (window.GlobalAudioToggle) {
        // Idempotent - never re-init on hot reload or a double script tag.
        return;
    }

    /** localStorage key for the one global on/off choice. */
    var STORAGE_KEY = 'cloude.audio.enabled';

    /** The one button node, built lazily on first use. */
    var btnEl = null;

    /**
     * The user's stored choice. Defaults to false (off) - audio has
     * always shipped silent-by-default in this app, and a control that
     * defaults to making sound on a page load nobody asked for would be
     * the autoplay problem this codebase has spent five fixes avoiding.
     *
     * @returns {boolean}
     */
    function isOn() {
        try {
            return localStorage.getItem(STORAGE_KEY) === 'on';
        } catch (err) {
            return false;
        }
    }

    /**
     * Persist the user's choice. Storage failures (Safari private mode,
     * quota) are swallowed - the in-memory engine state set by
     * applyToActiveSession() still takes effect for this page load, it
     * just will not survive a reload. That degradation is silent by
     * necessity (nothing calls this synchronously to check), which is
     * why isOn() always re-reads storage rather than trusting a cached
     * value that could disagree with what actually persisted.
     *
     * @param {boolean} on
     * @returns {void}
     */
    function persist(on) {
        try {
            localStorage.setItem(STORAGE_KEY, on ? 'on' : 'off');
        } catch (err) { /* see doc comment: degrades, does not throw */ }
    }

    /**
     * The tmux session name currently in scope, or null on the home
     * screen. Mirrors the lookup session-theme-menu.js used to do for
     * the same purpose.
     *
     * @returns {string|null}
     */
    function activeSession() {
        if (!window.Themes || typeof window.Themes.getActiveSession !== 'function') {
            return null;
        }
        return window.Themes.getActiveSession();
    }

    /**
     * Push the current global choice into the audio engine for whichever
     * session (or none) is in scope right now. This is the ONLY function
     * that calls ThemeAudio.setSessionAudio - the engine's gate is still
     * literally keyed by session name (so "no session in scope" still
     * means silence, automatically, exactly as before) but the boolean
     * half now always comes from isOn() instead of a per-session stored
     * key. Safe to call with no ThemeAudio loaded (older cached client,
     * or a page with the module missing) - degrades to a no-op.
     *
     * @returns {void}
     */
    function applyToActiveSession() {
        if (!window.ThemeAudio || typeof window.ThemeAudio.setSessionAudio !== 'function') {
            return;
        }
        try {
            window.ThemeAudio.setSessionAudio(activeSession(), isOn());
        } catch (err) {
            console.warn('GlobalAudioToggle: ThemeAudio gate failed', err);
        }
    }

    /**
     * Build the button once. Icon is the same speaker glyph the deleted
     * session-music row used (session-editor-menu.js ICONS.music before
     * its removal) so the control is visually continuous with the
     * feature it replaces rather than introducing a new unfamiliar mark.
     * A second, muted-speaker path is layered in and toggled by CSS off
     * `data-audio-state="off"` so on/off is legible at a glance without
     * relying on colour alone.
     *
     * @returns {HTMLButtonElement}
     */
    function build() {
        if (btnEl) return btnEl;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.id = 'globalAudioBtn';
        btn.className = 'global-audio-btn';
        btn.setAttribute('aria-pressed', 'false');
        btn.innerHTML =
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
            '<path class="global-audio-btn__speaker" d="M6.5 5.5 9.5 3v10L6.5 10.5H4a.5.5 0 0 1-.5-.5V6a.5.5 0 0 1 .5-.5h2.5Z" ' +
            'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
            '<path class="global-audio-btn__waves" d="M11.75 6.25a2.5 2.5 0 0 1 0 3.5" ' +
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path class="global-audio-btn__slash" d="M3 2.5 13 13.5" ' +
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '</svg>';
        btn.addEventListener('click', function () {
            toggle();
        });
        btnEl = btn;
        return btn;
    }

    /**
     * Diagnose the live audio state without assuming ThemeAudioStatus is
     * loaded. Absence of the diagnosis module is itself a
     * could-not-evaluate outcome, not a silent "off".
     *
     * @returns {{playing: boolean, settling: boolean, reason: string|null}|null}
     *          null means "could not evaluate" - the caller must render
     *          `unknown`, never fall back to a guess.
     */
    function diagnose() {
        if (!window.ThemeAudioStatus || typeof window.ThemeAudioStatus.current !== 'function') {
            return null;
        }
        try {
            return window.ThemeAudioStatus.current();
        } catch (err) {
            return null;
        }
    }

    /**
     * Classify the live state into one of the seven data-audio-state
     * values documented at the top of this file. Pure function of the
     * two inputs so it is testable without a DOM.
     *
     * @param {boolean} on - the stored global choice.
     * @param {{playing: boolean, settling: boolean, reason: string|null}|null} verdict
     * @returns {{state: string, label: string}}
     */
    function classify(on, verdict) {
        if (!on) {
            return { state: 'off', label: 'audio is off, tap to turn on' };
        }
        if (!verdict) {
            return { state: 'unknown', label: 'audio state could not be read' };
        }
        if (verdict.playing) {
            return { state: 'on-playing', label: 'audio is on' };
        }
        if (verdict.settling) {
            return { state: 'on-settling', label: 'audio is on, starting' };
        }
        var reason = verdict.reason || '';
        if (/only plays inside a session/.test(reason)) {
            return { state: 'on-no-session', label: 'audio is on, nothing to play here' };
        }
        if (/has no music track/.test(reason)) {
            return { state: 'on-no-track', label: 'audio is on, this theme has no music' };
        }
        if (/failed to load/.test(reason)) {
            return { state: 'on-error', label: 'audio is on, but the track failed to load' };
        }
        // Every other named reason (blocked by the browser, zero volume,
        // paused, master volume zero, hidden tab, ...) is still a real,
        // named fault - group it as on-error rather than inventing an
        // eighth bucket the CSS and tests would also have to carry.
        return { state: 'on-error', label: reason ? ('audio is on, but ' + reason) : 'audio is on, but not playing' };
    }

    /**
     * Repaint the one button node from current state. No-op if the
     * button has never been built (nothing has called place() yet).
     *
     * @returns {void}
     */
    function paint() {
        if (!btnEl) return;
        var on = isOn();
        var verdict = diagnose();
        var c = classify(on, verdict);
        btnEl.setAttribute('data-audio-state', c.state);
        btnEl.setAttribute('aria-pressed', on ? 'true' : 'false');
        btnEl.setAttribute('aria-label', c.label);
        btnEl.setAttribute('title', c.label);
    }

    /**
     * Flip the stored choice, persist it, push it into the engine for
     * whatever session is active, and repaint.
     *
     * @returns {boolean} the new stored choice.
     */
    function toggle() {
        var next = !isOn();
        persist(next);
        applyToActiveSession();
        paint();
        return next;
    }

    /**
     * Move the one button into the bar for the given screen, matching
     * App._placeStatusLight()'s re-parenting rule exactly (see the
     * top-of-file doc comment for the per-screen targets).
     *
     * @param {'auth'|'launchpad'|'terminal'|'archive'} screen
     * @returns {void}
     */
    function place(screen) {
        var btn = build();
        var targetId = screen === 'launchpad' ? 'home-bar-status'
            : screen === 'terminal' ? 'terminal-bar-status'
                : screen === 'archive' ? 'archive-bar-status'
                    : null;
        var target = targetId ? document.getElementById(targetId) : null;
        if (!target || !target.parentNode) {
            // No bar on this screen (auth) - detach so it renders nowhere
            // rather than lingering wherever it last was. parentNode over
            // parentElement on purpose: the latter is unsupported by this
            // app's hand-rolled test DOM (tests/mini-dom.mjs), and the two
            // agree for every node this module ever touches.
            if (btn.parentNode) btn.parentNode.removeChild(btn);
            return;
        }
        // insertBefore into the SAME position it is already in is a
        // harmless no-op (the DOM spec defines it as remove-then-insert),
        // so this runs unconditionally rather than first checking whether
        // a move is needed - one fewer sibling-pointer property to depend
        // on, at a cost too small to matter for a bottom-bar button.
        target.parentNode.insertBefore(btn, target);
        paint();
    }

    /**
     * Apply the global choice to whichever session (or none) just came
     * into scope, and repaint. Called by app.js in the same three spots
     * SessionThemeMenu.syncForSession() used to be called from: entering
     * the auth screen, entering the launchpad, and attaching a terminal.
     * The name is kept from that predecessor so the call sites read the
     * same way; the per-session lookup it used to do is gone.
     *
     * @returns {void}
     */
    function syncForSession() {
        applyToActiveSession();
        paint();
    }

    // Repaint whenever the engine's gate moves for a reason other than
    // this module's own click - a fade completing, a load error landing
    // asynchronously after the button was already painted "on-settling".
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
        document.addEventListener('cloude:audio-state', function () {
            paint();
        });
    }

    window.GlobalAudioToggle = {
        isOn: isOn,
        toggle: toggle,
        place: place,
        paint: paint,
        syncForSession: syncForSession,
        classify: classify,
        STORAGE_KEY: STORAGE_KEY
    };
})();
