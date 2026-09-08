/**
 * Archive navigation rail - hosts, corpora, projects, and the
 * unattributed bucket, with a filter over the rows already fetched.
 *
 * WHAT THIS FILE IS FOR. The corpus is 2 hosts, 3 corpora, 80 projects
 * and 21,039 transcripts (measured live 2026-08-31). There is no global
 * search, so this tree is the ONLY way into the archive, and a level
 * that fails quietly is a level nobody can get past.
 *
 * THREE RULES THIS FILE EXISTS TO KEEP.
 *
 *   1. A FAILED BRANCH NEVER COLLAPSES INTO A LEAF. When expanding a
 *      host answers cannot_determine, that host renders the COULD NOT
 *      EVALUATE block INLINE, at that node, and every sibling stays
 *      usable. A branch that quietly renders as childless is a claim
 *      that the host has no corpora - a verdict nobody measured.
 *
 *   2. THE UNATTRIBUTED BUCKET IS ALWAYS GIVEN A SHAPE. Corpus 2
 *      reports unattributed_transcript_count: 5 (measured). Those five
 *      belong to no project and are therefore INVISIBLE from the project
 *      tree by construction - the same never-onboarded blind spot that
 *      makes a thing safe from every check because no check can see it.
 *      The rail renders them as their own node, always, including when
 *      the count is 0 (it says 0) and when the server did not report one
 *      (it says NOT KNOWN).
 *
 *   3. "UNATTRIBUTED" AND "host_attribution: cannot_determine" ARE
 *      DIFFERENT CONDITIONS AND ARE NEVER MERGED. The first is a
 *      transcript with no project, the second one whose HOST link was
 *      not established. Either can be true without the other, so
 *      merging them invents a population that does not exist.
 *
 * THE FILTER IS HONEST ABOUT ITS OWN SCOPE, in words, every time it is
 * non-empty: "filter matches 3 of 71 loaded projects. This filters rows
 * already fetched, not the whole corpus." A filter that reads like a
 * search of the corpus is a false green with a text box on it.
 *
 * Does not read `result_status`, `scope_status` or `scan.status`. Every
 * envelope goes to ArchiveOutcomeView.renderOutcomeBlock, which routes
 * through archive-outcome.js, the client's only status interpreter.
 *
 * No innerHTML anywhere: host 2's display_name carries a real U+2019
 * and slugs are filesystem paths.
 */

console.log('[ArchiveNav Module] Loading...');

