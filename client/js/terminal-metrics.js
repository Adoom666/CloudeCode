/**
 * Terminal measurement guard.
 * ----------------------------------------------------------------------
 * Owns one question: "is it safe to measure the terminal right now, and is
 * the number we just measured plausible?" Extracted so terminal.js (2333
 * lines) and terminal-layout.js do not grow.
 *
 * WHY THIS EXISTS.
 *
 * A wrong cell measurement does not look like an error. It looks like a
 * working terminal rendering garbage, and it propagates: FitAddon derives
 * cols/rows from the measured cell, sendResize ships those to tmux, and
 * tmux reflows the real pane to a grid that matches nothing the user can
 * see. The user reported exactly this on a real phone while the desktop
 * and the desktop mobile emulator both looked correct.
 *
 * The measurement can go wrong for reasons that never raise:
 *
 *   1. xterm.css has not applied. Until it does, the char-measure element
 *      is not laid out the way xterm assumes and the cell comes out wrong.
 *      This used to be a third-party CDN request, so any content blocker
 *      (Brave Shields, ublock, a corporate proxy) could drop it and the
 *      only symptom was a wonky render. The assets are vendored same-origin
 *      now; this check is the defense that does not depend on that.
 *   2. The font is not resolved yet, so the cell is measured against a
 *      fallback with different metrics.
 *   3. The container is display:none or zero-sized, so the proposal is 0.
 *
 * The guard's contract: a measurement that cannot be trusted is SKIPPED
 * and retried, never shipped. Degrading to the previous known-good grid is
 * always better than corrupting the pane with a bad one.
 *
 * Loaded BEFORE terminal-layout.js, which calls guardedFit().
 */

console.log('[TerminalMetrics Module] Loading...');

