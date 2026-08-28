/**
 * Server controls - the app-scoped menu hung off the home bar.
 * ----------------------------------------------------------------------
 * SCOPE: things done to the SERVER PROCESS, not to a session and not to a
 * project. That is the rule this menu is drawn along, and it is why it
 * lives on the home bar rather than beside the two terminal FABs: the
 * session editor configures the session you are looking at, the tools menu
 * moves content across the terminal's boundary, and neither of those means
 * anything on the home screen.
 *
 * ONE ROW, and it used to be two. "restart server" was withdrawn together
 * with the endpoint behind it: POST /api/v1/server/reset spawned a reset.sh
 * that the packaged app has never shipped, so the row returned a 500 on every
 * packaged install. It was removed rather than shipped because restarting a
 * process belongs to whatever SUPERVISES it, and this server never supervises
 * itself - the full argument, and where each install shape's real restart
 * lives, is at the removal site in src/api/routes.py. Withdrawing it cost
 * exactly what this file's design promised, in reverse: one entry in
 * ENTRY_IDS, one icon, one row in buildItems(), and nothing
 * else. Keep it that way - the rendering, the fetching and the one
 * destructive control all live in server-status-panel.js, not here.
 *
 * WHY IT ROUTES THROUGH FabMenu. The open/close/outside-dismiss/Escape
 * plumbing lived twice before and the copies drifted, and - the part that
 * bites - FabMenu.notify() is the only feedback surface in this app that
 * is guaranteed to be visible. The terminal status pill sits at z-index 70
 * underneath the sticky header (1000) and every overlay, so a message
 * raised from a menu row through the pill is painted over and seen by
 * nobody. Rows here take notify() as their argument and must use it.
 *
 * ANCHORING FROM THE BOTTOM. AnchorPopover places a popover ABOVE its
 * anchor with their right edges flush and only drops below when there is
 * no room above. A trigger sitting in a bottom bar is the case that rule
 * was written for, so the menu opens upward with no special handling. The
 * menu is `position: fixed` on document.body, so the home screen's
 * scroller cannot clip it.
 */

console.log('[ServerControlsMenu Module] Loading...');

(function () {
    'use strict';

    /** Stable ids for the menu rows, in declaration order. Read by tests. */
    var ENTRY_IDS = [
        'serverStatusRow'
    ];

    /**
     * Icons in the shared 16x16 terminal-tool geometry: a 16x16 viewBox
     * rendered at 16x16, `fill="none"`, and stroke-width declared as a
     * presentation attribute on every stroked shape (a presentation
     * attribute beats a stylesheet rule that targets the `svg`, so the
     * value has to be right in the markup).
     *
     * @type {Object<string, string>}
     */
    var ICONS = {
        gauge:
            '<path d="M2.5 12A5.5 5.5 0 0 1 8 3a5.5 5.5 0 0 1 5.5 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M8 8.5L10.75 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M8 12.5V14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    };

    /**
     * Build one 16x16 icon from this menu's set.
     *
     * @param {string} name - a key of ICONS.
     * @returns {SVGElement|null} the icon, or null with no FabMenu.
     */
    function buildIcon(name) {
        return window.FabMenu ? window.FabMenu.buildIcon(ICONS, name) : null;
    }

    /** The controller. Its plumbing is shared; its rows are not. */
    var menu = window.FabMenu.create({
        menuId: 'serverControlsMenu',
        menuClass: 'server-controls-menu',
        ariaLabel: 'server controls',
        rows: buildItems
    });

    /**
     * Open the server-status panel. The panel owns the fetch, the render
     * and its one destructive control; this row only opens it.
     *
     * @param {Function} notify - FabMenu.notify, the one feedback channel
     *   guaranteed to be visible above every overlay in this app.
     * @returns {void}
     */
    function openServerStatus(notify) {
        var panel = window.ServerStatusPanel;
        if (panel && typeof panel.open === 'function') {
            panel.open(menu.trigger());
            return;
        }
        if (typeof notify === 'function') {
            notify('server status unavailable right now', 'error');
        }
    }

    /**
     * The rows, in order. Built per open, per the FabMenu contract, so a
     * row that ever reflects live state reports the current value.
     *
     * @param {object} ctl - the FabMenu controller building them.
     * @returns {HTMLButtonElement[]}
     */
    function buildItems(ctl) {
        var c = ctl || menu;
        var statusRow = c.item(ENTRY_IDS[0], buildIcon('gauge'),
            'server status', openServerStatus);
        // Read-only apart from one control the panel confirms for itself,
        // so nothing here is danger-styled.
        statusRow.setAttribute('aria-label',
            'server status, sessions and host health');
        statusRow.setAttribute('title',
            'server status, sessions and host health');
        // THERE IS NO "restart server" ROW ANY MORE. It called
        // POST /api/v1/server/reset, which spawned reset.sh from the
        // server's root - a file that has never shipped in the packaged
        // app, so the row 500'd on every packaged install. Restarting the
        // process belongs to whatever supervises it, never to the process
        // itself; the argument and each install shape's real restart are
        // recorded at the removal site in src/api/routes.py. Do not
        // re-add a row here without a supervisor-owned action behind it.
        return [statusRow];
    }

    /**
     * Wire the trigger. Idempotent through FabMenu.wire(), so a launchpad
     * re-render that hands over a fresh button cannot double-bind.
     *
     * @param {HTMLElement|null} btn - #server-controls-btn.
     * @returns {void}
     */
    function wire(btn) {
        menu.wire(btn);
    }

    window.ServerControlsMenu = {
        wire: wire,
        open: menu.open,
        close: menu.close,
        isOpen: menu.isOpen,
        buildItems: buildItems,
        ENTRY_IDS: ENTRY_IDS,
        ICONS: ICONS
    };
})();

console.log('[ServerControlsMenu Module] Exported as window.ServerControlsMenu');
