/**
 * Alternate-screen scrollback for Claude Code's `tui: fullscreen`.
 * ----------------------------------------------------------------------
 * WHY THIS EXISTS
 *
 * Claude Code's `tui` setting picks the renderer. Under `default` it draws
 * on the MAIN screen, so the terminal keeps real scrollback and
 * terminal-scroll.js can move it with `term.scrollLines()`. Under
 * `fullscreen` - the flicker-free mode, and the one a new user is most
 * likely to land on - claude switches to the ALTERNATE screen (`?1049h`).
 * The alternate screen has no scrollback by construction: measured on a
 * live 2.1.199 session, `tmux display-message -p '#{history_size}'` is
 * exactly 0 and xterm's `buffer.active.baseY` never leaves 0. There is
 * nothing for a scroll gesture to move. Scrolling appears to "stop at the
 * welcome header", which reads as a broken app.
 *
 * WHAT WAS MEASURED (claude 2.1.199, 100x30 pane, 2026-08-17)
 *
 *   - claude enables mouse tracking (`?1000h ?1002h ?1003h ?1006h`) and
 *     then IGNORES the wheel: SGR button 64 does nothing at any row,
 *     repeated or after a motion event.
 *   - PageUp, arrows, shift-arrows and X10 wheel all do nothing in the
 *     normal state.
 *   - `ctrl+o` (0x0f) opens claude's own "detailed transcript" view. In
 *     that view UP scrolls one row and PageUp jumps a page. 50 UPs in a
 *     single write moved exactly 50 rows - no coalescing, perfectly
 *     linear. Roughly 180 PageUps reached the very top of a 240-message
 *     transcript, so there is no depth limit.
 *   - A draft typed into the prompt SURVIVES a ctrl+o round trip intact.
 *   - The transcript view renders its own footer,
 *     "Showing detailed transcript - ctrl+o to toggle - up/down scroll",
 *     which is both the state signature and the user's way out.
 *
 * THE HAZARD, AND HOW IT IS CONTAINED
 *
 * This module synthesises keystrokes into a live session that may hold
 * the user's real work. `ctrl+o` means something else - sometimes
 * destructive - in a shell, in nano, and in several REPLs, and all of
 * those can also own the alternate screen. So the alternate screen alone
 * is NEVER sufficient authority to inject. Every send is gated on a
 * POSITIVE identification of claude's own chrome, read out of the xterm
 * buffer at the moment of the gesture, plus a typing quiet period. When
 * the read is anything other than a confident claude state the answer is
 * to do nothing at all. See detectState().
 */
