/**
 * THE THREE WAYS TO OPEN A ROW'S MENU, wired once.
 * ----------------------------------------------------------------------
 * client/js/session-row-menu.js owns the menu - what is in it and how it
 * behaves. This file owns only the gestures that ask for it, and every
 * one of them ends in the same two calls (`openForKebab` /
 * `openAtPoint`), so the three entry points cannot come to mean three
 * different menus.
 *
 *   TAP / CLICK the kebab   the primary affordance, and the only one
 *                           that is visible. A phone has no right click
 *                           and no hover, so the control has to be on
 *                           the row.
 *   RIGHT CLICK the row     what the owner asked for, and free on a
 *                           desktop. `contextmenu` is prevented so the
 *                           browser's own menu does not appear over ours.
 *   LONG PRESS the row      the touch twin of right click, because a
 *                           phone cannot produce one. 500 ms, cancelled
 *                           by movement.
 *
 * WHY LONG PRESS IS NOT SIMPLY A TIMER. Two things go wrong if it is:
 *
 *   1. IT FIRES ON A SCROLL. A finger resting on a row for half a second
 *      while flicking the list is not a long press. So the timer is
 *      cancelled by a pointermove beyond MOVE_SLOP_PX, by pointerup,
 *      by pointercancel, and by the list scrolling at all.
 *   2. IT ALSO OPENS THE CONVERSATION. The row's own click handler
 *      (session-sidebar-clicks.js `activateRow`) runs on the click that
 *      the browser synthesises when the finger lifts, so the user would
 *      get a menu AND be navigated out from under it. A flag set when
 *      the press fires is consumed by a CAPTURE-phase click listener on
 *      the list, which stops the event before the list's own bubble-phase
 *      router ever sees it. Capture is the load-bearing part: a second
 *      bubble listener cannot express "before".
 *
 * The flag is cleared on the next pointerdown as well as on the click it
 * was armed for, so a press whose click never arrives (the finger lifted
 * outside the list) cannot eat an unrelated tap later.
 *
 * ANDROID FIRES BOTH. A long press on Chrome for Android raises a real
 * `contextmenu` at about the same delay as our timer, so both paths can
 * run for one gesture. `openFor()` is checked first: if this row's menu
 * is already up, the contextmenu is swallowed and nothing is rebuilt,
 * which is what stops the panel jumping to a second position mid-press.
 *
 * A COARSE POINTER ANCHORS TO THE KEBAB, NEVER TO THE FINGER. Placing
 * the panel at the touch point would put it under the hand that opened
 * it. A mouse right click still opens at the pointer, which is what a
 * desktop expects.
 *
 * DRAG STILL WINS ON THE GRIP. `[data-grip-session]` is reorder's
 * gesture (session-sidebar-reorder.js) and is skipped here entirely, and
 * so is any press that starts on a control - long-pressing a button is
 * not a request for the row's menu.
 *
 * ONE INIT, ONE SET OF LISTENERS. `init()` is idempotent through a
 * module flag AND a marker attribute on the list, because re-running an
 * init to see whether it ran is how this project once convinced itself a
 * working control was dead: the second call attached a second listener,
 * one click fired both handlers and the state flipped twice.
 *
 * Load AFTER session-row-menu.js and session-sidebar.js.
 */

console.log('[SessionRowMenuGestures Module] Loading...');

