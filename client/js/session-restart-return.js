/**
 * AFTER A RESTART, LAND BACK IN THE SESSION.
 * ----------------------------------------------------------------------
 * "yes, and then fix so i can go right back into it."
 *
 * The old restart left the user in the sidebar looking at a list. The
 * session they had just revived was somewhere in it, and finding it again
 * was the part that hurt. This module closes that loop: a restart that
 * reports `ok` reopens that session's terminal.
 *
 * THE IDENTITY PROBLEM, AND WHY THE NAME IS NOT THE ANSWER. A tmux name
 * is re-minted by this app, so "reopen whatever is called cloude_api" can
 * legitimately land on a different session than the one that was
 * restarted. The durable identity of a session row is the instance triple
 * `(tmux_socket, tmux_name, tmux_created_epoch)`, and a respawn preserves
 * it - `#{session_created}` belongs to the tmux SESSION, not to the
 * pane's process. The row is therefore the same row, with the same
 * `session_uuid`.
 *
 * So the SERVER measures identity, not the client. `POST /sessions/respawn`
 * reads the row back AFTER the restart and returns `session_id` and
 * `session_uuid`. This module reopens by that id and checks the session
 * it got back carries the same id it asked for. A mismatch is reported
 * and does NOT navigate - putting the user into the wrong terminal is
 * worse than leaving them in the list, because the terminal looks right.
 *
 * `session_uuid` and the triple are NOT interchangeable and this module
 * does not pretend otherwise: the uuid is the durable row handle carried
 * across the call, and the triple is what the server matched it on. The
 * client never sees an epoch and never guesses one.
 *
 * NO SESSION ID IS NOT A FAILURE. A session the app has no live backend
 * for (an external or adopted one) has nothing to reopen by id, so it
 * goes through the adopt path by name - the same path the sidebar's own
 * row click uses. That path is name-based by nature; it is the app's
 * existing behaviour for those rows and this module does not invent a
 * stronger claim about it than the app already makes.
 *
 * Load AFTER api.js and app.js; BEFORE session-sidebar-clicks.js.
 */

console.log('[SessionRestartReturn Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: reopen the session a restart just revived.
     *
     *   Returns a verdict rather than throwing, because every one of the
     *   three outcomes needs different words in front of the user and a
     *   caller that only sees an exception cannot tell them apart.
     * Inputs:
     *   result (object) - the RespawnSessionResponse body. Reads
     *     `session_id`, `session_uuid` and `name`.
     * Output:
     *   Promise<{status: string, detail: string}> - `status` is one of:
     *     'reopened'         the terminal is now showing that session.
     *     'not_reopened'     nothing was reopened and the user is still
     *                        where they were. `detail` says why.
     *     'wrong_session'    a session came back that is NOT the one that
     *                        was restarted. Deliberately never navigates.
     * Example:
     *   await SessionRestartReturn.reopen({session_id: 'a1', name: 'x'})
     */
    async function reopen(result) {
        var name = (result && result.name) || '';
        var sessionId = (result && result.session_id) || null;

        if (sessionId) {
            var info;
            try {
                info = await window.API.getSession(sessionId, {
                    includeScrollback: true,
                });
            } catch (err) {
                return {
                    status: 'not_reopened',
                    detail: 'the session restarted but could not be reopened: '
                        + ((err && err.message) || String(err)),
                };
            }
            if (!info) {
                return {
                    status: 'not_reopened',
                    detail: 'the session restarted but the server returned '
                        + 'nothing for it, so it was not reopened',
                };
            }
            // WRAPPER VS .session - the id lives on the nested Session,
            // the tmux name on the wrapper. Reading `info.id` here would
            // be undefined and would fail this check for every session,
            // which is the single most repeated bug in this codebase.
            var gotId = (info.session && info.session.id) || info.id || null;
            if (gotId && sessionId && gotId !== sessionId) {
                return {
                    status: 'wrong_session',
                    detail: 'the session that came back is not the one that '
                        + 'was restarted, so nothing was opened',
                };
            }
            window.App.returnToExistingTerminal(info);
            return { status: 'reopened', detail: '' };
        }

        if (!name) {
            return {
                status: 'not_reopened',
                detail: 'the restart reported no session to reopen',
            };
        }

        try {
            var response = await window.API.adoptSession(name, true);
            var session = response.session || response;
            window.dispatchEvent(new CustomEvent('session-created', {
                detail: {
                    session: session,
                    initialScrollbackB64: response.initial_scrollback_b64 || '',
                    fifoStartOffset:
                        typeof response.fifo_start_offset === 'number'
                            ? response.fifo_start_offset
                            : null,
                    adopted: true,
                },
            }));
            return { status: 'reopened', detail: '' };
        } catch (err) {
            return {
                status: 'not_reopened',
                detail: 'the session restarted but could not be reopened: '
                    + ((err && err.message) || String(err)),
            };
        }
    }

    window.SessionRestartReturn = { reopen: reopen };
})();

console.log('[SessionRestartReturn Module] Exported as window.SessionRestartReturn');
