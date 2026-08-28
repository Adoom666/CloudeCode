/**
 * Deep-link router (Item 9).
 *
 * Reads the URL on page load. When the path matches `/session/<project>`,
 * validates the slug against a strict client-side regex, and - once the
 * user has authenticated - hands the project name to the launchpad so
 * it can auto-open that session.
 *
 * Security posture:
 * - The SERVER route `/session/{project}` intentionally serves the SPA
 *   shell for ANY single-path-segment value. The meaningful validation
 *   happens here, in the browser, so we can show a visible error rather
 *   than a 404 when a user pastes a malformed link.
 * - We REJECT invalid names (show a banner + clear the URL), we do NOT
 *   silently strip - silent stripping turns a bad link into a working
 *   one pointing at an unintended project, which is worse than failing
 *   loudly.
 * - Allowed chars are deliberately narrow: `[A-Za-z0-9_\- ]`. This
 *   excludes `.`, `/`, `..`, `?`, `#`, and all control bytes. The
 *   server-side slugify (src/core/tmux_backend._slugify) maps `.` to
 *   `_` before the slug becomes a tmux session name, so a deep link
 *   produced by a push notification will already be in our allowed set.
 *
 * Flow:
 * 1. `initRouter()` is called on page load from index.html.
 * 2. If the current path is `/session/<raw>`, we URL-decode + validate.
 * 3. Invalid → show banner, `history.replaceState` back to `/`, done.
 * 4. Valid → stash the target name on `window.DeepLinkTarget`.
 * 5. On the `authenticated` event (fired by auth.js after login), we
 *    hand the stashed name to `window.Launchpad.openProjectByName()`
 *    if the method exists; otherwise we fall back to firing a custom
 *    `deep-link-project` event that launchpad can listen for.
 * 6. We also listen for `popstate` so browser back/forward re-triggers
 *    the same flow.
 */
