/**
 * Session sidebar DENSITY - how much of each session a row spends space
 * saying.
 *
 * The request was "a little icon dropdown for how verbose, do i want
 * super thin project bars so i can see more, or more detail". The sidebar
 * is sessions-only (a decision already made elsewhere), so this governs
 * SESSION rows, and the two ends of that sentence are the two modes that
 * had to exist. A third sits between them and is the default, because it
 * is exactly what the sidebar rendered before this feature: adding a
 * control should not silently restyle the list of somebody who never
 * touches it.
 *
 *   compact   one tight line. Smallest row this list can draw.
 *   cozy      the row as it has always been. DEFAULT.
 *   detailed  two lines: the row, plus a second line carrying the
 *             tmux/external badge and the session's age.
 *
 * NO AGENT-FAMILY PILL IN ANY OF THE THREE, since "i dont think we need
 * the pills in the sidebar take out for now". This docblock used to
 * insist the pill was drawn in all three modes and that dropping it from
 * the smallest one would be hiding the state that matters most. That
 * reasoning was about a density control quietly deleting information the
 * user never asked to lose; it does not survive the user asking for the
 * pill to go. The home screen still draws its own pill from its own
 * builder in client/js/launchpad.js.
 *
 * WHERE THE HEIGHT DIFFERENCE COMES FROM, AND WHY IT IS NOW DECLARED:
 * row padding, line-height, the presence of the second line, and - new
 * this round - an explicit `min-height` per mode in
 * client/css/session-sidebar-density.css. The min-height is the point:
 * without it, the three numbers were whatever the tallest control on the
 * line happened to be, so REMOVING a glyph silently resized the mode.
 * A density contract that moves when an unrelated control is added or
 * removed is not a contract. No inline styles either way, so the numbers
 * are measurable from the stylesheet the browser actually loads - and
 * they ARE measured, in a real Chromium, by
 * scripts/verify_sidebar_sessions.py.
 *
 * Persistence follows the `cloude.*` localStorage convention. A stored
 * value that is not one of the three modes falls back to the default and
 * warns; it is a preference, not data, so there is nothing of the user's
 * to lose and nothing to announce in the UI.
 *
 * Must load AFTER session-sidebar.js (calls back into it to repaint).
 */

console.log('[SessionSidebarDensity Module] Loading...');

