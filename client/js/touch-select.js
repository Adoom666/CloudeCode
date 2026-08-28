/**
 * Touch Selection (long-press → drag → floating copy button)
 * ----------------------------------------------------------------------
 * xterm.js has NO touch selection: on coarse-pointer devices there is no
 * way to highlight terminal text, so there is nothing to copy. This
 * module adds that flow. Loaded right AFTER clipboard.js in index.html;
 * terminal.js calls window.TouchSelect.init(this) once from
 * initTerminal() (single attachment - all listeners ride on #terminal /
 * document, so term.reset() during session swap does not wipe them).
 *
 * Flow (coarse pointers only - the module no-ops entirely on desktop):
 *
 *  1. LONG-PRESS (~500ms, 10px movement tolerance) on the terminal enters
 *     "select mode": the shared status pill says "select mode - drag to
 *     highlight" and the gesture is hijacked so it can no longer scroll.
 *  2. DRAG moves the selection. Rather than re-implementing pixel→cell
 *     math (fragile under WebGL - there are no DOM text layers to hit),
 *     we synthesize the equivalent mousedown/mousemove/mouseup events at
 *     the touch point and let xterm's OWN SelectionService do the work.
 *     Verified against the bundled xterm@5.3.0:
 *       - the selection mousedown listener lives on term.element
 *         (bubbling from .xterm-screen reaches it);
 *       - it requires button === 0 and detail === 1 (a synthetic
 *         MouseEvent defaults detail to 0, which silently selects
 *         NOTHING - detail must be set explicitly);
 *       - drag listeners are added to the ownerDocument, so bubbling
 *         synthetic mousemove/mouseup events drive the drag;
 *       - coordinates come from clientX/clientY relative to
 *         .xterm-screen - exactly what we provide.
 *  3. LIFT with an active selection shows a small floating "copy" button
 *     anchored at the lift point (which is where xterm's selectionEnd
 *     sits), clamped inside the visible viewport. Tap →
 *     navigator.clipboard.writeText(term.getSelection()) → pill
 *     "copied" → selection cleared, button removed.
 *  4. TAP anywhere outside the copy button exits select mode and clears
 *     everything.
 *
 * Non-regressions by construction:
 *  - plain touch-drag still scrolls: the long-press timer is cancelled
 *    by >10px of movement, and nothing is ever hijacked before it fires;
 *  - desktop is untouched: init() returns early on fine pointers, and
 *    every listener is touch-only (plus a contextmenu guard that can
 *    only engage while a touch gesture is pending/active);
 *  - WebGL renderer safe: no reliance on DOM text layers;
 *  - keyboard safety: xterm's screenElement mousedown handler calls
 *    term.focus() - our synthetic mousedown fires from a timer (not a
 *    trusted gesture, so mobile browsers won't summon the keyboard), and
 *    we blur the textarea immediately after as belt-and-braces.
 */

