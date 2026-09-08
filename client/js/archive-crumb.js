/**
 * WHAT A BREADCRUMB SEGMENT SAYS. The vocabulary only - building the
 * PATH and rendering the nodes stay in archive-deeplink.js, beside the
 * route patterns.
 *
 * NO NUMERIC ID EVER REACHES A CRUMB. The screen used to render
 * `ARCHIVE > project 48 > transcript 5767`, which names two database
 * primary keys and nothing a person recognises. Worse, it reads like
 * information: somebody looking at it has no way to tell that the row
 * they clicked had a perfectly good name the crumb declined to use.
 * `hasNumericId()` exists so a test can assert the absence rather than
 * trusting that nobody reintroduces one.
 *
 * THREE OUTCOMES PER SEGMENT, AND THE MIDDLE ONE IS THE INTERESTING
 * ONE. A segment is either
 *   - a NAME - `display_name` for a project, `title` for a session;
 *   - a REFERENCE standing in for a name, when no name exists but
 *     something file-derived does (a project's `full_path` slug, a
 *     session's `session_ref`). It is labelled as such rather than
 *     presented as a name;
 *   - NOT KNOWN, when the fact simply has not arrived. This is a real
 *     state and not a rare one: a deep link paints its crumb before the
 *     header request resolves, so for a beat the app genuinely does not
 *     know what it is showing. It says so, and is repainted when the
 *     answer lands. Filling that beat with `transcript 5767` was the old
 *     behaviour and it is the false-green shape - an unknown rendered as
 *     a fact.
 *
 * `session_ref` IS NOT AN IDENTITY and never becomes one here: it is
 * display text only. Measured, `journal` names 14 different transcripts.
 * The route stays keyed on `transcript_id`, which is exactly why the id
 * must not ALSO be the label - the label is free to be the readable
 * thing precisely because the URL is carrying the identity.
 *
 * Pure. No DOM, no fetch, no globals beyond the export.
 */

console.log('[ArchiveCrumb Module] Loading...');

