/**
 * File-browser drawer PIN - keep the file drawer docked open.
 *
 * WHAT WAS ASKED FOR: "the file browser was a slide out from right lets
 * make it so, maybe even sticky and resizes the tmux until closed."
 * Sticky is this: a pinned drawer stays put, loses its backdrop, stops
 * being dismissed by an outside click, and the app pays for it in layout
 * so the terminal narrows to what is left instead of being covered.
 *
 * THIS IS THE SESSION SIDEBAR'S PIN, ON PURPOSE. Read
 * client/js/session-sidebar-pin.js beside this file: same persisted
 * '1'/'0' `cloude.*` key, same 700px cut-off, same
 * isEffectivelyPinned() gate that every behavioural question asks instead
 * of the raw preference, same body class driving the padding, same
 * announced refit after the transition settles. Two docking panels that
 * behave differently would be two things to learn; the only real
 * difference here is the edge, and therefore which padding moves.
 *
 * NOT ON A PHONE. Under MOBILE_MAX_PX the pin is ignored and the control
 * is hidden, and the drawer stays a plain overlay that dismisses on an
 * outside click. Docking a 360px drawer beside a 390px viewport would
 * leave 30px of terminal, which is not a terminal. The preference is
 * still PERSISTED, just not APPLIED, so rotating to a wide viewport or
 * opening the same browser on a desktop honours it again; a resize
 * listener re-evaluates so crossing the breakpoint live does the right
 * thing without a reload.
 *
 * THE RESIZE IS ANNOUNCED, NEVER INFERRED. Docking changes `.screen`'s
 * padding-right, which changes the terminal's box. terminal.js's
 * ResizeObserver would probably notice, and the sidebar pin shipped on
 * exactly that assumption and was reported not to resize. So this calls
 * window.TerminalLayout.requestFit() explicitly, after the CSS transition
 * has settled, and lets the observer stay the net for changes nobody
 * announces. Both routes land in the same debounced fit, so an announced
 * change that also trips the observer still produces one resize frame.
 * There is no second resize path in here and must never be one.
 *
 * Split into its own module for the project's 500-line rule; this and
 * config-editor-panel.js are one feature. Must load AFTER
 * config-editor-panel.js (window.ConfigEditorPanel).
 */

console.log('[ConfigDrawerPin Module] Loading...');

