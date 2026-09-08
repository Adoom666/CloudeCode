/**
 * The archive reader shell: composes archive-virtual-list.js,
 * archive-body-cache.js and archive-line-render.js into a scrollable
 * transcript view.
 *
 * WHAT THIS FILE OWNS, and nothing else does: the rAF loop, the paint
 * pipeline, the spine-to-items grouping and the outcome token. It owns
 * no geometry maths (the virtual list), no size policy (the body
 * cache), no markup for a row (the line renderer), no header markup
 * (archive-format.js), no state markup (archive-outcome-view.js) and no
 * interpretation of a server status (archive-outcome.js).
 *
 * FOUR SIBLINGS CARRY THE REST OF THE READER, and each is a policy this
 * file deliberately does not hold: archive-reader-dom.js owns the
 * element skeleton, the scroller geometry and the ONE delegated click
 * listener; archive-reader-paging.js owns the in-flight guard and the
 * pager button; archive-reader-select.js owns the selection verbs; and
 * archive-reader-body.js owns the body-request policy, which is the
 * security-relevant one - every body reaching a row passes through it.
 * All four MUST be loaded before this file.
 *
 * THE ANTI-JUMP CONTRACT IS HONOURED HERE OR NOWHERE. The virtual list
 * computes the pixel delta owed by rows above the viewport and pays it
 * in the same call, in the same animation frame, before paint.
 *
 * NO IntersectionObserver, AND NO PER-ROW LISTENERS. Rows are recycled
 * on every paint, so a listener bound to one would leak and go stale.
 * One delegated listener sits on the render window and resolves its
 * target out of the DOM.
 *
 * EVERY READER STATE IS ONE OF THE SIX OUTCOME TOKENS OR ONE OF THE TWO
 * NON-OUTCOME STATES, all rendered by archive-outcome-view.js. There is
 * no hand-rolled empty state and no hand-rolled error state.
 *
 * THE SCROLLBAR IS HONEST, AND IT IS ALREADY HONEST - DO NOT "FIX" IT.
 * Measured 2026-09-01 on /archive/t/5767 (30,805 lines, 500 loaded):
 * items 500, list.totalHeight() 109574.06, scroller.scrollHeight 109614,
 * i.e. 219.23 px per LOADED item. The spacer is sized from the running
 * sum over rows ACTUALLY LOADED, never from the transcript's declared
 * line count, so it never promises content nobody has, and it stays
 * honest as pages are appended for free because appending grows `items`
 * and regroup() reseeds the list. No page count is published here: over
 * variable-height rows a page count is a number nobody computed.
 *
 * Depends on archive-virtual-list.js, archive-body-cache.js,
 * archive-line-render.js, archive-outcome-view.js, archive-format.js,
 * archive-keys.js, and the four archive-reader-*.js siblings above.
 * Exports window.ArchiveReader.
 */

console.log('[ArchiveReader Module] Loading...');

