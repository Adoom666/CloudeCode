/**
 * Terminal layout / resize pipeline.
 * ----------------------------------------------------------------------
 * Owns the single question "the terminal's box may have changed - refit
 * it and tell tmux" and nothing else. Extracted from terminal.js, which
 * is over the project's 500-line ceiling and must not grow.
 *
 * WHY IT IS ALSO AN EXPLICIT API, not only a set of listeners.
 *
 * The implicit contract used to be "any layout change is caught by the
 * ResizeObserver on #terminal, so callers never have to say anything".
 * That is true only for changes that alter #terminal's own border box,
 * and only if the observer survived. The sidebar pin (session-sidebar-
 * pin.js) shipped on that assumption and the resize was reported not to
 * happen. Rather than debug an invisible dependency, a caller that KNOWS
 * it just changed the layout now says so:
 *
 *     window.TerminalLayout.requestFit('sidebar-pin');
 *
 * The observer stays as the safety net for changes nobody announces.
 * Both routes land in the same debounced fit, so an announced change
 * that ALSO trips the observer still produces exactly one resize frame,
 * and TerminalController#sendResize dedups on (cols, rows) on top of
 * that.
 *
 * NOTHING HERE SCROLLS THE TERMINAL. Refitting must never yank a user who
 * has scrolled up back to the bottom - that is the bug terminal-scroll.js
 * exists to prevent. `fit()` reflows; it is not a scroll command, and no
 * scrollToBottom() call belongs in this file.
 *
 * Loaded BEFORE terminal.js, which calls install() from initTerminal().
 */

console.log('[TerminalLayout Module] Loading...');