(function () {
    'use strict';

    /** How long a finger must rest before the menu opens, in ms. */
    var LONG_PRESS_MS = 500;

    /** Movement past this many px is a scroll or a drag, not a press. */
    var MOVE_SLOP_PX = 10;

    /** Marker so a second init over the same list is a no-op. */
    var WIRED_ATTR = 'data-row-menu-wired';

    /** Module-level guard for the same reason. */
    var wired = false;

    /** In-flight long press: {x, y, timer, kebab} or null. */
    var press = null;

    /** True while a synthesized click from a long press is still pending. */
    var swallowClick = false;

    /**
     * Description: is this a touch-sized pointer? Decides whether a
     *   context menu opens at the finger or against the kebab.
     * Inputs: none. Output: boolean.
     */
    function coarsePointer() {
        return !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    }

    /**
     * Description: cancel any in-flight long press. Safe to call when
     *   there is none.
     * Inputs: none. Output: void.
     */
    function cancelPress() {
        if (!press) return;
        clearTimeout(press.timer);
        press = null;
    }

    /**
     * Description: should a press starting on this node be ignored? True
     *   for the drag grip and for anything that is itself a control - a
     *   long press on the kebab, the rename input or the group chip is
     *   the user operating that control, not asking for the row's menu.
     * Inputs: target (EventTarget).
     * Output: boolean.
     */
    function pressExempt(target) {
        if (!target || typeof target.closest !== 'function') return true;
        if (target.closest('[data-grip-session]')) return true;
        return !!target.closest('button, input, textarea, select, [role="button"]');
    }

    /**
     * Description: the kebab for the row an event landed in, or null when
     *   the event was not inside a row that has one.
     * Inputs: target (EventTarget).
     * Output: Element|null.
     */
    function kebabFor(target) {
        if (!target || typeof target.closest !== 'function') return null;
        var row = target.closest('.session-sidebar-row');
        if (!row) return null;
        return window.SessionRowMenu.kebabIn(row);
    }

    /**
     * Description: open the menu for a kebab the way this pointer wants
     *   it - against the control on touch, at the cursor on a mouse.
     * Inputs: kebab (Element), x (number), y (number).
     * Output: void.
     */
    function openAt(kebab, x, y) {
        if (coarsePointer()) window.SessionRowMenu.openForKebab(kebab);
        else window.SessionRowMenu.openAtPoint(kebab, x, y);
    }

    /**
     * Description: a click landed in the list, in the CAPTURE phase,
     *   before session-sidebar.js's own router. Two jobs: eat the click a
     *   long press left behind, and claim a tap on the kebab.
     * Inputs: e (MouseEvent). Output: void.
     */
    function onCaptureClick(e) {
        if (swallowClick) {
            swallowClick = false;
            e.preventDefault();
            e.stopPropagation();
            return;
        }
        var kebab = e.target.closest && e.target.closest(
            '[' + window.SessionRowMenu.KEBAB_ATTR + ']');
        if (!kebab) return;
        // The row's click handler must not also run: a tap on the kebab
        // is not a request to switch conversation.
        e.preventDefault();
        e.stopPropagation();
        var name = kebab.getAttribute(window.SessionRowMenu.KEBAB_ATTR);
        if (window.SessionRowMenu.isOpen() && window.SessionRowMenu.openFor() === name) {
            window.SessionRowMenu.close();
            return;
        }
        window.SessionRowMenu.openForKebab(kebab);
    }

    /**
     * Description: right click anywhere on a row opens the same menu.
     * Inputs: e (MouseEvent). Output: void.
     */
    function onContextMenu(e) {
        var kebab = kebabFor(e.target);
        if (!kebab) return;
        e.preventDefault();
        cancelPress();
        var name = kebab.getAttribute(window.SessionRowMenu.KEBAB_ATTR);
        // Android raises this DURING a long press we may have already
        // answered. Rebuilding would make the panel jump.
        if (window.SessionRowMenu.isOpen() && window.SessionRowMenu.openFor() === name) {
            return;
        }
        openAt(kebab, e.clientX, e.clientY);
    }

    /**
     * Description: arm a long press. Mouse pointers are skipped - they
     *   have a right click, and arming here would fight text selection.
     * Inputs: e (PointerEvent). Output: void.
     */
    function onPointerDown(e) {
        swallowClick = false;
        cancelPress();
        if (e.pointerType === 'mouse') return;
        if (pressExempt(e.target)) return;
        var kebab = kebabFor(e.target);
        if (!kebab) return;
        var startX = e.clientX;
        var startY = e.clientY;
        press = {
            x: startX,
            y: startY,
            kebab: kebab,
            timer: setTimeout(function () {
                press = null;
                // Armed BEFORE the menu opens: the click that ends this
                // press is already on its way and must not activate the
                // row behind the panel.
                swallowClick = true;
                window.SessionRowMenu.openForKebab(kebab);
            }, LONG_PRESS_MS),
        };
    }

    /**
     * Description: a finger that has travelled is scrolling or dragging,
     *   not pressing.
     * Inputs: e (PointerEvent). Output: void.
     */
    function onPointerMove(e) {
        if (!press) return;
        var dx = e.clientX - press.x;
        var dy = e.clientY - press.y;
        if ((dx * dx) + (dy * dy) > (MOVE_SLOP_PX * MOVE_SLOP_PX)) cancelPress();
    }

    /**
     * Description: wire the gestures onto the sidebar list. Idempotent.
     * Inputs: none.
     * Output: boolean - true when this call did the wiring.
     */
    function init() {
        if (wired) return false;
        var list = document.getElementById('session-sidebar-list');
        if (!list) return false;
        if (list.getAttribute(WIRED_ATTR) === '1') { wired = true; return false; }
        list.setAttribute(WIRED_ATTR, '1');

        list.addEventListener('click', onCaptureClick, true);
        list.addEventListener('contextmenu', onContextMenu);
        list.addEventListener('pointerdown', onPointerDown);
        list.addEventListener('pointermove', onPointerMove);
        list.addEventListener('pointerup', cancelPress);
        list.addEventListener('pointercancel', cancelPress);
        // A list that starts moving under a resting finger was a scroll.
        list.addEventListener('scroll', cancelPress, { passive: true });

        wired = true;
        console.log('[SessionRowMenuGestures] wired');
        return true;
    }

    window.SessionRowMenuGestures = {
        LONG_PRESS_MS: LONG_PRESS_MS,
        MOVE_SLOP_PX: MOVE_SLOP_PX,
        WIRED_ATTR: WIRED_ATTR,
        init: init,
        onCaptureClick: onCaptureClick,
        onContextMenu: onContextMenu,
        onPointerDown: onPointerDown,
        onPointerMove: onPointerMove,
        cancelPress: cancelPress,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

console.log('[SessionRowMenuGestures Module] Exported as window.SessionRowMenuGestures');