(function () {
    /**
     * localStorage key for the pinned preference - '1' pinned, '0' not.
     * Sibling of the panel's own 'cloude.configEditor.collapsed' tree
     * state and of 'cloude.session.sidebar.pinned'.
     * @type {string}
     */
    const STORAGE_KEY = 'cloude.configEditor.pinned';

    /**
     * Widest viewport that counts as "mobile" for pin purposes. The same
     * 700px the modal stack, the editor modal and the sidebar pin use, so
     * "this is a phone" means one thing across the app.
     * @type {number}
     */
    const MOBILE_MAX_PX = 700;

    /**
     * Class on <body> while the drawer is docked. Drives the layout shift
     * in client/css/config-drawer.css.
     * @type {string}
     */
    const BODY_CLASS = 'config-drawer-docked';

    /**
     * How long the docked-layout padding takes to animate, per
     * config-drawer.css (`transition: padding-right 160ms ease`). The
     * refit is requested after this so it measures the settled box rather
     * than a mid-transition one.
     * @type {number}
     */
    const LAYOUT_SETTLE_MS = 200;

    /** @type {boolean} The persisted preference, as loaded. */
    let pinned = false;
    /** @type {?Element} The pin button, once wired. */
    let btnEl = null;
    /** @type {?boolean} Last docked state pushed, so a refit is asked for only on change. */
    let lastDocked = null;

    /**
     * True when the viewport is too narrow to dock a drawer.
     *
     * A reported width of 0 is not a narrow viewport, it is NO viewport -
     * a hidden or not-yet-laid-out tab reports that, and treating it as a
     * phone silently undocks a pinned panel until the next resize (observed
     * live on the sidebar pin). Unknown width means "not mobile".
     *
     * @returns {boolean} True below the breakpoint.
     */
    function isMobile() {
        const width = window.innerWidth;
        if (!width) return false;
        return width <= MOBILE_MAX_PX;
    }

    /**
     * Read the persisted preference.
     * @returns {boolean} True when the user has pinned the drawer.
     */
    function loadPinned() {
        try {
            return localStorage.getItem(STORAGE_KEY) === '1';
        } catch (_) {
            return false;
        }
    }

    /**
     * Write the preference. Never throws - a browser with storage denied
     * loses persistence, not the feature.
     * @param {boolean} value - The new preference.
     * @returns {void}
     */
    function savePinned(value) {
        try {
            localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
        } catch (_) { /* storage denied - session-only pin */ }
    }

    /**
     * True when the drawer should currently BEHAVE as docked: the user
     * pinned it and the viewport is wide enough. Every behavioural
     * question ("should an outside click close it?", "should the backdrop
     * show?") asks this, never the raw preference.
     * @returns {boolean} True when the pin is in force.
     */
    function isEffectivelyPinned() {
        return pinned && !isMobile();
    }

    /**
     * Push the current pin state into the DOM: the body class for the
     * layout shift, the announced terminal refit, and the button's own
     * pressed state and mobile visibility.
     * @returns {void}
     */
    function apply() {
        const panel = window.ConfigEditorPanel;
        const effective = isEffectivelyPinned();
        const docked = effective && !!panel && panel.isOpen;
        document.body.classList.toggle(BODY_CLASS, docked);

        // Docking pads `.screen` by the drawer width, so the terminal's box
        // changes and tmux has to be told. TerminalLayout.requestFit is the
        // ONE resize pipeline; do not add another.
        if (docked !== lastDocked) {
            lastDocked = docked;
            if (window.TerminalLayout) {
                setTimeout(
                    () => window.TerminalLayout.requestFit('config-drawer-pin'),
                    LAYOUT_SETTLE_MS,
                );
            }
        }

        if (btnEl) {
            btnEl.setAttribute('aria-pressed', String(pinned));
            btnEl.title = pinned ? 'unpin file drawer' : 'pin file drawer open';
            btnEl.setAttribute(
                'aria-label', pinned ? 'Unpin file drawer' : 'Pin file drawer open');
            // Hidden rather than disabled on a phone: an always-inert
            // control invites the user to wonder what is wrong with it.
            btnEl.hidden = isMobile();
        }
    }

    /**
     * Flip the pin and persist it.
     * @returns {void}
     */
    function toggle() {
        pinned = !pinned;
        savePinned(pinned);
        apply();
    }

    /**
     * Whether a click that landed on the overlay (outside the drawer)
     * should dismiss it. A docked drawer covers nothing, and its overlay
     * is click-through anyway, so this is belt and braces against the
     * handler firing from a synthetic event or a stale layout.
     * @returns {boolean} True when an outside click should close.
     */
    function shouldDismissOnOutsideClick() {
        return !isEffectivelyPinned();
    }

    /**
     * Wire the pin button and restore the persisted state. Idempotent -
     * called from ConfigEditorPanel._wire() on first open.
     * @returns {void}
     */
    function init() {
        if (!btnEl) {
            btnEl = document.getElementById('config-drawer-pin');
            if (btnEl) btnEl.addEventListener('click', () => toggle());
            window.addEventListener('resize', () => apply());
        }
        pinned = loadPinned();
        apply();
    }

    window.ConfigDrawerPin = {
        init,
        apply,
        toggle,
        isEffectivelyPinned,
        shouldDismissOnOutsideClick,
        STORAGE_KEY,
        MOBILE_MAX_PX,
    };
    console.log('[ConfigDrawerPin Module] Exported as window.ConfigDrawerPin');
})();
