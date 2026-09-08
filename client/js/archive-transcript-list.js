/**
 * Archive transcript list - a project's transcripts, or a corpus's
 * unattributed ones, paged by opaque keyset cursor.
 *
 * THE MEASURED PROBLEM THIS LIST HAS TO SURVIVE (live server, 2026-08-31).
 * 19,588 of 21,039 transcripts (93.1%) are `session_ref_scheme = 'agent'`
 * sidechain files; only 1,451 are `uuid`-scheme, and 19 of THOSE carry a
 * session_ref that is not a UUID at all (literal values like `audit` and
 * `journal`). Project 12 alone holds 3,416. Somebody looking for a
 * conversation they had is reading a list that is 93 percent noise.
 *
 * THE FILTER IS NOW SERVER-SIDE (`session_ref_scheme`, shipped
 * 2026-08-31). Choosing a scheme RELOADS the listing with the filter on
 * the request, so what comes back is the project narrowed, not the
 * fetched page narrowed, and paging continues inside the filtered set.
 * The honesty text had to change with it: leaving the old sentence
 * ("this filters what has been FETCHED, not the scope") would have made
 * the UI lie in the OTHER direction.
 *
 * WHAT IT STILL MUST NOT CLAIM. The filter matches on the
 * `session_ref_scheme` COLUMN and on nothing else, which does not prove
 * conversation-ness - see the 19 non-UUID `uuid`-scheme rows above. The
 * server ships that caveat in `meta.filters.session_ref_scheme_means`
 * and this file RENDERS the server's sentence rather than keeping a
 * second copy that can drift. Counts come from `meta.filters` and the
 * server labels them scoped, never corpus totals. An unknown scheme is a
 * 400 handled by `paintOutcome` like any other non-renderable envelope;
 * a known scheme matching nothing is a 200 with `matched_in_scope: 0`,
 * which is a different statement and renders differently.
 *
 * `session_ref` IS NOT AN IDENTITY. Measured: `journal` names 14
 * different transcripts, `audit` 5, `agent-a877057` 4. Every link, key
 * and callback here carries `transcript_id`; `session_ref` reaches the
 * DOM only as display text. This also drives the export filename
 * collision warning (archive-export.js).
 *
 * `has_more` IS A THREE-OUTCOME FIELD. The server returns `null` on
 * every failure path - measured, the budget_exhausted search answered
 * `"has_more": null`. `null` is not `false`. The load-more control is
 * rendered on `=== true` and nothing else; `null` renders "whether there
 * is more: NOT KNOWN". Treating null as false claims the end of a list
 * that was never read.
 *
 * UNATTRIBUTED AND cannot_determine ARE DIFFERENT AND ARE NEVER MERGED.
 * A transcript in the unattributed listing belongs to no PROJECT; one
 * with `host_attribution: "cannot_determine"` has no established HOST.
 * Either can be true without the other, and each gets its own badge.
 *
 * Does not read `result_status`. No innerHTML: session_ref and
 * source_path are file-derived strings.
 */

console.log('[ArchiveTranscriptList Module] Loading...');

