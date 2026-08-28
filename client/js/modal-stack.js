/**
 * Modal stack - the one place that knows which modal is on TOP.
 *
 * Exists because the config editor is now two modals that can be open at
 * once: the file picker (client/js/config-editor-panel.js) and, layered
 * over it, the editor (client/js/config-editor-modal.js). Before this
 * module each surface owned a `document`-level Escape listener, so one
 * Escape keypress reached BOTH and collapsed the whole stack - the exact
 * "escape closes only the top modal" requirement this fixes.
 *
 * Deliberately opt-in. The app's other `.modal-overlay` users
 * (settings-panel.js, providers.js, App.showConfirmModal) keep their own
 * dismissal handling and are NOT registered here; nothing about this
 * module changes their behavior. What it does do is stay out of their
 * way: the Escape handler below refuses to act whenever some OTHER
 * overlay sits later in `document.body` than the registered top, which is
 * exactly the case when App.showConfirmModal opens its confirm dialog
 * over the editor. Without that guard, capture-phase Escape would eat the
 * confirm dialog's own Escape and close the editor underneath it.
 *
 * Responsibilities, all of them stack-wide concerns no single modal can
 * own by itself:
 *   - route Escape to the top entry only (capture phase, so it wins over
 *     the per-overlay listeners underneath)
 *   - lock background scroll while anything is stacked, unlock on empty
 *   - mark covered entries so CSS can dim them (desktop) or take them off
 *     screen entirely (mobile - one thing at a time on a phone)
 *   - remember and restore focus per entry, so popping the editor puts
 *     the caret back on the picker row that opened it
 *
 * No dependencies. Load BEFORE config-editor-modal.js and
 * config-editor-panel.js.
 */

console.log('[ModalStack Module] Loading...');