(function () {
    'use strict';

    // The vocabulary and the pure row renderers live in
    // archive-nav-row.js, which MUST load before this file. They are
    // re-exported below so ArchiveNav stays the one name a caller has
    // to know.
    var ROW = window.ArchiveNavRow;
    if (!ROW) {
        console.error('[ArchiveNav] MISSING DEPENDENCY: window.ArchiveNavRow. ' +
            'Load client/js/archive-nav-row.js BEFORE this file; the rail ' +
            'cannot render a single row without it.');
    }
    var ROOT_CLASS = ROW.ROOT_CLASS;
    var NODE_KINDS = ROW.NODE_KINDS;
    var NOT_KNOWN = ROW.NOT_KNOWN;
    var el = ROW.el;
    var renderCount = ROW.renderCount;
    var filterRows = ROW.filterRows;
    var describeFilter = ROW.describeFilter;
    var labelFor = ROW.labelFor;
    var idFor = ROW.idFor;
    var countFor = ROW.countFor;
    var renderRow = ROW.renderRow;
    var renderOutcomeInto = ROW.renderOutcomeInto;
    // The drill-down's DOM helpers, extracted to keep this file under the
    // 500-line cap. Same hard-dependency contract as ROW above.
    var TREE = window.ArchiveNavTree;
    if (!TREE) {
        console.error('[ArchiveNav] MISSING DEPENDENCY: window.ArchiveNavTree. ' +
            'Load client/js/archive-nav-tree.js BEFORE this file.');
    }

    /**
     * Description: build the navigation rail.
     * Inputs: options (object) -
     *   document (Document) - defaults to window.document.
     *   api (object) - anything exposing listArchiveHosts,
     *     listArchiveCorpora, listArchiveProjects. Injected so tests do
     *     not need a network.
     *   onSelect (function(kind, id, row)) - called when a leaf or the
     *     unattributed node is chosen.
     * Output: {element, loadHosts, expand, setFilter, rowsLoaded}
     * Example:
     *   var nav = ArchiveNav.create({api: window.API, onSelect: fn});
     *   document.body.appendChild(nav.element);
     *   await nav.loadHosts();
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveNav.create needs a document');
        var api = opts.api;
        var onSelect = typeof opts.onSelect === 'function' ? opts.onSelect : function () {};

        var root = el(doc, 'nav', ROOT_CLASS, null);
        root.setAttribute('aria-label', 'Archive navigation');

        var filterInput = el(doc, 'input', ROOT_CLASS + '__filter', null);
        filterInput.setAttribute('type', 'search');
        filterInput.setAttribute('placeholder', 'filter projects (fuzzy)');
        filterInput.setAttribute('aria-label', 'Filter the rows already loaded');

        // NO VIEW BAR. The "All projects" / "By machine" buttons and the
        // machine dropdown were REMOVED at the owner's instruction:
        // "i dont think we need the button and dropdown on the left
        // column." Merged is now the only view anyone can reach by
        // clicking, which is what it was in practice - the machine a
        // project was born on is not how anyone navigates to it, and it
        // is already carried as a badge on each row.
        //
        // THE BY-MACHINE TREE IS NOT DELETED, only unexposed. `setView`,
        // `view()` and `setHostFilter()` still work and are still
        // reachable from a deep link and from tests, so nothing about
        // the data path changed. Deleting the tree as well would have
        // been a second, larger decision the owner did not ask for, and
        // it would have taken `hostList` and its paging with it.

        /**
         * THE ORDER CONTROL - a real <select>, matching the rail's other
         * dropdown. Options, persistence and comparators all live in
         * archive-nav-order.js. The stored choice is read ONCE here, not
         * per paint, which would hit localStorage on every keystroke in
         * the filter box.
         */
        var order = window.ArchiveNavOrder
            ? window.ArchiveNavOrder.mount(doc, {
                onChange: function (mode) { orderMode = mode; paintMerged(); }
            })
            : null;
        var orderMode = order ? order.value()
            : (window.ArchiveNavOrder ? window.ArchiveNavOrder.DEFAULT_MODE : 'recent');

        var filterNote = el(doc, 'p', ROOT_CLASS + '__filter-note', null);
        var hostList = el(doc, 'ul', ROOT_CLASS + '__level ' + ROOT_CLASS + '__level--hosts', null);
        var mergedList = el(doc, 'ul', ROOT_CLASS + '__level ' + ROOT_CLASS + '__level--merged', null);
        root.appendChild(filterInput);
        if (order) root.appendChild(order.element);
        root.appendChild(filterNote);
        root.appendChild(mergedList);
        root.appendChild(hostList);
        hostList.setAttribute('hidden', 'hidden');

        /** Rows fetched per level, keyed 'hosts' | 'corpora:<id>' | 'projects:<id>'. */
        var loaded = {};
        /** Server-declared totals per level key, for the honest filter note. */
        var totals = {};
        var filterText = '';
        /** 'merged' (default) or 'hosts'. */
        var view = 'merged';
        /** Merged project nodes, their unattributed rows, and the host filter. */
        var merged = { nodes: [], unattributed: [], hosts: [] };
        var hostFilter = null;
        var lastPaint = { rendered: 0, total: 0 };
        /**
         * A CLIENT-SIDE presentation overlay, only for a build with no
         * overlay endpoint. The real contract arrives ON each node from
         * GET /archive/overlay/projects and is read by
         * ArchiveNavCard.presentationFor; this rail never writes either.
         */
        var overlay = opts.overlay ||
            (typeof window !== 'undefined' ? window.ArchiveProjectOverlay : null) || null;
        /** The info-modal opener; its single-instance rule lives in
         *  archive-nav-info.js. Built lazily - the module may be absent. */
        var info = null;

        /**
         * Description: open the project info modal.
         * Inputs: row (object), pres (object|null).
         * Output: object|null - null when the modal module is not in this
         *   build. NOT thrown: an exception on a click inside the only way
         *   into the archive leaves a pane that looks like an empty archive.
         */
        function openInfo(row, pres) {
            if (!window.ArchiveNavInfo) {
                console.error('[ArchiveNav] window.ArchiveNavInfo is missing: load ' +
                    'client/js/archive-nav-info.js');
                return null;
            }
            if (!info) {
                info = window.ArchiveNavInfo.wire({
                    document: doc,
                    overlay: overlay,
                    onFilterHost: function (hostId) {
                        hostFilter = (hostId === null || hostId === undefined ||
                                      hostId === '') ? null : String(hostId);
                        paintMerged();
                    }
                });
            }
            return info.open(row, pres);
        }

        /**
         * Description: render one level's rows into a slot, applying the
         *   active filter and stating what it filtered.
         * Inputs: slot (Element), key (string), kind (string),
         *         rows (Array), fields (Array<string>), expandable (bool)
         * Output: void.
         */
        function paint(slot, key, kind, rows, fields, expandable) {
            slot.textContent = '';
            var visible = filterRows(rows, filterText, fields);
            if (filterText && visible.length === 0) {
                // Not an outcome block: nothing was measured here, the
                // person's own filter excluded everything. Saying that
                // is different from "the server returned nothing".
                var none = el(doc, 'li', ROOT_CLASS + '__filter-empty',
                    'No loaded rows match this filter. ' +
                    describeFilter(0, rows.length, totals[key] || null, kind + 's'));
                slot.appendChild(none);
                return;
            }
            for (var i = 0; i < visible.length; i++) {
                slot.appendChild(renderRow(doc, kind, visible[i], {
                    expandable: expandable,
                    onActivate: activate,
                    // The drill-down renders projects too, and they are the
                    // same card. Its rows carry no `hosts`, which is the
                    // case the modal reports as COULD NOT EVALUATE.
                    onInfo: openInfo, overlay: overlay
                }));
            }
        }

        /**
         * Description: handle a click on any row: expand a container,
         *   or hand a leaf to the caller.
         * Inputs: kind (string), id (number|string), row (object).
         * Output: void.
         */
        function activate(kind, id, row) {
            if (kind === NODE_KINDS.HOST || kind === NODE_KINDS.CORPUS) {
                expand(kind, id);
                return;
            }
            onSelect(kind, id, row);
        }

        /**
         * The by-machine drill-down, in its own file since this one hit
         * the 500-line cap. `loaded` and `totals` are handed over as the
         * rail's own objects, not copies - see archive-nav-drill.js.
         */
        var drill = window.ArchiveNavDrill
            ? window.ArchiveNavDrill.create({
                doc: doc, root: root, api: api, hostList: hostList,
                el: el, ROOT_CLASS: ROOT_CLASS, NODE_KINDS: NODE_KINDS,
                TREE: TREE, loaded: loaded, totals: totals,
                paint: paint, renderOutcomeInto: renderOutcomeInto,
                activate: function (k, i, r) { return activate(k, i, r); }
            })
            : null;

        /** The three drill-down entry points. A missing module is
         *  RENDERED, never thrown - a TypeError here would take the rail
         *  down on a click, leaving a blank pane. */
        function loadHosts() {
            if (drill) return drill.loadHosts();
            renderOutcomeInto(doc, hostList, null,
                'window.ArchiveNavDrill is missing: load ' +
                'client/js/archive-nav-drill.js before archive-nav.js');
            return Promise.resolve('transport-error');
        }
        function expand(kind, id) {
            return drill ? drill.expand(kind, id) : Promise.resolve('not-found');
        }
        function loadedCorpora() { return drill ? drill.loadedCorpora() : []; }

        /**
         * Description: re-filter every level already on screen.
         * Inputs: text (string). Output: void.
         */
        function setFilter(text) {
            filterText = String(text === null || text === undefined ? '' : text);
            if (view === 'merged') { paintMerged(); return; }
            if (loaded.hosts) {
                paint(hostList, 'hosts', NODE_KINDS.HOST, loaded.hosts,
                      ['display_name', 'hostname'], true);
            }
            filterNote.textContent = filterText
                ? describeFilter(filterRows(loaded.hosts || [], filterText,
                        ['display_name', 'hostname']).length,
                    (loaded.hosts || []).length, totals.hosts || null, 'hosts')
                : '';
        }

        /**
         * Description: fetch the merged project list - the default view.
         *   One request, not paginated: 77 nodes, and a page of a merged
         *   tree would let someone conclude a project lives on one
         *   machine because the row proving otherwise fell on page 2.
         * Inputs: none. Output: Promise<string> - the outcome token.
         */
        function loadMergedProjects() {
            if (typeof api.listArchiveMergedProjects !== 'function') {
                renderOutcomeInto(doc, mergedList, null,
                    'api.listArchiveMergedProjects is not available in this build');
                return Promise.resolve('transport-error');
            }
            return Promise.resolve(api.listArchiveMergedProjects()).then(function (r) {
                var classified = window.ArchiveOutcome.classify(
                    r.transportError ? null : r.envelope);
                if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                    renderOutcomeInto(doc, mergedList, r.envelope, r.transportError);
                    return classified.token;
                }
                var env = r.envelope || {};
                var meta = env.meta || {};
                merged.nodes = env.result || [];
                merged.unattributed = (meta.unattributed && meta.unattributed.by_corpus) || [];
                merged.hosts = meta.hosts || [];
                totals.merged = merged.nodes.length;
                paintMerged();
                if (classified.token === 'partial') TREE.renderPartialTail(doc, mergedList, r.envelope);
                return classified.token;
            });
        }

        /**
         * Description: repaint the merged list under the current fuzzy
         *   filter and machine filter, and restate what the filter did.
         * Inputs: none. Output: void.
         */
        function paintMerged() {
            // A MISSING DEPENDENCY IS RENDERED, NEVER THROWN. If
            // archive-nav-merged.js was not loaded, the merged view
            // cannot paint - and an exception here would take the whole
            // rail down on a keystroke, leaving a blank pane that looks
            // exactly like an archive with no projects in it. Say what
            // is missing, where the person is looking.
            if (!window.ArchiveNavMerged) {
                renderOutcomeInto(doc, mergedList, null,
                    'window.ArchiveNavMerged is missing: load ' +
                    'client/js/archive-nav-merged.js (and ' +
                    'archive-nav-fuzzy.js) before archive-nav.js');
                lastPaint = { rendered: 0, total: 0, hiddenUnattributed: 0 };
                return;
            }
            lastPaint = window.ArchiveNavMerged.paint(doc, mergedList, {
                nodes: merged.nodes,
                unattributed: merged.unattributed,
                hostId: hostFilter,
                filterText: filterText,
                onActivate: activate,
                onInfo: openInfo,
                overlay: overlay,
                orderMode: orderMode
            });
            filterNote.textContent = filterText
                ? describeFilter(lastPaint.rendered, lastPaint.total,
                                 totals.merged || null, 'projects')
                : '';
        }

        /**
         * Description: switch between the merged list and the
         *   by-machine drill-down. Both stay in the DOM; only one is
         *   visible, so switching back does not refetch.
         * Inputs: next (string) - 'merged' or 'hosts'. Output: Promise.
         */
        function setView(next) {
            view = next === 'hosts' ? 'hosts' : 'merged';
            if (view === 'merged') {
                hostList.setAttribute('hidden', 'hidden');
                mergedList.removeAttribute('hidden');
                return merged.nodes.length ? Promise.resolve('ok') : loadMergedProjects();
            }
            mergedList.setAttribute('hidden', 'hidden');
            hostList.removeAttribute('hidden');
            return loaded.hosts ? Promise.resolve('ok') : loadHosts();
        }

        filterInput.addEventListener('input', function () { setFilter(filterInput.value || ''); });

        return {
            element: root,
            loadHosts: loadHosts,
            loadMergedProjects: loadMergedProjects,
            setView: setView,
            /**
             * Description: load whatever the ACTIVE view needs, and
             *   nothing else. The screen used to call loadHosts() on
             *   every route, which fetched the drill-down the user was
             *   NOT looking at and never fetched the merged list that
             *   is the default view - so the rail opened empty, with no
             *   request made and no error to see. An empty list and a
             *   list nobody asked for render identically, which is the
             *   false green this rail is written against.
             *
             *   Idempotent: setView() already refuses to refetch a
             *   loaded view, so calling this on every route costs one
             *   request per view per session.
             * Inputs: none. Output: Promise.
             */
            ensureViewLoaded: function () { return setView(view); },
            /**
             * Description: the active view, for tests and the shell.
             * Inputs: none. Output: string - 'merged' | 'hosts'.
             */
            view: function () { return view; },
            /**
             * Description: narrow the merged list to one machine.
             * Inputs: hostId (number|string|null) - null for all.
             * Output: void.
             */
            setHostFilter: function (hostId) {
                hostFilter = (hostId === null || hostId === undefined || hostId === '')
                    ? null : String(hostId);
                paintMerged();
            },
            /**
             * Description: what the last paint rendered, so a test can
             *   assert the filter without recounting the DOM.
             * Inputs: none. Output: object.
             */
            lastPaint: function () { return lastPaint; },
            /** The order control, or null when its module is absent. */
            orderControl: function () { return order; },
            /** The active order id. Inputs: none. Output: string. */
            orderMode: function () { return orderMode; },
            /** Change the order as a click would: set the control,
             *  persist, repaint - so the three cannot get out of step.
             *  Inputs: mode (string). Output: boolean. */
            setOrder: function (mode) {
                if (!window.ArchiveNavOrder ||
                    !window.ArchiveNavOrder.isMode(mode)) return false;
                orderMode = mode;
                if (order) order.set(mode);
                window.ArchiveNavOrder.writeMode(mode);
                paintMerged();
                return true;
            },
            mergedNodes: function () { return merged.nodes.slice(); },
            /**
             * Description: open the info modal for one project node
             *   without a click, for deep links and tests. `infoModal`
             *   reports the open one, or null.
             * Inputs: row (object). Output: object|null - modal handle.
             */
            openInfo: function (row) { return openInfo(row, null); },
            infoModal: function () { return info ? info.current() : null; },
            expand: expand,
            setFilter: setFilter,
            // The composition root needs THREE things from the filter, and
            // had none of them: something to focus for `/`, the current
            // text (it was passing a hardcoded '' into ArchiveKeys.resolve,
            // which made rung 2 of the Escape ladder unreachable), and a
            // way to clear it.
            filterInput: filterInput,
            /**
             * Description: the filter box's current text.
             * Inputs: none. Output: string - '' when nothing is filtered.
             */
            filterText: function () { return filterText; },
            /**
             * Description: clear the filter and repaint, which is Escape
             *   rung 2. Clears the INPUT as well as the state: clearing
             *   only the state leaves the box showing text that no longer
             *   filters anything, which reads as a broken filter.
             * Inputs: none. Output: void.
             */
            clearFilter: function () {
                filterInput.value = '';
                setFilter('');
            },
            /**
             * Description: rows fetched for one level key, for tests.
             * Inputs: key (string). Output: Array.
             */
            rowsLoaded: function (key) { return (loaded[key] || []).slice(); }
        };
    }

    window.ArchiveNav = {
        create: create,
        filterRows: filterRows,
        describeFilter: describeFilter,
        labelFor: labelFor,
        renderCount: renderCount,
        NODE_KINDS: NODE_KINDS,
        ROOT_CLASS: ROOT_CLASS,
        NOT_KNOWN: NOT_KNOWN
    };
    console.log('[ArchiveNav Module] Exported as window.ArchiveNav');
})();
