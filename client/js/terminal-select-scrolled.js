/**
 * Force local selection while scrolled up and mouse tracking is active.
 * ----------------------------------------------------------------------
 * WHY THIS EXISTS (the "highlight lands off screen at the bottom" bug):
 *
 * xterm's own bindMouse() ONLY consults the running application when the
 * app has turned on mouse tracking (`?1000h` etc, `coreMouseService.
 * areMouseEventsActive`). When it has, a plain mousedown/drag is NOT
 * treated as a local text selection at all - `SelectionService.disable()`
 * runs the moment tracking turns on, and the click/drag/release are
 * instead ENCODED as SGR mouse reports and forwarded to the pty. See
 * `bindMouse()` and `SelectionService.handleMouseDown()` in the vendored
 * bundle: `if(!this._enabled){ if(!this.shouldForceSelection(e)) return; }`.
 *
 * That encoding is fundamentally SCREEN-relative, not buffer-relative: the
 * row/col in an SGR report describe a cell in the currently RENDERED grid
 * (1..term.rows), with no channel to say "this grid is showing scrollback,
 * not the live screen". Measured against the real vendored bundle: the
 * same drag over the same on-screen pixels encodes IDENTICAL row/col
 * whether viewportY equals baseY (live) or sits 30 rows above it (scrolled
 * up) - see tests/test_terminal_select_scrolled.node.mjs. The running
 * program has no idea the user is looking at history; it applies "row 3"
 * to whatever IT thinks occupies row 3 of its own live screen, which is
 * wherever its cursor-addressable output actually is - near the true
 * bottom of the buffer while the user is scrolled away from it. Any
 * highlight the program draws in response to that click lands there:
 * below the visible viewport, invisible until the user scrolls all the
 * way back down. That is the reported "highlites on bottom off screen".
 *
 * BEFORE commit 04139ac this was masked, not fixed: every mouse report -
 * including the very first mousedown of the drag - ALSO called
 * TerminalScroll.pinToBottom(), so the instant the user pressed the mouse
 * button the view snapped back to the live position first. The user was
 * never actually looking at scrollback while dragging; there was nothing
 * for the screen-relative encoding to get wrong. Fixing the mouse-jump bug
 * removed that side effect and exposed this pre-existing one.
 *
 * THE FIX, without reintroducing the jump: xterm ships exactly the escape
 * hatch this needs already - `SelectionService.shouldForceSelection(e)`
 * bypasses the report path for a shift-click (or option-click on macOS,
 * gated by `macOptionClickForcesSelection`) so a user can always select
 * text locally even while an app owns the mouse. This module applies that
 * bypass AUTOMATICALLY, but only when it is the only sensible outcome:
 * the terminal is scrolled away from the live bottom AND the app has
 * mouse tracking on. In that state a forwarded report cannot possibly mean
 * what the app thinks it means (see above), so a local selection is
 * strictly more correct than sending it. At the live bottom, nothing here
 * changes: `TerminalScroll.isPinnedToBottom()` is true, this module steps
 * aside, and the app's own mouse-driven UI (menus, scrollbars, pickers)
 * keeps working exactly as before.
 *
 * MECHANISM: a capture-phase `mousedown` on the #terminal container (the
 * PARENT of `term.element`, so this runs before xterm's own bubble-phase
 * listeners on `term.element` ever see the event - same technique
 * touch-select.js uses for its long-press synthesis) cancels the real
 * event and replaces it with an equivalent synthetic one carrying
 * `shiftKey` and `altKey` both set, with `macOptionClickForcesSelection`
 * flipped on for the instant of dispatch. Only the DOWN event needs
 * synthesis: once `SelectionService.handleMouseDown()` runs with the
 * force-selection gate satisfied, it wires its OWN document-level
 * mousemove/mouseup listeners (unconditional - they do not re-check the
 * modifier), so the REAL subsequent drag events drive the rest without
 * further help. See tests/test_terminal_select_scrolled.node.mjs for the
 * headless proof against the real vendored bundle.
 */
