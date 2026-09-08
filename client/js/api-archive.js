/**
 * API Module, archive half - the message browser's twelve read endpoints.
 *
 * WHY THIS IS A SEPARATE FILE. `api.js` reached 1,847 lines with the
 * archive methods in it, against this repo's 500-line file cap
 * (`~/.claude/rules/code-standards.md`). Splitting by FEATURE rather than
 * by size keeps one readable seam: everything below is the archive read
 * surface and nothing else in the app calls it.
 *
 * WHY `Object.assign(API.prototype, ...)` RATHER THAN A SUBCLASS OR A
 * SECOND SINGLETON. `window.API` is one instance built at the end of
 * `api.js`, and roughly a hundred existing call sites hold it. A subclass
 * would require changing which constructor is instantiated; a second
 * singleton would give the app two token-refresh mutexes, which is the
 * exact race `api.js`'s single-flight comment exists to prevent. Extending
 * the prototype leaves every call site, and the mutex, untouched.
 *
 * LOAD ORDER IS LOAD-BEARING: this file MUST come immediately after
 * `api.js` in `client/index.html`. `class API` is not hoisted across
 * scripts, so loading this first throws a ReferenceError at parse time.
 * `tests/test_archive_client_assets_registered.py` asserts the order.
 *
 * None of the methods below interprets `result_status`. That is
 * `archive-outcome.js`'s exclusive business.
 */

console.log('[API Archive Module] Loading...');

if (typeof API !== 'function') {
    // A named refusal, not a silent no-op. If this file is ever loaded
    // before api.js the archive screen would otherwise fail later, at a
    // call site, as "api.listArchiveHosts is not a function" - an error
    // that names the wrong cause. See hazard: an unloaded module is a
    // silent no-op, and a silent no-op is the hardest defect to trace.
    throw new ReferenceError(
        'api-archive.js loaded before api.js: class API is not defined. ' +
        'Fix the script order in client/index.html.');
}

/**
 * The route the rail's merged-project list comes from.
 *
 * Named rather than inlined because two routes serve the same node shape
 * and the difference between them is a behaviour, not a spelling:
 * `/archive/projects` is the archive's own names and
 * `/archive/overlay/projects` is those names with the owner's
 * presentation layered on. A reader who finds a bare string at the call
 * site cannot see that a choice was made there.
 * @type {string}
 */
const MERGED_PROJECTS_ENDPOINT = '/archive/overlay/projects';