(function () {
    'use strict';

    const LONG_PRESS_MS = 500;
    const MOVE_TOLERANCE = 10;
    const MARGIN = 8;

    /* =================================================================
     * State machine: 'idle' → 'pending' (finger down, timer running)
     * → 'selecting' (long-press confirmed, gesture hijacked) → 'idle'
     * ================================================================= */
    let state = 'idle';
    let termWrapper = null;
    let term = null;
    let screenEl = null;
    let timer = null;
    let touchId = null;
    let startX = 0;
    let startY = 0;
    let lastX = 0;
    let lastY = 0;
    let copyBtn = null;

    /**
     * Entry point called once from Terminal#initTerminal(). Hard gate on
     * coarse pointers / touch support so desktop behavior is bit-for-bit
     * unchanged.
     */
    function init(wrapper) {
        const coarse = (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) ||
            ('ontouchstart' in window);
        if (!coarse) return;
        if (!wrapper || !wrapper.term || !wrapper.term.element) return;

        termWrapper = wrapper;
        term = wrapper.term;
        screenEl = term.element.querySelector('.xterm-screen') || term.element;
        const container = term.element.parentElement; // #terminal
        if (!container) return;

        // Capture phase on the PARENT of term.element: xterm binds its own
        // touch handlers on term.element, so a capture listener one level
        // up reliably runs first and can starve them during select mode.
        container.addEventListener('touchstart', onTouchStart, { capture: true, passive: true });
        container.addEventListener('touchmove', onTouchMove, { capture: true, passive: false });
        container.addEventListener('touchend', onTouchEnd, { capture: true, passive: false });
        container.addEventListener('touchcancel', onTouchEnd, { capture: true, passive: false });

        // Suppress the iOS long-press callout / context menu while a
        // selection gesture is pending or active (desktop right-click
        // never touches this path - states are only reachable via touch).
        container.addEventListener('contextmenu', (e) => {
            if (state !== 'idle') e.preventDefault();
        }, true);

        // "Tap anywhere outside the copy button exits select mode." This
        // only listens while select mode is active, so normal touches are
        // never observed, let alone modified.
        document.addEventListener('touchstart', onDocTouchStart, { capture: true, passive: true });
    }

    /* =================================================================
     * Gesture handling
     * ================================================================= */

    function onTouchStart(e) {
        if (state !== 'idle') return; // doc-level handler manages dismissal
        const t = e.changedTouches[0];
        touchId = t.identifier;
        startX = lastX = t.clientX;
        startY = lastY = t.clientY;
        state = 'pending';
        timer = setTimeout(enterSelectMode, LONG_PRESS_MS);
    }

    function onTouchMove(e) {
        if (state === 'idle') return;
        const t = activeTouch(e);
        if (!t) return;

        if (state === 'pending') {
            // Moved too far before the timer - this is a scroll gesture.
            // Cancel the long-press and let xterm's own touch scrolling
            // have the gesture completely untouched.
            if (Math.abs(t.clientX - startX) > MOVE_TOLERANCE ||
                Math.abs(t.clientY - startY) > MOVE_TOLERANCE) {
                cancelPending();
                return;
            }
        } else if (state === 'selecting') {
            // Hijack: the drag must move the selection, never scroll.
            e.preventDefault();
            e.stopPropagation();
            lastX = t.clientX;
            lastY = t.clientY;
            dispatchMouse('mousemove', t.clientX, t.clientY);
        }
    }

    function onTouchEnd(e) {
        if (state === 'idle') return;
        const t = activeTouch(e);
        if (!t) return;

        if (state === 'pending') {
            cancelPending(); // plain tap - nothing to do
            return;
        }

        // state === 'selecting': finish the drag.
        e.preventDefault();
        e.stopPropagation();
        lastX = t.clientX;
        lastY = t.clientY;
        dispatchMouse('mouseup', t.clientX, t.clientY);
        state = 'idle';
        touchId = null;

        if (term.hasSelection()) {
            showCopyButton(lastX, lastY);
        } else {
            // Long-press released without a drag - no selection, exit
            // quietly (pull the pill down early rather than waiting out
            // its 3s auto-dismiss).
            hidePill();
        }
    }

    /** Dismiss-on-any-outside-tap. Only fires while select UI is live. */
    function onDocTouchStart(e) {
        if (state === 'idle' && !copyBtn) return;
        if (copyBtn && copyBtn.contains(e.target)) return; // button handles it
        exitSelectMode(true);
        // The event is deliberately NOT consumed: the tap proceeds as a
        // normal touch (focus / scroll / whatever it would have done).
    }

    function activeTouch(e) {
        for (let i = 0; i < e.changedTouches.length; i++) {
            if (e.changedTouches[i].identifier === touchId) return e.changedTouches[i];
        }
        return null;
    }

    function cancelPending() {
        if (timer) { clearTimeout(timer); timer = null; }
        state = 'idle';
        touchId = null;
    }

    /* =================================================================
     * Select mode
     * ================================================================= */

    function enterSelectMode() {
        timer = null;
        if (state !== 'pending') return; // cancelled meanwhile
        state = 'selecting';

        // Place the selection anchor at the long-press point via xterm's
        // own engine (detail: 1 is REQUIRED - see file header).
        dispatchMouse('mousedown', startX, startY);

        // xterm's screenElement mousedown handler calls term.focus();
        // undo it so a mobile keyboard can never pop over select mode.
        if (term.textarea && document.activeElement === term.textarea) {
            term.textarea.blur();
        }

        termWrapper._showStatusPill('select mode - drag to highlight', 'info');
    }

    /**
     * Keep a synthetic pointer inside the rendered rows.
     *
     * WHY THIS EXISTS - this is the "copy jumps to bottom" fix.
     *
     * xterm's SelectionService starts a 50ms `_dragScroll` interval on
     * mousedown and stops it on mouseup. While it runs, a pointer BELOW
     * the last rendered row makes it scroll the buffer down, and keep
     * scrolling, twenty times a second, until it reaches the live bottom.
     * On a phone the long-press that starts a selection routinely lands
     * in the bottom rows of the screen, so the act of selecting walked
     * the view to the bottom on its own.
     *
     * It is the same shape as tmux's default `copy-selection-and-cancel`,
     * where the CANCEL is what snaps the view down and the fix is to stop
     * doing the second half. Here the second half is the auto-scroll, and
     * clamping the synthetic coordinates into the row band removes it
     * without touching how selection itself works.
     *
     * COST, stated plainly: a drag can no longer extend a selection past
     * the visible screen by auto-scrolling. Under `tui: fullscreen` there
     * is nothing past the visible screen to reach (baseY is 0 by
     * construction), and elsewhere the user can scroll and select again.
     * A selection that silently relocates the view is the worse trade.
     *
     * @param {number} x - clientX of the touch.
     * @param {number} y - clientY of the touch.
     * @returns {{x: number, y: number}} coordinates inside the row band.
     *   Returned unchanged when the screen element cannot be measured -
     *   an unmeasurable box is not a reason to refuse the gesture.
     */
    function clampToRows(x, y) {
        if (!screenEl || typeof screenEl.getBoundingClientRect !== 'function') {
            return { x: x, y: y };
        }
        const r = screenEl.getBoundingClientRect();
        if (!r || !r.height || !r.width) return { x: x, y: y };
        // One row of inset: xterm compares against the row band, so the
        // edge row itself is enough to arm the auto-scroll.
        const rows = (term && term.rows) || 1;
        const pad = Math.max(1, r.height / rows);
        return {
            x: Math.min(Math.max(x, r.left + 1), r.right - 1),
            y: Math.min(Math.max(y, r.top + pad), r.bottom - pad),
        };
    }

    /**
     * Synthesize a bubbling mouse event at viewport coordinates. The
     * selection listener sits on term.element and the drag listeners on
     * the ownerDocument, so dispatching on .xterm-screen (bubbles: true)
     * reaches all of them.
     *
     * Coordinates are clamped into the rendered row band first - see
     * clampToRows() for why that is the whole "copy jumps to bottom" fix.
     *
     * @param {string} type - 'mousedown', 'mousemove' or 'mouseup'.
     * @param {number} x - clientX of the touch.
     * @param {number} y - clientY of the touch.
     * @returns {void}
     */
    function dispatchMouse(type, x, y) {
        if (!screenEl) return;
        const at = clampToRows(x, y);
        screenEl.dispatchEvent(new MouseEvent(type, {
            bubbles: true,
            cancelable: true,
            view: window,
            button: 0,
            buttons: type === 'mouseup' ? 0 : 1,
            detail: 1,
            clientX: at.x,
            clientY: at.y,
        }));
    }

    function exitSelectMode(clearSel) {
        cancelPending();
        if (clearSel && term && term.hasSelection()) term.clearSelection();
        removeCopyButton();
        hidePill();
    }

    /* =================================================================
     * Floating copy button
     * ================================================================= */

    /**
     * Show the copy button near the END of the selection - the finger
     * lift point, which is where xterm's selectionEnd sits - hard-clamped
     * inside the visible viewport so it can never render off-screen.
     */
    function showCopyButton(x, y) {
        removeCopyButton();
        hidePill(); // pull the "select mode" pill down once the drag ends
        copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'cloude-touch-copy';
        copyBtn.textContent = 'copy';
        document.body.appendChild(copyBtn);

        const vp = window.visualViewport || null;
        const vw = vp ? vp.width : window.innerWidth;
        const vh = vp ? vp.height : window.innerHeight;
        const offL = vp ? vp.offsetLeft : 0;
        const offT = vp ? vp.offsetTop : 0;
        const w = copyBtn.offsetWidth;
        const h = copyBtn.offsetHeight;

        // Prefer just above-right of the lift point (out from under the
        // user's thumb); clamp into the visible viewport with a margin.
        let left = x + MARGIN;
        let top = y - h - MARGIN;
        left = Math.min(Math.max(left, offL + MARGIN), offL + vw - w - MARGIN);
        top = Math.min(Math.max(top, offT + MARGIN), offT + vh - h - MARGIN);
        copyBtn.style.left = left + 'px';
        copyBtn.style.top = top + 'px';

        copyBtn.addEventListener('click', onCopyTap);
    }

    function onCopyTap(e) {
        e.stopPropagation();
        const text = term ? term.getSelection() : '';
        // Exit FIRST (clears selection, removes the button, hides the
        // select-mode pill) so the result pill below is never clobbered.
        exitSelectMode(true);
        if (!text) {
            termWrapper._showStatusPill('nothing selected', 'info');
            return;
        }
        // CopyCompat, not navigator.clipboard directly: this app is served
        // over plain http on a Tailscale hostname, where the async
        // clipboard API does not exist at all and this path used to be a
        // guaranteed dead end on the exact devices that need it most.
        window.CopyCompat.copyText(text).then((result) => {
            if (result.ok) {
                termWrapper._showStatusPill('copied', 'success');
            } else {
                termWrapper._showStatusPill('copy blocked - use the copy button for a selectable view', 'error');
            }
        });
    }

    function removeCopyButton() {
        if (copyBtn) {
            copyBtn.removeEventListener('click', onCopyTap);
            copyBtn.remove();
            copyBtn = null;
        }
    }

    /**
     * Take down whatever this module last reported. Routed through the
     * shared notice because the terminal's own pill element no longer
     * exists - it was painted under the header and both mechanisms
     * collapsed into FabMenu.notify.
     *
     * @returns {void}
     */
    function hidePill() {
        if (window.FabMenu && typeof window.FabMenu.dismissNotice === 'function') {
            window.FabMenu.dismissNotice();
        }
    }

    /**
     * Is a long-press selection drag currently driving the finger?
     *
     * Exposed because terminal-scroll.js now owns the touchmove in the
     * CAPTURE phase and runs BEFORE this module's own capture listener
     * (terminal.js wires TerminalScroll first). stopPropagation() cannot
     * separate them - it does not stop other listeners on the same node -
     * so the scroller has to ask.
     *
     * @returns {boolean} true while the drag is extending a selection.
     */
    function isSelecting() {
        return state === 'selecting';
    }

    window.TouchSelect = { init, isSelecting, clampToRows };
})();