(function () {
    'use strict';

    /**
     * Smallest grid we will ever accept as a real measurement. A proposal
     * below this is a measurement failure, not a very small phone: even a
     * 320px-wide device in portrait produces well over 20 columns at 14px.
     * @type {number}
     */
    const MIN_COLS = 8;
    /** @type {number} */
    const MIN_ROWS = 4;

    /**
     * Largest grid we will accept. A cell measured as a fraction of a pixel
     * (the classic unstyled-xterm failure) produces proposals in the tens of
     * thousands. Real terminals do not exceed this.
     * @type {number}
     */
    const MAX_COLS = 1000;
    /** @type {number} */
    const MAX_ROWS = 400;

    /**
     * How long to keep waiting for fonts before measuring anyway. A terminal
     * that never appears is worse than one measured against a fallback.
     * @type {number}
     */
    const FONT_TIMEOUT_MS = 3000;

    /**
     * Wait until font resolution has settled, bounded by a timeout.
     *
     * IMPORTANT AND EASY TO GET WRONG: `document.fonts.ready` only tracks
     * PENDING font loads. The terminal stack is local system fonts with no
     * @font-face rule, so `ready` resolves essentially immediately and
     * proves nothing on its own. It is still worth awaiting (it is correct
     * for any webfont a theme adds later), but it is not the guarantee it
     * looks like, which is why cell plausibility is checked separately in
     * proposalIsSane().
     *
     * @param {number} [timeoutMs=FONT_TIMEOUT_MS] - upper bound on the wait.
     * @returns {Promise<boolean>} true if fonts settled, false on timeout or
     *   when the Font Loading API is unavailable.
     */
    async function waitForFonts(timeoutMs = FONT_TIMEOUT_MS) {
        if (!document.fonts || !document.fonts.ready) return false;
        let timer = null;
        const timeout = new Promise((resolve) => {
            timer = setTimeout(() => resolve(false), timeoutMs);
        });
        try {
            const settled = await Promise.race([
                document.fonts.ready.then(() => true),
                timeout,
            ]);
            return settled === true;
        } catch (err) {
            console.warn('TerminalMetrics: font wait failed', err);
            return false;
        } finally {
            if (timer) clearTimeout(timer);
        }
    }

    /**
     * Has xterm's own stylesheet applied to this element tree?
     *
     * xterm.css sets `.xterm { position: relative }`. Nothing in this app's
     * CSS sets that, so a computed `position` of `static` on a live .xterm
     * element means the stylesheet is missing or has not applied yet, and
     * any cell measurement taken now is untrustworthy.
     *
     * @param {?Element} [root=document] - subtree to look in.
     * @returns {boolean} true when applied, or when there is no .xterm
     *   element yet to judge by (nothing to contradict).
     */
    function xtermStylesheetApplied(root) {
        const scope = root || document;
        let el = null;
        try {
            el = scope.querySelector('.xterm');
        } catch (err) {
            return true;
        }
        if (!el || typeof window.getComputedStyle !== 'function') return true;
        try {
            return window.getComputedStyle(el).position !== 'static';
        } catch (err) {
            return true;
        }
    }

    /**
     * Is a FitAddon proposal a plausible terminal grid?
     *
     * @param {?{cols: number, rows: number}} proposal - proposeDimensions()
     *   output, which is undefined when xterm could not measure at all.
     * @returns {{ok: boolean, reason: string}} ok plus a short tag naming
     *   the rejection, for the log line.
     */
    function proposalIsSane(proposal) {
        if (!proposal) return { ok: false, reason: 'no-proposal' };
        const { cols, rows } = proposal;
        if (!Number.isFinite(cols) || !Number.isFinite(rows)) {
            return { ok: false, reason: 'non-finite' };
        }
        if (cols < MIN_COLS || rows < MIN_ROWS) {
            return { ok: false, reason: 'too-small' };
        }
        if (cols > MAX_COLS || rows > MAX_ROWS) {
            return { ok: false, reason: 'too-large' };
        }
        return { ok: true, reason: 'ok' };
    }

    /**
     * Fit the terminal only if the measurement can be trusted.
     *
     * @param {object} controller - a TerminalController with .fitAddon and
     *   .term.
     * @returns {{fitted: boolean, reason: string}} fitted false means the
     *   caller must NOT ship a resize; the previous grid still stands.
     */
    function guardedFit(controller) {
        if (!controller || !controller.fitAddon || !controller.term) {
            return { fitted: false, reason: 'no-controller' };
        }
        if (!xtermStylesheetApplied()) {
            return { fitted: false, reason: 'xterm-css-not-applied' };
        }
        let proposal = null;
        try {
            proposal = typeof controller.fitAddon.proposeDimensions === 'function'
                ? controller.fitAddon.proposeDimensions()
                : null;
        } catch (err) {
            return { fitted: false, reason: 'propose-threw' };
        }
        // A build without proposeDimensions cannot be pre-validated. Fit
        // anyway rather than deadlocking the terminal, and say so.
        if (proposal === null) {
            try {
                controller.fitAddon.fit();
                return { fitted: true, reason: 'unvalidated' };
            } catch (err) {
                return { fitted: false, reason: 'fit-threw' };
            }
        }
        const verdict = proposalIsSane(proposal);
        if (!verdict.ok) return { fitted: false, reason: verdict.reason };
        try {
            controller.fitAddon.fit();
        } catch (err) {
            return { fitted: false, reason: 'fit-threw' };
        }
        return { fitted: true, reason: 'ok' };
    }

    /**
     * Sub-pixel slack, in CSS px, before a grid counts as overflowing.
     *
     * Two different roundings sit between the cell measurement and the
     * painted canvas (xterm rounds the canvas to whole CSS px, then
     * divides by cols), so an exactly-fitting grid routinely lands a
     * fraction of a pixel over. A real overflow is a whole cell, eight
     * px or more. Anything under one px is rounding noise and spending a
     * column on it would cost the user a column of text for nothing.
     * @type {number}
     */
    const WIDTH_EPSILON_PX = 1;

    /**
     * Read the box FitAddon fits into: the xterm parent's content width,
     * less the .xterm element's horizontal padding, less the scrollbar
     * xterm reserves. Deliberately mirrors FitAddon.proposeDimensions so
     * the two cannot drift.
     *
     * @param {object} controller - a TerminalController with .term.
     * @returns {?number} width in CSS px, or null when it cannot be read.
     */
    function availableWidth(controller) {
        try {
            const el = controller.term.element;
            if (!el || !el.parentElement) return null;
            const parent = window.getComputedStyle(el.parentElement);
            const own = window.getComputedStyle(el);
            const outer = Math.max(0, parseInt(parent.getPropertyValue('width'), 10));
            const pad = parseInt(own.getPropertyValue('padding-left'), 10)
                + parseInt(own.getPropertyValue('padding-right'), 10);
            const core = controller.term._core;
            const scrollbar = controller.term.options.scrollback === 0
                ? 0
                : (core.viewport ? core.viewport.scrollBarWidth : 0);
            const width = outer - pad - scrollbar;
            return Number.isFinite(width) ? width : null;
        } catch (err) {
            return null;
        }
    }

    /**
     * Width the RENDERER will actually paint for the current column
     * count, in CSS px.
     *
     * The load-bearing detail: this reads `dimensions.css.cell.width`,
     * which is the pitch the active renderer lays glyphs out on. It is
     * NOT the font's natural advance width (`_charSizeService.width`) -
     * on an iPhone 16e those are 8.3333 and 8.65625 respectively,
     * because the WebGL renderer floors the cell to whole device pixels.
     * Comparing a column count against the natural advance invents an
     * overflow that does not exist; comparing it against this number is
     * the only comparison that means anything.
     *
     * @param {object} controller - a TerminalController with .term.
     * @returns {?number} width in CSS px, or null when it cannot be read.
     */
    function renderedWidth(controller) {
        try {
            const dims = controller.term._core._renderService.dimensions;
            const cell = dims.css.cell.width;
            if (!Number.isFinite(cell) || cell <= 0) return null;
            return cell * controller.term.cols;
        } catch (err) {
            return null;
        }
    }

    /**
     * Shrink the grid until the painted width fits the available box.
     *
     * FitAddon derives cols from the cell width the renderer reported
     * BEFORE the resize, and for the DOM renderer that number is itself
     * a function of the old column count, so the post-resize pitch can
     * come out slightly wider than the one the division used. This is
     * the check that the two agree after the fact, and it always errs
     * toward FEWER columns: a column that does not fit is text the user
     * cannot read, while a spare pixel of margin is invisible.
     *
     * Called after a successful fit and before the resize is shipped to
     * tmux, so the pty never learns about a grid that does not fit.
     *
     * @param {object} controller - a TerminalController with .term.
     * @returns {{changed: boolean, cols: ?number, painted: ?number,
     *   available: ?number, reason: string}} changed true when columns
     *   were dropped; reason names the outcome for the log line.
     */
    function enforceWidthFit(controller) {
        const out = { changed: false, cols: null, painted: null, available: null,
            reason: 'ok' };
        if (!controller || !controller.term) {
            out.reason = 'no-controller';
            return out;
        }
        const available = availableWidth(controller);
        let painted = renderedWidth(controller);
        if (available === null || painted === null) {
            out.reason = 'unmeasurable';
            return out;
        }
        out.available = available;
        out.cols = controller.term.cols;
        out.painted = painted;
        if (painted <= available + WIDTH_EPSILON_PX) return out;

        const cell = painted / controller.term.cols;
        const fitted = Math.max(MIN_COLS, Math.floor(available / cell));
        if (fitted >= controller.term.cols) {
            out.reason = 'overflow-unfixable';
            return out;
        }
        try {
            controller.term.resize(fitted, controller.term.rows);
        } catch (err) {
            out.reason = 'resize-threw';
            return out;
        }
        painted = renderedWidth(controller);
        out.changed = true;
        out.cols = controller.term.cols;
        out.painted = painted === null ? out.painted : painted;
        out.reason = 'shrunk';
        return out;
    }

    /**
     * The grid the live terminal is currently rendering, fitted first.
     *
     * THE ONE PLACE that answers "what dimensions should the server give
     * this pane". Every session-birth path needs it - create, rejoin and
     * adopt - and adopt was the one that did not ask, which is why an
     * adopted session sat at tmux's 80x24 birth size forever while an
     * app-created one on the same socket was 163x46. A shared reader
     * makes that class of omission visible instead of per-caller.
     *
     * @returns {{cols: number, rows: number}|{}} an empty object when the
     *   terminal is not ready. Empty is a real third answer: the caller
     *   must send nothing rather than invent a default, because the
     *   server has its own and the WS handshake reshapes shortly after.
     */
    function currentGrid() {
        try {
            const ctl = window.TerminalController;
            const t = ctl && ctl.term;
            if (!t || typeof t.cols !== 'number' || typeof t.rows !== 'number') {
                return {};
            }
            if (t.cols <= 0 || t.rows <= 0) return {};
            // Fit first so the numbers describe what the renderer will
            // actually use post-connect, not a stale pre-layout grid.
            try {
                if (ctl.fitAddon) ctl.fitAddon.fit();
            } catch (err) {
                console.warn('TerminalMetrics: pre-read fit failed', err);
            }
            return { cols: t.cols, rows: t.rows };
        } catch (err) {
            console.warn('TerminalMetrics: currentGrid failed', err);
            return {};
        }
    }

    window.TerminalMetrics = {
        currentGrid,
        waitForFonts,
        xtermStylesheetApplied,
        proposalIsSane,
        guardedFit,
        availableWidth,
        renderedWidth,
        enforceWidthFit,
        WIDTH_EPSILON_PX,
        MIN_COLS,
        MIN_ROWS,
        MAX_COLS,
        MAX_ROWS,
        FONT_TIMEOUT_MS,
    };
    console.log('[TerminalMetrics Module] Exported as window.TerminalMetrics');
})();