Object.assign(API.prototype, {
    // =====================================================================
    // ARCHIVE (message browser)
    //
    // WHY THESE DO NOT USE call(). call() throws on any non-2xx and
    // returns only the parsed body, which is correct for the three
    // screens already built on it and wrong for every archive route.
    // Measured on the live server 2026-08-31: GET
    // /api/v1/archive/transcripts/99999 answers HTTP 404 carrying a
    // COMPLETE, renderable envelope, and
    // /api/v1/archive/projects/12/transcripts?cursor=@@@ answers HTTP
    // 400 the same way. Both are findings a person must read, not
    // errors. Pushing them through call() means either losing the
    // envelope on the throw path or teaching call() archive semantics
    // and putting three working screens at risk. A parallel method is
    // the cheaper trade.
    //
    // None of the methods below interprets result_status. That is
    // archive-outcome.js's exclusive business, and the moment a second
    // place branches on it the two branch sets drift.
    // =====================================================================

    /**
     * Perform an archive API call and return the parsed three-outcome
     * envelope WITHOUT throwing on a non-2xx status.
     *
     * NEVER REJECTS. That is the whole contract. A network failure, a
     * non-JSON body and a deadline expiry all RESOLVE, with
     * `envelope: null` and `transportError` set, because "the server did
     * not answer" is a finding the screen has to render, and a rejected
     * promise is how a finding turns into an unhandled console line
     * nobody sees.
     *
     * @param {string} endpoint - Path under /api/v1, leading slash.
     * @param {object} options - fetch options, plus one of ours:
     *   ``timeoutMs`` (number) - abort the request after this long and
     *   resolve with a transportError naming the deadline. A request
     *   with no deadline is a state that can never fail.
     * @returns {Promise<{envelope: object|null, httpStatus: number|null,
     *                    headers: object|null, transportError: string|null}>}
     * @example
     *   const r = await api.callEnvelope('/archive/transcripts/99999');
     *   // r.httpStatus === 404
     *   // r.envelope.result_status === 'not_found'
     *   // r.transportError === null
     */
    async callEnvelope(endpoint, options = {}, _meta = {}) {
        const { timeoutMs = null, ...fetchOnlyOptions } = options;
        const token = this.getToken();

        const headers = { ...(options.headers || {}) };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        // AbortController is the only way to stop a fetch that has
        // already been handed to the browser. Racing a timer against the
        // promise leaves the request running and its response landing on
        // a view that stopped waiting.
        const controller = (typeof AbortController !== 'undefined')
            ? new AbortController() : null;
        let timer = null;
        if (controller && typeof timeoutMs === 'number' && timeoutMs > 0) {
            timer = setTimeout(() => controller.abort(), timeoutMs);
        }

        const fetchOptions = { ...fetchOnlyOptions, headers };
        if (controller) fetchOptions.signal = controller.signal;

        try {
            const response = await fetch(`${this.baseURL}${endpoint}`, fetchOptions);

            // A 401 here means the access token expired mid-browse. Run
            // the SAME single-flight refresh the rest of the app uses -
            // see the constructor comment on _refreshPromise - and replay
            // once. Two archive requests racing must not each burn the
            // refresh chain.
            if (response.status === 401 && !_meta._retrying &&
                    window.Auth && window.Auth.getRefreshToken()) {
                const refreshed = await this._singleFlightRefresh();
                if (refreshed === true) {
                    if (timer) clearTimeout(timer);
                    return await this.callEnvelope(endpoint, options, { _retrying: true });
                }
            }

            let envelope = null;
            let transportError = null;
            try {
                envelope = await response.json();
            } catch (parseError) {
                // A body that is not JSON is not an envelope. Reporting
                // it as one would put an object with no result_status in
                // front of archive-outcome.js, which classifies that as
                // transport-error anyway - but saying so HERE keeps the
                // real reason (the parse failure) instead of losing it.
                transportError = `response body was not JSON: ${parseError.message}`;
            }

            return {
                envelope: envelope,
                httpStatus: response.status,
                headers: response.headers || null,
                transportError: transportError
            };
        } catch (error) {
            const aborted = error && error.name === 'AbortError';
            const reason = aborted
                ? `no response in ${Math.round(timeoutMs / 1000)}s`
                : `request failed: ${error && error.message ? error.message : String(error)}`;
            console.debug(`API archive [${endpoint}]: ${reason}`);
            return {
                envelope: null, httpStatus: null, headers: null,
                transportError: reason
            };
        } finally {
            if (timer) clearTimeout(timer);
        }
    },

    /**
     * Build a query string from named params, dropping every one the
     * caller did not set.
     *
     * A null that reaches the wire as `&cursor=null` is a malformed
     * cursor, and the server correctly answers cannot_determine for it -
     * a self-inflicted third outcome. Only params with a real value are
     * serialized.
     *
     * @param {object} params - name to value; null/undefined are dropped.
     * @returns {string} '' or '?a=1&b=2', already encoded.
     * @example _archiveQuery({limit: 50, cursor: null})  // -> '?limit=50'
     */
    _archiveQuery(params) {
        const parts = [];
        for (const [key, value] of Object.entries(params || {})) {
            if (value === null || value === undefined || value === '') continue;
            parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
        }
        return parts.length ? `?${parts.join('&')}` : '';
    },

    /**
     * Archive: every host that has contributed transcripts.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveHosts() {
        return await this.callEnvelope('/archive/hosts',
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: the corpora collected from one host.
     * @param {number|string} hostId - Numeric host id.
     * @param {object} [opts] - {limit, cursor}.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveCorpora(hostId, { limit, cursor } = {}) {
        const q = this._archiveQuery({ limit, cursor });
        return await this.callEnvelope(
            `/archive/hosts/${encodeURIComponent(hostId)}/corpora${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: the projects inside one corpus.
     * @param {number|string} corpusId - Numeric corpus id.
     * @param {object} [opts] - {limit, cursor}.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveProjects(corpusId, { limit, cursor } = {}) {
        const q = this._archiveQuery({ limit, cursor });
        return await this.callEnvelope(
            `/archive/corpora/${encodeURIComponent(corpusId)}/projects${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: EVERY project as one list, machine demoted to a field.
     *
     * The merged view the rail defaults to. One node per distinct
     * observed_cwd across all hosts and corpora, carrying display_name
     * (the folder name), full_path (the raw slug), hosts (every machine
     * it appears on) and members (the underlying per-corpus rows).
     *
     * Deliberately takes no limit/cursor: the server does not paginate
     * it, because a page of a merged tree would let a caller conclude a
     * project lives on one machine because the row proving otherwise
     * fell on page 2.
     *
     * WHY THIS CALLS THE OVERLAY ROUTE AND NOT `/archive/projects`.
     * `/archive/overlay/projects` returns the SAME merged nodes from the
     * same `merged_projects` read, plus the owner's presentation: an
     * override name where one exists, the group, hidden projects
     * filtered, and per node an `overlay` block whose status is 'none',
     * 'applied' or 'cannot_determine'. Pointed at the raw route the rail
     * renders the archive's own names and every card reports its overlay
     * state as ABSENT - which is honest, and is also the owner's rename
     * silently not taking effect. The archive's own name is not lost by
     * moving: every node still carries it as `archive_display_name`, so
     * the modal can show "shown as X, really Y" from one request.
     *
     * `/archive/projects` IS DELIBERATELY STILL THERE and unchanged. Two
     * routes answer two questions - what is in the archive, and how the
     * owner wants it shown - and folding the second into the first would
     * make the raw list unreachable. Anything that needs the archive's
     * own truth calls that route directly rather than reaching for this.
     *
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveMergedProjects() {
        return await this.callEnvelope(MERGED_PROJECTS_ENDPOINT,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: the transcripts in a corpus that belong to NO project.
     *
     * Its own endpoint because a transcript attributed to no project is
     * invisible from the project tree by construction - measured, corpus
     * 2 reported unattributed_transcript_count 5. A thing that cannot
     * appear in an enumeration needs a shape of its own or it is never
     * seen at all.
     *
     * @param {number|string} corpusId - Numeric corpus id.
     * @param {object} [opts] - {limit, cursor}.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveUnattributed(corpusId, { limit, cursor } = {}) {
        const q = this._archiveQuery({ limit, cursor });
        return await this.callEnvelope(
            `/archive/corpora/${encodeURIComponent(corpusId)}/unattributed${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: one project's transcripts, cursor-paged.
     *
     * `sessionRefScheme` is sent under its wire name
     * `session_ref_scheme` and is a SERVER-SIDE post-filter across the
     * whole project, not a filter of the page. It filters on that column
     * and on nothing else - the server's own `meta.filters` carries the
     * caveat that 19 of the 1,451 uuid-scheme transcripts have a
     * session_ref that is not a UUID - so a caller must not render it as
     * "these are the conversations".
     *
     * A scheme value the archive does not hold answers HTTP 400 with a
     * `cannot_determine` envelope naming the schemes that exist. That is
     * deliberately NOT the same response as a scheme that exists but
     * matches nothing here, which is a 200 with an empty `result`.
     *
     * @param {number|string} projectId - Numeric project id.
     * @param {object} [opts] - {limit, cursor, sessionRefScheme}.
     * @returns {Promise<object>} A callEnvelope result.
     * @example listArchiveTranscripts(12, {sessionRefScheme: 'uuid'})
     *          // GET /archive/projects/12/transcripts?session_ref_scheme=uuid
     */
    async listArchiveTranscripts(projectId, { limit, cursor, sessionRefScheme } = {}) {
        const q = this._archiveQuery({
            limit: limit,
            cursor: cursor,
            session_ref_scheme: sessionRefScheme
        });
        return await this.callEnvelope(
            `/archive/projects/${encodeURIComponent(projectId)}/transcripts${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.hierarchy });
    },

    /**
     * Archive: one transcript's header record.
     * @param {number|string} transcriptId - Numeric transcript id.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async getArchiveTranscript(transcriptId) {
        return await this.callEnvelope(
            `/archive/transcripts/${encodeURIComponent(transcriptId)}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.transcript });
    },

    /**
     * Archive: the line spine of one transcript.
     *
     * `includeBodies`, `maxPageBytes` and `startLine` are sent under
     * their wire names (`include_bodies`, `max_page_bytes`,
     * `start_line`); the camelCase argument names match the rest of this
     * file.
     *
     * `startLine` IS 0-BASED and is MUTUALLY EXCLUSIVE with `cursor`.
     * Sending both is HTTP 400 with a `cannot_determine` naming
     * `start_line`, because they are two absolute statements about where
     * the page begins. Open a walk with `startLine`, then continue it
     * with the `next_cursor` the server hands back. A `startLine` past
     * the transcript's last line is HTTP 404 naming that last line - it
     * is never an empty page, which would read as the end of the
     * transcript.
     *
     * NOTE `_archiveQuery` DROPS the empty string but KEEPS 0, which is
     * load-bearing here: `startLine: 0` is a real request for the first
     * line and must reach the wire.
     *
     * @param {number|string} transcriptId - Numeric transcript id.
     * @param {object} [opts] - {limit, cursor, includeBodies,
     *   maxPageBytes, role, recordType, model, startLine}.
     * @returns {Promise<object>} A callEnvelope result.
     * @example listArchiveLines(5767, {startLine: 7111, limit: 200})
     *          // GET /archive/transcripts/5767/lines?limit=200&start_line=7111
     */
    async listArchiveLines(transcriptId, {
        limit, cursor, includeBodies, maxPageBytes, role, recordType, model,
        startLine
    } = {}) {
        const q = this._archiveQuery({
            limit: limit,
            cursor: cursor,
            start_line: startLine,
            include_bodies: includeBodies,
            max_page_bytes: maxPageBytes,
            role: role,
            record_type: recordType,
            model: model
        });
        return await this.callEnvelope(
            `/archive/transcripts/${encodeURIComponent(transcriptId)}/lines${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.transcript });
    },

    /**
     * Archive: one transcript's turns, shaped for the CHAT view.
     *
     * This is the reading endpoint, as opposed to `listArchiveLines`,
     * which is the byte-exact record. It returns `turns[]`, each with
     * `line_no`, `body_id`, `role`, `record_type`, `ts`, `model`,
     * `blocks[]`, `subagents[]`, `secret_finding_count` and `info`.
     *
     * IT MAY NOT EXIST. This client shipped alongside the route rather
     * than after it, and a 404 from a route that was never deployed is
     * NOT an empty conversation - see archive-chat-screen.js, which
     * turns that case into a named could-not-determine. Do not "fix" a
     * caller that treats a 404 here as no messages.
     *
     * @param {number|string} transcriptId - Numeric transcript id.
     * @param {object} [opts] - {limit, cursor, startLine}.
     * @returns {Promise<object>} A callEnvelope result.
     * @example listArchiveMessages(4, {limit: 200})
     *          // GET /archive/transcripts/4/messages?limit=200
     */
    async listArchiveMessages(transcriptId, { limit, cursor, startLine } = {}) {
        const q = this._archiveQuery({
            limit: limit, cursor: cursor, start_line: startLine
        });
        return await this.callEnvelope(
            `/archive/transcripts/${encodeURIComponent(transcriptId)}/messages${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.transcript });
    },

    /**
     * Archive: one message body.
     *
     * The 30 s deadline is deliberately the longest of the read paths: a
     * single body in this corpus measured 54,376,879 bytes, which is a
     * legitimately slow transfer rather than a hung request.
     *
     * @param {number|string} bodyId - Numeric body id.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async getArchiveBody(bodyId) {
        return await this.callEnvelope(
            `/archive/bodies/${encodeURIComponent(bodyId)}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.body });
    },

    /**
     * Archive: the subagent sessions spawned from one transcript.
     * @param {number|string} transcriptId - Numeric transcript id.
     * @param {object} [opts] - {limit, cursor}.
     * @returns {Promise<object>} A callEnvelope result.
     */
    async listArchiveSubagents(transcriptId, { limit, cursor } = {}) {
        const q = this._archiveQuery({ limit, cursor });
        return await this.callEnvelope(
            `/archive/transcripts/${encodeURIComponent(transcriptId)}/subagents${q}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.transcript });
    },

    /**
     * Archive: search, scoped to exactly one of transcript/project/
     * corpus/host.
     *
     * The scope arguments are passed through as given rather than
     * validated here: the server answers a two-scope request with a
     * cannot_determine envelope naming the conflict, and that is a
     * better message than anything this client could invent.
     *
     * @param {object} [opts] - {q, transcriptId, projectId, corpusId,
     *   hostId, limit, cursor, caseSensitive}.
     * @returns {Promise<object>} A callEnvelope result.
     * @example searchArchive({q: 'restic', projectId: 12, limit: 3})
     *          // GET /archive/search?q=restic&project_id=12&limit=3
     */
    async searchArchive({
        q, transcriptId, projectId, corpusId, hostId, limit, cursor, caseSensitive
    } = {}) {
        const query = this._archiveQuery({
            q: q,
            transcript_id: transcriptId,
            project_id: projectId,
            corpus_id: corpusId,
            host_id: hostId,
            limit: limit,
            cursor: cursor,
            case_sensitive: caseSensitive
        });
        return await this.callEnvelope(`/archive/search${query}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.search });
    },

    /**
     * Archive: read an export's headers without consuming its body.
     *
     * A GET whose body is discarded, not a HEAD: the verified export
     * route computes its sha256 while streaming, and a HEAD would report
     * headers for work the server never did.
     *
     * This does NOT start a download. It cannot: the export endpoints
     * are Bearer-only (src/api/auth.py, HTTPBearer with no query or
     * cookie fallback), and a browser navigation or an <a download>
     * click sends no Authorization header. Measured 2026-08-31, all
     * three fallbacks - ?token=, ?access_token=, Cookie: - answered 401.
     * The export UI states that blocker rather than rendering a button
     * that produces a 401 page.
     *
     * @param {number|string} transcriptId - Numeric transcript id.
     * @param {object} [opts] - {verified} true for the hash-verified route.
     * @returns {Promise<object>} A callEnvelope result; read `.headers`.
     */
    async preflightArchiveExport(transcriptId, { verified } = {}) {
        const suffix = verified ? '/export/verified' : '/export';
        return await this.callEnvelope(
            `/archive/transcripts/${encodeURIComponent(transcriptId)}${suffix}`,
            { timeoutMs: this.ARCHIVE_TIMEOUTS.exportPreflight });
    }
});

console.log('[API Archive Module] Loaded: 12 archive endpoints on API.prototype');
