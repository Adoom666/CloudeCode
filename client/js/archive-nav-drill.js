/**
 * The BY-MACHINE drill-down: host -> corpus -> project, plus the
 * project-less node that hangs off a corpus.
 *
 * WHY IT IS A SEPARATE FILE. archive-nav.js owns the rail's state and
 * had reached this repo's 500-line cap exactly, so the ordering control
 * could not be added to it without taking something out first. This is
 * the honest thing to take out: these three functions are ONE level of
 * behaviour - fetch a level, expand a node, flatten what was loaded -
 * they talk to nothing but the context handed to them, and they are the
 * part of the rail nobody currently clicks.
 *
 * THIS TREE IS UNEXPOSED, NOT DELETED. The view bar that used to reach
 * it was removed at the owner's instruction ("i dont think we need the
 * button and dropdown on the left column"), but `setView('hosts')` and
 * the deep links still work, so the code stays reachable and tested. It
 * moved files; it did not change.
 *
 * Exports window.ArchiveNavDrill.
 */

console.log('[ArchiveNavDrill Module] Loading...');

(function () {
    'use strict';

    /**
     * Description: bind the drill-down to one rail instance. Everything
     *   these functions used to close over inside ArchiveNav.create is
     *   passed in explicitly instead, so the seam is a stated interface
     *   rather than an accident of scope.
     * Inputs: ctx (object) - {doc, root, api, hostList, el, ROOT_CLASS,
     *   NODE_KINDS, TREE, loaded, totals, paint, activate,
     *   renderOutcomeInto}. `loaded` and `totals` are the rail's own
     *   objects and are MUTATED in place, deliberately: they are the
     *   rail's cache and copying them here would give the drill-down a
     *   second, silently diverging one.
     * Output: {loadHosts, expand, loadedCorpora}.
     * Example: ArchiveNavDrill.create(ctx).loadHosts()
     */
    function create(ctx) {
        var doc = ctx.doc;
        var root = ctx.root;
        var api = ctx.api;
        var hostList = ctx.hostList;
        var el = ctx.el;
        var ROOT_CLASS = ctx.ROOT_CLASS;
        var NODE_KINDS = ctx.NODE_KINDS;
        var TREE = ctx.TREE;
        var loaded = ctx.loaded;
        var totals = ctx.totals;
        var paint = ctx.paint;
        var renderOutcomeInto = ctx.renderOutcomeInto;
        function activate(kind, id, row) { return ctx.activate(kind, id, row); }

    /**
     * Description: fetch and render the top level.
     * Inputs: none. Output: Promise<string> - the outcome token, so a
     *   caller (or a test) can assert what happened without reading
     *   the DOM.
     */
    function loadHosts() {
        return Promise.resolve(api.listArchiveHosts()).then(function (r) {
            var classified = window.ArchiveOutcome.classify(r.transportError ? null : r.envelope);
            if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                renderOutcomeInto(doc, hostList, r.envelope, r.transportError);
                return classified.token;
            }
            loaded.hosts = (r.envelope && r.envelope.result) || [];
            totals.hosts = loaded.hosts.length;
            paint(hostList, 'hosts', NODE_KINDS.HOST, loaded.hosts,
                  ['display_name', 'hostname'], true);
            if (classified.token === 'partial') {
                // Rows AND the banner. Dropping the rows to show the
                // banner would hide what did come back; dropping the
                // banner would claim the list is complete.
                TREE.renderPartialTail(doc, hostList, r.envelope);
            }
            return classified.token;
        });
    }

    /**
     * Description: expand a host into its corpora, or a corpus into
     *   its projects plus its unattributed node.
     * Inputs: kind (string) - HOST or CORPUS. id (number|string).
     * Output: Promise<string> - the outcome token.
     */
    function expand(kind, id) {
        var slot = TREE.slotFor(root, kind, id);
        if (!slot) return Promise.resolve('not-found');
        slot.textContent = '';
        slot.appendChild(el(doc, 'li', ROOT_CLASS + '__loading', 'loading...'));

        var isHost = kind === NODE_KINDS.HOST;
        var key = (isHost ? 'corpora:' : 'projects:') + id;
        var call = isHost ? api.listArchiveCorpora(id) : api.listArchiveProjects(id);

        return Promise.resolve(call).then(function (r) {
            var classified = window.ArchiveOutcome.classify(r.transportError ? null : r.envelope);
            if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                renderOutcomeInto(doc, slot, r.envelope, r.transportError);
                return classified.token;
            }
            var rows = (r.envelope && r.envelope.result) || [];
            loaded[key] = rows;
            totals[key] = rows.length;
            paint(slot, key, isHost ? NODE_KINDS.CORPUS : NODE_KINDS.PROJECT, rows,
                  isHost ? ['corpus_key', 'root_path'] : ['slug', 'observed_cwd'], isHost);
            if (!isHost) TREE.appendUnattributedNode(doc, slot, loadedCorpora(), id, activate);
            if (classified.token === 'partial') TREE.renderPartialTail(doc, slot, r.envelope);
            return classified.token;
        });
    }

    /** Every corpus row loaded, flattened, so the unattributed node
     *  can find its own count. Inputs: none. Output: Array<object>. */
    function loadedCorpora() {
        var out = [];
        for (var k in loaded) {
            if (k.indexOf('corpora:') === 0) out = out.concat(loaded[k]);
        }
        return out;
    }

        return {
            loadHosts: loadHosts,
            expand: expand,
            loadedCorpora: loadedCorpora
        };
    }

    window.ArchiveNavDrill = { create: create };
    console.log('[ArchiveNavDrill Module] Exported as window.ArchiveNavDrill');
})();
