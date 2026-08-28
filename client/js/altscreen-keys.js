/**
 * Key runs for scrolling claude's detailed-transcript view.
 * ----------------------------------------------------------------------
 * WHY THIS IS ITS OWN FILE: altscreen-scroll.js owns the DECISION of
 * whether it is safe to synthesise anything at all, which is the whole
 * safety argument of that module and should not have arithmetic mixed
 * into it. This owns only the arithmetic: given a signed row count, what
 * exact bytes move the view that far in the fewest keystrokes.
 *
 * WHAT WAS MEASURED (claude 2.1.199, 2026-08-17)
 *
 *   - In the transcript view UP scrolls exactly one row and PageUp jumps
 *     exactly 16. Both are linear and neither coalesces: 50 UPs in one
 *     write moved 50 rows.
 *   - Reaching the top of a long transcript took about 440 UP presses.
 *     The same distance is 27 PageUps plus 8 UPs - 35 keystrokes, an
 *     order of magnitude fewer bytes on the wire and an order of
 *     magnitude less work for claude's own input loop.
 *
 * The decomposition is exact, not approximate: pages carry as much of
 * the distance as they can and arrows carry the remainder, so the view
 * lands on the row that was asked for. An approximation here would make
 * a scroll gesture land somewhere the user did not aim.
 */
(function () {
    'use strict';

    /** CSI cursor up/down. claude does not set DECCKM, so CSI, not SS3. */
    var CSI_UP = '\x1b[A';
    var CSI_DOWN = '\x1b[B';

    /** CSI PageUp / PageDown (VT220 form, which is what claude reads). */
    var CSI_PAGE_UP = '\x1b[5~';
    var CSI_PAGE_DOWN = '\x1b[6~';

    /** Rows one PageUp/PageDown moves. Measured, not assumed. */
    var PAGE_ROWS = 16;

    /**
     * Hard ceiling on the rows ONE dispatch may ask for.
     *
     * Generous because the cost is now keystrokes, not rows: 240 rows is
     * 15 page keys. The cap exists so a runaway delta cannot fire an
     * unbounded write into a live session, not to limit scroll distance.
     */
    var MAX_ROWS = 240;

    /**
     * Build the key bytes that scroll the transcript by `rows`.
     *
     * @param {number} rows - signed row count; >0 scrolls down, <0 up.
     *   Non-finite or zero yields an empty string.
     * @param {number} [maxRows=MAX_ROWS] - clamp on the magnitude.
     * @returns {string} the exact bytes to write to the pty.
     */
    function keysForRows(rows, maxRows) {
        var cap = (typeof maxRows === 'number' && maxRows > 0) ? maxRows : MAX_ROWS;
        if (typeof rows !== 'number' || !isFinite(rows) || !rows) return '';
        var n = Math.min(Math.abs(Math.trunc(rows)), cap);
        if (!n) return '';
        var down = rows > 0;
        var pages = Math.floor(n / PAGE_ROWS);
        var singles = n - (pages * PAGE_ROWS);
        var pageKey = down ? CSI_PAGE_DOWN : CSI_PAGE_UP;
        var arrowKey = down ? CSI_DOWN : CSI_UP;
        var out = '';
        var i;
        for (i = 0; i < pages; i++) out += pageKey;
        for (i = 0; i < singles; i++) out += arrowKey;
        return out;
    }

    /**
     * How many keystrokes `keysForRows` would send for a row count.
     *
     * Exists so the cost can be asserted directly in a test rather than
     * inferred from a byte length, and so a future change that quietly
     * stops using page keys shows up as a number.
     *
     * @param {number} rows - signed row count.
     * @param {number} [maxRows=MAX_ROWS] - clamp on the magnitude.
     * @returns {number} keystrokes sent.
     */
    function keystrokesForRows(rows, maxRows) {
        var cap = (typeof maxRows === 'number' && maxRows > 0) ? maxRows : MAX_ROWS;
        if (typeof rows !== 'number' || !isFinite(rows) || !rows) return 0;
        var n = Math.min(Math.abs(Math.trunc(rows)), cap);
        return Math.floor(n / PAGE_ROWS) + (n % PAGE_ROWS);
    }

    window.AltScreenKeys = {
        keysForRows: keysForRows,
        keystrokesForRows: keystrokesForRows,
        PAGE_ROWS: PAGE_ROWS,
        MAX_ROWS: MAX_ROWS,
        _keys: {
            UP: CSI_UP,
            DOWN: CSI_DOWN,
            PAGE_UP: CSI_PAGE_UP,
            PAGE_DOWN: CSI_PAGE_DOWN
        }
    };
})();
