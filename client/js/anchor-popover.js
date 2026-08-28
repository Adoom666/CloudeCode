/**
 * anchor-popover - place a fixed-position popover against a button.
 * ----------------------------------------------------------------------
 * One placement rule, shared by every anchored surface that floats over
 * the terminal (the session tools menu, the per-session theme picker).
 * It used to exist three times, copied, and the copies drifted: the
 * version in clipboard.js had already been fixed for the iOS visual
 * viewport while the others had not.
 *
 * The rule: put the popover ABOVE the anchor with their right edges
 * flush, drop it BELOW when there is no room above, and clamp it into
 * the actually-visible viewport either way.
 *
 * Coordinates come from the anchor's own viewport rect and are written
 * as left/top, so the measurement and the placement always live in the
 * same coordinate space. Clamp bounds come from window.visualViewport
 * when it exists - on iOS Safari the layout and visual viewports diverge
 * whenever the URL bar collapses, the keyboard opens or the page is
 * pinch-zoomed, and computing right/bottom from innerWidth/innerHeight
 * in that state parks the popover off-screen.
 */

console.log('[AnchorPopover Module] Loading...');

(function () {
    'use strict';

    /** Gap between the popover and the anchor, and from any screen edge. */
    var MARGIN = 8;

    /**
     * Position an element against an anchor. The element must already be
     * in the document and `position: fixed` so it can be measured.
     *
     * @param {HTMLElement} el - the popover to place.
     * @param {HTMLElement} anchor - the button it belongs to.
     * @returns {{left: number, top: number}} the applied offsets, in px.
     */
    function place(el, anchor) {
        var rect = anchor.getBoundingClientRect();
        var vp = window.visualViewport || null;
        var vw = vp ? vp.width : window.innerWidth;
        var vh = vp ? vp.height : window.innerHeight;
        var offL = vp ? vp.offsetLeft : 0;
        var offT = vp ? vp.offsetTop : 0;

        var w = el.offsetWidth;
        var h = el.offsetHeight;

        var left = rect.right - w;
        var top = rect.top - h - MARGIN;
        if (top < offT + MARGIN) top = rect.bottom + MARGIN;

        left = Math.min(Math.max(left, offL + MARGIN), offL + vw - w - MARGIN);
        top = Math.min(Math.max(top, offT + MARGIN), offT + vh - h - MARGIN);

        el.style.left = left + 'px';
        el.style.top = top + 'px';
        return { left: left, top: top };
    }

    window.AnchorPopover = { place: place, MARGIN: MARGIN };
})();

console.log('[AnchorPopover Module] Exported as window.AnchorPopover');
