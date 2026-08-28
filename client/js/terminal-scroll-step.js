/**
 * How far is this gesture worth?
 * ----------------------------------------------------------------------
 * THE ONE PIXELS-TO-ROWS CONVERSION. terminal-scroll.js owns WHERE a
 * gesture goes; this owns HOW FAR it goes, and both the wheel and the
 * touch drag read it, so a notch and a finger cover the same distance
 * for the same travel and the two cannot drift apart.
 *
 * MEASURED PROBLEM (2026-08-17): at one row per row-height a swipe was
 * worth about five lines, and reaching the top of a long transcript took
 * roughly 440 rows - about twenty five swipes, each of which had to make
 * a round trip to the pty.
 *
 * One-to-one is the right feel for DIRECT MANIPULATION, where the
 * content is glued to the finger. This is not that. Under claude's
 * `tui: fullscreen` the gesture is translated into keystrokes for
 * claude's own transcript view, so nothing tracks the finger at any
 * gain, and the only question left is how much distance a comfortable
 * swipe should buy.
 *
 * Extracted from terminal-scroll.js, which is at the project's 500-line
 * ceiling and must not grow.
 */
(function () {
    'use strict';

    /** Fallback row height (px) when the viewport cannot be measured. */
    var FALLBACK_CELL_HEIGHT = 17;

    /**
     * Rows scrolled per row-height of gesture travel. Four makes one
     * comfortable swipe worth roughly a screen and a half.
     */
    var GESTURE_GAIN = 4;

    /**
     * Height of one terminal row in CSS pixels.
     *
     * Measured from the DOM rather than read out of xterm's private
     * render service, so it cannot break on a vendored-bundle bump.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {number} row height in px, never zero.
     */
    function cellHeight(term) {
        try {
            var vp = document.querySelector('.xterm-viewport');
            if (vp && vp.clientHeight > 0 && term && term.rows > 0) {
                return vp.clientHeight / term.rows;
            }
        } catch (err) {
            console.warn('TerminalScrollStep: cell height read failed', err);
        }
        return FALLBACK_CELL_HEIGHT;
    }

    /**
     * Convert gesture travel in pixels into rows.
     *
     * Fractional on purpose: the caller accumulates the remainder across
     * events so a slow drag still moves rather than rounding to nothing.
     *
     * @param {number} px - travel in CSS pixels, forward-positive.
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {number} rows, signed and fractional.
     */
    function rowsForPixels(px, term) {
        if (!px) return 0;
        return (px / cellHeight(term)) * GESTURE_GAIN;
    }

    /**
     * Convert one wheel event into whole rows.
     *
     * `deltaMode` is honoured because only mode 0 is pixels. Reading a
     * LINE delta as pixels is what makes one browser's notch worth a
     * fraction of a row while another's is worth three - the same
     * divergence between input devices this module exists to remove.
     * Always at least one row in the event's direction, so a tiny
     * trackpad delta is never silently dropped.
     *
     * @param {WheelEvent} ev - the wheel event.
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {number} whole rows, signed; 0 only for a zero delta.
     */
    function rowsForWheel(ev, term) {
        if (!ev || !ev.deltaY) return 0;
        var rows;
        if (ev.deltaMode === 1) {
            rows = ev.deltaY * GESTURE_GAIN;          // lines
        } else if (ev.deltaMode === 2) {
            rows = ev.deltaY * ((term && term.rows) || 1);  // pages
        } else {
            rows = rowsForPixels(ev.deltaY, term);    // pixels
        }
        var dir = ev.deltaY > 0 ? 1 : -1;
        return Math.trunc(rows) || dir;
    }

    window.TerminalScrollStep = {
        cellHeight: cellHeight,
        rowsForPixels: rowsForPixels,
        rowsForWheel: rowsForWheel,
        GESTURE_GAIN: GESTURE_GAIN,
        FALLBACK_CELL_HEIGHT: FALLBACK_CELL_HEIGHT
    };
})();
