/**
 * Decide whether a WebSocket event may touch the shared terminal.
 *
 * WHY THIS FILE EXISTS. There is exactly ONE xterm instance in the page and
 * the user moves between sessions inside it. Each move closes the old socket
 * and opens a new one, but `WebSocket.close()` only STARTS the closing
 * handshake: the socket sits in CLOSING until the peer answers, and every
 * frame already in flight still dispatches a `message` event. The handlers
 * installed by `Terminal.setupWebSocketHandlers()` are closures over the
 * controller, not over the socket, and nothing ever detaches them - so a
 * frame belonging to the session the user just LEFT was written straight
 * into the terminal of the session they had just arrived at.
 *
 * That is a confidentiality failure, not a rendering glitch: session A's
 * transcript rendered inside session B's terminal. It also corrupts the
 * screen, because an attach paint carries an absolute cursor-position
 * sequence, so a foreign frame moves the live session's cursor and the
 * user's own typing lands somewhere else entirely.
 *
 * The rule is deliberately a PURE function with no DOM and no socket I/O,
 * so the isolation claim can be tested directly rather than inferred from
 * the order of two side effects.
 *
 * TWO INDEPENDENT CHECKS, and both are load-bearing:
 *
 *   1. SOCKET IDENTITY. The event's socket must still be the controller's
 *      live socket. This is what catches the in-flight frame, and it is the
 *      only check that works when the session id is unknown.
 *   2. SESSION IDENTITY. The session the socket was opened FOR must still be
 *      the session on screen. This catches a socket that outlives a session
 *      swap without being replaced, and it states the isolation rule out
 *      loud instead of leaving it as a consequence of check 1.
 *
 * An UNKNOWN session id NEVER rejects. `Terminal._sessionId()` returns null
 * before the first session resolves, and the server documents that a WS with
 * no `?session_id=` falls back to the current session. Refusing on null
 * would break that legacy single-session path, and "not knowing" is not
 * evidence of a mismatch. Check 1 still applies in that case.
 */

(function () {
    'use strict';

    const TerminalFrameGuard = {
        /**
         * May this WebSocket event act on the shared terminal?
         *
         * @param {object} ev - The event's provenance.
         * @param {*} ev.socket - The socket the handler was installed on.
         * @param {*} ev.liveSocket - The controller's current socket
         *   (`Terminal.ws`), or null when it has already been torn down.
         * @param {?string} ev.boundSessionId - Session id this socket was
         *   opened for; null when it was not known at open time.
         * @param {?string} ev.currentSessionId - Session id on screen now;
         *   null when none is resolved.
         * @returns {{ok: boolean, reason: string}} `ok` false means DROP the
         *   event. `reason` is one of `live`, `superseded-socket`,
         *   `session-changed` - always a word, never an empty string, so a
         *   caller can log why without inventing one.
         *
         * Example:
         *   TerminalFrameGuard.accepts({
         *       socket: sock, liveSocket: this.ws,
         *       boundSessionId: 'ses_a', currentSessionId: 'ses_b',
         *   })  // => {ok: false, reason: 'session-changed'}
         */
        accepts(ev) {
            const e = ev || {};
            if (!e.socket || e.socket !== e.liveSocket) {
                return { ok: false, reason: 'superseded-socket' };
            }
            if (
                e.boundSessionId &&
                e.currentSessionId &&
                e.boundSessionId !== e.currentSessionId
            ) {
                return { ok: false, reason: 'session-changed' };
            }
            return { ok: true, reason: 'live' };
        },
    };

    if (typeof window !== 'undefined') {
        window.TerminalFrameGuard = TerminalFrameGuard;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = TerminalFrameGuard;
    }
})();
