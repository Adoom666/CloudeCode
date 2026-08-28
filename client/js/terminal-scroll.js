/**
 * Terminal scroll anchoring.
 * ----------------------------------------------------------------------
 * Owns the single question "after this write, should the viewport jump
 * back to the bottom?" and nothing else.
 *
 * WHY THIS EXISTS (the scrollback bug):
 *
 * The original code answered that question with a boolean
 * (`Terminal#autoScrollEnabled`) that was flipped to false by a
 * `.xterm-viewport` scroll listener behind a 100ms debounce, while
 * `Terminal#flush()` called `scrollToBottom()` on EVERY write for as
 * long as the flag was still true. While an agent is streaming output
 * (a spinner alone redraws several times a second) those two race, and
 * the write always wins:
 *
 *   1. finger drags up  -> scroll event -> 100ms debounce timer starts
 *   2. output arrives inside that 100ms -> flag still true -> yanked
 *      back to the bottom
 *   3. the yank is itself a scroll event, so the debounce restarts; when
 *      it finally runs the viewport IS at the bottom, so the flag is set
 *      back to true
 *
 * The user can never stay scrolled up. The desktop wheel path dodged it
 * by clearing the flag synchronously (its own comment called this
 * "bypass the 100ms scroll-listener debounce race"), which is why the
 * wheel half-worked and touch did not at all.
 *
 * THE FIX: stop tracking intent in a racing flag and measure the actual
 * buffer position BEFORE each write. xterm exposes it exactly:
 * `buffer.active.viewportY` is the top row on screen and
 * `buffer.active.baseY` is the top row when scrolled fully down, so
 * `viewportY >= baseY` IS "the user is at the bottom". Sampled before
 * `term.write()` it cannot race the write, because it describes where
 * the user had already put the viewport. A short gesture latch covers
 * the remaining case of momentum scrolling passing through the bottom
 * mid-drag.
 *
 * No timers decide anything here; the latch only suppresses.
 *
 * WHY xterm's `scrollOnUserInput` IS OFF (the "copy jumps to bottom" fix)
 *
 * xterm defaults it to true, and it re-pins the viewport from inside
 * CoreService.triggerDataEvent on any data event flagged as user input.
 * That is a SECOND owner of the same decision this module exists to own,
 * and it fires on paths that are not a request to go to the bottom -
 * notably the mouse reports a selection produces whenever the running
 * application has mouse tracking on.
 *
 * The user diagnosed the shape of it from his own tmux config years ago:
 * tmux binds `copy-selection-and-cancel` by default, and it is the CANCEL
 * that leaves copy mode and snaps to the bottom, so he rebound it to
 * `copy-selection`. Same fix here - stop doing the second half. The
 * mechanism differs (xterm's own selection plus this module's pin logic,
 * never a tmux copy-mode: the app attaches no tmux client at all) but the
 * principle is identical: COMPLETING A SELECTION MUST NOT RE-PIN THE VIEW.
 *
 * Nothing is lost, because "back to live" still has explicit owners:
 * pinToBottom() from the d-pad, from post-reconnect repaint, and from
 * terminal.js's onData handler, which is the only path a real keystroke
 * takes. See also touch-select.js clampToRows(), which removes the other
 * half of the same defect - xterm's selection drag-scroll interval.
 */