(function () {
    'use strict';

    var ROOT_CLASS = 'archive-reader';

    // The pager's own vocabulary lives with the pager, and the
    // selection's with the selection. They are re-exported below so
    // ArchiveReader stays the one name a caller has to know.
    var PAGING = window.ArchiveReaderPaging;
    var SELECT = window.ArchiveReaderSelect;
    var BODY = window.ArchiveReaderBody;
    var DOM = window.ArchiveReaderDom;
    if (!PAGING || !SELECT || !BODY || !DOM) {
        console.error('[ArchiveReader] MISSING DEPENDENCY: archive-reader-' +
            'paging.js, archive-reader-select.js, archive-reader-body.js ' +
            'and archive-reader-dom.js must ALL load BEFORE ' +
            'archive-reader.js. The reader cannot be ' +
            'built without them; nothing below will work.');
    }

    var DEFAULT_PAGE_ROWS = PAGING.DEFAULT_PAGE_ROWS;
    var NOTHING_SELECTED = SELECT.NOTHING_SELECTED;

    /** @type {string} data-action on a collapsed progress run. */
    var ACTION_EXPAND = 'expand-progress';
    /** @type {string} data-action on an expanded progress run. */
    var ACTION_COLLAPSE = 'collapse-progress';
    var ACTION_LOAD_MORE = PAGING.ACTION_LOAD_MORE;

    /**
     * Build a reader bound to one document and one API.
     * @param {object} options `document` (required); `api` (passed to
     *   the body cache); `cache` (an existing cache, for tests);
     *   `requestAnimationFrame` (injectable scheduler, so the reconcile
     *   loop is testable without a browser); `overscan`; `pageRows`
     *   (default DEFAULT_PAGE_ROWS); `onLoadMore`.
     * @returns {object} the reader, see methods below
     * @example
     *   const reader = createReader({document, api});
     *   reader.mount(document.body);
     *   reader.setOnLoadMore(() => fetchNextPage());
     *   reader.setSpine(rows, false);
     */
    function createReader(options) {
        var opts = options || {};
        var doc = opts.document;
        if (!doc) throw new Error('createReader needs a document');

        var VL = window.ArchiveVirtualList;
        var LR = window.ArchiveLineRender;
        var OV = window.ArchiveOutcomeView;
        var AF = window.ArchiveFormat;
        var KEYS = window.ArchiveKeys;

        var raf = opts.requestAnimationFrame ||
            (typeof requestAnimationFrame === 'function'
                ? requestAnimationFrame
                : function (fn) { fn(); return 0; });

        var cache = opts.cache || (opts.api
            ? window.ArchiveBodyCache.createCache({ api: opts.api })
            : null);

        var list = VL.createList({ overscan: opts.overscan });

        // The selection is a PURE INDEX CURSOR - a count and an index,
        // never an element - which is why it survives its row being
        // unmounted by the virtual window. Its absence is named out
        // loud rather than degraded to silently.
        if (!KEYS || typeof KEYS.createSelection !== 'function') {
            console.error('[ArchiveReader] MISSING DEPENDENCY: ' +
                'window.ArchiveKeys.createSelection. Rows render with no ' +
                'selection and moveSelection/openSelected do nothing. ' +
                'Load client/js/archive-keys.js before this file.');
        }
        var selection = (KEYS && typeof KEYS.createSelection === 'function')
            ? KEYS.createSelection() : null;

        var pageRows = Number.isFinite(opts.pageRows) && opts.pageRows > 0
            ? Math.floor(opts.pageRows) : DEFAULT_PAGE_ROWS;

        var items = [];        // spine rows plus collapsed progress runs
        var spine = [];        // raw spine, kept so an expand can re-group
        var spineComplete = false;  // false renders a named sentinel row
        // EXPANSIONS ARE KEYED BY THE RUN'S `from` LINE NUMBER, NOT BY
        // ITEM INDEX. An append can re-group - a progress run at the end
        // of the old spine merges with progress rows at the start of the
        // new page and every later index shifts - so an index key would
        // silently land somebody's expansions on the wrong rows. A run's
        // `from` is its first row's line_no, which appending cannot move.
        var expanded = Object.create(null);
        var token = 'idle';    // an outcome token, or idle / loading
        var envelope = null;   // the envelope backing a failure token
        var header = null;     // header facts, from a separate request
        var onLoadMore = typeof opts.onLoadMore === 'function'
            ? opts.onLoadMore : null;

        // DOM handles, all null until mount(). `window_` is the
        // translated render window; `spacer` carries the honest height.
        var root = null, scroller = null, spacer = null;
        var window_ = null, statusEl = null;
        var frameQueued = false, destroyed = false;

        /**
         * Whether the item at `index` is an EXPANDED progress run.
         * @param {number} index @returns {boolean}
         */
        function isExpandedAt(index) {
            var it = items[index];
            return !!(it && it.kind === 'progress-run' && expanded[it.from]);
        }

        // THE SUB-API CONTEXT. One object, three consumers, all reading
        // through GETTERS rather than captured values, because `items`
        // is reassigned by every regroup() and the DOM handles do not
        // exist until mount(). A captured value would go stale on the
        // first append and nothing would say so.
        var ctx = {
            selection: selection, VL: VL, list: list, cache: cache,
            rootClass: ROOT_CLASS, pageRows: pageRows,
            document: doc,
            actionLoadMore: ACTION_LOAD_MORE,
            actionExpand: ACTION_EXPAND,
            actionCollapse: ACTION_COLLAPSE,
            el: function (tag, cls, text) { return el(tag, cls, text); },
            requestMoreLines: function () { return requestMoreLines(); },
            scroller: function () { return scroller; },
            items: function () { return items; },
            onLoadMore: function () { return onLoadMore; },
            spineComplete: function () { return spineComplete; },
            viewportHeight: function () { return viewportHeight(); },
            isExpandedAt: function (i) { return isExpandedAt(i); },
            setProgressExpanded: function (i, on) { setProgressExpanded(i, on); },
            renderAnyway: function (i) { return renderAnyway(i); },
            schedule: function () { schedule(); },
            render: function () { render(); }
        };

        var shell = DOM.createShell(ctx);
        var el = shell.el;
        var viewportHeight = shell.viewportHeight;
        var scrollTop = shell.scrollTop;
        var pager = PAGING.createPager(ctx);
        var selectApi = SELECT.createSelectionApi(ctx);
        var bodyPolicy = BODY.createBodyPolicy(ctx);

        var entryFor = bodyPolicy.entryFor;
        var requestBodies = bodyPolicy.requestBodies;
        var renderAnyway = bodyPolicy.renderAnyway;
        var requestMoreLines = pager.requestMoreLines;
        var pagerButton = pager.pagerButton;
        var selectedIndex = selectApi.selectedIndex;

        /**
         * Rebuild `items` from `spine`, reseed the list's estimates, and
         * tell the selection cursor how many rows exist.
         * @returns {void}
         */
        function regroup() {
            items = LR.groupRows(spine);
            list.setCount(items.length, function (i) {
                var it = items[i];
                if (it && it.kind === 'progress-run' && isExpandedAt(i)) {
                    // An expanded run is its children stacked; the real
                    // height is measured next frame like any other row.
                    var sum = 0;
                    for (var k = 0; k < it.rows.length; k++) {
                        sum += VL.estimateHeight(it.rows[k]);
                    }
                    return sum + VL.PROGRESS_ROW_PX;
                }
                return VL.estimateHeight(it);
            });
            // setCount PRESERVES the index when the count grows, so an
            // appended page never moves somebody's selection.
            if (selection) selection.setCount(items.length);
        }

        /**
         * Render the rows in the window into the DOM.
         * @param {object} win a windowFor() result @returns {void}
         */
        function paint(win) {
            while (window_.firstChild) window_.removeChild(window_.firstChild);
            window_.style.transform = 'translateY(' + win.offsetTop + 'px)';
            spacer.style.height = win.totalHeight + 'px';

            var sel = selectedIndex();
            for (var i = win.first; i <= win.last; i++) {
                var it = items[i];
                if (!it) continue;
                var node = LR.renderItem(doc, it, entryFor(it), {
                    index: i, expanded: isExpandedAt(i), entryFor: entryFor
                });
                // Roving tabindex: exactly one RENDERED row is reachable
                // by Tab. A selection outside the window puts neither
                // attribute anywhere, which is correct - the cursor
                // still holds the index and the row gets both back when
                // it next paints.
                node.setAttribute('tabindex', i === sel ? '0' : '-1');
                if (i === sel) node.setAttribute('data-selected', 'true');
                window_.appendChild(node);
            }

            if (!spineComplete) {
                // A partial spine ends in a named sentinel, not in a
                // silent stop. A list that just ends looks complete.
                window_.appendChild(el('p', ROOT_CLASS + '__sentinel',
                    'More lines not loaded yet. ' + spine.length +
                    ' of this transcript loaded so far.'));
                // NO CALLBACK, NO BUTTON: a control that cannot do
                // anything is worse than the sentence alone, because it
                // offers a way forward that does not exist.
                if (onLoadMore) window_.appendChild(pagerButton());
            }
        }

        /**
         * Pay the anti-jump debt for the window just painted and write
         * the corrected total onto the spacer, which this file owns.
         * @param {object} win @returns {number} the delta applied
         */
        function reconcile(win) {
            var r = VL.reconcileMeasured(list, window_, win, scroller);
            if (r.applied > 0) spacer.style.height = r.totalHeight + 'px';
            return r.delta;
        }

        /**
         * Render the current state. Only `ok` reaches the virtual list.
         * @returns {void}
         */
        function render() {
            if (destroyed || !root) return;
            renderHeader();

            if (token === 'ok') {
                statusEl.setAttribute('data-reader-state', 'ok');
                while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
                scroller.style.display = '';
                var win = list.windowFor(scrollTop(), viewportHeight());
                paint(win);
                requestBodies(win);
                reconcile(win);
                return;
            }

            scroller.style.display = 'none';
            statusEl.setAttribute('data-reader-state', token);
            while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
            statusEl.appendChild(OV.renderReaderState(token, envelope, {
                document: doc, rootClass: ROOT_CLASS,
                deadlineMs: window.ArchiveState.DEADLINES_MS.transcript
            }));
        }

        /**
         * Put the header facts into the header box. The markup is
         * archive-format.js's business; the box is ours.
         * @returns {void}
         */
        function renderHeader() {
            var box = root.querySelector('.' + ROOT_CLASS + '__header');
            if (!box) return;
            while (box.firstChild) box.removeChild(box.firstChild);
            var facts = AF.renderTranscriptHeader(doc, header, ROOT_CLASS);
            if (facts) box.appendChild(facts);
        }

        /**
         * Queue one render on the next animation frame. Coalescing keeps
         * a burst of resolved body fetches from repainting once each.
         * @returns {void}
         */
        function schedule() {
            if (destroyed || frameQueued) return;
            frameQueued = true;
            raf(function () {
                frameQueued = false;
                render();
            });
        }

        /**
         * Attach the reader to a host element. The skeleton is built by
         * archive-reader-dom.js; the handles are copied out BEFORE the
         * first render, because render() bails on a null root and would
         * otherwise draw nothing on mount.
         * @param {Element} host @returns {Element} the reader root
         */
        function mount(host) {
            var built = shell.mount(host);
            var h = shell.handles();
            root = h.root; scroller = h.scroller; spacer = h.spacer;
            window_ = h.window_; statusEl = h.statusEl;
            render();
            return built;
        }

        /**
         * Install the spine. NORMATIVE: `complete` is a three-outcome
         * input in disguise - an incomplete spine renders a named
         * sentinel rather than simply ending, because a list that just
         * stops looks finished.
         * @param {Array<object>} rows @param {boolean} complete
         * @returns {void}
         */
        function setSpine(rows, complete) {
            spine = Array.isArray(rows) ? rows : [];
            spineComplete = complete === true;
            // A NEW transcript, so nothing opened in the old one
            // applies. appendSpine deliberately does the opposite.
            expanded = Object.create(null);
            regroup();
            token = spine.length ? 'ok' : token;
            render();
        }

        /**
         * Append a page of spine rows. `complete === true` is the ONLY
         * input that marks the spine complete, exactly as in setSpine.
         *
         * EXPANSIONS ARE NOT RESET. setSpine resets them because that is
         * a NEW transcript; an append is the SAME transcript, and
         * throwing away the runs a person opened is data loss from their
         * point of view. `expanded` is keyed by the run's `from` line
         * number precisely so this is safe across the re-group; see the
         * declaration above. The SELECTION is not reset either -
         * regroup() calls selection.setCount(), which preserves the
         * index whenever the count grows.
         *
         * SECRET MASKING: appended rows go through the same regroup() ->
         * paint() -> entryFor() -> cache.request() path as the first
         * page, so archive-mask.js applies to them identically. Any
         * future shortcut that renders an appended body WITHOUT going
         * through entryFor would be a credential-disclosure path.
         *
         * @param {Array<object>} rows the next page of spine rows
         * @param {boolean} complete true only when this was the last page
         * @returns {number} the new raw spine length
         */
        function appendSpine(rows, complete) {
            var add = Array.isArray(rows) ? rows : [];
            for (var i = 0; i < add.length; i++) spine.push(add[i]);
            spineComplete = complete === true;
            regroup();
            token = spine.length ? 'ok' : token;
            render();
            return spine.length;
        }

        /**
         * Install the callback one paging request invokes. Without one,
         * no pager control is rendered at all.
         * @param {?function(): Promise} fn @returns {void}
         */
        function setOnLoadMore(fn) {
            onLoadMore = typeof fn === 'function' ? fn : null;
            render();
        }

        /**
         * Set the reader's outcome state.
         * @param {string} newToken an outcome token, or 'idle'/'loading'
         * @param {?object} env the envelope backing a failure
         * @returns {void}
         */
        function setToken(newToken, env) {
            token = String(newToken);
            envelope = env || null;
            render();
        }

        /**
         * Set the transcript header facts.
         * @param {?object} h @returns {void}
         */
        function setHeader(h) { header = h || null; render(); }

        /**
         * Expand or collapse one progress run by item index. Expansion is
         * an ordinary height correction. A non-run index is a no-op:
         * there is no run there to key.
         * @param {number} index @param {boolean} on @returns {void}
         */
        function setProgressExpanded(index, on) {
            var it = items[index];
            if (!it || it.kind !== 'progress-run') return;
            if (on) expanded[it.from] = true; else delete expanded[it.from];
            regroup();
            render();
        }

        return {
            mount: mount,
            setSpine: setSpine,
            appendSpine: appendSpine,
            setOnLoadMore: setOnLoadMore,
            requestMoreLines: requestMoreLines,
            setToken: setToken,
            setHeader: setHeader,
            setProgressExpanded: setProgressExpanded,
            renderAnyway: renderAnyway,
            moveSelection: selectApi.moveSelection,
            selectIndex: selectApi.selectIndex,
            selectedIndex: selectApi.selectedIndex,
            openSelected: selectApi.openSelected,
            schedule: schedule,
            render: render,
            list: list,      // the geometry engine, for callers and tests
            cache: cache,    // the body cache, for callers and tests
            /** The selection cursor. @returns {?object} */
            selection: function () { return selection; },
            /** Is a page in flight. @returns {boolean} */
            isLoadingMore: pager.isLoadingMore,
            /** The raw loaded spine rows. @returns {Array} */
            spine: function () { return spine; },
            /** Grouped items currently laid out. @returns {Array} */
            items: function () { return items; },
            /** Current token. @returns {string} */
            token: function () { return token; },
            /** The reader root element. @returns {?Element} */
            root: function () { return root; },
            /** Detach and stop scheduling. @returns {void} */
            destroy: function () {
                destroyed = true;
                shell.destroy();
                root = null;
            }
        };
    }

    window.ArchiveReader = {
        createReader: createReader,
        ROOT_CLASS: ROOT_CLASS,
        FALLBACK_VIEWPORT_PX: DOM.FALLBACK_VIEWPORT_PX,
        DEFAULT_PAGE_ROWS: DEFAULT_PAGE_ROWS,
        ACTION_LOAD_MORE: ACTION_LOAD_MORE,
        ACTION_EXPAND: ACTION_EXPAND,
        ACTION_COLLAPSE: ACTION_COLLAPSE,
        PAGE_NO_PAGER: PAGING.PAGE_NO_PAGER,
        PAGE_COMPLETE: PAGING.PAGE_COMPLETE,
        PAGE_FAILED: PAGING.PAGE_FAILED
    };
    console.log('[ArchiveReader Module] Exported as window.ArchiveReader');
})();
