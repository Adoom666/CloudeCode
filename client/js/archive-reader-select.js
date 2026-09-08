/**
 * The reader's SELECTION API: the four verbs that move, read and act on
 * the reader's selection cursor.
 *
 * WHY THIS IS ITS OWN FILE. The cursor itself lives in archive-keys.js
 * (`createSelection`), which owns a count and an index and nothing else.
 * The reader owns the scroller. Neither owns the JOIN between them -
 * "move the cursor AND keep the selected row on screen AND repaint" -
 * and that join is what this file is. Splitting it out of the reader
 * shell keeps the shell under the 500-line cap without inventing a
 * seam: every function here reads the cursor and writes the scroller,
 * and nothing else in the reader does both.
 *
 * THE SELECTION IS A PURE INDEX CURSOR, never an element. That is what
 * lets it survive its own row being unmounted by the virtual window,
 * which happens constantly - rows are recycled on every paint. A
 * selection that held an element would go stale within one frame.
 *
 * NOTHING_SELECTED (-1) IS A THIRD OUTCOME, not a failure and not a
 * row. Every function here returns it rather than throwing or
 * returning 0, because "no row is selected" and "row zero is selected"
 * are different findings and a caller must be able to tell them apart.
 *
 * A MISSING CURSOR IS NOT AN ERROR HERE. The reader already logs the
 * missing dependency once, loudly, at construction. These functions
 * then degrade to NOTHING_SELECTED rather than throwing, so a page
 * with a broken script load still renders its transcript instead of
 * dying on the first keypress.
 *
 * Depends on archive-keys.js (for the cursor the reader passes in) and
 * archive-virtual-list.js (for scrollRowIntoView).
 * Exports window.ArchiveReaderSelect.
 */

console.log('[ArchiveReaderSelect Module] Loading...');

(function () {
    'use strict';

    /** @type {number} the selection cursor's value for "nothing". */
    var NOTHING_SELECTED = -1;

    /**
     * Build the selection API for one reader instance.
     *
     * @param {object} ctx the reader's own handles:
     *   `selection` the cursor from ArchiveKeys.createSelection(), or
     *     null when archive-keys.js did not load;
     *   `VL` window.ArchiveVirtualList;
     *   `list` the geometry engine this reader owns;
     *   `scroller()` returns the scroller element, or null before
     *     mount() has built it - it is a GETTER precisely because the
     *     element does not exist when this factory runs;
     *   `viewportHeight()` returns the scroller's usable height;
     *   `items()` returns the current grouped item array;
     *   `isExpandedAt(index)` whether that item is an expanded run;
     *   `setProgressExpanded(index, on)` toggles one run;
     *   `renderAnyway(index)` fetches a soft-gated body on request;
     *   `render()` repaints.
     * @returns {object} {selectedIndex, moveSelection, selectIndex,
     *   openSelected}
     * @example
     *   var sel = ArchiveReaderSelect.createSelectionApi(ctx);
     *   sel.moveSelection(1);   // the `j` key
     */
    function createSelectionApi(ctx) {
        /**
         * The selected item index, or NOTHING_SELECTED.
         * @returns {number}
         */
        function selectedIndex() {
            return ctx.selection ? ctx.selection.index() : NOTHING_SELECTED;
        }

        /**
         * Move the selection cursor and keep the selected row in view.
         * @param {number} delta rows, negative moves up
         * @returns {number} the new index, or NOTHING_SELECTED
         */
        function moveSelection(delta) {
            if (!ctx.selection) return NOTHING_SELECTED;
            var i = ctx.selection.move(delta);
            ctx.VL.scrollRowIntoView(ctx.list, ctx.scroller(), i,
                ctx.viewportHeight());
            ctx.render();
            return i;
        }

        /**
         * Select one row outright. select(-1) clears the selection.
         * @param {number} index @returns {number} the new index
         */
        function selectIndex(index) {
            if (!ctx.selection) return NOTHING_SELECTED;
            var i = ctx.selection.select(index);
            ctx.VL.scrollRowIntoView(ctx.list, ctx.scroller(), i,
                ctx.viewportHeight());
            ctx.render();
            return i;
        }

        /**
         * Open the selected row. A progress run TOGGLES - Enter on a
         * collapsed run is unambiguously "show me this" - and anything
         * else goes through renderAnyway, the existing "open this row"
         * verb. Nothing selected resolves null and says why.
         * @returns {Promise<?object>}
         */
        function openSelected() {
            var i = selectedIndex();
            var it = i >= 0 ? ctx.items()[i] : null;
            if (!it) {
                console.log('[ArchiveReader] openSelected: no row is selected ' +
                    '(index ' + i + '), so there is nothing to open.');
                return Promise.resolve(null);
            }
            if (it.kind === 'progress-run') {
                ctx.setProgressExpanded(i, !ctx.isExpandedAt(i));
                return Promise.resolve(null);
            }
            return ctx.renderAnyway(i);
        }

        return {
            selectedIndex: selectedIndex,
            moveSelection: moveSelection,
            selectIndex: selectIndex,
            openSelected: openSelected
        };
    }

    window.ArchiveReaderSelect = {
        createSelectionApi: createSelectionApi,
        NOTHING_SELECTED: NOTHING_SELECTED
    };
    console.log('[ArchiveReaderSelect Module] Exported as window.ArchiveReaderSelect');
})();