(function () {
    /**
     * Class applied to <body> while at least one modal is stacked.
     * Styling lives in client/css/modal-stack.css.
     * @type {string}
     */
    const BODY_LOCK_CLASS = 'modal-stack-locked';

    /**
     * Class applied to every stacked overlay that is NOT the top one.
     * @type {string}
     */
    const COVERED_CLASS = 'modal-stack--covered';

    /**
     * Base z-index for the bottom entry; each further entry is +2. The
     * whole ladder deliberately sits BELOW styles.css's `.modal-overlay`
     * default of 9999, so any overlay this module does not manage lands
     * on top of the stack rather than under it.
     *
     * That is not a detail. App.showConfirmModal is the "discard unsaved
     * changes?" dialog the editor raises on dismissal, and it is an
     * unregistered `.modal-overlay` at the default 9999. With the stack
     * numbered ABOVE 9999 (observed: 10000/10010) that confirm rendered
     * BEHIND the editor that raised it - present in the DOM, focused, and
     * completely invisible, so a dirty editor looked like it was refusing
     * to close. Keeping the stack under the default fixes it without
     * touching the shared confirm-modal code.
     * @type {number}
     */
    const BASE_Z = 9990;

    /**
     * Hard ceiling for the ladder, one below the app default. A stack deep
     * enough to reach it has entries TIE rather than cross the line, and a
     * tie resolves by DOM order - which already puts the top modal last,
     * because that is the order they were appended in. Staying under 9999
     * matters more than separating a hypothetical fifth layer.
     * @type {number}
     */
    const MAX_Z = 9998;

    // [{ overlayEl: Element, onEscape: function, previousFocus: Element|null }]
    const stack = [];

    /**
     * Re-apply per-entry z-index and the covered marker after any push or
     * pop, so the class/z state always matches the array rather than
     * being patched at each call site.
     * Inputs: none. Output: void.
     */
    function restack() {
        stack.forEach((entry, index) => {
            entry.overlayEl.style.zIndex = String(Math.min(BASE_Z + index * 2, MAX_Z));
            const covered = index < stack.length - 1;
            entry.overlayEl.classList.toggle(COVERED_CLASS, covered);
            // A covered modal must not be reachable by Tab or by a screen
            // reader - it is behind (desktop) or entirely off screen
            // (mobile), and either way it is not the surface the user is
            // operating.
            entry.overlayEl.setAttribute('aria-hidden', covered ? 'true' : 'false');
            if (covered) {
                entry.overlayEl.setAttribute('inert', '');
            } else {
                entry.overlayEl.removeAttribute('inert');
            }
        });
        document.body.classList.toggle(BODY_LOCK_CLASS, stack.length > 0);
    }

    /**
     * True when the registered top entry is also the last `.modal-overlay`
     * in document order - i.e. nothing unregistered (a confirm dialog, the
     * settings panel) has been layered over the stack since.
     * Inputs: none. Output: boolean.
     */
    function topIsOutermost() {
        if (stack.length === 0) return false;
        const overlays = document.body.querySelectorAll(':scope > .modal-overlay');
        if (overlays.length === 0) return false;
        return overlays[overlays.length - 1] === stack[stack.length - 1].overlayEl;
    }

    /**
     * Capture-phase Escape router. Only ever dismisses ONE modal: the top
     * registered entry, and only when no foreign overlay is above it.
     * Inputs: e (KeyboardEvent). Output: void.
     */
    function onKeydown(e) {
        if (e.key !== 'Escape') return;
        if (stack.length === 0) return;
        if (!topIsOutermost()) return;
        e.stopPropagation();
        e.preventDefault();
        const top = stack[stack.length - 1];
        try {
            top.onEscape();
        } catch (err) {
            console.error('ModalStack: escape handler threw:', err);
        }
    }

    document.addEventListener('keydown', onKeydown, true);

    /**
     * Register an overlay as the new top of the stack.
     * Inputs:
     *   overlayEl (Element) - the `.modal-overlay` root element.
     *   options (object) - { onEscape: function } - called with no
     *     arguments when Escape is pressed while this entry is on top.
     *     Required; a modal with no dismissal path has no business here.
     * Output: void.
     */
    function push(overlayEl, options) {
        if (!overlayEl || !options || typeof options.onEscape !== 'function') {
            throw new TypeError('ModalStack.push requires an element and an onEscape function');
        }
        if (stack.some((entry) => entry.overlayEl === overlayEl)) return;
        // Duck-typed rather than `instanceof HTMLElement`: that check is
        // realm-bound (it is false for an element from another document,
        // and HTMLElement does not exist at all outside a browser), and
        // the only thing this needs of the element is that it can take
        // focus back later.
        const active = document.activeElement;
        const previousFocus = active && typeof active.focus === 'function' ? active : null;
        stack.push({ overlayEl, onEscape: options.onEscape, previousFocus });
        restack();
    }

    /**
     * Remove one overlay from the stack and restore the focus it took.
     * Safe to call for an element that was never pushed (no-op) so a
     * modal's close path does not need to track whether it registered.
     * Inputs: overlayEl (Element). Output: void.
     */
    function pop(overlayEl) {
        const index = stack.findIndex((entry) => entry.overlayEl === overlayEl);
        if (index === -1) return;
        const [entry] = stack.splice(index, 1);
        restack();
        const target = entry.previousFocus;
        if (target && document.contains(target)) {
            try { target.focus(); } catch (_) { /* element may be unfocusable now */ }
        }
    }

    /**
     * Current stack depth - how many registered modals are open.
     * Inputs: none. Output: number.
     */
    function depth() {
        return stack.length;
    }

    /**
     * True when `overlayEl` is the top registered entry.
     * Inputs: overlayEl (Element). Output: boolean.
     */
    function isTop(overlayEl) {
        return stack.length > 0 && stack[stack.length - 1].overlayEl === overlayEl;
    }

    window.ModalStack = { push, pop, depth, isTop, BODY_LOCK_CLASS, COVERED_CLASS };
    console.log('[ModalStack Module] Exported as window.ModalStack');
})();
