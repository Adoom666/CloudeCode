/**
 * ThemeNavigation - the ONE place that decides which theme is painted for
 * the thing being navigated to.
 *
 * WHY THIS MODULE EXISTS, in the owner's words on 2026-09-07: "clicking
 * from the left sidebar into a session that has a pinned theme changes the
 * theme correctly. Clicking a DIFFERENT session in that list does NOT
 * change the theme back. Clicking the title to go back to the home page
 * DOES change it back."
 *
 * That asymmetry was the whole diagnosis. app.js carried the restore
 * sequence THREE times, copy-pasted verbatim into showAuth(), showArchive()
 * and showLaunchpad(), under a comment that said out loud: "Same block as
 * showLaunchpad(), deliberately identical rather than shared: it is six
 * lines, and the two screens are free to diverge." Meanwhile the two
 * session-entry paths, showTerminal() and returnToExistingTerminal(), each
 * read
 *
 *     if (pinnedTheme) Themes.applyTheme(pinnedTheme, ...);
 *
 * with no else. So entering a session that HAS a pin painted it, and
 * entering a session that has NO pin painted nothing at all and simply
 * inherited whatever the previous session had left on :root. Going home
 * looked like it "fixed" the theme only because the home screen happened to
 * own one of the three copies of the restore.
 *
 * The bug is therefore not a missing reset on one branch. It is that no
 * function owned the question "what theme should be showing now?", so the
 * answer had to be re-derived, by hand, at every navigation site - and the
 * session-to-session switch (session-sidebar-clicks.js calling
 * App.returnToExistingTerminal directly, never passing through the home
 * screen) was written without it.
 *
 * THE SHAPE THAT KILLS THE BUG CLASS. applyForTarget() is TOTAL: every path
 * through it ends in an applyTheme() call with a resolved theme id. There is
 * no branch that paints nothing. Bolting a second reset onto the
 * session-switch path would have fixed the reported symptom and left the
 * next navigation path someone adds free to reintroduce it. A navigation
 * site cannot forget to restore a theme it never had to restore by hand.
 *
 * WHAT IT DELIBERATELY DOES NOT OWN. Themes.applySession(agentType) is the
 * per-AGENT terminal scope - a different axis from the per-SESSION pin, and
 * replay-gated inside the registry. It stays at its existing call sites in
 * app.js, after terminal setup, untouched by this module.
 */
