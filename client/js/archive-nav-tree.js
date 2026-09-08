/**
 * THE BY-MACHINE DRILL-DOWN'S DOM HELPERS - finding a node's child slot,
 * appending the partial banner after the rows that did arrive, and giving
 * every expanded corpus its unattributed node.
 *
 * WHY THIS IS A SEPARATE FILE FROM archive-nav.js. All three are PURE
 * FUNCTIONS OF THEIR ARGUMENTS once the rail's state is passed in, and
 * archive-nav.js is the rail's STATE - the fetches, the filter text, the
 * per-level row cache, the active view. Adding the project-card info
 * modal took that file past this repo's 500-line cap, and these three
 * were the part of it that never needed the state at all: each one was
 * only reaching into the closure for `doc`, `root` or the `loaded` map.
 * Handing those in turns three closures into three testable functions.
 *
 * THE PARTIAL BANNER IS APPENDED, NEVER SUBSTITUTED. A `partial` envelope
 * carries rows AND an admission that the server did not finish looking.
 * Dropping the rows to show the banner hides what did come back; dropping
 * the banner claims the list is complete. Both are rendered, in that
 * order, which is why this is an append and not a replace.
 *
 * THE UNATTRIBUTED NODE IS APPENDED TO EVERY EXPANDED CORPUS, ALWAYS -
 * including when its count is 0 and when no count was reported. Whether
 * it is then SHOWN is ArchiveNavRow.shouldShowUnattributed's decision and
 * is not re-litigated here.
 *
 * Exports window.ArchiveNavTree.
 */

console.log('[ArchiveNavTree Module] Loading...');

(function () {
    'use strict';

    var ROW = window.ArchiveNavRow;
    if (!ROW) {
        console.error('[ArchiveNavTree] MISSING DEPENDENCY: window.ArchiveNavRow. ' +
            'Load client/js/archive-nav-row.js BEFORE this file.');
    }

    /**
     * Description: find one rendered node's child slot in the tree.
     * Inputs: root (Element) - the rail root. kind (string) - a
     *   NODE_KINDS value. id (number|string).
     * Output: Element|null - null when the node is not on screen, which
     *   is a real state (the view may be the merged list) and not an
     *   error.
     * Example: slotFor(root, 'host', 2)
     */
    function slotFor(root, kind, id) {
        var nodes = root.querySelectorAll('[data-node-kind="' + kind + '"]');
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-node-id') === String(id)) {
                return nodes[i].querySelector('.' + ROW.ROOT_CLASS + '__children');
            }
        }
        return null;
    }

    /**
     * Description: append the partial banner AFTER the rows that did
     *   arrive, so both are visible at once.
     * Inputs: doc (Document), slot (Element), envelope (object).
     * Output: void.
     */
    function renderPartialTail(doc, slot, envelope) {
        var tail = ROW.el(doc, 'li', ROW.ROOT_CLASS + '__outcome', null);
        tail.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock(
            envelope, { document: doc }));
        slot.appendChild(tail);
    }

    /**
     * Description: give the no-project transcripts a visible node. The
     *   count comes from the corpora listing, where the server already
     *   reported it; a corpus this rail has not listed yields a node with
     *   NO count, which renders NOT KNOWN rather than 0.
     * Inputs: doc (Document), slot (Element),
     *   corpora (Array<object>) - every corpus row this rail has loaded,
     *   corpusId (number|string), onActivate (function).
     * Output: void.
     */
    function appendUnattributedNode(doc, slot, corpora, corpusId, onActivate) {
        var list = Array.isArray(corpora) ? corpora : [];
        var match = null;
        for (var i = 0; i < list.length; i++) {
            if (String(list[i].corpus_id) === String(corpusId)) match = list[i];
        }
        slot.appendChild(ROW.renderRow(doc, ROW.NODE_KINDS.UNATTRIBUTED,
            match || { corpus_id: corpusId },
            { expandable: false, onActivate: onActivate }));
    }

    window.ArchiveNavTree = {
        slotFor: slotFor,
        renderPartialTail: renderPartialTail,
        appendUnattributedNode: appendUnattributedNode
    };
    console.log('[ArchiveNavTree Module] Exported as window.ArchiveNavTree');
})();
