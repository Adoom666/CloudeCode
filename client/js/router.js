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
 * - The allow-list used to be `[A-Za-z0-9_\- ]` only, which rejected a
 *   name the app itself produces: a fork label is `<parent title>(fork)`
 *   (src/core/session_fork.py `fork_label`), and both project names and
 *   session titles are free-form - unicode, punctuation, digits - per
 *   the two server-side sources of truth: `validate_project_dir_name()`
 *   (src/core/project_directory.py) for a project name and
 *   `name_is_pushable()` (src/core/claude_rename.py) for a session
 *   title. Between them, the only things either one refuses are `/`,
 *   `\`, a null byte, other control characters, and the bare `.` / `..`
 *   segments - so that is exactly what `isLegalDeepLinkName()` below
 *   refuses too. Everything else either side can produce is accepted.
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

    // Control characters (C0, DEL, C1) - never legal in a project name
    // or a session title (`name_is_pushable()` requires
    // `ch.isprintable()` or space) and never legal in a URL path
    // segment either.
    var CONTROL_CHAR_RX = /[\x00-\x1F\x7F-\x9F]/;

    /**
     * Whether a decoded `/session/<name>` segment is legal to resolve.
     *
     * Description: rejects exactly what is hostile in a URL path
     *   segment - empty, `/`, `\`, a control character, or the reserved
     *   relative segments `.` / `..` - and nothing else. This is the
     *   UNION of what the server accepts for the two kinds of name a
     *   deep link can name: a project name
     *   (`project_directory.validate_project_dir_name`) or a session
     *   title / fork label (`claude_rename.name_is_pushable`). Neither
     *   of those refuses unicode, spaces, parentheses, hyphens or
     *   digits, so this does not either - a regex allow-list narrower
     *   than what the app can actually create rejects names the app
     *   itself produces (see the header comment above).
     * Inputs: decoded (string) - already `decodeURIComponent`-ed.
     * Output: boolean.
     * Example: isLegalDeepLinkName('Punchlist Browser Rename(fork)') -> true
     * Example: isLegalDeepLinkName('..') -> false
     */
    function isLegalDeepLinkName(decoded) {
        if (typeof decoded !== 'string' || decoded.length === 0) {
            return false;
        }
        if (decoded === '.' || decoded === '..') {
            return false;
        }
        if (decoded.indexOf('/') !== -1 || decoded.indexOf('\\') !== -1) {
            return false;
        }
        if (CONTROL_CHAR_RX.test(decoded)) {
            return false;
        }
        return true;
    }

    // Path segment: exactly one `/session/<raw>` - we do NOT match
    // deeper paths like `/session/foo/bar` (FastAPI wouldn't route
    // them to us anyway, but be explicit client-side as well).
    var DEEPLINK_RX = /^\/session\/([^\/]+)\/?$/;

    // ARCHIVE ROUTES. Four of them: /archive, /archive/p/<id>,
    // /archive/t/<id> and /archive/t/<id>/l/<n>.
    //
    // NUMERIC IDS ONLY. `session_ref` is not unique and is not close to
    // unique (measured 2026-08-31: "journal" is the session_ref of 14
    // different transcripts, "audit" of 5), so it can never appear in a
    // path - a link like /archive/s/journal would resolve to one of
    // fourteen with no error, which is the failure mode where the link
    // works for the sender and shows the recipient a different document.
    //
    // WHY THE PATTERNS ARE NOT DECLARED HERE. The design doc (H.5) has
    // this file carrying its own four regexes. archive-deeplink.js was
    // built afterwards and already owns them as ROUTE_PATTERNS, in the
    // canonical MATCH ORDER (line before transcript), with its own test
    // asserting that order fails on the swallowing form. Declaring them
    // a second time here would be two declarations of one fact that can
    // drift, which is the defect class this repo keeps paying for.
    // So: ONE owner, and this file delegates. If archive-deeplink.js is
    // not loaded, `parseArchivePath` below returns a NAMED refusal, not
    // a silent non-match.
    var ARCHIVE_PREFIX = '/archive';

    /**
     * Parse the current path as an archive route.
     *
     * Description: delegates to ArchiveDeeplink.parse(), which owns the
     *   four patterns and their match order. Returns three outcomes, not
     *   two: a route, a stated could-not-resolve, or "not an archive
     *   path at all" so the caller falls through to the session table.
     * Inputs: pathname (string), search (string).
     * Output: {ok: true, route: object}
     *      or {ok: false, token: 'cannot-determine', reason: string}
     *      or {ok: false, token: 'no-match', reason: string}.
     */
    function parseArchivePath(pathname, search) {
        var path = typeof pathname === 'string' ? pathname : '';
        if (path !== ARCHIVE_PREFIX && path.indexOf(ARCHIVE_PREFIX + '/') !== 0) {
            return { ok: false, token: 'no-match', reason: 'not an archive path' };
        }
        if (!window.ArchiveDeeplink || typeof window.ArchiveDeeplink.parse !== 'function') {
            // NOT a silent fall-through. The path IS an archive path and
            // this build cannot resolve it; saying so beats rendering the
            // launcher and letting the person think the link was wrong.
            return { ok: false, token: 'cannot-determine',
                     reason: 'archive routing is unavailable: ' +
                             'archive-deeplink.js did not load.' };
        }
        return window.ArchiveDeeplink.parse(path, search || '');
    }

    /**
     * Hand a parsed archive route to the app, or stash it until auth.
     *
     * Description: the ONE place an archive route becomes a screen, so a
     *   fresh load, a popstate and a post-login delivery take the
     *   identical path.
     * Inputs: route (object) - from parseArchivePath.
     * Output: boolean - true when the route was delivered now.
     */
    function deliverArchiveRoute(route) {
        if (window.App && typeof window.App.showArchive === 'function') {
            try {
                window.App.showArchive(route);
            } catch (err) {
                console.error('Router: App.showArchive threw:', err);
            }
            window.ArchiveDeepLinkTarget = null;
            return true;
        }
        window.ArchiveDeepLinkTarget = route;
        return false;
    }

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
        // An archive URL is a legitimate destination, not a stale session
        // link, and this function runs from showLaunchpad()/showAuth()
        // during boot - BEFORE Router.init() has read the path. Without
        // this guard a fresh load of /archive/t/5767 has its URL rewritten
        // to '/' before anything parses it, and the deep link is lost with
        // no error anywhere. Guarded on the PATH rather than on the stash,
        // because the stash is populated by the very code this would have
        // run ahead of.
        if (window.location.pathname === ARCHIVE_PREFIX ||
                window.location.pathname.indexOf(ARCHIVE_PREFIX + '/') === 0) {
            return;
        }
        if (window.ArchiveDeepLinkTarget) return;
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
        if (!isLegalDeepLinkName(decoded)) {
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
    function applyCurrentPath(immediate) {
        // Archive first: /archive/... can never be a /session/... link,
        // and the archive router is strict where the session router is
        // permissive.
        var archive = parseArchivePath(window.location.pathname,
                                       window.location.search);
        if (archive.ok) {
            clearError();
            // FIRST LOAD ALWAYS STASHES, EVEN WHEN ALREADY AUTHENTICATED.
            // MEASURED 2026-08-31 on a warm reload of /archive/p/12:
            // App.init() is async, so Router.init() runs while auth is
            // still resolving; showing the archive here won the first
            // paint and then App.init()'s existing-token branch called
            // showLaunchpad(), whose hideAllScreens() took it straight
            // back down. The observable result was an archive screen
            // fully built and loaded - 50 transcript rows in the DOM,
            // data-pane="list" - inside a `display: none` div, with the
            // launcher on screen and no error anywhere. Stashing instead
            // hands the route to showLaunchpad()'s first line, which is
            // the ONE owner and consumes it before touching any screen.
            //
            // `immediate` is true only for popstate, where the app has
            // long since settled and there is nothing to race.
            if (immediate && window.Auth &&
                    typeof window.Auth.isAuthenticated === 'function' &&
                    window.Auth.isAuthenticated()) {
                deliverArchiveRoute(archive.route);
            } else {
                window.ArchiveDeepLinkTarget = archive.route;
            }
            return;
        }
        if (archive.token === 'cannot-determine') {
            // NO SILENT REDIRECT. The URL stays as typed and the reason
            // names the offending segment, because rewriting it to
            // /archive would turn a broken link into a working one
            // pointing somewhere the sender never meant.
            showError('archive link could not be resolved: ' + archive.reason);
            window.ArchiveDeepLinkTarget = { view: 'root', projectId: null,
                                             transcriptId: null, lineNo: null,
                                             query: {} };
            if (immediate) deliverArchiveRoute(window.ArchiveDeepLinkTarget);
            return;
        }

        var parsed = parseCurrentPath();

        if (!parsed.match) {
            clearError();
            // Outbound URL sync (Item: session URL reflects current
            // session) - Back/Forward navigated to `/` while a session
            // was on screen. Only a VIEW change, no API call: the
            // session is left running, same as clicking the header title.
            if (window.location.pathname === '/' &&
                window.App &&
                (window.App.currentScreen === 'terminal' ||
                 window.App.currentScreen === 'archive') &&
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

        // 1. First-load parse. NOT immediate: an archive route is stashed
        //    for App.showLaunchpad() to consume, never rendered from here
        //    while App.init() is still resolving auth. See the comment in
        //    applyCurrentPath().
        applyCurrentPath(false);

        // 2. Back/forward navigation re-applies the same logic, and here
        //    delivery IS immediate - the app has settled, there is no
        //    boot sequence left to race, and stashing would mean Back
        //    silently doing nothing.
        window.addEventListener('popstate', function () {
            applyCurrentPath(true);
        });

        // 3. After auth completes, the App controller broadcasts
        //    `authenticated` - that's our cue to deliver any stashed
        //    target. Use a small timeout so the launchpad has time to
        //    mount before we call into it.
        window.addEventListener('authenticated', function () {
            setTimeout(deliverTargetToLaunchpad, 0);
            // NO archive delivery here, deliberately. App.showLaunchpad()
            // consumes window.ArchiveDeepLinkTarget on its FIRST line and
            // returns. Delivering from this listener as well raced that
            // path and the launchpad won: the archive screen was
            // activated and then hidden again, leaving /archive in the
            // address bar and the launcher on screen with no error.
            // One owner for the stash.
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
        // Archive routing - the inbound half. Outbound path building
        // lives in archive-deeplink.js and archive-screen.js.
        parseArchivePath: parseArchivePath,
        deliverArchiveRoute: deliverArchiveRoute,
        ARCHIVE_PREFIX: ARCHIVE_PREFIX,
        // Exposed for tests / debugging.
        _parseCurrentPath: parseCurrentPath,
        _isLegalDeepLinkName: isLegalDeepLinkName,
    };
})();
