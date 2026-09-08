/**
 * NAMING A PROJECT NOBODY CLICKED. The crumb learns a project's name
 * from the row the user selected in the rail - which is fine until the
 * user arrives by URL, at which point no row was ever clicked and the
 * crumb has nothing to say. A fresh deep link to `/archive/p/<id>`
 * therefore rendered `project NOT NAMED YET` permanently, not for a
 * beat: the fact was never going to arrive, because the only thing that
 * produced it was a click that never happened.
 *
 * That is a worse failure than it looks. NOT NAMED YET is honest for a
 * fact still in flight, and this module exists so it keeps meaning that.
 * Left as it was, the one string covered both "not yet" and "never",
 * and a permanent unknown wearing the label of a temporary one is the
 * false-green shape the rest of this screen is written against.
 *
 * WHY IT READS THE MERGED LIST RATHER THAN ADDING A ROUTE. The merged
 * project list is ONE unpaginated request the rail already makes, it
 * already carries `display_name` for every project, and it already
 * carries `members`, which is the only place the ids of the folded-up
 * projects survive. A per-id lookup route would be a second way to ask
 * a question this response already answers, and it would have to
 * re-derive the same names from the same rows.
 *
 * THREE OUTCOMES, AND THE MIDDLE ONE IS NOT A NAME.
 *   - `name`             - the id was found; the node is returned.
 *   - `unresolved`       - the list was read and this id is not in it.
 *   - `cannot_determine` - the list could not be read at all.
 * Only the first ever reaches the crumb. The other two leave NOT NAMED
 * YET standing, because a crumb that invents a name is worse than one
 * that admits it has none - a wrong name is believable.
 *
 * A FAILED READ IS NOT CACHED. The successful list is memoised, so a
 * session costs one request no matter how many projects are opened; a
 * failure clears the memo so the next navigation retries rather than
 * inheriting one bad minute for the life of the tab.
 *
 * No DOM. Its only dependency is `window.ArchiveCrumb.indexNodes`.
 */

console.log('[ArchiveCrumbResolve Module] Loading...');

(function () {
    'use strict';

    /** The id was found and the node is a real name. @type {string} */
    var NAME = 'name';
    /** The list was read; this id is not in it. @type {string} */
    var UNRESOLVED = 'unresolved';
    /** The list could not be read. NOT the same as an empty list.
     *  @type {string} */
    var CANNOT_DETERMINE = 'cannot_determine';

    /**
     * Description: build a resolver that turns a project id into the
     *   merged node describing it, reading the merged project list at
     *   most once per success.
     * Inputs: api (object) - anything exposing
     *   `listArchiveMergedProjects()` returning a callEnvelope result.
     * Output: {resolve: function(number|string): Promise<{node, status}>}
     * Example: createResolver(window.API).resolve(48)
     *          //   -> {node: {display_name: 'Infrastructure', ...},
     *          //       status: 'name'}
     */
    function createResolver(api) {
        var pending = null;

        /** Description: the id -> node index, memoised on success only.
         *  Inputs: none. Output: Promise<{status, index}>. */
        function load() {
            if (pending) return pending;
            pending = Promise.resolve()
                .then(function () { return api.listArchiveMergedProjects(); })
                .then(function (r) {
                    var env = r && r.envelope;
                    // A transport error, a missing envelope, a non-ok
                    // status or a non-array result are all "could not
                    // read", and none of them is an empty list.
                    if (!r || r.transportError || !env) return null;
                    if (env.result_status !== 'ok') return null;
                    if (!Array.isArray(env.result)) return null;
                    return window.ArchiveCrumb.indexNodes(env.result);
                })
                .catch(function () { return null; })
                .then(function (index) {
                    if (index === null) {
                        // Do not poison the tab with one bad request.
                        pending = null;
                        return { status: CANNOT_DETERMINE, index: null };
                    }
                    return { status: NAME, index: index };
                });
            return pending;
        }

        /**
         * Description: the merged node that names this project id.
         * Inputs: projectId (number|string).
         * Output: Promise<{node: object|null, status: string}>.
         */
        function resolve(projectId) {
            if (projectId === null || projectId === undefined) {
                return Promise.resolve({ node: null, status: UNRESOLVED });
            }
            return load().then(function (r) {
                if (!r.index) return { node: null, status: CANNOT_DETERMINE };
                var node = r.index[String(projectId)] || null;
                return { node: node, status: node ? NAME : UNRESOLVED };
            });
        }

        return { resolve: resolve };
    }

    window.ArchiveCrumbResolve = {
        createResolver: createResolver,
        NAME: NAME,
        UNRESOLVED: UNRESOLVED,
        CANNOT_DETERMINE: CANNOT_DETERMINE
    };
    console.log('[ArchiveCrumbResolve Module] Exported as window.ArchiveCrumbResolve');
})();