(function () {
    'use strict';

    /**
     * How long after the last touch/wheel movement we keep refusing to
     * auto-scroll. Covers iOS momentum, which keeps moving the viewport
     * after touchend with no further touch events.
     */
    var GESTURE_LATCH_MS = 1200;

    /** Timestamp (ms) of the last user scroll gesture. 0 = never. */
    var lastGestureAt = 0;

    /** True between touchstart and touchend on the viewport. */
    var touchActive = false;

    /** Guard so init() is idempotent across session swaps. */
    var wired = false;

    /**
     * Returns the live xterm Terminal, or null. Set by init(). A getter
     * rather than the instance because the listeners are wired once onto
     * #terminal (never recreated) while `term` IS replaced on a session
     * swap.
     *
     * @type {function(): (object|null)}
     */
    var getTerm = function () { return null; };

    /** pageY of the previous touchmove, for per-event deltas. */
    var lastTouchY = 0;

    /** Sub-row remainder carried between touchmoves so slow drags move. */
    var pendingRows = 0;

    /**
     * Is the terminal viewport scrolled to the live bottom?
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {boolean} true when the bottom row is on screen, and also
     *   true when the buffer cannot be read (fail safe: an unreadable
     *   buffer should keep following output rather than freeze).
     */
    function isPinnedToBottom(term) {
        if (!term) return true;
        try {
            var buf = term.buffer && term.buffer.active;
            if (!buf) return true;
            return buf.viewportY >= buf.baseY;
        } catch (err) {
            console.warn('TerminalScroll: buffer read failed', err);
            return true;
        }
    }

    /**
     * Record that the user just scrolled by hand. Suppresses auto-scroll
     * for GESTURE_LATCH_MS so momentum cannot be cut short.
     *
     * @returns {void}
     */
    function noteUserScroll() {
        lastGestureAt = Date.now();
    }

    /**
     * Is a user scroll gesture in progress (finger down, or inside the
     * momentum latch)?
     *
     * @returns {boolean}
     */
    function isGestureActive() {
        if (touchActive) return true;
        return (Date.now() - lastGestureAt) < GESTURE_LATCH_MS;
    }

    /**
     * The whole decision, sampled BEFORE a write.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {boolean} true when the write that follows should be
     *   chased with scrollToBottom().
     */
    function shouldFollowOutput(term) {
        if (isGestureActive()) return false;
        return isPinnedToBottom(term);
    }

    /**
     * Drop the gesture latch and jump to the live bottom. Used by the
     * d-pad "scroll to bottom" control and by post-reconnect repaint,
     * both of which are explicit "take me back to live" intents that
     * must beat a latch the user's last drag left behind.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {void}
     */
    function pinToBottom(term) {
        lastGestureAt = 0;
        touchActive = false;
        if (!term) return;
        try {
            term.scrollToBottom();
        } catch (err) {
            console.warn('TerminalScroll: scrollToBottom failed', err);
        }
    }

    /**
     * Can the terminal buffer still move in the direction of this drag?
     *
     * This is the whole distinction between a drag the terminal can
     * CONSUME and a drag that is trying to ESCAPE to the page. It is
     * asked of the xterm buffer, not of any DOM scroll position, because
     * the buffer is what the user is actually looking at.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {number} rows - whole rows to scroll; >0 forward, <0 back.
     * @returns {boolean} true when scrolling by `rows` would move the view.
     */
    function canConsumeScroll(term, rows) {
        if (!term || !rows) return false;
        try {
            var buf = term.buffer && term.buffer.active;
            if (!buf) return false;
            return rows < 0 ? buf.viewportY > 0 : buf.viewportY < buf.baseY;
        } catch (err) {
            console.warn('TerminalScroll: buffer read failed', err);
            return false;
        }
    }

    /**
     * THE ONE SCROLL PRIMITIVE. Both the wheel and the touch drag go
     * through it, so the two cannot drift apart.
     *
     * When the buffer has NO scrollback to move (`baseY === 0`, which is
     * what claude's `tui: fullscreen` leaves behind) the whole gesture is
     * handed to altscreen-scroll.js, which drives claude's own transcript
     * view instead. That module answers false only when real scrollback
     * exists, which is exactly when `scrollLines()` is the right tool.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {number} rows - whole rows to scroll; >0 forward, <0 back.
     * @returns {boolean} true when the view actually moved (or the
     *   alternate-screen path took ownership of the gesture).
     */
    function scrollByRows(term, rows) {
        if (!term || !rows) return false;
        var alt = window.AltScreenScroll;
        if (alt && alt.scrollByRows(rows)) return true;
        if (!canConsumeScroll(term, rows)) return false;
        try {
            term.scrollLines(rows);
        } catch (err) {
            console.warn('TerminalScroll: scrollLines failed', err);
            return false;
        }
        return true;
    }

    /**
     * Scroll the terminal by hand for a drag xterm declined to handle.
     *
     * WHY THIS IS NEEDED (measured on iPhone 16e / iOS 26.1, 2026-08-16):
     * xterm 5.3.0 gates BOTH of its touch handlers on
     * `!coreMouseService.areMouseEventsActive`, so the moment an
     * application turns on mouse reporting - which Claude Code does,
     * VT200 with SGR encoding - xterm stops scrolling the viewport by
     * touch entirely and stops cancelling the touchmove as well. The
     * wheel path is gated on a narrower condition (only the wheel bit of
     * the active protocol, which VT200 does not set), so the desktop
     * wheel keeps working. That asymmetry IS the reported bug: scrollback
     * exists, desktop scrolls, the phone cannot.
     *
     * Native scrolling is not an available fallback either: the touch
     * lands on `canvas.xterm-link-layer`, which is a SIBLING of
     * `.xterm-viewport`, so the scrollable element is not on the touch
     * target's ancestor chain at all.
     *
     * @param {number} dy - pixels the finger moved this event, up-positive.
     * @returns {boolean} true when the view actually moved.
     */
    function consumeDragScroll(dy) {
        var term = getTerm();
        if (!term || !dy) return false;
        pendingRows += window.TerminalScrollStep.rowsForPixels(dy, term);
        // EPSILON, not decoration: the accumulator is a running sum of
        // px/rowHeight*gain, so a drag whose steps divide exactly still
        // lands a hair under the whole row it should have reached (six
        // 6px steps at an 18px pitch summed to 7.999999999999999, and
        // truncation ate a row per gesture). Tolerate that much drift.
        var eps = 1e-9;
        var rows = pendingRows > 0
            ? Math.floor(pendingRows + eps)
            : Math.ceil(pendingRows - eps);
        if (!rows) return false;
        pendingRows -= rows;
        if (!scrollByRows(term, rows)) {
            // At the boundary. Drop the remainder so it cannot accumulate
            // into a jump when the user drags back the other way.
            pendingRows = 0;
            return false;
        }
        return true;
    }

    /**
     * Wheel handling, sharing consumeDragScroll's destination.
     *
     * Capture phase on the #terminal container, which is an ANCESTOR of
     * xterm's own element, so stopPropagation here means xterm's bubble
     * handler never runs. That matters on the alternate screen, where
     * xterm translates a wheel into cursor keys and claude reads those as
     * "cycle previous prompts" - the wheel would edit the prompt instead
     * of scrolling.
     *
     * @param {WheelEvent} ev - the capture-phase wheel event.
     * @returns {void}
     */
    function handleWheel(ev) {
        if (!ev || ev.deltaY === 0) return;
        noteUserScroll();
        var term = getTerm();
        if (!term) return;
        scrollByRows(term, window.TerminalScrollStep.rowsForWheel(ev, term));
        if (ev.cancelable) ev.preventDefault();
        if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
    }

    /**
     * Is a long-press text selection currently driving the finger?
     *
     * @returns {boolean} false whenever touch-select.js is absent or idle.
     */
    function isSelecting() {
        try {
            var ts = window.TouchSelect;
            return !!(ts && typeof ts.isSelecting === 'function' && ts.isSelecting());
        } catch (err) {
            console.warn('TerminalScroll: select-state read failed', err);
            return false;
        }
    }

    /**
     * OWN the one-finger drag and scroll the xterm BUFFER with it.
     *
     * WHY THIS IS THE CAPTURE-PHASE OWNER (measured 2026-08-17 against a
     * live Claude Code 2.1.199 session, 324 buffer rows, baseY 287,
     * `.xterm-viewport` scrollHeight 5184 vs clientHeight 592):
     *
     *   - Claude Code 2.1.199 does NOT enable mouse reporting. The pane
     *     byte stream sets only ?25, ?2004, ?1004 and ?2031, and the live
     *     client reports `activeProtocol` NONE. The previous fix assumed
     *     the opposite, so its whole "xterm declined the event" branch was
     *     unreachable in the case the user was reporting.
     *   - With reporting off, xterm's own touchmove handler DOES run. It
     *     scrolls `.xterm-viewport.scrollTop` and then calls
     *     preventDefault. The old bubble-phase guard read that cancel as
     *     "xterm scrolled the buffer" and stood down.
     *   - It had not. `.xterm-viewport` is inert in this app: setting
     *     scrollTop to 1000 left `buffer.active.viewportY` at 236, fired
     *     no scroll event, and left xterm's own `_lastScrollTop` at 4592.
     *     xterm's viewport-to-buffer sync never runs here, so anything
     *     routed through the DOM scroller moves pixels and no rows.
     *
     * Net: the drag was cancelled by xterm, skipped by our guard, and
     * moved nothing. That is the reported "does not scroll in the tui".
     *
     * The wheel path never had this problem because it already bypasses
     * the DOM and calls `term.scrollLines()` directly. This makes touch do
     * exactly the same thing, so the two agree by construction and neither
     * depends on mouse reporting, on the DOM scroller, or on xterm's
     * internal gating.
     *
     * @param {TouchEvent} ev - the capture-phase touchmove.
     * @returns {void}
     */
    function handleDragScroll(ev) {
        noteUserScroll();
        // Multi-touch is pinch-zoom. Leave it entirely to the browser.
        if (!ev || !ev.touches || ev.touches.length !== 1) return;
        var y = ev.touches[0].pageY;
        var dy = lastTouchY - y;
        // Track the finger even when we are not going to act on it, so
        // the first move after a selection ends is a delta and not the
        // whole distance travelled since the finger went down.
        lastTouchY = y;
        // The long-press selection drag owns the finger; moving the
        // viewport under it would tear the selection.
        if (isSelecting()) return;
        consumeDragScroll(dy);
        if (!ev.cancelable) return;
        // Cancel even at a scrollback boundary: that is the
        // pull-to-refresh block. stopPropagation keeps xterm from also
        // moving `.xterm-viewport`, which would only desync the scroller
        // from the buffer we just moved.
        ev.preventDefault();
        if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
    }

    /**
     * Backstop that stops a boundary drag from escaping to the page.
     *
     * handleDragScroll normally cancels and stops the touchmove before it
     * ever gets here. This bubble-phase listener catches the drags that do
     * not go through it - anything that reached #terminal uncancelled by
     * some other path - because on a page whose body is `overflow: hidden`
     * an uncancelled boundary drag is what iOS Safari reads as
     * pull-to-refresh, and reloads. The CSS `overscroll-behavior: contain`
     * on `.terminal-container` is the declarative half of the same fix;
     * this is the half that does not depend on an engine honouring that
     * property on a non-scrollable scroll container.
     *
     * Deliberately narrow: `defaultPrevented` short-circuits so a drag
     * somebody else already handled is never second-guessed, and
     * multi-touch is left alone so pinch-zoom still reaches the browser.
     *
     * @param {TouchEvent} ev - the bubbling touchmove.
     * @returns {void}
     */
    function blockOverscrollEscape(ev) {
        if (ev.defaultPrevented) return;
        if (!ev.cancelable) return;
        if (ev.touches && ev.touches.length !== 1) return;
        ev.preventDefault();
    }

    /**
     * Wire the gesture listeners. Idempotent; listeners ride on the
     * #terminal container (never recreated) so a term.reset() during a
     * session swap does not wipe them.
     *
     * @param {HTMLElement} container - the #terminal element.
     * @param {function(): (object|null)} [termGetter] - returns the live
     *   xterm Terminal. Required for touch scrolling under mouse
     *   reporting; omitting it degrades to block-only behaviour.
     * @returns {void}
     */
    function init(container, termGetter) {
        if (typeof termGetter === 'function') getTerm = termGetter;
        if (wired || !container) return;
        wired = true;

        // Passive: this one only records where the finger went down.
        container.addEventListener('touchstart', function (ev) {
            touchActive = true;
            pendingRows = 0;
            if (ev && ev.touches && ev.touches.length) lastTouchY = ev.touches[0].pageY;
            noteUserScroll();
        }, { capture: true, passive: true });

        // Capture phase AND cancelling: this is the owner of the drag.
        // It has to run before xterm, because xterm cancels every
        // in-range touchmove and its scroll goes to a DOM element that
        // does not drive the buffer. See handleDragScroll().
        container.addEventListener('touchmove', handleDragScroll, {
            capture: true, passive: false
        });

        var endGesture = function () {
            touchActive = false;
            noteUserScroll();
        };
        container.addEventListener('touchend', endGesture, { capture: true, passive: true });
        container.addEventListener('touchcancel', endGesture, { capture: true, passive: true });

        container.addEventListener('wheel', handleWheel, {
            capture: true, passive: false
        });

        // Bubble-phase backstop - see blockOverscrollEscape().
        container.addEventListener('touchmove', blockOverscrollEscape, { passive: false });
    }

    /** Test seam: reset module state between assertions. */
    function _reset() {
        lastGestureAt = 0;
        touchActive = false;
        wired = false;
        lastTouchY = 0;
        pendingRows = 0;
        getTerm = function () { return null; };
    }

    window.TerminalScroll = {
        init: init,
        blockOverscrollEscape: blockOverscrollEscape,
        canConsumeScroll: canConsumeScroll,
        consumeDragScroll: consumeDragScroll,
        scrollByRows: scrollByRows,
        handleWheel: handleWheel,
        isPinnedToBottom: isPinnedToBottom,
        shouldFollowOutput: shouldFollowOutput,
        noteUserScroll: noteUserScroll,
        isGestureActive: isGestureActive,
        pinToBottom: pinToBottom,
        _reset: _reset
    };
})();
