/**
 * Session transport state - is the browser's socket to a session up?
 *
 * WHY THIS IS A MODULE AND NOT A FIELD ON A ROW. Every other signal the
 * status LED reads is a fact about the SESSION, measured on the Mac and
 * shipped down `/sessions/list`. "Disconnected" is a fact about THIS
 * BROWSER instead: the WebSocket in client/js/terminal.js closed. The
 * server cannot report it, a poll cannot discover it, and it is true of
 * at most one session at a time - the one the terminal is attached to.
 * So it is held here, next to the LED that renders it, rather than
 * smuggled onto a row model that means something else.
 *
 * ONE SESSION, NEVER MORE. This browser opens exactly one terminal
 * socket. Asking about any other session therefore returns `unknown`,
 * NOT `connected` and NOT `disconnected` - we have not looked, and this
 * project's whole status model turns on not dressing an absent
 * measurement up as a measured one. A sidebar full of red lights because
 * one socket dropped would be exactly that mistake.
 *
 * WHO WRITES IT. client/js/terminal.js, from its own `ws.onopen` and
 * `ws.onclose`, which are the only two places in the app that know.
 *
 * WHO READS IT. client/js/session-sidebar-rows.js and
 * client/js/launchpad.js, which pass the answer into
 * SessionStatusUI.dotHtml as the `transport` signal;
 * client/js/status-led.js turns `disconnected` into a red LED whose
 * label says "no live connection to this session", deliberately
 * different words from the dead pane's "the process exited".
 *
 * PURE STATE, NO DOM, NO FETCH. Loads before terminal.js, launchpad.js
 * and the sidebar modules.
 */

console.log('[SessionTransport Module] Loading...');

(function () {
    /** @type {string} No socket has been opened, or it was for another
     * session. The honest default and the only value most rows get. */
    const UNKNOWN = 'unknown';

    /** @type {string} The socket for this session is open. */
    const CONNECTED = 'connected';

    /** @type {string} The socket for this session closed unexpectedly. */
    const DISCONNECTED = 'disconnected';

    /**
     * The tmux name of the session this browser has (or had) a socket
     * for, and that socket's state. Null name means never attached.
     * @type {{name: (string|null), state: string}}
     */
    const held = { name: null, state: UNKNOWN };

    /**
     * Record what this browser's terminal socket is doing.
     *
     * Description: called by client/js/terminal.js only. A `state` that
     *   is neither `connected` nor `disconnected` is stored as `unknown`
     *   rather than rejected, so a future caller passing something new
     *   degrades to "not measured" instead of asserting a colour.
     * Inputs:
     *   name (string|null|undefined) - the tmux session name the socket
     *     belongs to. Falsy clears the record entirely.
     *   state (string) - CONNECTED, DISCONNECTED, or anything else.
     * Output: undefined.
     * Example:
     *   SessionTransport.mark('cloude_api', 'disconnected');
     */
    function mark(name, state) {
        if (!name) {
            held.name = null;
            held.state = UNKNOWN;
            return;
        }
        held.name = String(name);
        held.state =
            state === CONNECTED || state === DISCONNECTED ? state : UNKNOWN;
    }

    /**
     * Forget the socket record. Used on a deliberate detach, where the
     * absence of a socket is not a fault and must not paint red.
     *
     * Inputs: none.
     * Output: undefined.
     * Example: SessionTransport.clear();
     */
    function clear() {
        mark(null, UNKNOWN);
    }

    /**
     * What, if anything, this browser knows about one session's socket.
     *
     * Description: PURE. Returns `unknown` for every session except the
     *   one currently recorded, which is the normal case for every row on
     *   screen. Name comparison is exact: a session id is not a tmux name
     *   in this app and guessing between them loses sessions.
     * Inputs:
     *   name (string|null|undefined) - a tmux session name.
     * Output:
     *   string - CONNECTED, DISCONNECTED or UNKNOWN.
     * Example:
     *   SessionTransport.stateFor('cloude_api') -> 'disconnected'
     */
    function stateFor(name) {
        if (!name || !held.name) return UNKNOWN;
        return String(name) === held.name ? held.state : UNKNOWN;
    }

    const api = {
        UNKNOWN: UNKNOWN,
        CONNECTED: CONNECTED,
        DISCONNECTED: DISCONNECTED,
        mark: mark,
        clear: clear,
        stateFor: stateFor,
    };

    // Published on globalThis rather than window by name so the same file
    // loads unchanged in a browser and under `node --test`, matching
    // client/js/status-led.js.
    globalThis.SessionTransport = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }

    console.log('[SessionTransport Module] Loaded');
})();
