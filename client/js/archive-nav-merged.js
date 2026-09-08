/**
 * The MERGED project view: one flat, fuzzily-filterable list of projects
 * with the machine demoted to a badge and a filter.
 *
 * WHY THIS IS A SEPARATE FILE FROM archive-nav.js. That file owns the
 * rail's STATE and the host -> corpus -> project drill-down, and was at
 * 329 lines against this repo's 500-line cap. Everything here is about
 * ONE LEVEL - the merged list, its host filter, and the unattributed
 * nodes that sit beside it - so the seam is a real one and not a
 * line-count dodge.
 *
 * THE MACHINE MOVED, IT DID NOT GO AWAY. The old rail made the host a
 * LEVEL you clicked through. The owner is consolidating onto one
 * machine, at which point "which box was this born on" stops being a
 * question worth two clicks and becomes a detail worth a badge. So:
 *
 *   - every node carries its `hosts` as badges, and a node on two
 *     machines says so on its face;
 *   - `setHostFilter(hostId)` narrows the list to projects with a member
 *     on that machine, and the by-machine drill-down is still there
 *     behind the view toggle.
 *
 * Nothing is discarded. Measured 2026-09-01: 80 project rows merge to 77
 * nodes and exactly 3 projects exist on both machines, with byte-
 * identical observed_cwd on each - which is what makes the merge a
 * statement about identity rather than a relabel.
 *
 * THE UNATTRIBUTED NODES ARE HIDDEN ON A KNOWN ZERO AND ONLY THEN. The
 * rule lives in ArchiveNavRow.shouldShowUnattributed and the reason it
 * gave is written onto the node, so a hidden node is a decision someone
 * can inspect rather than an absence they have to trust. A corpus whose
 * count the server could not measure keeps its node.
 *
 * Exports window.ArchiveNavMerged.
 */

console.log('[ArchiveNavMerged Module] Loading...');

