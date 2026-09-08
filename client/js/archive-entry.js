/**
 * The way INTO the archive from the rest of the app.
 *
 * WHY THIS FILE EXISTS. Before it, there was no way in at all. Measured
 * on the running app at 9d190df:
 *
 *   grep -rn 'archive' client/js/launchpad.js client/js/header-menu.js
 *       -> no matches
 *   /archive/i.test(document.body.innerText) on the launchpad
 *       -> false
 *   the only control in the entire DOM matching /archive/
 *       -> the archive screen's own Back button
 *
 * So the message browser shipped complete and reachable only by typing
 * the URL. A screen with no entry point is a screen nobody uses, and
 * "the feature is broken" and "the feature is unreachable" look
 * identical from outside.
 *
 * ONE IMPLEMENTATION, TWO ENTRY POINTS. The launchpad row and the header
 * overflow item both call `open()` here rather than each doing their own
 * pushState-and-show. Two copies of a navigation is two copies that can
 * drift - one of them gains a guard, or a route parameter, or a
 * different history mode, and from then on the two doors lead to subtly
 * different places with nothing to say so.
 *
 * WHY pushState AND THEN showArchive, IN THAT ORDER. `App.showArchive()`
 * activates the screen but does not write the address bar - the router
 * owns the inbound half and archive-screen.js owns the outbound half for
 * routes WITHIN the archive. The bare `/archive` entry belongs to
 * neither, so it is written here, BEFORE the screen is shown, because
 * `Router.resetToLauncher()` explicitly refuses to clobber the URL once
 * the path already reads `/archive` (see its guard). Doing it the other
 * way round races that guard.
 *
 * THE ARCHIVE IS OFF BY DEFAULT, AND THIS MODULE IS WHERE THE CLIENT
 * FINDS THAT OUT. `ensure()` measures it once per page load against
 * `GET /api/v1/features`, which the server mounts whether the feature is
 * on or off precisely so that "off" and "on but broken" are different
 * answers rather than the same silence. Both entry points - the launchpad
 * row and the header overflow item - render HIDDEN and reveal themselves
 * only on `enabled`. `unknown` (a failed probe, an API client that never
 * loaded, a server that answered something this build does not
 * understand) leaves them hidden: a door drawn on a guess leads onto a
 * 302 and a wall of 404s, and the two failure directions are not
 * symmetric.
 *
 * A FAILED pushState IS NOT A FAILED NAVIGATION. The History API throws
 * in a sandboxed iframe; router.js already swallows exactly this and
 * carries on, and so does this. The screen still opens; only the address
 * bar is wrong, which is a strictly smaller problem than not opening.
 */

console.log('[ArchiveEntry Module] Loading...');