(function () {
    'use strict';

    /** What a segment renders when the fact has not arrived. Spelled
     *  once, and deliberately not a blank: an empty crumb slot is
     *  indistinguishable from a screen with no location at all.
     *  @type {string} */
    var PROJECT_UNKNOWN = 'project NOT NAMED YET';

    /** @type {string} */
    var SESSION_UNKNOWN = 'session NOT NAMED YET';

    /** Marks a segment whose text is a file-derived reference rather
     *  than a name anybody chose. @type {string} */
    var REF_PREFIX = 'ref ';

    /**
     * Description: the crumb segment for a project.
     * Inputs: node (object|null) - a nav project node, or null when the
     *   rail has not supplied one yet. Reads `display_name` (the folder
     *   name alone) and falls back to `full_path` (the original slug).
     * Output: {text: string, kind: string} - kind is 'name', 'ref' or
     *   'unknown'.
     * Example: projectSegment({display_name: 'Infrastructure'})
     *          // -> {text: 'Infrastructure', kind: 'name'}
     */
    function projectSegment(node) {
        var n = node || {};
        if (typeof n.display_name === 'string' && n.display_name.length > 0) {
            return { text: n.display_name, kind: 'name' };
        }
        if (typeof n.full_path === 'string' && n.full_path.length > 0) {
            // The slug IS the only thing known about this project, so it
            // is shown - labelled, so it does not read as a chosen name.
            return { text: REF_PREFIX + n.full_path, kind: 'ref' };
        }
        return { text: PROJECT_UNKNOWN, kind: 'unknown' };
    }

    /**
     * Description: the crumb segment for a transcript.
     * Inputs: row (object|null) - a transcript list row or a transcript
     *   header result, or null before either has arrived.
     * Output: {text: string, kind: string}.
     * Example: sessionSegment({session_ref: 'journal'})
     *          // -> {text: 'ref journal', kind: 'ref'}
     */
    function sessionSegment(row) {
        var r = row || {};
        if (typeof r.title === 'string' && r.title.length > 0) {
            return { text: r.title, kind: 'name' };
        }
        if (typeof r.session_ref === 'string' && r.session_ref.length > 0) {
            return { text: REF_PREFIX + r.session_ref, kind: 'ref' };
        }
        return { text: SESSION_UNKNOWN, kind: 'unknown' };
    }

    /**
     * Description: the whole crumb tail for a route, in order, given
     *   whatever facts have arrived. The ROOT label is added by
     *   `ArchiveDeeplink.renderCrumb`; this returns only what follows it.
     * Inputs: route (object) - {view, projectId, transcriptId},
     *         facts (object) - {project: object|null, transcript:
     *           object|null} - the nav node and the row/header, either
     *           of which may be absent.
     * Output: string[] - segment texts, none of which is a bare id.
     * Example: labels({view: 'transcript', projectId: 8, transcriptId: 5},
     *                 {project: {display_name: 'Infra'}, transcript: null})
     *          // -> ['Infra', 'session NOT NAMED YET']
     */
    function labels(route, facts) {
        var r = route || {};
        var f = facts || {};
        var out = [];
        var hasProject = r.projectId !== null && r.projectId !== undefined;
        var hasTranscript = r.transcriptId !== null && r.transcriptId !== undefined;
        if (hasProject) out.push(projectSegment(f.project).text);
        if (hasTranscript) {
            // A transcript reached by a direct link has no project in the
            // route at all. Saying so is a fact; silently rendering a
            // one-segment path implies the transcript has no project.
            if (!hasProject) out.push('project NOT KNOWN from this link');
            out.push(sessionSegment(f.transcript).text);
        }
        return out;
    }

    /**
     * Description: does any segment render a bare numeric id? Exists so
     *   the absence can be ASSERTED rather than assumed - the defect this
     *   module replaces produced perfectly plausible output, so nothing
     *   about a crumb's appearance would reveal a regression.
     * Inputs: parts (string[]).
     * Output: boolean - true if any segment is `<word> <digits>` or is
     *   itself only digits.
     * Example: hasNumericId(['project 48'])  // -> true
     */
    function hasNumericId(parts) {
        var list = Array.isArray(parts) ? parts : [];
        for (var i = 0; i < list.length; i++) {
            if (/(^|\s)\d+\s*$/.test(String(list[i]))) return true;
        }
        return false;
    }

    /**
     * Description: remember what has been learned about the project and
     *   the transcript currently open, so a crumb can be repainted the
     *   moment a fact arrives.
     *
     *   IT IS KEYED, AND THAT IS THE WHOLE POINT. A fact is stored
     *   against the id it describes, and read back only for the id the
     *   route names. Storing "the last row clicked" instead would render
     *   the PREVIOUS session's name over the current one for as long as
     *   the header request took - a wrong name, which is worse than the
     *   numeric id it replaced, because a wrong name is believable.
     *
     *   It holds ONE entry per kind rather than a growing cache: the
     *   crumb only ever describes what is open now, so a cache would be
     *   an unbounded map serving a question that is never asked about
     *   anything else.
     * Inputs: none.
     * Output: {learnProject, learnTranscript, labelsFor, facts}
     * Example: var t = createTracker();
     *          t.learnProject(8, {display_name: 'Infra'});
     *          t.labelsFor({view: 'project', projectId: 8});
     */
    function createTracker() {
        var project = { id: null, node: null };
        var transcript = { id: null, row: null };

        /** Description: record a project node against its id.
         *  Inputs: id, node. Output: void. */
        function learnProject(id, node) {
            if (id === null || id === undefined || !node) return;
            project = { id: id, node: node };
        }

        /** Description: record a transcript row or header against its id.
         *  Inputs: id, row. Output: void. */
        function learnTranscript(id, row) {
            if (id === null || id === undefined || !row) return;
            transcript = { id: id, row: row };
        }

        /** Description: the facts that describe THIS route, and no
         *  others. A fact stored against a different id is not returned.
         *  Inputs: route. Output: {project, transcript}. */
        function facts(route) {
            var r = route || {};
            return {
                project: project.id === r.projectId ? project.node : null,
                transcript: transcript.id === r.transcriptId ? transcript.row : null
            };
        }

        return {
            learnProject: learnProject,
            learnTranscript: learnTranscript,
            facts: facts,
            /** Description: the crumb tail for a route. Output: string[]. */
            labelsFor: function (route) { return labels(route, facts(route)); }
        };
    }

    /**
     * Description: map EVERY project id a merged node answers for onto
     *   that node, so a deep link can be named without a rail click.
     *
     *   IT INDEXES THE MEMBERS, NOT JUST THE NODE. A merged node carries
     *   `project_id` for its FIRST member only, because the merge folds
     *   one node per distinct `observed_cwd` and has to pick a
     *   representative for the existing per-project route. Measured on
     *   the live corpus 2026-09-01: 80 project rows merge to 77 nodes,
     *   so 3 real project ids belong to a node that does not carry them
     *   at the top level - exactly the 3 projects that exist on both
     *   machines. Indexing only `node.project_id` would leave a deep
     *   link to any of those 3 rendering NOT NAMED YET forever, and it
     *   would do so for the three projects most likely to be shared,
     *   which is the worst possible sample to be wrong about.
     * Inputs: nodes (Array<object>) - merged project nodes.
     * Output: object - String(project_id) -> node. Empty when nodes is
     *   not an array, which keeps a caller's lookup a miss rather than
     *   a throw.
     * Example: indexNodes([{project_id: 4, members: [{project_id: 9}]}])
     *          // -> {'4': node, '9': the SAME node}
     */
    function indexNodes(nodes) {
        var list = Array.isArray(nodes) ? nodes : [];
        var out = {};
        for (var i = 0; i < list.length; i++) {
            var node = list[i];
            if (!node) continue;
            if (node.project_id !== null && node.project_id !== undefined) {
                out[String(node.project_id)] = node;
            }
            var members = Array.isArray(node.members) ? node.members : [];
            for (var j = 0; j < members.length; j++) {
                var m = members[j];
                if (m && m.project_id !== null && m.project_id !== undefined) {
                    out[String(m.project_id)] = node;
                }
            }
        }
        return out;
    }

    window.ArchiveCrumb = {
        labels: labels,
        createTracker: createTracker,
        projectSegment: projectSegment,
        sessionSegment: sessionSegment,
        hasNumericId: hasNumericId,
        indexNodes: indexNodes,
        PROJECT_UNKNOWN: PROJECT_UNKNOWN,
        SESSION_UNKNOWN: SESSION_UNKNOWN,
        REF_PREFIX: REF_PREFIX
    };
    console.log('[ArchiveCrumb Module] Exported as window.ArchiveCrumb');
})();
