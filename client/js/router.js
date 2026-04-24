/**
 * Deep-link router (Item 9).
 *
 * Reads the URL on page load. When the path matches `/session/<project>`,
 * validates the slug against a strict client-side regex, and — once the
 * user has authenticated — hands the project name to the launchpad so
 * it can auto-open that session.
 *
 * Security posture:
 * - The SERVER route `/session/{project}` intentionally serves the SPA
 *   shell for ANY single-path-segment value. The meaningful validation
 *   happens here, in the browser, so we can show a visible error rather
 *   than a 404 when a user pastes a malformed link.
 * - We REJECT invalid names (show a banner + clear the URL), we do NOT
 *   silently strip — silent stripping turns a bad link into a working
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

    // Path segment: exactly one `/session/<raw>` — we do NOT match
    // deeper paths like `/session/foo/bar` (FastAPI wouldn't route
    // them to us anyway, but be explicit client-side as well).
    var DEEPLINK_RX = /^\/session\/([^\/]+)\/?$/;

    /**
     * Show the top-of-page error banner with the given message. No-op
     * if the target div is missing (shouldn't happen — index.html owns it).
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
     *   { match: true,  project: "<decoded-name>" }  — valid deep link
     *   { match: true,  project: null, bad: "<raw>" } — bad deep link
     *   { match: false }                              — not a deep link
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
     * multiple times (idempotent — clears the global after the first
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
     * - not a deep link → clear any stale error and exit.
     * - valid deep link → stash target; if already authed, deliver now.
     * - invalid deep link → show banner and rewrite URL to `/`.
     */
    function applyCurrentPath() {
        var parsed = parseCurrentPath();

        if (!parsed.match) {
            clearError();
            return;
        }

        if (parsed.project === null) {
            // Rejected URL — show banner and clean the address bar so
            // a page refresh doesn't re-show the error forever.
            showError('Invalid project name in URL — returned to home.');
            try {
                window.history.replaceState({}, '', '/');
            } catch (e) {
                // History API blocked (sandboxed iframe etc.) — silent.
            }
            return;
        }

        // Valid. Stash globally so whichever component boots later can
        // pick it up. Also: if launchpad is already mounted (i.e. the
        // user was already authed before this URL loaded), deliver now.
        window.DeepLinkTarget = parsed.project;
        clearError();

        // If auth completed before we loaded, there's no event to catch —
        // check the flag and deliver immediately.
        if (window.Auth && typeof window.Auth.isAuthenticated === 'function' && window.Auth.isAuthenticated()) {
            // Defer one tick so launchpad has a chance to finish its own init.
            setTimeout(deliverTargetToLaunchpad, 0);
        }
    }

    /**
     * Public entry point — called from index.html after all other JS
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
        //    `authenticated` — that's our cue to deliver any stashed
        //    target. Use a small timeout so the launchpad has time to
        //    mount before we call into it.
        window.addEventListener('authenticated', function () {
            setTimeout(deliverTargetToLaunchpad, 0);
        });
    }

    // Expose on window so index.html can call it explicitly.
    window.Router = {
        init: initRouter,
        // Exposed for tests / debugging.
        _parseCurrentPath: parseCurrentPath,
        _SLUG_RX: SLUG_RX,
    };
})();