(function () {
    'use strict';

    /**
     * Debounce window in ms. One physical layout change fires several of
     * the sources below (a CSS transition alone produces a ResizeObserver
     * callback per frame); collapsing them to one fit keeps a single
     * pty_resize on the wire.
     * @type {number}
     */
    const DEBOUNCE_MS = 100;

    /** @type {?object} The TerminalController install() was given. */
    let controller = null;
    /** @type {?number} Pending debounce timer id. */
    let timer = null;
    /** @type {?ResizeObserver} Observer on the xterm container. */
    let observer = null;
    /** @type {boolean} Guard so install() is idempotent. */
    let listenersWired = false;
    /** @type {string[]} Every reason seen since the last flush, for logs. */
    let pendingReasons = [];

    /**
     * How many times to retry a fit the measurement guard refused before
     * giving up. Bounded so a genuinely broken page does not spin.
     * @type {number}
     */
    const MAX_FIT_RETRIES = 8;

    /** Base retry delay in ms; multiplied by the attempt number. @type {number} */
    const RETRY_MS = 120;

    /** @type {number} Consecutive guard refusals since the last good fit. */
    let retries = 0;

    /**
     * Per-element bookkeeping for requestFitAfterTransition: which "wait
     * for this element's transition to settle" request is still current,
     * plus the fallback timer and transitionend listener it owns so a
     * newer call can cancel both before they fire.
     * @type {WeakMap<Element, {gen: number, timer: ?number, listener: ?function}>}
     */
    const transitionWaiters = new WeakMap();

    /**
     * Refit the terminal to its container and ship the new geometry to
     * the backend. Runs at most once per debounce window.
     *
     * @param {string} reason - tag forwarded to sendResize for the
     *   [TERM-RESIZE] log line.
     * @returns {void}
     */
    function flush(reason) {
        timer = null;
        pendingReasons = [];
        if (!controller || !controller.fitAddon || !controller.term) return;

        // The fit goes through TerminalMetrics.guardedFit, which refuses to
        // measure when xterm.css has not applied or when the proposed grid
        // is implausible. A bad measurement is invisible: it produces a
        // working-looking terminal whose cols/rows match nothing on screen,
        // and sendResize would then reflow the real tmux pane to that wrong
        // grid. Skipping and retrying keeps the last known-good grid, which
        // is always the better failure.
        const metrics = window.TerminalMetrics;
        if (metrics && typeof metrics.guardedFit === 'function') {
            const result = metrics.guardedFit(controller);
            if (!result.fitted) {
                console.warn(
                    `TerminalLayout: fit skipped, reason=${result.reason} source=${reason}`);
                scheduleRetry(reason);
                return;
            }
            // The fit divided the box by the cell width the renderer
            // reported BEFORE the resize. Confirm the post-resize grid
            // still paints inside the box, and drop columns if it does
            // not. Runs before sendResize so tmux is never told about a
            // grid wider than the screen.
            const width = metrics.enforceWidthFit(controller);
            if (width.changed) {
                console.warn(
                    `TerminalLayout: grid shrunk to ${width.cols} cols, `
                    + `painted ${width.painted}px in ${width.available}px `
                    + `source=${reason}`);
            }
        } else {
            // Module missing (load order regression). Fall back to the raw
            // fit rather than leaving the terminal unsized.
            try {
                controller.fitAddon.fit();
            } catch (err) {
                console.warn('TerminalLayout: fit failed', err);
                return;
            }
        }

        retries = 0;
        // sendResize is the ONLY path to tmux. A client-side fit that is
        // not followed by this leaves xterm and the pty disagreeing about
        // the grid, which is what "tmux does not resize" looks like.
        controller.sendResize(reason);
    }

    /**
     * Re-attempt a fit that the guard refused, with a bounded backoff.
     *
     * The conditions the guard rejects on are transient by nature: a
     * stylesheet still arriving, a container mid-transition, a font not yet
     * resolved. Retrying a handful of times covers all of them without
     * spinning forever if something is genuinely broken.
     *
     * @param {string} reason - original fit reason, carried into the retry.
     * @returns {void}
     */
    function scheduleRetry(reason) {
        if (retries >= MAX_FIT_RETRIES) {
            console.warn('TerminalLayout: giving up on fit retries');
            return;
        }
        retries += 1;
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => flush(reason), RETRY_MS * retries);
    }

    /**
     * Queue a refit. Safe to call from anywhere, any number of times.
     *
     * @param {string} [reason='unknown'] - what changed, for the log line.
     * @param {{immediate?: boolean}} [opts] - immediate skips the
     *   debounce. Use only when the caller knows layout has already
     *   settled; a mid-transition immediate fit measures the wrong box.
     * @returns {void}
     */
    function requestFit(reason = 'unknown', opts = {}) {
        pendingReasons.push(reason);
        if (opts.immediate) {
            if (timer) { clearTimeout(timer); timer = null; }
            flush(reason);
            return;
        }
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => flush(reason), DEBOUNCE_MS);
    }

    /**
     * Queue a refit that waits for a docking/undocking CSS transition on
     * `el` to actually finish before measuring, instead of guessing a fixed
     * delay against a box that might still be mid-animation.
     *
     * WHY transitionend FIRST. Firing on click measures the PRE-transition
     * box - the bug this exists to fix. `transitionend` measures the real
     * settled box, and it is naturally immune to a second toggle
     * interrupting the first: per the CSS Transitions spec, an interrupted
     * transition never fires `transitionend` for the property it never
     * reached, only the transition that actually completes does. So a
     * rapid pin/unpin/pin sequence fires this callback at most once, for
     * whichever toggle's transition is the one that actually finishes.
     *
     * WHY A FALLBACK TIMER TOO. `transitionend` can be legitimately absent:
     * `prefers-reduced-motion` (or any stylesheet) can drop the transition
     * to 0s, which fires no event at all, and a caller can mis-name the
     * property or element. `fallbackMs` is a safety-net ceiling, not the
     * primary mechanism - whichever of the two fires first wins, and the
     * other is cancelled.
     *
     * COALESCING. Calling this again for the same `el` before it has fired
     * bumps a per-element generation counter and tears down the previous
     * listener/timer, so only the LATEST call's settle can ever call
     * requestFit. This is what makes a rapid double-toggle produce exactly
     * one resize instead of one per click.
     *
     * A future second panel (a right-hand dock) reuses this unchanged: call
     * it with that panel's own element/property/reason, no new module.
     *
     * @param {?Element} el - element carrying the transitioned property. A
     *   missing element (wrong id, not yet in the DOM) degrades to an
     *   immediate requestFit rather than waiting forever for an event that
     *   can never fire on nothing.
     * @param {string} propertyName - CSS property to wait on, e.g.
     *   'padding-left'. Must match `event.propertyName` exactly.
     * @param {number} fallbackMs - safety-net delay in ms if transitionend
     *   never arrives.
     * @param {string} reason - forwarded to requestFit for the log line.
     * @returns {void}
     */
    function requestFitAfterTransition(el, propertyName, fallbackMs, reason) {
        if (!el) { requestFit(reason); return; }

        let waiter = transitionWaiters.get(el);
        if (!waiter) {
            waiter = { gen: 0, timer: null, listener: null };
            transitionWaiters.set(el, waiter);
        }
        if (waiter.timer) clearTimeout(waiter.timer);
        if (waiter.listener) el.removeEventListener('transitionend', waiter.listener);

        waiter.gen += 1;
        const myGen = waiter.gen;

        const settle = () => {
            if (waiter.gen !== myGen) return; // superseded by a newer call
            if (waiter.timer) { clearTimeout(waiter.timer); waiter.timer = null; }
            if (waiter.listener) {
                el.removeEventListener('transitionend', waiter.listener);
                waiter.listener = null;
            }
            requestFit(reason);
        };

        waiter.listener = (evt) => {
            if (evt.target === el && evt.propertyName === propertyName) settle();
        };
        el.addEventListener('transitionend', waiter.listener);
        waiter.timer = setTimeout(settle, fallbackMs);
    }

    /**
     * Wire the pipeline to a TerminalController and start listening.
     * Idempotent: a second call re-points the controller but does not
     * stack a second set of listeners.
     *
     * Sources, and what each one alone would miss:
     *   window.resize          - desktop window / browser chrome
     *   orientationchange      - phone rotation
     *   visualViewport.resize  - on-screen keyboard, iOS URL bar collapse
     *   ResizeObserver         - any layout change of #terminal that no
     *                            caller announced (font load, CSS
     *                            transition, docked panel)
     *
     * @param {object} ctl - a TerminalController with .fitAddon, .term
     *   and .sendResize(reason).
     * @returns {void}
     */
    function install(ctl) {
        controller = ctl;
        if (listenersWired) return;
        listenersWired = true;

        window.addEventListener('resize', () => requestFit('window.resize'));
        window.addEventListener('orientationchange', () => requestFit('orientationchange'));
        if (window.visualViewport) {
            window.visualViewport.addEventListener(
                'resize', () => requestFit('visualViewport.resize'));
        }

        const container = document.getElementById('terminal');
        if (container && typeof ResizeObserver !== 'undefined') {
            try {
                observer = new ResizeObserver(() => requestFit('ResizeObserver'));
                observer.observe(container);
            } catch (err) {
                console.warn('TerminalLayout: ResizeObserver setup failed', err);
            }
        }
    }

    window.TerminalLayout = {
        install,
        requestFit,
        requestFitAfterTransition,
        DEBOUNCE_MS,
        MAX_FIT_RETRIES,
        RETRY_MS,
        /** Test seam: the live observer, or null when unsupported. */
        get observer() { return observer; },
    };
    console.log('[TerminalLayout Module] Exported as window.TerminalLayout');
})();