(function () {
    'use strict';

    /**
     * Description: the user's own global theme id - the answer to "what
     *   should be showing when no session pin overrides it".
     * Inputs: none.
     * Output: string - a theme id, never null.
     *
     * Reads through the registry rather than touching localStorage here.
     * The three copies in app.js each hardcoded the key 'cloude.theme' and
     * the default 'claude', which is exactly the magic-string duplication
     * that let them drift out of step with the registry that owns both.
     */
    function globalThemeId() {
        if (window.Themes && typeof window.Themes.getStoredThemeId === 'function') {
            return window.Themes.getStoredThemeId();
        }
        // Registry not loaded yet (script-order accident). Fall back to the
        // registry's own default rather than painting nothing, because
        // painting nothing is the defect this module exists to remove.
        return (window.Themes && window.Themes.DEFAULT_THEME_ID) || 'claude';
    }

    /**
     * Description: read a session's pinned theme off a `/sessions/list`
     *   payload, checking BOTH levels.
     * Inputs: sessionInfo (object|null) - a SessionInfo wrapper, or an inner
     *   Session row from an older caller.
     * Output: string|null - the pinned theme id, or null when unpinned.
     * Example: resolvePinnedTheme({pinned_theme: 'matrix'}) === 'matrix'
     *
     * `pinned_theme` rides on the SessionInfo WRAPPER, not on the nested
     * `.session`. Reading the wrong level returns undefined silently and
     * looks exactly like the backend not sending it - the single most
     * repeated bug in this project. Doing the two-level read in one place
     * means no navigation site can get the level wrong again.
     */
    function resolvePinnedTheme(sessionInfo) {
        if (!sessionInfo) return null;
        var inner = sessionInfo.session || null;
        return sessionInfo.pinned_theme
            || (inner && inner.pinned_theme)
            || null;
    }

    /**
     * Description: paint the correct theme for a navigation target and put
     *   the theme system into the matching scope. Call on EVERY navigation.
     * Inputs: target (object) -
     *   - kind (string) - 'session' or 'global'. 'global' covers every
     *     screen that is not a session: launchpad, archive, auth.
     *   - sessionName (string|null) - kind 'session' only. The canonical
     *     bare tmux name. MUST NOT be a session id: an adopted session's id
     *     is "adopted:<tmux-name>", which the backend's theme PATCH rejects
     *     with a 404 and which silently breaks pin persistence. Callers
     *     resolve this as `tmux_session || name`.
     *   - pinnedTheme (string|null) - kind 'session' only.
     * Output: string - the theme id actually painted. Returned so a caller
     *   or a test can assert on the decision rather than infer it.
     * Example: applyForTarget({kind: 'session', sessionName: 'cloude_a',
     *                          pinnedTheme: null})  // paints the global theme
     */
    function applyForTarget(target) {
        var t = target || {};
        var isSession = t.kind === 'session';
        var themes = window.Themes;

        // Entering a non-session screen drops the per-agent terminal scope,
        // so the launchpad chrome renders under pure global-theme rules and
        // the next session entry starts from a known baseline. Runs BEFORE
        // the paint below, as all three app.js copies did.
        if (!isSession && themes && typeof themes.clearSession === 'function') {
            themes.clearSession();
        }

        // Scope first: applyGlobal() routes a later user-driven swap to the
        // server pin when a session is in scope, and to localStorage when it
        // is not. An unpinned session still gets its name here - it is
        // pinnable, so a picker swap there must CREATE a pin rather than
        // overwrite the user's global default.
        var scopeName = isSession ? (t.sessionName || null) : null;
        if (themes && typeof themes.setActiveSession === 'function') {
            themes.setActiveSession(scopeName);
        }

        // The paint. `fallback` is the answer for the unpinned case, which
        // is the case that was silently doing nothing.
        var fallback = globalThemeId();
        var wanted = (isSession && t.pinnedTheme) ? t.pinnedTheme : fallback;
        var painted = fallback;
        if (themes && typeof themes.applyTheme === 'function') {
            // persist:false - the server owns a session pin, and localStorage
            // already owns the global choice. This is a re-paint of a choice
            // already made, never a new choice.
            // forXterm:true inside a session - the freshly-attached session
            // must repaint the terminal palette, not just the page chrome.
            var opts = isSession
                ? { persist: false, forXterm: true }
                : { persist: false };
            if (themes.applyTheme(wanted, opts)) {
                painted = wanted;
            } else if (wanted !== fallback) {
                // Unknown theme id, e.g. a pin naming a theme the user has
                // since uninstalled. applyTheme() warns and KEEPS THE CURRENT
                // theme, which on a session switch is the stale-theme bug all
                // over again. Fall back to the global theme instead. Mirrors
                // the registry's own `if (!manifests.has(stored))` guard in
                // init().
                themes.applyTheme(fallback, opts);
            }
        }

        // The audio gate is keyed on the active session name, so it has to be
        // re-synced AFTER setActiveSession() above. Every one of the five
        // navigation sites carried this call and a comment saying so, which
        // is precisely the kind of ordering constraint that gets dropped when
        // a new path is hand-written. It lives inside the function now.
        if (window.GlobalAudioToggle
            && typeof window.GlobalAudioToggle.syncForSession === 'function') {
            window.GlobalAudioToggle.syncForSession();
        }

        return painted;
    }

    /**
     * Description: convenience wrapper for a session navigation - resolves
     *   the pin off either payload level, then applies.
     * Inputs: sessionInfo (object|null) - SessionInfo wrapper or inner row.
     *   sessionName (string|null) - canonical bare tmux name, resolved by
     *   the caller (never a session id - see applyForTarget).
     * Output: string - the theme id actually painted.
     * Example: applyForSession(info, 'cloude_myproject')
     */
    function applyForSession(sessionInfo, sessionName) {
        return applyForTarget({
            kind: 'session',
            sessionName: sessionName || null,
            pinnedTheme: resolvePinnedTheme(sessionInfo)
        });
    }

    /**
     * Description: convenience wrapper for every screen that is not a
     *   session - launchpad, archive, auth.
     * Inputs: none.
     * Output: string - the theme id actually painted.
     */
    function applyForGlobal() {
        return applyForTarget({ kind: 'global' });
    }

    window.ThemeNavigation = {
        applyForTarget: applyForTarget,
        applyForSession: applyForSession,
        applyForGlobal: applyForGlobal,
        resolvePinnedTheme: resolvePinnedTheme
    };
})();
