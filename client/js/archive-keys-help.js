/**
 * THE KEYBOARD HELP MODAL, rendered from archive-keys.js's binding table.
 *
 * WHY IT IS NOT IN archive-keys.js ANY MORE. That file's own header
 * describes it as the pure decision layer - "no DOM" - and it had a
 * hundred lines of modal construction in it. When the conversation view
 * added a `v` binding the file crossed this repo's 500-line cap, and the
 * honest cut was the half that contradicted the header rather than the
 * half that matched it. archive-keys.js is now pure again.
 *
 * ONE TABLE, STILL. The panel is built from ArchiveKeys.bindings() and
 * holds no key list of its own, because a help panel that lies is worse
 * than no help panel, and two tables always drift. This module reads
 * that table at OPEN time rather than at load time, so it cannot capture
 * a stale copy.
 *
 * LOAD ORDER: archive-keys.js first. `ArchiveKeys.openHelp` delegates
 * here and names this file if it is missing, rather than failing later
 * as "openHelp is not a function" - an error that reports the wrong
 * cause.
 *
 * Exports window.ArchiveKeysHelp.
 */

console.log('[ArchiveKeysHelp Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: the binding table, read at call time so this module
     *   can never hold a stale copy of a list that lives elsewhere.
     * Inputs: none.
     * Output: Array<{keys, action, note}> - empty when archive-keys.js
     *   is absent, which renders an empty table rather than throwing:
     *   the modal itself is not worth taking the screen down for.
     */
    function bindings() {
        return (window.ArchiveKeys && typeof window.ArchiveKeys.bindings === 'function')
            ? window.ArchiveKeys.bindings() : [];
    }

    /**
     * Attribute the help overlay is tagged with, so the idempotency check
     * and any test share one named string rather than four literals.
     * @type {string} */
    var HELP_MODAL_ATTR = 'data-modal';

    /** Value of HELP_MODAL_ATTR on the help overlay. @type {string} */
    var HELP_MODAL_NAME = 'archive-help';
    /** Selector for an already-open help overlay. @type {string} */
    var HELP_MODAL_SELECTOR = '[' + HELP_MODAL_ATTR + '="' + HELP_MODAL_NAME + '"]';

    /**
     * Class prefix for this modal's elements, mirroring the BEM-ish shape
     * archive-export.js uses.
     * @type {string}
     */
    var HELP_ROOT_CLASS = 'archive-help';

    /** data-action on the close button. @type {string} */
    var HELP_CLOSE_ACTION = 'close-help';

    /** Column headings for the rendered binding table. @type {Array<string>} */
    var HELP_COLUMNS = ['Keys', 'What it does'];
    /**
     * Description: build one element with a class and optional text. Text
     *   goes in via textContent, never as markup - a binding note is data.
     * Inputs: doc (Document), tag, className, text (strings|null).
     * Output: Element.
     */
    function helpEl(doc, tag, className, text) {
        var node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: render bindings() as a table. Iterates the live table
     *   rather than restating it, so a binding added above appears here
     *   with no second edit. Each row carries data-action so a test can
     *   assert coverage.
     * Inputs: doc (Document). Output: Element - a <table>.
     */
    function buildHelpTable(doc) {
        var table = helpEl(doc, 'table', HELP_ROOT_CLASS + '__table', null);
        var thead = helpEl(doc, 'thead', null, null);
        var headRow = thead.appendChild(helpEl(doc, 'tr', null, null));
        HELP_COLUMNS.forEach(function (label) {
            var th = helpEl(doc, 'th', null, label);
            th.setAttribute('scope', 'col');
            headRow.appendChild(th);
        });
        table.appendChild(thead);
        var tbody = helpEl(doc, 'tbody', null, null);
        bindings().forEach(function (binding) {
            var row = tbody.appendChild(helpEl(doc, 'tr', null, null));
            row.setAttribute('data-action', binding.action);
            row.appendChild(helpEl(doc, 'td', HELP_ROOT_CLASS + '__keys', binding.keys));
            row.appendChild(helpEl(doc, 'td', HELP_ROOT_CLASS + '__note', binding.note));
        });
        table.appendChild(tbody);
        return table;
    }

    /**
     * Description: open the keyboard help as a modal, rendered from
     *   bindings(). Registers with ModalStack, which is what makes Escape
     *   close THIS and not the screen behind it: resolveEscape() already
     *   returns null while a modal is open, so the ordering is settled and
     *   this adds no Escape listener of its own.
     * Inputs: options (object) - document (Document) REQUIRED, absent
     *   throws a TypeError naming it because returning quietly would leave
     *   a `?` key that does nothing and reports nothing; onClose
     *   (function|undefined) called once when the modal closes.
     * Output: {overlay: Element, close: function} - `close` is safe to
     *   call twice. If a help modal is already in `document`, the existing
     *   one's handle comes back rather than a second being stacked.
     * Example: ArchiveKeys.openHelp({document: document}).close();
     */
    function openHelp(options) {
        var opts = options || {};
        var doc = opts.document;
        if (!doc) throw new TypeError('ArchiveKeys.openHelp requires a "document" argument');
        var stack = (typeof window !== 'undefined' && window.ModalStack) ? window.ModalStack : null;

        /**
         * Description: the close path for one overlay, shared by the fresh
         *   and already-open branches so they cannot drift.
         * Inputs: node (Element). Output: function - idempotent close.
         */
        function closerFor(node) {
            var closed = false;
            return function close() {
                if (closed) return;
                closed = true;
                if (stack) stack.pop(node);
                if (node.parentNode) node.parentNode.removeChild(node);
                if (typeof opts.onClose === 'function') opts.onClose();
            };
        }

        // Already open? Two identical dialogs stacked on one `?` press is
        // worse than a no-op, so hand back the live one.
        var existing = typeof doc.querySelector === 'function'
            ? doc.querySelector(HELP_MODAL_SELECTOR) : null;
        if (existing) return { overlay: existing, close: closerFor(existing) };

        var overlay = helpEl(doc, 'div', 'modal-overlay ' + HELP_ROOT_CLASS + '-overlay', null);
        overlay.setAttribute(HELP_MODAL_ATTR, HELP_MODAL_NAME);

        var content = helpEl(doc, 'div', 'modal-content ' + HELP_ROOT_CLASS + '__content', null);
        content.setAttribute('role', 'dialog');
        content.setAttribute('aria-modal', 'true');
        var header = helpEl(doc, 'div', 'modal-header ' + HELP_ROOT_CLASS + '__header',
            'Keyboard shortcuts');
        var body = helpEl(doc, 'div', 'modal-body ' + HELP_ROOT_CLASS + '__body', null);
        body.appendChild(buildHelpTable(doc));
        var closeBtn = helpEl(doc, 'button', HELP_ROOT_CLASS + '__close', 'Close');
        closeBtn.setAttribute('type', 'button');
        closeBtn.setAttribute('data-action', HELP_CLOSE_ACTION);
        body.appendChild(closeBtn);
        content.appendChild(header);
        content.appendChild(body);
        overlay.appendChild(content);

        var close = closerFor(overlay);
        if (typeof closeBtn.addEventListener === 'function') {
            closeBtn.addEventListener('click', close);
        }

        if (doc.body) doc.body.appendChild(overlay);
        if (stack) stack.push(overlay, { onEscape: close });

        // Guarded: a mini-DOM test harness may build elements with no
        // focus method, and a help panel is not worth throwing over.
        if (typeof closeBtn.focus === 'function') closeBtn.focus();

        return { overlay: overlay, close: close };
    }
    window.ArchiveKeysHelp = {
        openHelp: openHelp,
        buildHelpTable: buildHelpTable,
        HELP_MODAL_ATTR: HELP_MODAL_ATTR,
        HELP_MODAL_NAME: HELP_MODAL_NAME,
        HELP_ROOT_CLASS: HELP_ROOT_CLASS,
        HELP_CLOSE_ACTION: HELP_CLOSE_ACTION
    };
    console.log('[ArchiveKeysHelp Module] Exported as window.ArchiveKeysHelp');
})();