(function () {
    'use strict';

    // Strict client-side slug regex. Mirror the tmux-legal charset
    // (letters, digits, underscore, dash) plus space for human-typed
    // project names. NO dots, NO slashes, NO path traversal.
    var SLUG_RX = /^[A-Za-z0-9_\- ]+$/;

    // Path segment: exactly one `/session/<raw>` - we do NOT match
    // deeper paths like `/session/foo/bar` (FastAPI wouldn't route
    // them to us anyway, but be explicit client-side as well).
    var DEEPLINK_RX = /^\/session\/([^\/]+)\/?$/;

    // Single source of truth for the `/session/<name>` path prefix, used
    // by both the inbound parser (DEEPLINK_RX above) and the outbound
    // builder (buildSessionPath below) so the two halves can never drift.
    var DEEPLINK_PREFIX = '/session/';

    /**
     * Build the URL path for a session, using the SAME encoding the
     * server's `build_deep_link()` (src/core/notifications/events.py)
     * uses to compose push-notification links: `encodeURIComponent` on
     * the browser side is the direct equivalent of Python's
     * `quote(safe="")` there - both percent-encode every non-alphanumeric
     * character, so a link built here and a link built server-side for
     * the same session name land on the identical path. `parseCurrentPath()`
     * above is this function's inverse (`decodeURIComponent` + `SLUG_RX`).
     * Inputs: name (string) - session/project name (NOT yet encoded).
     * Output: string - e.g. `"/session/my-project"`.
     */
    function buildSessionPath(name) {
        return DEEPLINK_PREFIX + encodeURIComponent(String(name));
    }

    /**
     * Update the address bar to reflect the currently-open session, so a
     * hard refresh (or a copy-pasted/bookmarked URL) lands back in the
     * same place instead of the launcher. This is the OUTBOUND half of
     * the deep-link feature - the inbound half (parsing `/session/<name>`
     * on load and routing to it) already existed; this is what was
     * missing.
     *
     * Description: no-ops if the address bar already shows this exact
     *   path (switching to the session you're already viewing shouldn't
     *   add a redundant history entry). Otherwise pushes or replaces
     *   depending on `opts.replace` - see the call sites in app.js
     *   (`showTerminal` / `returnToExistingTerminal`) for the push-vs-
     *   replace decision. Swallows History API exceptions (sandboxed
     *   iframe etc.), matching the existing pattern in this file.
     * Inputs:
     *   name (string) - session/project name to encode into the path.
     *   opts (object|undefined) - `{replace: boolean}`; default push.
     * Output: void.
     */
    function enterSession(name, opts) {
        if (!name) return;
        var path = buildSessionPath(name);
        if (window.location.pathname === path) return;
        var replace = !!(opts && opts.replace);
        try {
            if (replace) {
                window.history.replaceState({}, '', path);
            } else {
                window.history.pushState({}, '', path);
            }
        } catch (e) {
            // History API blocked (sandboxed iframe etc.) - silent, same
            // tolerance as the invalid-deep-link path below.
        }
    }

    /**
     * Update the address bar back to `/` on leaving a session (detach,
     * delete, logout, or navigating to the launcher). Defaults to
     * `replaceState` - a session URL that no longer has a live session
     * behind it (detached/destroyed) should NOT remain a Back-button
     * target; replacing it means pressing Back goes past it instead of
     * bouncing straight back into a session that just went away.
     *
     * Skips the reset entirely while a deep-link target is still pending
     * delivery (`window.DeepLinkTarget` set by `applyCurrentPath` below)
     * - that means the app is mid-way through auto-opening a session from
     * the URL the page loaded with, and clobbering the address bar here
     * would race that in-flight navigation for no benefit (the session
     * entry, once it opens, calls `enterSession` itself and repaints the
     * URL correctly anyway).
     * Inputs: none.
     * Output: void.
     */
    function resetToLauncher() {
        if (window.DeepLinkTarget) return;
        if (window.location.pathname === '/') return;
        try {
            window.history.replaceState({}, '', '/');
        } catch (e) {
            // History API blocked - silent.
        }
    }

    /**
     * Show the top-of-page error banner with the given message. No-op
     * if the target div is missing (shouldn't happen - index.html owns it).
     */
    function showError(message) {
        var el = document.getElementById('deep-link-error');
        if (!el) {
            console.warn('Router: #deep-link-error div missing, cannot display', message);
            return;
        }
        el.textContent = message;
        el.style.display = 'block';
    }

    /**
     * Reject a deep-link target that validated as a legal slug but could
     * not be resolved to anything live (no launcher project, no running
     * or adoptable tmux session) - the failure path documented in this
     * file's header comment ("show banner, replaceState back to /").
     *
     * Description: called by `Launchpad.openProjectByName()` when its
     *   full resolution chain (launcher projects, then live/adopted
     *   sessions) comes up empty. Centralized here - rather than each
     *   caller rolling its own alert()/console.warn - so the failure
     *   path is the SAME UI (the `#deep-link-error` banner) whichever
     *   component determines the target is unresolvable, and so the
     *   `/` reset can never diverge into a "silent bounce" again.
     * Inputs: name (string) - the (already slug-validated) target that
     *   could not be resolved; interpolated into the banner text.
     * Output: void.
     */
    function rejectTarget(name) {
        showError(`session not found: "${name}" - returned to home.`);
        try {
            window.history.replaceState({}, '', '/');
        } catch (e) {
            // History API blocked (sandboxed iframe etc.) - silent.
        }
    }

    /**
     * Hide the error banner (used when navigating to a valid URL via
     * popstate back to `/`).
     */
    function clearError() {
        var el = document.getElementById('deep-link-error');
        if (el) {
            el.style.display = 'none';
            el.textContent = '';
        }
    }

    /**
     * Parse `window.location.pathname`. Returns:
     *   { match: true,  project: "<decoded-name>" }  - valid deep link
     *   { match: true,  project: null, bad: "<raw>" } - bad deep link
     *   { match: false }                              - not a deep link
     */
    function parseCurrentPath() {
        var path = window.location.pathname || '/';
        var m = path.match(DEEPLINK_RX);
        if (!m) {
            return { match: false };
        }
        var raw = m[1];
        var decoded;
        try {
            decoded = decodeURIComponent(raw);
        } catch (e) {
            // Malformed %-encoding is a reject condition, not a recoverable one.
            return { match: true, project: null, bad: raw };
        }
        if (!SLUG_RX.test(decoded)) {
            return { match: true, project: null, bad: decoded };
        }
        return { match: true, project: decoded };
    }

    /**
     * Deliver the stashed project name to the launchpad. Safe to call
     * multiple times (idempotent - clears the global after the first
     * successful hand-off). Uses a direct method call when possible,
     * otherwise falls back to a custom event.
     */
    function deliverTargetToLaunchpad() {
        var target = window.DeepLinkTarget;
        if (!target) {
            return;
        }
        if (window.Launchpad && typeof window.Launchpad.openProjectByName === 'function') {
            console.log('Router: delivering deep-link target to launchpad:', target);
            try {
                window.Launchpad.openProjectByName(target);
            } catch (err) {
                console.error('Router: launchpad.openProjectByName threw:', err);
            }
            window.DeepLinkTarget = null;
            return;
        }
        // Fallback: broadcast an event. Any component that wants to
        // react can listen for `deep-link-project`.
        console.log('Router: launchpad hook missing, firing deep-link-project event for:', target);
        window.dispatchEvent(
            new CustomEvent('deep-link-project', { detail: { project: target } })
        );
        window.DeepLinkTarget = null;
    }

    /**
     * Apply the current URL:
     * - not a deep link → clear any stale error; if this is a `popstate`
     *   arriving back at `/` while a session is showing, sync the VIEW
     *   back to the launcher (pure navigation - never calls detach or
     *   destroy; the session, if any, keeps running server-side exactly
     *   like the header title-click already does).
     * - valid deep link → stash target; if already authed, deliver now
     *   (also covers `popstate` landing on a DIFFERENT `/session/<name>`
     *   than the one currently showing - same delivery path, whether it's
     *   the initial page load or Back/Forward).
     * - invalid deep link → show banner and rewrite URL to `/`.
     */
    function applyCurrentPath() {
        var parsed = parseCurrentPath();

        if (!parsed.match) {
            clearError();
            // Outbound URL sync (Item: session URL reflects current
            // session) - Back/Forward navigated to `/` while a session
            // was on screen. Only a VIEW change, no API call: the
            // session is left running, same as clicking the header title.
            if (window.location.pathname === '/' &&
                window.App && window.App.currentScreen === 'terminal' &&
                typeof window.App.showLaunchpad === 'function') {
                window.App.showLaunchpad();
            }
            return;
        }

        if (parsed.project === null) {
            // Rejected URL - show banner and clean the address bar so
            // a page refresh doesn't re-show the error forever.
            showError('Invalid project name in URL - returned to home.');
            try {
                window.history.replaceState({}, '', '/');
            } catch (e) {
                // History API blocked (sandboxed iframe etc.) - silent.
            }
            return;
        }

        // Valid. Stash globally so whichever component boots later can
        // pick it up. Also: if launchpad is already mounted (i.e. the
        // user was already authed before this URL loaded), deliver now.
        window.DeepLinkTarget = parsed.project;
        clearError();

        // If auth completed before we loaded, there's no event to catch -
        // check the flag and deliver immediately.
        if (window.Auth && typeof window.Auth.isAuthenticated === 'function' && window.Auth.isAuthenticated()) {
            // Defer one tick so launchpad has a chance to finish its own init.
            setTimeout(deliverTargetToLaunchpad, 0);
        }
    }

    /**
     * Public entry point - called from index.html after all other JS
     * modules have loaded. Wires up the initial parse + popstate/auth
     * listeners.
     */
    function initRouter() {
        console.log('Router: initializing deep-link router');

        // 1. First-load parse.
        applyCurrentPath();

        // 2. Back/forward navigation re-applies the same logic.
        window.addEventListener('popstate', function () {
            applyCurrentPath();
        });

        // 3. After auth completes, the App controller broadcasts
        //    `authenticated` - that's our cue to deliver any stashed
        //    target. Use a small timeout so the launchpad has time to
        //    mount before we call into it.
        window.addEventListener('authenticated', function () {
            setTimeout(deliverTargetToLaunchpad, 0);
        });
    }

    // Expose on window so index.html can call it explicitly.
    window.Router = {
        init: initRouter,
        // Outbound URL sync - called from app.js (showTerminal /
        // returnToExistingTerminal / showLaunchpad / showAuth).
        enterSession: enterSession,
        resetToLauncher: resetToLauncher,
        buildSessionPath: buildSessionPath,
        // Deep-link failure path (Task 5 fix) - the ONE place that shows
        // the error banner and resets the URL when a valid-looking target
        // (project OR session) turns out not to exist.
        rejectTarget: rejectTarget,
        showError: showError,
        // Exposed for tests / debugging.
        _parseCurrentPath: parseCurrentPath,
        _SLUG_RX: SLUG_RX,
    };
})();
