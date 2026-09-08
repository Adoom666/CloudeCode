/**
 * The reader pane's TOOLBAR - the view toggle, the transcript search box
 * and the export button.
 *
 * WHY IT IS NOT IN `archive-reader.js`. Both controls are CROSS-PANE:
 * search writes its hits into the LIST column and export opens a modal,
 * and the reader knows about neither. Putting them there would give the
 * renderer a reason to reach out of its own pane, which is the coupling
 * that file is built to avoid.
 *
 * WHY IT IS NOT IN `archive-screen.js` EITHER. It was, and the
 * composition root went over the repo's 500-line cap when the pane
 * resizers and the crumb's knowledge landed. The seam is honest rather
 * than arbitrary: everything here is DOM construction for one strip of
 * controls, it decides nothing, and it hands back the two nodes the
 * composition root needs to keep (the input, so `/` can focus it, and
 * the export button).
 *
 * Exports window.ArchiveScreenTools.
 */

console.log('[ArchiveScreenTools Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: build the toolbar and insert it at the top of the
     *   reader pane.
     * Inputs: options (object) -
     *   document (Document), pane (Element) - the reader pane,
     *   rootClass (string), onSearch (function(text)),
     *   onExport (function()), onToggleView (function()).
     * Output: {element, viewBtn, searchInput, searchBtn, exportBtn}
     * Example: ArchiveScreenTools.create({document: document,
     *              pane: readPane, rootClass: 'archive-screen',
     *              onSearch: runSearch, onExport: openExport})
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document;
        var pane = opts.pane;
        if (!doc || !pane) throw new Error('ArchiveScreenTools.create needs a document and a pane');
        var cls = opts.rootClass || 'archive-screen';
        var onSearch = typeof opts.onSearch === 'function' ? opts.onSearch : function () {};
        var onExport = typeof opts.onExport === 'function' ? opts.onExport : function () {};
        var onToggleView = typeof opts.onToggleView === 'function'
            ? opts.onToggleView : function () {};

        /** Description: element with a class, text and attributes.
         *  Inputs: tag, suffix, text, map. Output: Element. */
        function el(tag, suffix, text, map) {
            var n = doc.createElement(tag);
            n.setAttribute('class', cls + suffix);
            if (text !== null && text !== undefined) n.textContent = text;
            var names = Object.keys(map || {});
            for (var i = 0; i < names.length; i++) n.setAttribute(names[i], map[names[i]]);
            return n;
        }

        var tools = el('div', '__tools', null, {});
        // THE VIEW TOGGLE COMES FIRST because it decides what the rest
        // of the pane is. Its LABEL NAMES THE DESTINATION, not the
        // current state ("Raw" while showing the conversation), which is
        // the only wording that stays unambiguous when the control is
        // read on its own. The key is named in the label so the
        // keystroke is discoverable without opening the help panel.
        var viewBtn = el('button', '__view-btn', 'Raw (v)',
                         { type: 'button', 'data-action': 'toggle-view',
                           'aria-pressed': 'false', 'data-view': 'chat',
                           'aria-label': 'Switch between the conversation ' +
                               'view and the byte-exact raw view' });
        tools.appendChild(viewBtn);
        var searchInput = el('input', '__search-input', null, {
            type: 'search',
            placeholder: 'search this transcript',
            'aria-label': 'Search in the open transcript'
        });
        var searchBtn = el('button', '__search-btn', 'Search',
                           { type: 'button', 'data-action': 'run-search' });
        var exportBtn = el('button', '__export-btn', 'Export',
                           { type: 'button', 'data-action': 'open-export' });
        tools.appendChild(searchInput);
        tools.appendChild(searchBtn);
        tools.appendChild(exportBtn);
        pane.insertBefore(tools, pane.firstChild);

        searchBtn.addEventListener('click', function () { onSearch(searchInput.value); });
        searchInput.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') onSearch(searchInput.value);
        });
        exportBtn.addEventListener('click', function () { onExport(); });
        viewBtn.addEventListener('click', function () { onToggleView(); });

        return { element: tools, viewBtn: viewBtn, searchInput: searchInput,
                 searchBtn: searchBtn, exportBtn: exportBtn };
    }

    window.ArchiveScreenTools = { create: create };
    console.log('[ArchiveScreenTools Module] Exported as window.ArchiveScreenTools');
})();
