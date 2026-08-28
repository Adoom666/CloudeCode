/**
 * Dismiss Guard
 *
 * ONE root fix for a bug class that had already shipped twice: a container
 * with a "click me to go away" listener swallowing clicks that were really
 * aimed at an interactive child.
 *
 * The two live symptoms this module retires:
 *
 *   1. The slash-commands modal. `.modal-overlay` carried a bare
 *      `click -> close()` with no target check, and the filter input lives
 *      INSIDE that overlay. Clicking the search box bubbled to the overlay
 *      and closed the whole menu, so it could never be typed into.
 *
 *   2. The in-session header title. `#appTitle` carried a bare
 *      `click -> showLaunchpad()`, and the rename input + pencil button are
 *      injected INSIDE `#appTitle`. Clicking the title control navigated
 *      away from the session instead of letting the rename happen.
 *
 * Both are the same mistake: the listener asked "was there a click?" when
 * the question is "was this click aimed at ME, or at something inside me
 * that the user actually wants?". Every other modal in this codebase
 * happens to get it right by hand-writing `if (e.target === overlay)`.
 * Hand-written checks are exactly how the two above drifted, so the check
 * lives here now and callers name their intent instead.
 *
 * No dependencies. Must load BEFORE any module that dismisses on click.
 */

console.log('[DismissGuard Module] Loading...');

(function () {
    'use strict';

    /**
     * Elements that own their own click semantics. A click landing on (or
     * inside) one of these is never a dismiss gesture, it is the user
     * operating that control. `[data-keep-open]` is the manual escape
     * hatch for anything custom that this list cannot know about.
     */
    var INTERACTIVE_SELECTOR = [
        'input',
        'textarea',
        'select',
        'button',
        'a[href]',
        'label',
        'option',
        '[contenteditable=""]',
        '[contenteditable="true"]',
        '[role="textbox"]',
        '[role="searchbox"]',
        '[role="combobox"]',
        '[tabindex]:not([tabindex="-1"])',
        '[data-keep-open]'
    ].join(',');

    /**
     * Description: is this event aimed at the container ITSELF (the
     *   backdrop/scrim area) rather than at any descendant of it? This is
     *   the correct test for overlay-click-to-close.
     * Inputs: event (Event) - a click/pointer event; container (Element).
     * Output: boolean - true when the container is the literal event target.
     */
    function isSelfTarget(event, container) {
        if (!event || !container) return false;
        return event.target === container;
    }

    /**
     * Description: does the event originate from an interactive control at
     *   or inside `container`? Walks up from the event target, stopping at
     *   the container so controls OUTSIDE it never count.
     * Inputs: event (Event); container (Element) - search boundary.
     * Output: boolean.
     */
    function isInteractiveTarget(event, container) {
        var node = event && event.target;
        if (!node || !container) return false;
        if (typeof node.closest !== 'function') return false;
        var hit = node.closest(INTERACTIVE_SELECTOR);
        return !!(hit && container.contains(hit));
    }

    /**
     * Description: wire overlay-click-to-dismiss the safe way. The handler
     *   runs ONLY when the click landed on the overlay element itself, so
     *   anything rendered inside it (inputs, buttons, lists) is untouched.
     *   Replaces the `overlay.addEventListener('click', close)` shape that
     *   caused symptom 1 above.
     * Inputs:
     *   overlayEl (Element) - the scrim/overlay node.
     *   onDismiss (function(Event): void) - called when the scrim is hit.
     * Output: void.
     * Example: DismissGuard.onOverlayDismiss(overlay, () => this.close());
     */
    function onOverlayDismiss(overlayEl, onDismiss) {
        if (!overlayEl || typeof onDismiss !== 'function') return;
        overlayEl.addEventListener('click', function (e) {
            if (!isSelfTarget(e, overlayEl)) return;
            onDismiss(e);
        });
    }

    /**
     * Description: wire a whole-container click action (navigate, expand,
     *   dismiss) that must NOT fire when the user is operating a control
     *   inside that container. Replaces the bare
     *   `container.addEventListener('click', navigate)` shape that caused
     *   symptom 2 above.
     * Inputs:
     *   containerEl (Element) - the clickable region.
     *   onActivate (function(Event): void) - called for a genuine
     *     container click.
     * Output: void.
     * Example: DismissGuard.onContainerActivate(appTitle, () => goHome());
     */
    function onContainerActivate(containerEl, onActivate) {
        if (!containerEl || typeof onActivate !== 'function') return;
        containerEl.addEventListener('click', function (e) {
            if (isInteractiveTarget(e, containerEl)) return;
            onActivate(e);
        });
    }

    /**
     * Description: wire document-level click-outside dismissal for a
     *   popover/dropdown. Ignores clicks inside the panel, inside the
     *   trigger, and any click while `isOpen()` says the thing is closed.
     *   Focus moving into a child input is NOT a dismiss, because this
     *   listens for clicks and not for blur/focusout - a focusout-based
     *   dismiss is the third way to reproduce this same bug and is
     *   deliberately not offered here.
     * Inputs:
     *   panelEl (Element) - the popover content.
     *   onDismiss (function(Event): void).
     *   options (object) - {trigger: Element|null, isOpen: function(): boolean}.
     * Output: function(): void - unsubscribe.
     */
    function onOutsideDismiss(panelEl, onDismiss, options) {
        if (!panelEl || typeof onDismiss !== 'function') return function () {};
        var opts = options || {};
        var handler = function (e) {
            if (typeof opts.isOpen === 'function' && !opts.isOpen()) return;
            if (panelEl.contains(e.target)) return;
            if (opts.trigger && opts.trigger.contains(e.target)) return;
            onDismiss(e);
        };
        document.addEventListener('click', handler);
        return function () { document.removeEventListener('click', handler); };
    }

    window.DismissGuard = {
        INTERACTIVE_SELECTOR: INTERACTIVE_SELECTOR,
        isSelfTarget: isSelfTarget,
        isInteractiveTarget: isInteractiveTarget,
        onOverlayDismiss: onOverlayDismiss,
        onContainerActivate: onContainerActivate,
        onOutsideDismiss: onOutsideDismiss
    };
})();

console.log('[DismissGuard Module] Exported as window.DismissGuard');
