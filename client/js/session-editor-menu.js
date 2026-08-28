/**
 * Session editor - everything that acts on THIS SESSION.
 * ----------------------------------------------------------------------
 * TWO ROWS: session theme, detach session. Neither moves content; both
 * act on the session you are looking at. That is the distinction from
 * the terminal tools menu, whose rows all move content across the
 * terminal's boundary.
 *
 * THE MUSIC ROW THAT USED TO BE HERE IS GONE. "play music" / "stop
 * music" was the per-session background-music opt-in
 * (session-theme-menu.js), buried two taps deep inside this dropdown.
 * It has been replaced by a single global on/off living in the bottom
 * bar on every screen (client/js/globalAudioToggle.js) - see that
 * file's doc comment for the reasoning. Do not add a music row back
 * here; the control now lives elsewhere on purpose.
 *
 * WHY DETACH IS HERE AND NOT IN THE TOOLS MENU. It was in the header
 * kebab, which is APP-scoped and mounts on the launchpad where there is
 * no session to detach from. Of the two FABs, tools is "move content
 * across the terminal's boundary" - copy, paste, attach - and detach
 * moves no content. It ends this session's attachment, which is an act
 * on the session itself, the exact rule this control already carries.
 * So it lands here, and the split the user corrected us on once still
 * holds: one menu for content, one for the session.
 *
 * Detach is also DESTRUCTIVE-ADJACENT (the tmux session survives, but
 * this tab stops owning it), so it is last, separated, and styled as a
 * danger row.
 *
 * WHY IT IS ITS OWN CONTROL AND NOT A ROW OF SOMETHING ELSE.
 *
 *   - Not the terminal tools menu. They were merged into one drawer once
 *     and the grouping had no rule a user could learn. Configuring a
 *     session and copying its output are different jobs.
 *   - Not the header kebab. That is APP-scoped - home, detach, logout,
 *     settings - and is mounted on every screen including the launchpad,
 *     where "theme for this session" names nothing.
 *   - Not the session sidebar row menu. That surface acts on sessions you
 *     are NOT looking at; theme is a per-session identity cue you set
 *     while looking at the session it marks, so it belongs on the
 *     terminal screen beside the terminal it recolours.
 *
 * So: its own FAB, its own glyph, on the top-right rail above the
 * terminal, hidden on every screen with no session attached.
 *
 * The theme row delegates to session-theme-menu.js, which still owns
 * the theme picker. This module is the control surface only.
 */

console.log('[SessionEditorMenu Module] Loading...');

(function () {
    'use strict';

    /** Stable ids for the menu rows, in declaration order. Read by tests. */
    var ENTRY_IDS = [
        'sessionThemeRow',
        'sessionDetachRow'
    ];

    /** The Terminal wrapper handed over by terminal.js, or null. */
    var termWrapper = null;

    /**
     * Icons, in the terminal-tool set's exact geometry: a 16x16 viewBox
     * rendered at 16x16, `fill="none"`, and stroke-width declared on
     * every stroked shape as a presentation attribute.
     *
     * @type {Object<string, string>}
     */
    var ICONS = {
        theme:
            '<path d="M8 1.75a6.25 6.25 0 1 0 0 12.5c.69 0 1.25-.56 1.25-1.25 0-.32-.13-.62-.34-.84a1.19 1.19 0 0 1 .84-2.03h1.44A3.06 3.06 0 0 0 14.25 7 6.25 6.25 0 0 0 8 1.75Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
            '<circle cx="5.25" cy="6" r="0.9" fill="currentColor"/>' +
            '<circle cx="8" cy="4.5" r="0.9" fill="currentColor"/>' +
            '<circle cx="10.75" cy="6" r="0.9" fill="currentColor"/>',
        // The header's detach glyph, carried over unchanged: a box with
        // its top-right corner left open and a diagonal arrow shooting
        // out through the gap. Kept identical so the control is still
        // recognisable after moving surfaces.
        detach:
            '<path d="M6.5 3.5H4.25C3.69772 3.5 3.25 3.94772 3.25 4.5V11.5C3.25 12.0523 3.69772 12.5 4.25 12.5H11.25C11.8023 12.5 12.25 12.0523 12.25 11.5V9.25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M9.25 3.5H12.5V6.75" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M7 9L12.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    };

    /**
     * Build one 16x16 icon from this menu's set.
     *
     * @param {string} name - a key of ICONS.
     * @returns {SVGElement|null}
     */
    function buildIcon(name) {
        return window.FabMenu ? window.FabMenu.buildIcon(ICONS, name) : null;
    }

    /** The controller. Its plumbing is shared; its rows are not. */
    var menu = window.FabMenu.create({
        menuId: 'sessionEditorMenu',
        menuClass: 'session-editor-menu',
        ariaLabel: 'session editor',
        rows: buildItems
    });

    /**
     * Detach this session: the soft exit. The tmux session stays alive
     * server-side for re-adoption from the launchpad; only this tab's
     * attachment ends.
     *
     * Delegates to TerminalController.detachSession(), which is the same
     * method #detachSessionBtn used to call - the behaviour is untouched,
     * only the surface moved.
     *
     * @returns {void}
     */
    function detachSession() {
        var ctl = window.TerminalController;
        if (ctl && typeof ctl.detachSession === 'function') {
            ctl.detachSession();
        }
    }

    /**
     * The two rows, in order. Built per open, matching every other
     * FabMenu-backed menu in this app even though nothing here is
     * per-open state any more (that was the music row's job).
     *
     * @param {object} ctl - the FabMenu controller building them.
     * @returns {HTMLButtonElement[]}
     */
    function buildItems(ctl) {
        var c = ctl || menu;
        var rows = [
            c.item(ENTRY_IDS[0], buildIcon('theme'), 'session theme', function () {
                var anchor = c.trigger();
                if (window.SessionThemeMenu && anchor) {
                    window.SessionThemeMenu.open(anchor);
                }
            })
        ];

        // Last, and marked. Detach ends this tab's ownership of the
        // session, so it must not sit flush against the theme row where
        // a mis-tap costs nothing.
        var detachRow = c.item(ENTRY_IDS[1], buildIcon('detach'),
            'detach session', detachSession);
        detachRow.classList.add('fab-menu__item--separated');
        detachRow.classList.add('fab-menu__item--danger');
        detachRow.setAttribute('aria-label',
            'detach session, leaves it running for later');
        detachRow.setAttribute('title',
            'detach session, leaves it running for later');
        rows.push(detachRow);
        return rows;
    }

    /**
     * Wire the trigger and remember the terminal the rows act on.
     * Idempotent: a later session swap only replaces the wrapper.
     *
     * @param {object} wrapper - the Terminal wrapper (status pill).
     * @param {HTMLElement} btn - #sessionEditorBtn.
     * @returns {void}
     */
    function wire(wrapper, btn) {
        termWrapper = wrapper;
        menu.wire(btn);
    }

    window.SessionEditorMenu = {
        wire: wire,
        open: menu.open,
        close: menu.close,
        isOpen: menu.isOpen,
        buildItems: buildItems,
        ENTRY_IDS: ENTRY_IDS,
        ICONS: ICONS
    };
})();

console.log('[SessionEditorMenu Module] Exported as window.SessionEditorMenu');