(function () {
    'use strict';

    // The vocabulary and the pure row renderers live in
    // archive-tlist-row.js, which MUST load before this file. They are
    // re-exported below so ArchiveTranscriptList stays the one name a
    // caller has to know.
    var ROW = window.ArchiveTlistRow;
    if (!ROW) {
        console.error('[ArchiveTranscriptList] MISSING DEPENDENCY: ' +
            'window.ArchiveTlistRow. Load client/js/archive-tlist-row.js ' +
            'BEFORE this file; nothing below can render a row without it.');
    }
    var ROOT_CLASS = ROW.ROOT_CLASS;
    var PAGE_SIZE = ROW.PAGE_SIZE;
    var SCHEME_FILTERS = ROW.SCHEME_FILTERS;
    var SCHEME_DEFS = ROW.SCHEME_DEFS;
    var UNESTABLISHED_ATTRIBUTION = ROW.UNESTABLISHED_ATTRIBUTION;
    var el = ROW.el;
    var wireScheme = ROW.wireScheme;
    var describeFilter = ROW.describeFilter;
    var fmtCount = ROW.fmtCount;
    var isUnestablished = ROW.isUnestablished;
    var renderRow = ROW.renderRow;

    /**
     * Description: build the transcript list view.
     * Inputs: options (object) -
     *   document (Document), api (object) - exposing
     *     listArchiveTranscripts and listArchiveUnattributed.
     *   onSelect (function(transcriptId, row)).
     * Output: {element, load, loadMore, setSchemeFilter, rows}
     * Example:
     *   var list = ArchiveTranscriptList.create({api: window.API});
     *   await list.load({kind: 'project', id: 12, inScope: 3416});
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveTranscriptList.create needs a document');
        var api = opts.api;
        var onSelect = typeof opts.onSelect === 'function' ? opts.onSelect : function () {};

        var root = el(doc, 'section', ROOT_CLASS, null);
        var header = el(doc, 'div', ROOT_CLASS + '__header', null);
        var splitNote = el(doc, 'p', ROOT_CLASS + '__split-note', null);
        var rowList = el(doc, 'ul', ROOT_CLASS + '__rows', null);
        var footer = el(doc, 'div', ROOT_CLASS + '__footer', null);
        root.appendChild(header);
        root.appendChild(splitNote);
        root.appendChild(rowList);
        root.appendChild(footer);

        var scope = { kind: null, id: null, inScope: null };
        var rows = [];
        var nextCursor = null;
        var hasMore = null;
        var scheme = ROW.DEFAULT_SCHEME;
        // The server's meta.filters from the LAST renderable page. Null
        // until one arrives, so "no filter block" is distinguishable
        // from "a filter block saying applied:false".
        var filters = null;

        // The compact scheme chooser and the three fuzzy inputs live in
        // archive-tlist-filter.js. It emits changes and renders a header;
        // the rows, the cursor and the paging stay here.
        var filterUi = window.ArchiveTlistFilter.create({
            document: doc,
            rootClass: ROOT_CLASS,
            schemeDefs: SCHEME_DEFS,
            scheme: scheme,
            onScheme: function (v) { setSchemeFilter(v); },
            onQuery: function () { paint(); }
        });
        header.appendChild(filterUi.element);

        /**
         * Description: the rows to draw, after the CLIENT-side fuzzy
         *   filter, each with its match spans.
         *
         *   THE TWO FILTERS ARE APPLIED IN DIFFERENT PLACES ON PURPOSE.
         *   The scheme filter is the SERVER's and has already narrowed
         *   `rows` across the whole scope, so it is never re-applied
         *   here - a second, invisible copy of that rule could disagree
         *   with the counts the note quotes. The fuzzy filter is this
         *   client's and can only ever see what has been fetched, which
         *   is exactly what its own note says.
         * Inputs: none.
         * Output: Array<{row, spans}>.
         */
        function visibleRows() {
            var q = filterUi.queries();
            if (!window.ArchiveFuzzy || !window.ArchiveFuzzy.isActive(q)) {
                return rows.map(function (r) { return { row: r, spans: {} }; });
            }
            return window.ArchiveFuzzy.rank(rows, q, ROW.rowValue)
                .map(function (m) { return { row: m.row, spans: m.spans }; });
        }

        /**
         * Description: repaint the row list and both honesty notes.
         * Inputs: none. Output: void.
         */
        function paint() {
            rowList.textContent = '';
            var vis = visibleRows();
            for (var i = 0; i < vis.length; i++) {
                rowList.appendChild(renderRow(doc, vis[i].row, {
                    onSelect: onSelect,
                    unattributed: scope.kind === 'unattributed',
                    spans: vis[i].spans
                }));
            }
            splitNote.textContent = describeFilter(rows.length, filters);
            filterUi.setNote(vis.length, rows.length, hasMore);
            // A typed filter that matches nothing is an EMPTY result, and
            // it is rendered by the one outcome renderer like every other
            // empty - never as a blank pane, which is indistinguishable
            // from a list that has not loaded.
            if (vis.length === 0 && rows.length > 0 && filterUi.isActive()) {
                paintFilteredEmpty();
                return;
            }
            paintFooter();
        }

        /**
         * Description: render "your filter matched none of the loaded
         *   rows" through ArchiveOutcomeView, from a SYNTHESISED envelope
         *   carrying the same fields the server would send. It is not a
         *   special case and there is no second rendering path: the view
         *   cannot tell where the envelope came from.
         * Inputs: none. Output: void.
         */
        function paintFilteredEmpty() {
            footer.textContent = '';
            footer.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock({
                result: [],
                result_status: 'ok',
                scope_status: 'resolved',
                unevaluated: [],
                meta: {}
            }, { document: doc }));
            footer.appendChild(el(doc, 'p', ROOT_CLASS + '__end',
                'None of the ' + fmtCount(rows.length) + ' rows loaded so far' +
                ' match the name/ref/date filter. Rows on pages that have not' +
                ' been loaded were NOT searched.'));
        }

        /**
         * Description: render the paging footer. `has_more === true` is
         *   the ONLY input that produces a load-more control; `null`
         *   produces a stated unknown and `false` produces a stated end.
         * Inputs: none. Output: void.
         */
        function paintFooter() {
            footer.textContent = '';
            if (hasMore === true) {
                var more = el(doc, 'button', ROOT_CLASS + '__more',
                    'Load ' + PAGE_SIZE + ' more');
                more.setAttribute('type', 'button');
                more.setAttribute('data-action', 'load-more');
                more.addEventListener('click', function () { loadMore(); });
                footer.appendChild(more);
                return;
            }
            if (hasMore === false) {
                footer.appendChild(el(doc, 'p', ROOT_CLASS + '__end',
                    'End of the list. All ' + fmtCount(rows.length) +
                    ' rows in this scope have been loaded.'));
                return;
            }
            footer.appendChild(el(doc, 'p',
                ROOT_CLASS + '__end ' + ROOT_CLASS + '__end--unknown',
                'Whether there is more beyond these ' + fmtCount(rows.length) +
                ' rows: NOT KNOWN. The server did not answer has_more, which is' +
                ' not the same as answering that there is nothing more.'));
        }

        /**
         * Description: render a non-renderable envelope in place of the
         *   rows, or beneath them for `partial`.
         * Inputs: envelope (object|null), transportError (string|null),
         *         keepRows (boolean). Output: void.
         */
        function paintOutcome(envelope, transportError, keepRows) {
            if (!keepRows) rowList.textContent = '';
            footer.textContent = '';
            var block = window.ArchiveOutcomeView.renderOutcomeBlock(
                transportError ? null : envelope, { document: doc });
            if (transportError) {
                block.appendChild(el(doc, 'p', ROOT_CLASS + '__transport-reason',
                    transportError));
            }
            footer.appendChild(block);
        }

        /**
         * Description: apply one page response, appending rows.
         * Inputs: r (object) - a callEnvelope result.
         * Output: string - the outcome token.
         */
        function applyPage(r) {
            var envelope = r.transportError ? null : r.envelope;
            var classified = window.ArchiveOutcome.classify(envelope);
            if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                // The malformed-cursor case must NOT silently restart.
                // A client paging 3,416 rows that restarts on its own
                // renders duplicates forever, so the two recovery paths
                // are offered as explicit actions instead.
                paintOutcome(r.envelope, r.transportError, rows.length > 0);
                nextCursor = null;
                hasMore = null;
                return classified.token;
            }
            var page = (envelope && envelope.result) || [];
            rows = rows.concat(page);
            var meta = classified.meta || {};
            var paging = (meta.paging && typeof meta.paging === 'object') ? meta.paging : {};
            nextCursor = typeof paging.next_cursor === 'string' ? paging.next_cursor : null;
            hasMore = window.ArchiveOutcome.hasMore(envelope);
            filters = (meta.filters && typeof meta.filters === 'object')
                ? meta.filters : null;
            if (typeof meta.unattributed_transcript_count === 'number' &&
                    scope.kind === 'unattributed') {
                scope.inScope = meta.unattributed_transcript_count;
            }
            paint();
            if (classified.token === 'partial') {
                paintOutcome(envelope, null, true);
            }
            return classified.token;
        }

        /**
         * Description: issue one page request for the current scope.
         * Inputs: cursor (string|null). Output: Promise<object>.
         */
        function fetchPage(cursor) {
            if (scope.kind === 'unattributed') {
                // The unattributed route takes no scheme filter, so the
                // control is hidden for that scope rather than sent and
                // silently ignored - a control that does nothing is
                // worse than no control.
                return Promise.resolve(
                    api.listArchiveUnattributed(scope.id, { limit: PAGE_SIZE, cursor: cursor }));
            }
            return Promise.resolve(
                api.listArchiveTranscripts(scope.id, {
                    limit: PAGE_SIZE,
                    cursor: cursor,
                    sessionRefScheme: wireScheme(scheme)
                }));
        }

        /**
         * Description: start a NEW listing. Discards every prior row,
         *   because a scope change is a different question.
         * Inputs: next (object) - {kind: 'project'|'unattributed', id,
         *   inScope: number|null}.
         * Output: Promise<string> - the outcome token.
         */
        function load(next) {
            scope = {
                kind: (next && next.kind) || 'project',
                id: next && next.id,
                inScope: (next && typeof next.inScope === 'number') ? next.inScope : null
            };
            rows = [];
            nextCursor = null;
            hasMore = null;
            filters = null;
            rowList.textContent = '';
            footer.textContent = '';
            footer.appendChild(el(doc, 'p', ROOT_CLASS + '__loading', 'loading...'));
            return fetchPage(null).then(applyPage);
        }

        /**
         * Description: fetch the next page. Refuses when the previous
         *   response did not supply a cursor, rather than re-requesting
         *   page one, which would duplicate rows.
         * Inputs: none. Output: Promise<string>.
         */
        function loadMore() {
            if (!nextCursor) return Promise.resolve('no-cursor');
            return fetchPage(nextCursor).then(applyPage);
        }

        /**
         * Description: change the scheme filter and RE-QUERY the scope.
         *
         *   It reloads rather than repaints, because the filter is now
         *   the server's and the answer to "show me the conversations"
         *   is a different query, not a subset of the rows this list
         *   happens to hold. Repainting would silently return the old
         *   client-side behaviour while the honesty text claimed the
         *   whole scope had been filtered.
         *
         *   The cursor is discarded deliberately: a cursor minted under
         *   one filter positions inside that filter's result set, and
         *   replaying it under another would skip rows.
         * Inputs: value (string) - a SCHEME_FILTERS value.
         * Output: Promise<string> - the outcome token for the reload, or
         *   'no-scope' when nothing has been loaded yet.
         */
        function setSchemeFilter(value) {
            scheme = value || ROW.DEFAULT_SCHEME;
            // Painted BEFORE the reload, not after: the control must show
            // the choice the moment it is made, not when the server
            // answers. The reload can fail; the choice was still made.
            filterUi.setScheme(scheme);
            if (scope.id === null || scope.id === undefined) {
                return Promise.resolve('no-scope');
            }
            return load({ kind: scope.kind, id: scope.id, inScope: scope.inScope });
        }

        return {
            element: root,
            load: load,
            loadMore: loadMore,
            setSchemeFilter: setSchemeFilter,
            /**
             * Description: the scheme filter currently applied.
             * Inputs: none. Output: string - a SCHEME_FILTERS value.
             */
            scheme: function () { return scheme; },
            /**
             * Description: advance to the next scheme filter, which is
             *   what the `t` key does.
             *
             *   THE CYCLE ORDER LIVES HERE, not in the composition root,
             *   and that is the point: `SCHEME_DEFS` is also the order
             *   the three buttons are drawn in, so the keyboard order and
             *   the visual order are one declaration and cannot drift
             *   into disagreeing about what "next" means.
             *
             *   An unrecognised current value yields index -1, and -1 + 1
             *   is 0, so the cycle RESTARTS at 'all' rather than throwing
             *   or sticking. That is a deliberate recovery, not an
             *   accident of the arithmetic.
             * Inputs: none.
             * Output: Promise<string> - the reload's outcome token, from
             *   setSchemeFilter.
             * Example: cycleScheme()  // 'all' -> 'uuid' -> 'agent' -> 'all'
             */
            cycleScheme: function () {
                var at = -1;
                for (var i = 0; i < SCHEME_DEFS.length; i++) {
                    if (SCHEME_DEFS[i].v === scheme) { at = i; break; }
                }
                return setSchemeFilter(
                    SCHEME_DEFS[(at + 1) % SCHEME_DEFS.length].v);
            },
            /** Description: loaded rows, for tests. Output: Array. */
            rows: function () { return rows.slice(); },
            /** Description: the paging cursor, for tests. Output: string|null. */
            cursor: function () { return nextCursor; },
            /** Description: has_more as received. Output: boolean|null. */
            hasMore: function () { return hasMore; },
            /** Description: the filter header, for tests and for the
             *  composition root. Output: object. */
            filters: function () { return filterUi; },
            /** Description: the rows actually drawn, after the fuzzy
             *  filter. Output: Array<{row, spans}>. */
            visible: visibleRows
        };
    }

    window.ArchiveTranscriptList = {
        create: create,
        wireScheme: wireScheme,
        describeFilter: describeFilter,
        isUnestablished: isUnestablished,
        renderRow: renderRow,
        SCHEME_FILTERS: SCHEME_FILTERS,
        SCHEME_DEFS: SCHEME_DEFS,
        DEFAULT_SCHEME: ROW.DEFAULT_SCHEME,
        UNESTABLISHED_ATTRIBUTION: UNESTABLISHED_ATTRIBUTION,
        PAGE_SIZE: PAGE_SIZE,
        ROOT_CLASS: ROOT_CLASS
    };
    console.log('[ArchiveTranscriptList Module] Exported as window.ArchiveTranscriptList');
})();