(function () {
    'use strict';

    /** The bare archive route. Matches Router.ARCHIVE_PREFIX. @type {string} */
    var PATH = '/archive';

    /** Label used by both entry points, so they cannot disagree. */
    var LABEL = 'message archive';

    /**
     * One-line description of what the archive is, shown under the label
     * on the launchpad row. It says READ-ONLY out loud because that is
     * the single most useful thing to know before clicking into a
     * browser over 21,039 transcripts.
     * @type {string}
     */
    var DESCRIPTION =
        'browse ingested transcripts by host, project and line. read-only.';

    /**
     * Description: navigate to the archive screen.
     * Inputs: none.
     * Output: boolean - true when the screen was shown, false when the
     *   app shell was not available to show it. Returning a third
     *   possibility rather than throwing keeps a dead entry point
     *   diagnosable from a test without a live App.
     * Example: window.ArchiveEntry.open();
     */
    /**
     * The three states this module reports. `unknown` is not a flavour of
     * `disabled`: a user who turned the archive ON and hit a broken
     * config must not see what a user who left it off sees, and an entry
     * point that appears on a guess is exactly the leak the server-side
     * switch exists to prevent.
     * @type {string}
     */
    var STATE_ENABLED = 'enabled';
    var STATE_DISABLED = 'disabled';
    var STATE_UNKNOWN = 'unknown';

    /** Where the switch is published. Always mounted, on or off. */
    var FEATURES_PATH = '/features';

    /** @type {string} Last measured state. Starts UNKNOWN, never ENABLED. */
    var _state = STATE_UNKNOWN;

    /** @type {string} One sentence from the server naming why. */
    var _reason = 'the message archive switch has not been read yet.';

    /** @type {Promise|null} Single-flight probe, so N callers make 1 request. */
    var _probe = null;

    /** @type {Function[]} Callbacks fired once per resolution. */
    var _listeners = [];

    /**
     * Description: report the last measured availability of the archive.
     *   Callers that gate a door must treat anything other than
     *   'enabled' as "do not show it" - the safe direction, because the
     *   cost of a wrong 'enabled' is a door onto routes that 404.
     * Inputs: none.
     * Output: string - 'enabled', 'disabled' or 'unknown'.
     * Example: window.ArchiveEntry.state() === 'enabled'
     */
    function state() {
        return _state;
    }

    /**
     * Description: the server's sentence explaining the current state.
     *   Exists so a refusal can be reported rather than merely obeyed.
     * Inputs: none.
     * Output: string.
     * Example: window.ArchiveEntry.reason()
     */
    function reason() {
        return _reason;
    }

    /**
     * Description: register a callback fired once the state resolves. If
     *   it has already resolved, the callback runs immediately, so a late
     *   subscriber cannot miss the only event it cares about.
     * Inputs: fn (Function) - called with the state string.
     * Output: void.
     * Example: window.ArchiveEntry.onResolved(function (s) { ... });
     */
    function onResolved(fn) {
        if (typeof fn !== 'function') return;
        if (_state !== STATE_UNKNOWN) { fn(_state); return; }
        _listeners.push(fn);
    }

    /**
     * Description: publish a resolution to every subscriber exactly once.
     * Inputs: next (string) - the new state. why (string) - the reason.
     * Output: void.
     */
    function _settle(next, why) {
        _state = next;
        _reason = why;
        var pending = _listeners;
        _listeners = [];
        for (var i = 0; i < pending.length; i++) {
            try { pending[i](next); } catch (e) { /* one bad subscriber
                must not stop the others from learning the answer */ }
        }
    }

    /**
     * Description: measure whether this server has the message archive,
     *   once per page load. Backed by `GET /api/v1/features`, which is
     *   mounted whether the feature is on or off - that is what makes
     *   "off" distinguishable from "broken". A failed probe resolves to
     *   'unknown' and NOT to 'enabled': an entry point is never drawn on
     *   a guess.
     * Inputs: none.
     * Output: Promise<string> - the resolved state.
     * Example: window.ArchiveEntry.ensure().then(function (s) { ... });
     */
    function ensure() {
        if (_probe) return _probe;
        if (!window.API || typeof window.API.call !== 'function') {
            _settle(STATE_UNKNOWN,
                    'the API client is not loaded, so the message archive ' +
                    'switch could not be read.');
            _probe = Promise.resolve(_state);
            return _probe;
        }
        _probe = window.API.call(FEATURES_PATH).then(function (data) {
            var block = (data && data.message_archive) || null;
            if (!block || typeof block.state !== 'string') {
                _settle(STATE_UNKNOWN,
                        'the server did not report a message archive state.');
                return _state;
            }
            if (block.state === STATE_ENABLED) {
                _settle(STATE_ENABLED, block.reason || '');
            } else if (block.state === STATE_DISABLED) {
                _settle(STATE_DISABLED, block.reason || '');
            } else {
                // cannot_determine, or a value this client does not know.
                // Both are "nobody measured it", which is UNKNOWN here.
                _settle(STATE_UNKNOWN, block.reason || '');
            }
            return _state;
        }).catch(function (err) {
            _settle(STATE_UNKNOWN,
                    'the message archive switch could not be read: ' +
                    (err && err.message ? err.message : String(err)));
            return _state;
        });
        return _probe;
    }

    function open() {
        if (_state === STATE_DISABLED) {
            // A DEFINITE no. Both doors are hidden when this is the state,
            // so reaching here means something called open() anyway - a
            // stale handler, a console call, a future caller. Refusing is
            // cheap and the alternative is a screen whose every request
            // 404s. UNKNOWN deliberately does NOT refuse: nothing was
            // measured, and turning "I could not tell" into a refusal is
            // the same false verdict in the other direction.
            console.warn('[ArchiveEntry] the message archive is switched ' +
                         'off on this server: ' + _reason);
            return false;
        }
        try {
            if (window.location.pathname !== PATH) {
                window.history.pushState({}, '', PATH);
            }
        } catch (e) {
            // History API blocked (sandboxed iframe etc.). Same tolerance
            // as router.js's enterSession. The navigation below still
            // runs - a wrong address bar is not a reason to refuse.
        }
        if (window.App && typeof window.App.showArchive === 'function') {
            window.App.showArchive({});
            return true;
        }
        console.warn('[ArchiveEntry] App.showArchive is unavailable; ' +
                     'the archive could not be shown.');
        return false;
    }

    /**
     * Description: leave the archive and return to the launcher.
     *
     *   WHY THIS EXISTS AT ALL. Until it did there was NO in-app way out.
     *   The archive screen's own `Back` button is `data-action=back-pane`
     *   and steps one pane WITHIN the archive (see
     *   archive-screen.js::goBackPane, whose backRoute() bottoms out at
     *   'root' and stays there), and the header title's click handler was
     *   gated on `currentScreen === 'terminal'`, so on the archive it did
     *   nothing. The only exit was the browser's own Back button.
     *
     *   WHY THE URL IS WRITTEN HERE AND NOT LEFT TO showLaunchpad().
     *   showLaunchpad() ends in `Router.resetToLauncher()`, which
     *   EXPLICITLY REFUSES to touch the address bar while the path starts
     *   with `/archive` - a guard that exists so a cold load of
     *   /archive/t/5767 is not rewritten to `/` before anything parses
     *   it. Correct for that job, and it means a bare showLaunchpad()
     *   call from the archive leaves `/archive` in the address bar with
     *   the launcher on screen; the next refresh silently lands the user
     *   back in the archive they just left. So the exit writes `/` for
     *   the same reason open() writes `/archive`: this transition belongs
     *   to neither the inbound router nor archive-screen.js's within-the-
     *   archive sync, so it is owned here, next to its opposite, where
     *   the two cannot drift apart.
     *
     *   pushState, NOT history.back(). Back would return the user to
     *   whatever preceded the archive, which is nicer when they arrived
     *   by clicking - and wrong when they arrived by typing the URL or
     *   following a link, where there is no such entry and Back leaves
     *   the app entirely. A deterministic push to `/` never strands
     *   anyone; the archive stays one Back press away either way.
     *
     * Inputs: none.
     * Output: boolean - true when the launcher was shown, false when the
     *   app shell was unavailable to show it.
     * Example: window.ArchiveEntry.close();
     */
    function close() {
        try {
            if (window.location.pathname !== '/') {
                window.history.pushState({}, '', '/');
            }
        } catch (e) {
            // Same tolerance as open(): a wrong address bar is not a
            // reason to refuse to navigate.
        }
        if (window.App && typeof window.App.showLaunchpad === 'function') {
            window.App.showLaunchpad();
            return true;
        }
        console.warn('[ArchiveEntry] App.showLaunchpad is unavailable; ' +
                     'the archive could not be left.');
        return false;
    }

    window.ArchiveEntry = {
        open: open,
        close: close,
        ensure: ensure,
        state: state,
        reason: reason,
        onResolved: onResolved,
        PATH: PATH,
        LABEL: LABEL,
        DESCRIPTION: DESCRIPTION,
        STATE_ENABLED: STATE_ENABLED,
        STATE_DISABLED: STATE_DISABLED,
        STATE_UNKNOWN: STATE_UNKNOWN
    };
    console.log('[ArchiveEntry Module] Exported as window.ArchiveEntry');
})();