(function () {
    'use strict';

    /** ctrl+o - claude's detailed-transcript toggle. */
    var CTRL_O = '\x0f';

    /** CSI cursor up/down. Verified against claude 2.1.199: it does not
     *  set DECCKM, so the CSI form (not SS3) is what it reads. */
    var CSI_UP = '\x1b[A';
    var CSI_DOWN = '\x1b[B';

    /** Box-drawing horizontal, U+2500. claude's prompt frame is built of it. */
    var RULE_CHAR = '─';

    /** How many consecutive RULE_CHARs count as a frame edge. */
    var RULE_RUN = 20;

    /** claude's prompt caret, U+276F. */
    var CARET = '❯';

    /**
     * Substring that appears ONLY in the transcript view's footer. Lower
     * cased before comparison so a theme that upper cases it still matches.
     */
    var TRANSCRIPT_MARK = 'ctrl+o to toggle';

    /**
     * Refuse to inject anything within this long of the user's last
     * keystroke. Deliberately generous: a missed scroll is a shrug, a
     * keystroke injected into a half-typed prompt is not.
     */
    var TYPING_QUIET_MS = 1200;

    /** Give up waiting for the transcript view to paint after this long. */
    var OPEN_SETTLE_MS = 1500;

    /** How often to re-read the buffer while waiting for that paint. */
    var OPEN_POLL_MS = 40;

    /**
     * Never move more than this many rows for one dispatch.
     *
     * Was 40 back when every row cost one arrow key. Rows are now carried
     * by PageUp/PageDown where they can be (altscreen-keys.js), so 240
     * rows is 15 keystrokes rather than 240 and the cap can be what it
     * was always meant to be - a guard against a runaway delta firing an
     * unbounded write into a live session - instead of a ceiling on how
     * far a flick may scroll.
     */
    var MAX_ROWS_PER_GESTURE = 240;

    /** Timestamp (ms) of the user's last real keystroke. 0 = never. */
    var lastInputAt = 0;

    /** Timestamp (ms) of the ctrl+o we sent to OPEN the view. 0 = none. */
    var openSentAt = 0;

    /** Returns the live xterm Terminal, or null. */
    var getTerm = function () { return null; };

    /** Writes a raw string to the pty. Set by init(). */
    var sendKeys = function () { };

    /** setTimeout handle for the settle poll, so it cannot stack up. */
    var settleTimer = null;

    /**
     * Read the visible rows of the terminal as plain strings.
     *
     * Only the VISIBLE window is read, never the whole buffer: the
     * question is "what is on screen right now", and on the alternate
     * screen the visible window is the whole buffer anyway.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {string[]} one string per visible row, empty on any failure.
     */
    function visibleRows(term) {
        var out = [];
        try {
            var buf = term && term.buffer && term.buffer.active;
            if (!buf) return out;
            var top = buf.viewportY;
            var n = term.rows || 0;
            for (var i = 0; i < n; i++) {
                var line = buf.getLine(top + i);
                out.push(line ? line.translateToString(true) : '');
            }
        } catch (err) {
            console.warn('AltScreenScroll: buffer read failed', err);
            return [];
        }
        return out;
    }

    /**
     * Is this row one edge of claude's prompt frame?
     *
     * @param {string} row - a visible row's text.
     * @returns {boolean} true when it holds a long run of U+2500.
     */
    function isRule(row) {
        if (!row) return false;
        var run = 0;
        for (var i = 0; i < row.length; i++) {
            if (row.charAt(i) === RULE_CHAR) {
                run++;
                if (run >= RULE_RUN) return true;
            } else {
                run = 0;
            }
        }
        return false;
    }

    /**
     * Does this row look like claude's prompt line?
     *
     * Measured: claude renders the caret followed by U+00A0, so a plain
     * `startsWith(CARET)` after trimming leading whitespace is the test.
     *
     * @param {string} row - a visible row's text.
     * @returns {boolean}
     */
    function isPromptLine(row) {
        if (!row) return false;
        return row.replace(/^\s+/, '').indexOf(CARET) === 0;
    }

    /**
     * WHICH PROGRAM OWNS THE ALTERNATE SCREEN, AND IN WHAT STATE.
     *
     * This is the whole safety argument, so it is worth being explicit
     * about what each answer is evidence FOR.
     *
     *   'main'       - not the alternate screen. Real scrollback exists;
     *                  the caller must use term.scrollLines() and this
     *                  module must not touch anything.
     *   'transcript' - claude's detailed-transcript view is open. Proven
     *                  by its own footer text, which no other program
     *                  prints, and which claude only draws in that view.
     *                  Arrow keys here are the documented scroll keys.
     *   'live'       - claude's normal view. Proven by its prompt frame:
     *                  a rule row, a caret row, another rule row, in three
     *                  consecutive visible rows. `less`, `htop`, `top` and
     *                  `vim` were each run on the alternate screen and
     *                  none of them produces that shape (verified
     *                  2026-08-17); a shell prompt using U+276F lives on
     *                  the MAIN screen and is excluded by the gate above.
     *   'unknown'    - the alternate screen is owned by something we
     *                  cannot identify, OR it is claude showing a dialog
     *                  in place of its prompt. Both mean the same thing:
     *                  we have no authority to synthesise a keystroke.
     *
     * The third outcome is a real answer, not a flavour of the other two.
     * Callers must treat 'unknown' as "inject nothing", never as "probably
     * fine".
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {('main'|'live'|'transcript'|'unknown')}
     */
    function detectState(term) {
        var type = null;
        var baseY = null;
        try {
            var buf = term && term.buffer && term.buffer.active;
            if (buf) {
                type = buf.type;
                baseY = buf.baseY;
            }
        } catch (err) {
            console.warn('AltScreenScroll: buffer read failed', err);
            return 'unknown';
        }
        // "I could not read the buffer" is NOT "it is the main screen".
        // Collapsing the two would let an unreadable buffer authorise the
        // main-screen path, which is the same false-green shape this
        // module exists to avoid on the other side.
        if (typeof type !== 'string' || typeof baseY !== 'number') return 'unknown';
        // THE GATE IS "IS THERE SCROLLBACK", NOT "IS IT THE ALT BUFFER".
        //
        // `type === 'alternate'` looked like the obvious gate and it is
        // WRONG on the path that matters most. Measured 2026-08-17
        // against a live fullscreen session: this app streams the pane
        // with `tmux pipe-pane`, which only carries bytes emitted AFTER it
        // attached, and a reconnecting client is repainted with Ctrl+L
        // (src/api/ws_startup_paint.py, gated on `#{alternate_on}`).
        // Claude answers a Ctrl+L by redrawing - it does NOT re-send
        // `?1049h`, because from its side it never left the alternate
        // screen. So the pane is in fullscreen, the pane's own
        // `#{alternate_on}` is 1, and the browser's xterm is sitting on
        // the NORMAL buffer with identical content. Gating on the buffer
        // type there means the feature is dead for every user who
        // backgrounds the tab and comes back, which on a phone is most of
        // them.
        //
        // `baseY` answers the question we actually have. It is the number
        // of rows that have scrolled off the top, so `baseY === 0` IS
        // "there is no scrollback to move" - true on the alternate buffer
        // by construction, and true on the mislabelled normal buffer of a
        // reconnect. Above zero there is real scrollback and
        // `term.scrollLines()` is the right tool, so we stand down.
        if (baseY > 0) return 'main';

        var rows = visibleRows(term);
        if (!rows.length) return 'unknown';

        for (var i = 0; i < rows.length; i++) {
            if (rows[i].toLowerCase().indexOf(TRANSCRIPT_MARK) !== -1) {
                return 'transcript';
            }
        }
        for (var j = 0; j + 2 < rows.length; j++) {
            if (isRule(rows[j]) && isPromptLine(rows[j + 1]) && isRule(rows[j + 2])) {
                return 'live';
            }
        }
        return 'unknown';
    }

    /**
     * Record a real keystroke from the user.
     *
     * Wired at the two places raw input reaches the pty (xterm's onData
     * and the d-pad's sendKeyToTerminal). Keys this module synthesises go
     * out through sendKeys() and deliberately do NOT come back through
     * here, so our own writes cannot extend our own quiet period.
     *
     * @returns {void}
     */
    function noteUserInput() {
        lastInputAt = Date.now();
    }

    /**
     * Is the user mid-keystroke?
     *
     * @returns {boolean} true inside TYPING_QUIET_MS of the last keystroke.
     */
    function isTyping() {
        if (!lastInputAt) return false;
        return (Date.now() - lastInputAt) < TYPING_QUIET_MS;
    }

    /**
     * Are we still waiting for a ctrl+o we already sent to take effect?
     *
     * @returns {boolean}
     */
    function openPending() {
        if (!openSentAt) return false;
        return (Date.now() - openSentAt) < OPEN_SETTLE_MS;
    }

    /**
     * Build the key bytes for a signed row count.
     *
     * Delegates to altscreen-keys.js, which carries as much of the
     * distance as it can on PageUp/PageDown (16 rows each, measured) and
     * the remainder on arrows. Reaching the top of a long transcript was
     * about 440 arrow presses; the same distance is 27 pages plus 8
     * arrows. The decomposition is exact, so the view still lands on the
     * row that was asked for.
     *
     * @param {number} rows - signed row count; >0 scrolls down, <0 up.
     * @returns {string} the bytes to write, capped at MAX_ROWS_PER_GESTURE.
     *   Empty when the key builder is missing, which is a load-order
     *   regression and must send NOTHING rather than guess at arrows.
     */
    function arrowRun(rows) {
        var keys = window.AltScreenKeys;
        if (!keys || typeof keys.keysForRows !== 'function') {
            console.warn('AltScreenScroll: AltScreenKeys missing, sending nothing');
            return '';
        }
        return keys.keysForRows(rows, MAX_ROWS_PER_GESTURE);
    }

    /**
     * Poll the buffer until the transcript view has actually painted, then
     * send the arrows that the opening gesture was asking for.
     *
     * WHY A POLL AND NOT A FIXED DELAY: arrows written in the SAME pty
     * write as the ctrl+o are DROPPED - measured, the view opened and the
     * 10 following UPs moved nothing. Claude discards input across the
     * view switch. A fixed delay would have to guess a round trip that
     * varies with the network. This waits for the evidence instead, and if
     * the evidence never arrives it sends nothing at all rather than
     * firing arrows at whatever is actually on screen.
     *
     * @param {number} rows - signed row count the gesture asked for.
     * @param {number} deadline - Date.now() after which to give up.
     * @returns {void}
     */
    function sendAfterOpen(rows, deadline) {
        settleTimer = null;
        var term = getTerm();
        var state = detectState(term);
        if (state === 'transcript') {
            openSentAt = 0;
            if (!isTyping()) sendKeys(arrowRun(rows));
            return;
        }
        if (Date.now() >= deadline || state === 'main') {
            // Gave up, or claude left the alternate screen underneath us.
            // Send nothing: we have no idea what would receive it.
            openSentAt = 0;
            return;
        }
        settleTimer = setTimeout(function () {
            sendAfterOpen(rows, deadline);
        }, OPEN_POLL_MS);
    }

    /**
     * Handle one scroll gesture on the alternate screen.
     *
     * Called by BOTH the wheel path and the touch path so the two cannot
     * drift apart; they hand in the same signed row count they would have
     * passed to term.scrollLines().
     *
     * IDEMPOTENCE: the open/closed state is re-read from the SCREEN on
     * every gesture, never from a flag alone. The flag (openSentAt) only
     * suppresses - it covers the window where the ctrl+o is still in
     * flight and the screen still shows 'live', which is exactly when two
     * fast gestures would otherwise send ctrl+o twice and toggle the view
     * shut. During that window this returns handled-but-does-nothing.
     *
     * @param {number} rows - signed row count; >0 down, <0 up.
     * @returns {boolean} true when this module owns the gesture, in which
     *   case the caller must NOT also call term.scrollLines(). false only
     *   for the main screen, where normal scrollback works.
     */
    function scrollByRows(rows) {
        var term = getTerm();
        if (!term) return false;
        var state = detectState(term);
        if (state === 'main') return false;
        if (!rows) return true;
        // Alternate screen, but not identified as claude. Swallow the
        // gesture - there is nothing to scroll and nothing safe to send.
        if (state === 'unknown') return true;
        if (openPending() && state !== 'transcript') return true;
        if (isTyping()) return true;

        if (state === 'live') {
            // Opening gesture. Send ONLY the toggle here; the arrows go
            // out from sendAfterOpen() once the view is proven open.
            openSentAt = Date.now();
            sendKeys(CTRL_O);
            if (settleTimer) clearTimeout(settleTimer);
            settleTimer = setTimeout(function () {
                sendAfterOpen(rows, Date.now() + OPEN_SETTLE_MS);
            }, OPEN_POLL_MS);
            return true;
        }

        openSentAt = 0;
        sendKeys(arrowRun(rows));
        return true;
    }

    /**
     * Close the transcript view and return to the live conversation.
     *
     * Backs the d-pad's "scroll to bottom" control. Screen-verified: a
     * blind ctrl+o sent while already live would OPEN the view instead of
     * closing it, which was measured and is the reason this cannot be a
     * plain toggle.
     *
     * @returns {boolean} true when a close was sent, in which case the
     *   caller should not also pin the (empty) alternate-screen viewport.
     */
    function exitTranscript() {
        var term = getTerm();
        if (detectState(term) !== 'transcript') return false;
        if (settleTimer) {
            clearTimeout(settleTimer);
            settleTimer = null;
        }
        openSentAt = 0;
        sendKeys(CTRL_O);
        return true;
    }

    /**
     * Wire dependencies. Idempotent; safe to call again on session swap.
     *
     * @param {function(): (object|null)} termGetter - returns the live
     *   xterm Terminal.
     * @param {function(string): void} writer - writes a raw string to the
     *   pty WITHOUT counting as user input.
     * @returns {void}
     */
    function init(termGetter, writer) {
        if (typeof termGetter === 'function') getTerm = termGetter;
        if (typeof writer === 'function') sendKeys = writer;
    }

    /** Test seam: reset module state between assertions. */
    function _reset() {
        lastInputAt = 0;
        openSentAt = 0;
        if (settleTimer) clearTimeout(settleTimer);
        settleTimer = null;
        getTerm = function () { return null; };
        sendKeys = function () { };
    }

    window.AltScreenScroll = {
        init: init,
        detectState: detectState,
        scrollByRows: scrollByRows,
        exitTranscript: exitTranscript,
        noteUserInput: noteUserInput,
        isTyping: isTyping,
        _reset: _reset,
        _keys: { CTRL_O: CTRL_O, UP: CSI_UP, DOWN: CSI_DOWN },
        _timing: {
            TYPING_QUIET_MS: TYPING_QUIET_MS,
            OPEN_SETTLE_MS: OPEN_SETTLE_MS,
            OPEN_POLL_MS: OPEN_POLL_MS,
            MAX_ROWS_PER_GESTURE: MAX_ROWS_PER_GESTURE
        }
    };
})();
