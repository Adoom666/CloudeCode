/**
 * Transient-geometry guard for the terminal resize pipeline.
 * ----------------------------------------------------------------------
 * Owns one question: this layout change came from the ResizeObserver -
 * has it SETTLED, and is it actually different from what tmux already
 * has?
 *
 * WHY THIS EXISTS. A pty_resize is not a cheap message. tmux answers it
 * with SIGWINCH and Claude Code answers THAT with `ESC[2J` plus a full
 * redraw (measured against claude 2.1.263, 2026-09-08). On the alternate
 * screen, where every Claude Code session lives, there is no scrollback,
 * so `ESC[2J` erases the entire visible conversation. Any element that
 * briefly changes the terminal's height therefore does not merely cause
 * a reflow - it destroys what the user was reading.
 *
 * That already happened once: `#localServersContainer` was an in-flow
 * sibling of `.terminal-container`, toggled on every local-servers fetch
 * and websocket event, and each appearance took about four rows from
 * `#terminal`. It has been made an overlay, which is the real fix. This
 * module is the GUARD, so the next element to do the same thing costs a
 * wasted debounce instead of the user's screen.
 *
 * WHAT IT DOES NOT DO. It never suppresses an ANNOUNCED resize. A window
 * resize, an orientation change, a sidebar pin, the dims handshake and
 * the ws.onopen fit are all things a human or the server did on purpose,
 * and they ship immediately on the normal debounce. Only the observer -
 * the safety net for changes nobody announced, and the only source that
 * fires for a transient element - is held longer and checked.
 *
 * HOW THE "RETURNS TO THE PREVIOUS VALUE" RULE IS ACTUALLY ENFORCED. Not
 * by remembering a sequence of samples: by measuring LATE. An observer
 * change waits SETTLE_MS before anything is measured at all, so a flap
 * that resolves inside that window is never sampled in its intermediate
 * state, and the single measurement that does happen is the settled one.
 * If that settled grid equals the grid tmux already has, there is nothing
 * to say and we say nothing. A sample buffer would be a more complicated
 * way to reach the same answer and would still be wrong for a flap that
 * straddles the window edge.
 *
 * Loaded as a plain script (no build step). Exposes
 * `window.TerminalResizeSettle`.
 */
(function () {
    'use strict';

    /**
     * How long an observer-sourced change waits before it is measured.
     *
     * Long enough that a show/hide pair from one page-layout event lands
     * entirely inside it, short enough that a real drag-resize still
     * feels immediate - a user dragging a window edge produces a
     * continuous stream, so the debounce restarts and only the final
     * geometry is ever shipped, which is the behaviour we want anyway.
     * @type {number}
     */
    var SETTLE_MS = 500;

    /**
     * The tags that mean "someone announced this change on purpose".
     * Everything not in this set is treated as observer-grade.
     * @type {string[]}
     */
    var ANNOUNCED = [
        'window.resize',
        'orientationchange',
        'visualViewport.resize',
        'handshake',
        'ws.onopen',
        'sidebar-pin'
    ];

    /**
     * Is this resize source an explicit, human- or server-driven change?
     *
     * @param {string} source - the reason tag from requestFit.
     * @returns {boolean} true for an announced source.
     * @example
     *   isAnnounced('orientationchange') // true
     *   isAnnounced('ResizeObserver')    // false
     */
    function isAnnounced(source) {
        return ANNOUNCED.indexOf(String(source)) !== -1;
    }

    /**
     * How long to wait before measuring, for a given source.
     *
     * @param {string} source - the reason tag from requestFit.
     * @param {number} baseMs - the pipeline's normal debounce.
     * @returns {number} milliseconds to wait.
     * @example
     *   settleMsFor('ResizeObserver', 100) // 500
     *   settleMsFor('sidebar-pin', 100)    // 100
     */
    function settleMsFor(source, baseMs) {
        var base = typeof baseMs === 'number' && baseMs > 0 ? baseMs : 0;
        if (isAnnounced(source)) return base;
        return Math.max(base, SETTLE_MS);
    }

    /**
     * Should this settled geometry be shipped to the pane?
     *
     * @param {object} [input]
     * @param {string} [input.source] - the reason tag.
     * @param {number} [input.cols] - measured columns.
     * @param {number} [input.rows] - measured rows.
     * @param {number} [input.lastCols] - columns tmux was last told.
     * @param {number} [input.lastRows] - rows tmux was last told.
     * @returns {string} 'ship' | 'skip_transient'. `skip_transient` means
     *   an unannounced change settled back to the geometry the pane
     *   already has, so shipping it would cost an ESC[2J for nothing.
     * @example
     *   decideResize({source: 'ResizeObserver', cols: 215, rows: 45,
     *                 lastCols: 215, lastRows: 45}) // 'skip_transient'
     */
    function decideResize(input) {
        var o = input || {};
        // An announced change always ships. Even at identical dims it is
        // a real event with real intent behind it, and sendResize has its
        // own dedup gate for the no-op case - this module must not become
        // a second, differently-behaved copy of that decision.
        if (isAnnounced(o.source)) return 'ship';

        var same = typeof o.cols === 'number' && typeof o.rows === 'number'
            && o.cols === o.lastCols && o.rows === o.lastRows;
        return same ? 'skip_transient' : 'ship';
    }

    /**
     * Best-effort name of the element whose appearance changed the box,
     * for the log line. Never throws and never blocks a resize.
     *
     * Reports the LAST child that is out of the normal document flow's
     * expected height - in practice the panel or banner that just
     * appeared. It is a diagnostic, so an empty string is an acceptable
     * answer and much better than a wrong confident one.
     *
     * @param {Element|null} container - the observed element's parent.
     * @returns {string} a `tag#id.class` label, or '' when unknown.
     * @example
     *   describeCulprit(document.querySelector('.terminal-container'))
     *   // 'div#localServersContainer.local-servers'
     */
    function describeCulprit(container) {
        try {
            if (!container || !container.children) return '';
            var shown = [];
            for (var i = 0; i < container.children.length; i++) {
                var el = container.children[i];
                if (!el || el.id === 'terminal') continue;
                var visible = !(el.style && el.style.display === 'none');
                if (!visible) continue;
                var label = String(el.tagName || '').toLowerCase();
                if (el.id) label += '#' + el.id;
                if (el.className && typeof el.className === 'string') {
                    label += '.' + el.className.trim().split(/\s+/).join('.');
                }
                shown.push(label);
            }
            return shown.join(' ');
        } catch (err) {
            // Diagnostic only - a failure here must never affect whether
            // the terminal gets resized.
            console.warn('TerminalResizeSettle: culprit read failed', err);
            return '';
        }
    }

    window.TerminalResizeSettle = {
        SETTLE_MS: SETTLE_MS,
        ANNOUNCED: ANNOUNCED,
        isAnnounced: isAnnounced,
        settleMsFor: settleMsFor,
        decideResize: decideResize,
        describeCulprit: describeCulprit
    };
})();