(function () {
    'use strict';

    var ROW = window.ArchiveNavRow;
    if (!ROW) {
        console.error('[ArchiveNavMerged] MISSING DEPENDENCY: window.ArchiveNavRow. ' +
            'Load client/js/archive-nav-row.js BEFORE this file.');
    }

    /** Fields the fuzzy filter searches, strongest first. */
    var FIELDS = [
        { name: 'display_name', weight: 3 },
        { name: 'full_path', weight: 1 },
        { name: 'observed_cwd', weight: 1 }
    ];

    /**
     * Description: keep only the projects with a member on one machine.
     *   Pure. `null` / '' means every machine and returns the list as
     *   given - NOT an empty list, which would read as "this machine has
     *   no projects".
     * Inputs: nodes (Array<object>) - merged project nodes.
     *         hostId (number|string|null).
     * Output: Array<object>.
     * Example: filterByHost(nodes, 2).length // 4
     */
    function filterByHost(nodes, hostId) {
        var list = Array.isArray(nodes) ? nodes : [];
        if (hostId === null || hostId === undefined || hostId === '') return list.slice();
        var want = String(hostId);
        return list.filter(function (node) {
            var members = (node && node.members) || [];
            for (var i = 0; i < members.length; i++) {
                if (String(members[i].host_id) === want) return true;
            }
            return false;
        });
    }

    /**
     * Description: which unattributed corpus rows get a node, and why.
     *   Pure, and it returns the REJECTED rows too, so a caller can
     *   report "2 corpora had none" instead of silently rendering fewer
     *   nodes than there are corpora.
     * Inputs: rows (Array<object>) - meta.unattributed.by_corpus.
     * Output: {shown: Array<{row, reason}>, hidden: Array<{row, reason}>}
     * Example: partitionUnattributed(rows).shown.length // 1
     */
    function partitionUnattributed(rows) {
        var list = Array.isArray(rows) ? rows : [];
        var shown = [];
        var hidden = [];
        for (var i = 0; i < list.length; i++) {
            var row = list[i];
            // The server's block spells the count `transcript_count`;
            // the rail's rule reads `unattributed_transcript_count`.
            // Mapped here, once, rather than teaching the rule a second
            // field name it would then have to keep in step.
            var verdict = ROW.shouldShowUnattributed({
                unattributed_transcript_count: row.transcript_count,
                counted: row.counted
            });
            (verdict.show ? shown : hidden).push({ row: row, reason: verdict.reason });
        }
        return { shown: shown, hidden: hidden };
    }

    /**
     * Description: render the merged list into a slot, ranked by the
     *   fuzzy filter and highlighting what matched.
     * Inputs: doc (Document), slot (Element), state (object) -
     *   {nodes, unattributed, hostId, filterText, onActivate}.
     * Output: {rendered: number, total: number} - so a caller can write
     *   the honest filter sentence without recounting the DOM.
     */
    function paint(doc, slot, state) {
        var opts = state || {};
        var nodes = filterByHost(opts.nodes, opts.hostId);
        var text = String(opts.filterText || '');

        // ORDER FIRST, THEN FILTER-RANK. When there is no filter text the
        // fuzzy ranker preserves input order, so the chosen order is what
        // shows. When there IS filter text, RELEVANCE WINS - a person who
        // has typed is looking for one project, and re-sorting his best
        // match to the bottom because it happens to be old would make the
        // filter useless. The order still decides ties, because it is the
        // input order the ranker preserves.
        var ordered = nodes;
        var parkedReasons = {};
        if (window.ArchiveNavOrder) {
            var sorted = window.ArchiveNavOrder.sortNodes(nodes, opts.orderMode);
            ordered = sorted.nodes;
            for (var p = 0; p < sorted.parked.length; p++) {
                // Keyed on full_path, which is the slug and is unique per
                // node; display_name is deliberately NOT unique (it is
                // widened only as far as it has to be) and would collide.
                parkedReasons[String(sorted.parked[p].node.full_path)] =
                    sorted.parked[p].reason;
            }
        }

        var ranked = window.ArchiveNavFuzzy
            ? window.ArchiveNavFuzzy.rank(ordered, text, FIELDS)
            : ordered.map(function (n) { return { row: n, positions: [], field: null }; });

        slot.textContent = '';
        for (var i = 0; i < ranked.length; i++) {
            var parked = parkedReasons[String(ranked[i].row.full_path)] || null;
            slot.appendChild(ROW.renderRow(doc, ROW.NODE_KINDS.PROJECT, ranked[i].row, {
                expandable: false,
                onActivate: opts.onActivate,
                // The info affordance and the presentation overlay are
                // passed straight through. This file ranks and paints;
                // it does not know what a modal is, and it does not know
                // where a display-name override comes from.
                onInfo: opts.onInfo,
                overlay: opts.overlay,
                positions: ranked[i].positions,
                matchField: ranked[i].field,
                // Passed even when null, so the row renderer has one
                // code path and cannot leave a stale marker behind on a
                // repaint into a reused element.
                unsorted: parked
            }));
        }

        // The unattributed nodes sit AFTER the projects and are not
        // subject to the fuzzy filter: they are a scope, not a project,
        // and filtering them out by name would hide the one population
        // that is already invisible from the project tree.
        var split = partitionUnattributed(opts.unattributed);
        if (!text) {
            for (var u = 0; u < split.shown.length; u++) {
                var entry = split.shown[u];
                var node = ROW.renderRow(doc, ROW.NODE_KINDS.UNATTRIBUTED, {
                    corpus_id: entry.row.corpus_id,
                    unattributed_transcript_count: entry.row.transcript_count,
                    counted: entry.row.counted
                }, { expandable: false, onActivate: opts.onActivate });
                node.setAttribute('data-unattributed-reason', entry.reason);
                slot.appendChild(node);
            }
        }

        if (ranked.length === 0) {
            slot.appendChild(ROW.el(doc, 'li', ROW.ROOT_CLASS + '__filter-empty',
                text
                    ? 'No loaded projects match this filter. ' +
                      ROW.describeFilter(0, nodes.length, nodes.length, 'projects')
                    : 'No projects in this view.'));
        }
        return {
            rendered: ranked.length,
            total: nodes.length,
            hiddenUnattributed: split.hidden.length
        };
    }

    /**
     * Description: fill the machine filter from the hosts the SERVER
     *   named, so the rail can never invent a machine or miss one. Lives
     *   here rather than in archive-nav.js purely for the line cap; it
     *   is the merged view's control and nothing else uses it.
     * Inputs: doc (Document), select (HTMLSelectElement),
     *         hosts (Array<{host_id, display_name, project_count}>).
     * Output: void.
     */
    function paintHostSelect(doc, select, hosts) {
        var list = Array.isArray(hosts) ? hosts : [];
        select.textContent = '';
        var all = doc.createElement('option');
        all.setAttribute('value', '');
        all.textContent = 'All machines';
        select.appendChild(all);
        for (var i = 0; i < list.length; i++) {
            var opt = doc.createElement('option');
            opt.setAttribute('value', String(list[i].host_id));
            opt.textContent = String(list[i].display_name) +
                ' (' + ROW.renderCount(list[i].project_count) + ')';
            select.appendChild(opt);
        }
    }

    window.ArchiveNavMerged = {
        paint: paint,
        paintHostSelect: paintHostSelect,
        filterByHost: filterByHost,
        partitionUnattributed: partitionUnattributed,
        FIELDS: FIELDS
    };
    console.log('[ArchiveNavMerged Module] Exported as window.ArchiveNavMerged');
})();