(function () {
    /**
     * localStorage key for the density preference.
     * @type {string}
     */
    const STORAGE_KEY = 'cloude.session.sidebar.density';

    /**
     * The three modes, in the order the menu lists them: thinnest first,
     * which is the order the request itself names them in.
     * @type {Array<string>}
     */
    const MODES = ['compact', 'cozy', 'detailed'];

    /**
     * Mode used when nothing is stored or the stored value is not a mode.
     * Deliberately the row as it looked before this feature existed.
     * @type {string}
     */
    const DEFAULT_MODE = 'cozy';

    /**
     * Human label per mode, shown in the dropdown. Lowercase to match the
     * app's UI voice.
     * @type {Object<string, string>}
     */
    const LABELS = {
        compact: 'compact',
        cozy: 'cozy',
        detailed: 'detailed',
    };

    /**
     * One-line explanation per mode, shown under the label so the choice
     * is not three adjectives with no consequence attached.
     * @type {Object<string, string>}
     */
    const HINTS = {
        compact: 'thinnest rows, most sessions on screen',
        cozy: 'the default row',
        detailed: 'moves the tmux badge onto a second line with the age',
    };

    let mode = DEFAULT_MODE;
    let btnEl = null;
    let menuEl = null;
    let wired = false;

    /**
     * Description: read the stored density, falling back to the default
     *   for anything absent, unreadable, or not one of MODES.
     * Inputs: none (reads localStorage).
     * Output: string - one of MODES.
     */
    function loadMode() {
        let raw = null;
        try {
            raw = localStorage.getItem(STORAGE_KEY);
        } catch (err) {
            console.warn('SessionSidebarDensity: storage unreadable, using default density');
            return DEFAULT_MODE;
        }
        if (raw === null || raw === undefined || raw === '') return DEFAULT_MODE;
        if (MODES.indexOf(raw) === -1) {
            console.warn(`SessionSidebarDensity: stored density ${JSON.stringify(raw)} is not a mode, using default`);
            return DEFAULT_MODE;
        }
        return raw;
    }

    /**
     * Description: the density currently in effect.
     * Inputs: none.
     * Output: string - one of MODES.
     */
    function currentMode() { return mode; }

    /**
     * Description: push the current mode into the DOM - the panel's
     *   `data-density` attribute (which every density rule in
     *   client/css/session-sidebar-density.css keys off), the button's
     *   accessible name, and the menu's checked item.
     * Inputs: none.
     * Output: void.
     */
    function apply() {
        const sidebar = window.SessionSidebar;
        const panel = sidebar && sidebar.panel;
        if (panel) panel.setAttribute('data-density', mode);
        if (btnEl) {
            btnEl.setAttribute('data-density', mode);
            btnEl.title = `row detail: ${LABELS[mode]}`;
            btnEl.setAttribute('aria-label', `Row detail: ${LABELS[mode]}. Change how much each session row shows.`);
        }
        if (menuEl) {
            menuEl.querySelectorAll('[data-density-mode]').forEach((item) => {
                const on = item.getAttribute('data-density-mode') === mode;
                item.setAttribute('aria-checked', String(on));
            });
        }
    }

    /**
     * Description: set the density, persist it, repaint the panel, and
     *   force the sidebar's next render to actually rewrite the DOM (its
     *   signature diff would otherwise skip a repaint whose only change
     *   is the mode).
     * Inputs: next (string) - one of MODES; anything else is ignored.
     * Output: boolean - true when the mode changed.
     */
    function setMode(next) {
        if (MODES.indexOf(next) === -1) return false;
        if (next === mode) { closeMenu(); return false; }
        mode = next;
        try {
            localStorage.setItem(STORAGE_KEY, mode);
        } catch (err) {
            console.warn('SessionSidebarDensity: could not persist density:', err);
        }
        apply();
        closeMenu();
        const sidebar = window.SessionSidebar;
        if (sidebar) sidebar.repaint();
        return true;
    }

    /** Description: true when the dropdown is open. Inputs: none. Output: boolean. */
    function isMenuOpen() {
        return !!menuEl && !menuEl.hidden;
    }

    /**
     * Description: show the dropdown and move focus to the checked item,
     *   so the control is usable from the keyboard alone.
     * Inputs: none. Output: void.
     */
    function openMenu() {
        if (!menuEl || !btnEl) return;
        menuEl.hidden = false;
        btnEl.setAttribute('aria-expanded', 'true');
        const items = menuItems();
        const checked = items.find((i) => i.getAttribute('aria-checked') === 'true');
        (checked || items[0] || btnEl).focus();
    }

    /**
     * Description: hide the dropdown, returning focus to the button when
     *   focus was inside the menu (never stealing it otherwise).
     * Inputs: none. Output: void.
     */
    function closeMenu() {
        if (!menuEl || menuEl.hidden) return;
        const inside = menuEl.contains(document.activeElement);
        menuEl.hidden = true;
        if (btnEl) {
            btnEl.setAttribute('aria-expanded', 'false');
            if (inside) btnEl.focus();
        }
    }

    /**
     * Description: the dropdown's option elements, top to bottom.
     * Inputs: none. Output: Array<Element>.
     */
    function menuItems() {
        if (!menuEl) return [];
        return Array.prototype.slice.call(menuEl.querySelectorAll('[data-density-mode]'));
    }

    /**
     * Description: keyboard handling for the open dropdown - arrows to
     *   move, Enter/Space to choose, Escape to leave without changing
     *   anything, Tab to close and move on.
     * Inputs: e (KeyboardEvent). Output: void.
     */
    function onMenuKeydown(e) {
        const items = menuItems();
        const idx = items.indexOf(e.target);
        if (e.key === 'Escape') {
            e.preventDefault();
            closeMenu();
            return;
        }
        if (e.key === 'Tab') { closeMenu(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            if (!items.length) return;
            const step = e.key === 'ArrowDown' ? 1 : -1;
            const nextIdx = (idx + step + items.length) % items.length;
            items[nextIdx].focus();
            return;
        }
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            if (idx !== -1) setMode(items[idx].getAttribute('data-density-mode'));
        }
    }

    /**
     * Description: wire the button and the dropdown once. Idempotent -
     *   SessionSidebar.init() calls it every time a screen is shown.
     * Inputs: none. Output: void.
     */
    function init() {
        mode = loadMode();
        if (!wired) {
            btnEl = document.getElementById('session-sidebar-density');
            menuEl = document.getElementById('session-sidebar-density-menu');
            if (btnEl && menuEl) {
                btnEl.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (isMenuOpen()) closeMenu(); else openMenu();
                });
                btnEl.addEventListener('keydown', (e) => {
                    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
                    e.preventDefault();
                    openMenu();
                });
                menuEl.addEventListener('click', (e) => {
                    const item = e.target.closest('[data-density-mode]');
                    if (!item) return;
                    e.stopPropagation();
                    setMode(item.getAttribute('data-density-mode'));
                });
                menuEl.addEventListener('keydown', onMenuKeydown);
                document.addEventListener('click', (e) => {
                    if (!isMenuOpen()) return;
                    if (menuEl.contains(e.target) || btnEl.contains(e.target)) return;
                    closeMenu();
                });
                wired = true;
            }
        }
        apply();
    }

    window.SessionSidebarDensity = {
        init, apply, setMode, currentMode, openMenu, closeMenu, isMenuOpen,
        STORAGE_KEY, MODES, DEFAULT_MODE, LABELS, HINTS,
    };
    console.log('[SessionSidebarDensity Module] Exported as window.SessionSidebarDensity');
})();
