/**
 * Terminal tools menu - getting content IN AND OUT of the terminal.
 * ----------------------------------------------------------------------
 * THREE ROWS, ONE IDEA: copy output, paste from clipboard, attach file.
 * Every one of them moves content across the terminal's boundary, which
 * is the rule a user can actually learn for what lives behind this
 * button. It is the successor to the paperclip FAB, which already owned
 * two of the three.
 *
 * WHAT IS NOT HERE, AND WHY. Session theme and session music were briefly
 * merged into this menu and that was wrong: they do not move content,
 * they configure the session's appearance and sound. They are the SESSION
 * EDITOR (#sessionEditorBtn, session-editor-menu.js), a separate control
 * with its own glyph on the top-right rail. Two coherent groups beat one
 * drawer of everything.
 *
 * ALSO NOT HERE: the header kebab (#header-menu-toggle, header-menu.js)
 * is APP-scoped - home, detach, logout, settings, claude config - and is
 * present on every screen, including the launchpad where a terminal tool
 * would be meaningless. This menu only exists while a session is
 * attached.
 *
 * The open/close/dismiss/wire plumbing is shared with the session editor
 * and lives in fab-menu.js. Only the rows are declared here.
 */

console.log('[TerminalToolsMenu Module] Loading...');

(function () {
    'use strict';

    /** Stable ids for the menu rows, in declaration order. Read by tests. */
    var ENTRY_IDS = [
        'toolCopyOutput',
        'toolPasteClipboard',
        'toolAttachImage'
    ];

    /** The Terminal wrapper handed over by terminal.js, or null. */
    var termWrapper = null;

    /** The hidden file input, or null. */
    var fileInputEl = null;

    /**
     * Icons, in the terminal-tool set's exact geometry: a 16x16 viewBox
     * rendered at 16x16, `fill="none"`, and stroke-width declared on
     * every path as a presentation attribute, because a presentation
     * attribute beats a stylesheet rule that targets the svg.
     *
     * @type {Object<string, string>}
     */
    var ICONS = {
        copy:
            '<rect x="5.5" y="5.5" width="8" height="9" rx="1.5" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="M10.5 3.5v-1a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
        paste:
            '<path d="M4.5 3.5h-1a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-9a1 1 0 0 0-1-1h-1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<rect x="5.5" y="1.5" width="5" height="3" rx="1" stroke="currentColor" stroke-width="1.5"/>',
        image:
            '<rect x="2" y="3.5" width="12" height="9" rx="1.5" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="m3 11 3-3 2.5 2.5L11 8l2 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<circle cx="6" cy="6.5" r="1" stroke="currentColor" stroke-width="1.5"/>'
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
        menuId: 'terminalToolsMenu',
        menuClass: 'terminal-tools-menu',
        ariaLabel: 'terminal tools',
        rows: buildItems
    });

    /**
     * The three rows, in order.
     *
     * @param {object} ctl - the FabMenu controller building them.
     * @returns {HTMLButtonElement[]}
     */
    function buildItems(ctl) {
        var c = ctl || menu;
        return [
            c.item(ENTRY_IDS[0], buildIcon('copy'), 'copy output', function () {
                if (window.CopyOutput) window.CopyOutput.open(termWrapper);
            }),
            c.item(ENTRY_IDS[1], buildIcon('paste'), 'paste from clipboard', function () {
                if (window.ClipboardTools) {
                    window.ClipboardTools.pasteFromClipboard(termWrapper);
                }
            }),
            c.item(ENTRY_IDS[2], buildIcon('image'), 'attach file', function () {
                if (fileInputEl) fileInputEl.click();
            })
        ];
    }

    /**
     * Wire the trigger and remember what the rows act on. Idempotent: a
     * later session swap only replaces the wrapper and the file input.
     *
     * @param {object} wrapper - the Terminal wrapper (status pill, ws).
     * @param {HTMLElement} btn - #terminalToolsBtn.
     * @param {HTMLElement} input - the hidden file input.
     * @returns {void}
     */
    function wire(wrapper, btn, input) {
        termWrapper = wrapper;
        fileInputEl = input || fileInputEl;
        menu.wire(btn);
    }

    window.TerminalToolsMenu = {
        wire: wire,
        open: menu.open,
        close: menu.close,
        isOpen: menu.isOpen,
        buildItems: buildItems,
        ENTRY_IDS: ENTRY_IDS,
        ICONS: ICONS
    };
})();

console.log('[TerminalToolsMenu Module] Exported as window.TerminalToolsMenu');