(function () {
    'use strict';

    /**
     * Is the running application currently reading raw mouse events?
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {boolean}
     */
    function areMouseEventsActive(term) {
        try {
            var core = term && term._core;
            var svc = core && core.coreMouseService;
            return !!(svc && svc.areMouseEventsActive);
        } catch (err) {
            console.warn('TerminalSelectScrolled: mouse-service read failed', err);
            return false;
        }
    }

    /**
     * Is the terminal showing something other than the true live output?
     *
     * TWO INDEPENDENT WAYS THIS CAN BE TRUE, because they are two
     * independent ways a terminal can stop tracking its own scrollback:
     *
     * 1. REAL SCROLLBACK, MOVED. Delegates to terminal-scroll.js's
     *    `isPinnedToBottom()` (`viewportY >= baseY`) so the two modules
     *    cannot disagree about what "scrolled up" means. This is the
     *    ONLY case xterm's own buffer state can answer, and only on the
     *    NORMAL screen - see next.
     *
     * 2. THE ALTERNATE SCREEN, SHOWING SOMETHING OTHER THAN THE LIVE
     *    PROMPT. `buffer.active.baseY` is 0 on the alternate screen BY
     *    CONSTRUCTION - it has no scrollback dimension at all, so
     *    `isPinnedToBottom()` is tautologically true there NO MATTER
     *    WHAT is on screen. Measured live 2026-08-17 against a real
     *    session: forced tmux into copy-mode 10 rows back on an
     *    alternate-screen pane (`vim`, mouse reporting on) - the browser
     *    visibly rendered the scrolled-back rows, `getSelection()` after
     *    a real drag over them came back empty, and `term.buffer.active`
     *    read `{viewportY: 0, baseY: 0}` throughout, `isPinnedToBottom()`
     *    true the entire time. Case 1 cannot see this by construction, so
     *    it needs its own check. altscreen-scroll.js already answers the
     *    identifiable version of this question for claude specifically -
     *    `detectState(term) === 'transcript'` is proven (by the
     *    transcript view's own unique footer text) to mean the alternate
     *    screen is showing scrolled-back messages, not the live prompt.
     *    `'live'` and `'unknown'` are deliberately NOT treated as
     *    scrolled: a fresh non-claude alt-screen program (vim, htop) that
     *    the user has not scrolled at all is still legitimately owed its
     *    own mouse clicks (positioning a cursor, opening its own visual
     *    selection), and forcing local selection there would be a new
     *    regression, not a fix - see altscreen-scroll.js's own contract
     *    that 'unknown' means "do nothing", not "assume the worst".
     *
     * Fails closed on both paths (an unreadable state counts as "at the
     * bottom" / "not transcript") so an error here can only skip this
     * module's behaviour, never suppress a legitimate app mouse report.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @returns {boolean}
     */
    function isScrolledUp(term) {
        var ts = window.TerminalScroll;
        var realScrollback = !!(ts && typeof ts.isPinnedToBottom === 'function'
            && !ts.isPinnedToBottom(term));
        if (realScrollback) return true;

        var as = window.AltScreenScroll;
        if (as && typeof as.detectState === 'function') {
            try {
                return as.detectState(term) === 'transcript';
            } catch (err) {
                console.warn('TerminalSelectScrolled: altscreen state read failed', err);
                return false;
            }
        }
        return false;
    }

    /**
     * True only while this module is dispatching its own synthetic
     * mousedown. See handleMouseDown() for why this cannot be omitted.
     */
    var dispatching = false;

    /**
     * True between a mousedown this module forced into a local selection
     * and the mouseup that ends it. See handleMouseUp() for why the up of
     * a forced gesture must not reach xterm's report listener.
     */
    var forcedGesture = false;

    /**
     * Replace a real mousedown with an equivalent synthetic one that
     * forces xterm's local-selection bypass, on both platforms.
     *
     * `macOptionClickForcesSelection` is flipped on only for the duration
     * of this synchronous dispatch (xterm reads it synchronously inside
     * the event listener chain triggered by dispatchEvent) so this module
     * never permanently changes the option the user may have set.
     *
     * `dispatching` is raised for exactly the same window, and for the
     * same reason: everything xterm does with this event happens INSIDE
     * dispatchEvent(), synchronously, so a flag cleared in `finally` is
     * cleared no earlier than the last listener that could see it.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {Element} screenEl - `.xterm-screen`, the node xterm's own
     *   mouse listeners are bound to (bubbling from here reaches them).
     * @param {MouseEvent} real - the original mousedown, for its
     *   coordinates and button/detail fields.
     * @returns {void}
     */
    function dispatchForcedMouseDown(term, screenEl, real) {
        var opts = term.options;
        var prevForce = opts.macOptionClickForcesSelection;
        opts.macOptionClickForcesSelection = true;
        dispatching = true;
        try {
            screenEl.dispatchEvent(new MouseEvent('mousedown', {
                bubbles: true,
                cancelable: true,
                view: window,
                button: real.button,
                buttons: real.buttons || 1,
                detail: real.detail || 1,
                clientX: real.clientX,
                clientY: real.clientY,
                shiftKey: true,
                altKey: true,
            }));
        } finally {
            dispatching = false;
            opts.macOptionClickForcesSelection = prevForce;
        }
    }

    /**
     * Capture-phase mousedown handler. Intercepts and replaces only the
     * gestures that would otherwise be misinterpreted - see file header.
     *
     * THE RE-ENTRANCY GUARD IS LOAD-BEARING, NOT DEFENSIVE POLISH.
     *
     * This listener is registered on `#terminal`, and the replacement is
     * dispatched on `.xterm-screen`, a DESCENDANT of it, with
     * `bubbles: true`. The capture phase runs from the window DOWN to the
     * target, so `#terminal` sits on the synthetic event's capture path
     * too. Without this guard the handler therefore re-enters on its own
     * replacement, calls `stopPropagation()` on it, and dispatches
     * another one - so the forced mousedown NEVER reaches xterm's
     * SelectionService, and the gesture recurses instead.
     *
     * That is not a theory. Measured 2026-08-19 against a live
     * `tui: fullscreen` claude 2.1.199 on the alternate screen, scrolled
     * into the transcript view (`detectState() === 'transcript'`,
     * `isScrolledUp() === true`, `areMouseEventsActive === true`), with
     * listeners spying on every stage of the path: ONE real mousedown
     * produced FORTY-FOUR synthetic `mousedown` events at `#terminal`,
     * every one of them `isTrusted: false`, `shiftKey: true`,
     * `altKey: true`, `defaultPrevented: true` - and ZERO events at
     * `.xterm-screen`, ZERO calls to `SelectionService.shouldForceSelection`
     * and ZERO calls to `SelectionService._onMouseDown`. `getSelection()`
     * came back empty and `hasSelection()` was false.
     *
     * This is why the v2 fix measured correct and behaved broken: its
     * gate was firing exactly as designed. The gate was never the
     * problem. The replacement event was being eaten by the very handler
     * that created it, one layer below where anyone was looking.
     *
     * The guard is a flag rather than an `isTrusted` test on purpose.
     * `isTrusted === false` would also reject touch-select.js's
     * long-press synthesis, which is a legitimate caller that needs this
     * same forcing on a phone. The flag is true for exactly the window in
     * which WE are dispatching, and for nothing else.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {MouseEvent} ev - the mousedown, real or synthetic.
     * @returns {void}
     */
    function handleMouseDown(term, ev) {
        // Our own replacement, on its way down to xterm. Touching it here
        // is the whole bug: let it through untouched.
        if (dispatching) return;
        if (!term || !ev || ev.button !== 0) return;
        if (!areMouseEventsActive(term)) return; // normal path already works
        if (!isScrolledUp(term)) return;         // live bottom: leave the app in control
        var screenEl = term.element && term.element.querySelector('.xterm-screen');
        if (!screenEl) return;
        ev.preventDefault();
        ev.stopPropagation();
        forcedGesture = true;
        dispatchForcedMouseDown(term, screenEl, ev);
    }

    /**
     * Capture-phase mouseup handler for a gesture this module forced.
     *
     * WHY THE UP MATTERS AS MUCH AS THE DOWN.
     *
     * xterm gives the DOWN an escape hatch and the UP none. Its own
     * mousedown listener reads
     * `areMouseEventsActive && !shouldForceSelection(e)` before reporting,
     * so a forced mousedown is correctly NOT sent, and the document-level
     * drag/up report listeners are only attached inside that same
     * branch - so they are never attached for a forced gesture either.
     * But the protocol-change handler ALSO binds a STANDING `mouseup`
     * listener on `term.element` whenever the active protocol carries UP
     * events, and that one consults nothing at all. It reports every
     * mouseup unconditionally.
     *
     * `CoreMouseService.triggerMouseEvent` sends through
     * `CoreService.triggerDataEvent(encoded, true)` - user input - and
     * `SelectionService`'s constructor registers
     * `this._coreService.onUserInput(() => this.hasSelection && this.clearSelection())`.
     * So that one unconditional report DESTROYS the selection the drag
     * just finished making.
     *
     * Measured 2026-08-19 on a live fullscreen claude in the transcript
     * view, sampling the selection model at each stage of one drag:
     *
     *   MODEL@down  start=[10,6] end=null            termSel=""
     *   MODEL@mid   start=[10,6] end=[20,6]          termSel="HLV3MARKER"
     *   MODEL@end   start=[10,6] end=[30,6]          termSel="HLV3MARKER%03g-alpha"
     *   MODEL@up    start=null   end=null            termSel=""
     *
     * The selection was built perfectly, at exactly the pressed
     * coordinates, and then wiped on release. The stack captured at the
     * wipe named the mechanism outright:
     * `CoreMouseService.triggerMouseEvent -> CoreService.triggerDataEvent
     * -> EventEmitter.fire -> SelectionService.clearSelection`.
     *
     * WHY A RE-DISPATCH ONTO `document` AND NOT JUST stopPropagation().
     *
     * The two listeners that must be separated sit at different nodes:
     * xterm's report listener is on `term.element`, and
     * SelectionService's own mouseup is on `document` (attached by
     * `_addMouseDownListeners`). Cancelling propagation at `#terminal` in
     * the capture phase stops the event before `term.element` - which is
     * what we want - but it also stops it ever bubbling back up to
     * `document`, which would starve SelectionService of the mouseup that
     * calls `_removeMouseDownListeners()`. Its document mousemove handler
     * would stay attached and the selection would keep following the
     * pointer after release.
     *
     * Re-dispatching on `document` separates them cleanly with no vendor
     * patching: an event dispatched AT `document` has `document` as its
     * whole propagation path, so SelectionService's listener runs and
     * `term.element` - a descendant, not an ancestor - is never on the
     * path and never reports.
     *
     * @param {MouseEvent} ev - the real mouseup.
     * @returns {void}
     */
    function handleMouseUp(ev) {
        if (!forcedGesture) return;
        forcedGesture = false;
        if (!ev) return;
        ev.stopPropagation();
        document.dispatchEvent(new MouseEvent('mouseup', {
            bubbles: false,
            cancelable: true,
            view: window,
            button: ev.button,
            buttons: 0,
            detail: ev.detail || 1,
            clientX: ev.clientX,
            clientY: ev.clientY,
        }));
    }

    /**
     * Capture-phase mousemove handler that protects a finished selection.
     *
     * WHY A FINISHED SELECTION IS NOT YET A SAFE SELECTION.
     *
     * claude turns on `?1003h` - ANY-motion tracking - so xterm reports
     * every pointer move over the terminal, button or no button. Each of
     * those reports is `triggerDataEvent(..., true)`, each one is user
     * input, and user input clears the selection (see handleMouseUp for
     * the full chain). So fixing the mouseup alone buys a selection that
     * survives release and then dies on the very next twitch of the
     * mouse - which is indistinguishable, to the person using it, from
     * the selection never having worked.
     *
     * Measured 2026-08-19, immediately after the mouseup fix landed and
     * before this one, on the same live fullscreen claude:
     *
     *   MODEL@up         start=[10,6] end=[30,6]  termSel="HLV3MARKER%03g-alpha"
     *   MODEL@aftermove  start=null   end=null    termSel=""
     *
     * One pointer move, and the finished selection was gone. Worth
     * stating plainly because it is the trap this whole exercise keeps
     * falling into: an automated drag that never moves the pointer
     * afterwards reports PASS on that state. A human never does that.
     *
     * THE GATE IS DELIBERATELY NARROW. Motion is suppressed only when all
     * of these hold, so the blast radius is exactly the broken case:
     *
     *   - a gesture is NOT in flight. During the drag itself xterm's own
     *     standing mousemove handler is `e.buttons || send(e)` and
     *     already declines to report while a button is held, so there is
     *     nothing to suppress - and suppressing anyway would starve
     *     SelectionService's document-level mousemove and stop the drag
     *     from extending.
     *   - the terminal actually HAS a selection. With nothing to protect
     *     this does nothing at all, so claude's hover UI in the
     *     transcript behaves exactly as before until the user selects.
     *   - the view is scrolled away from the live bottom. This is the
     *     module's founding premise: a screen-relative mouse report
     *     cannot mean what the application thinks it means while the user
     *     is looking at scrollback, so withholding it costs the
     *     application nothing it could have used correctly.
     *   - mouse tracking is on. Otherwise the normal path already works.
     *
     * There is no deadlock: the state is left by the user's next
     * mousedown, which is not suppressed and which replaces or clears the
     * selection in the ordinary way.
     *
     * @param {object} term - an xterm.js Terminal instance.
     * @param {MouseEvent} ev - the real mousemove.
     * @returns {void}
     */
    function handleMouseMove(term, ev) {
        if (forcedGesture) return;
        if (!term || !ev) return;
        if (!areMouseEventsActive(term)) return;
        var has = false;
        try {
            has = !!(typeof term.hasSelection === 'function' && term.hasSelection());
        } catch (err) {
            console.warn('TerminalSelectScrolled: selection read failed', err);
            return;
        }
        if (!has) return;
        if (!isScrolledUp(term)) return;
        ev.stopPropagation();
    }

    /**
     * Wire the capture-phase listener. Idempotent; rides on #terminal
     * (never recreated) so a term.reset() during session swap does not
     * wipe it - same guarantee terminal-scroll.js and touch-select.js
     * rely on.
     *
     * @param {HTMLElement} container - the #terminal element.
     * @param {function(): (object|null)} termGetter - returns the live
     *   xterm Terminal.
     * @returns {void}
     */
    var wired = false;
    function init(container, termGetter) {
        if (wired || !container || typeof termGetter !== 'function') return;
        wired = true;
        container.addEventListener('mousedown', function (ev) {
            handleMouseDown(termGetter(), ev);
        }, { capture: true });
        container.addEventListener('mouseup', function (ev) {
            handleMouseUp(ev);
        }, { capture: true });
        container.addEventListener('mousemove', function (ev) {
            handleMouseMove(termGetter(), ev);
        }, { capture: true });
    }

    /** Test seam: reset module state between assertions. */
    function _reset() {
        wired = false;
        dispatching = false;
        forcedGesture = false;
    }

    /**
     * Test seam: is the re-entrancy guard currently raised?
     *
     * @returns {boolean} true only inside dispatchForcedMouseDown().
     */
    function _isDispatching() {
        return dispatching;
    }

    /**
     * Test seam: is a forced gesture currently in flight?
     *
     * @returns {boolean} true between a forced mousedown and its mouseup.
     */
    function _isForcedGesture() {
        return forcedGesture;
    }

    window.TerminalSelectScrolled = {
        init: init,
        handleMouseDown: handleMouseDown,
        handleMouseUp: handleMouseUp,
        handleMouseMove: handleMouseMove,
        areMouseEventsActive: areMouseEventsActive,
        isScrolledUp: isScrolledUp,
        _reset: _reset,
        _isDispatching: _isDispatching,
        _isForcedGesture: _isForcedGesture
    };
})();
