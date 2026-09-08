/**
 * Archive screen state - the single state object and its reducer.
 *
 * WHAT THIS FILE IS FOR. Every view on the archive screen is in exactly
 * one named state at any moment. There is no implicit default and no
 * state meaning "still figuring it out". The vocabulary is the six
 * outcome tokens from archive-outcome.js plus exactly two states that
 * are not outcomes - `idle` and `loading` - and the reducer is the only
 * thing permitted to move a view between them.
 *
 * THREE INVARIANTS THIS FILE EXISTS TO HOLD.
 *
 *   1. EVERY `loading` CARRIES A DEADLINE. A spinner with no terminal
 *      condition is a state that can never fail, which is the single
 *      worst defect shape available: a verification step that cannot
 *      report a problem. `REQUEST` stamps `deadlineAt`, and `TICK` past
 *      it turns the view into `transport-error` with the reason
 *      "no response in <n>s". A view can therefore always answer the
 *      question "what happened", even when nothing happened.
 *
 *   2. A RESPONSE ONLY LANDS ON A `loading` VIEW. Anything else is a
 *      response to a request this state object never made - a late
 *      arrival from a superseded query, or a bug. It is dropped rather
 *      than applied, because applying it silently overwrites the view a
 *      person is currently reading with the answer to a question they
 *      already moved on from.
 *
 *   3. `partial` NEVER BECOMES A SUCCESS WITHOUT AN EXPLICIT RESUME.
 *      `partial` means "I did not finish looking". The only two legal
 *      moves out of it are RESUME (continue where the scan stopped,
 *      keeping the rows already found) and REQUEST (a brand new
 *      question, which discards them). There is no path by which a
 *      partial quietly becomes an `ok`, because that would report 2,615
 *      unread transcripts as searched.
 *
 * No fetching. No DOM. No timers - `TICK` is fed in, so a test can
 * advance time without waiting for it.
 *
 * Depends on archive-outcome.js for classification. Reads no status
 * field itself.
 */

console.log('[ArchiveState Module] Loading...');

(function () {
    'use strict';

    /**
     * Request deadlines in milliseconds, per request class. Each is a
     * measured server timing with headroom, not a round number:
     * hierarchy reads are indexed and measured sub-millisecond; a full
     * 30,805-row spine measured 0.132 s server-side; a 54 MB body is a
     * legitimately slow transfer; a budget-exhausted search measured
     * 1.70 s and 2.25 s on two runs, and 45 s allows for a cold page
     * cache on a loaded host.
     * @type {Object<string,number>}
     */
    var DEADLINES_MS = {
        hierarchy: 10000,
        transcript: 15000,
        body: 30000,
        search: 45000,
        exportPreflight: 20000
    };

    /**
     * The two states that are not outcome tokens.
     * @type {string}
     */
    var IDLE = 'idle';
    var LOADING = 'loading';

    /**
     * Which key on each view slice holds its result rows, so RESUME can
     * append and REQUEST can clear without the reducer special-casing
     * five view names in five places.
     * @type {Object<string,string>}
     */
    var ROW_KEY = {
        nav: 'hosts',
        list: 'rows',
        reader: 'spine',
        search: 'hits'
    };

    /**
     * Description: the state every archive screen starts in. Mirrors the
     *   design doc's D.4 object exactly.
     * Inputs: none.
     * Output: object - a fresh state, safe to mutate by the caller only
     *   through reduce().
     */
    function initial() {
        return {
            route: { view: 'root', hostId: null, corpusId: null,
                     projectId: null, transcriptId: null, lineNo: null },
            nav: viewSlice({ hosts: [], expanded: {} }),
            list: viewSlice({ projectId: null, rows: [], nextCursor: null, hasMore: null }),
            reader: viewSlice({ transcriptId: null, header: null, spine: [], spineComplete: false }),
            search: viewSlice({ q: '', scope: null, hits: [], scan: null, resumeCursor: null }),
            exportUI: viewSlice({ transcriptId: null, headers: null }),
            // Exactly one permitted value in v1. The slot exists so the
            // reader renders "NOT CHECKED" rather than rendering
            // nothing, and so the next implementer sees the gap instead
            // of inventing an absence.
            liveSession: { token: 'not-checked' }
        };
    }

    /**
     * Description: build a view slice with the fields every view shares.
     * Inputs: extra (object) - view-specific fields.
     * Output: object.
     */
    function viewSlice(extra) {
        var base = {
            token: IDLE,
            reasons: [],
            deadlineAt: null,
            requestClass: null,
            resuming: false
        };
        for (var k in extra) {
            if (Object.prototype.hasOwnProperty.call(extra, k)) base[k] = extra[k];
        }
        return base;
    }

    /**
     * Description: shallow-copy the state with one view slice replaced.
     *   The reducer is pure; nothing here mutates its input.
     * Inputs: state (object), view (string), slice (object).
     * Output: object - a new state.
     */
    function withView(state, view, slice) {
        var next = {};
        for (var k in state) {
            if (Object.prototype.hasOwnProperty.call(state, k)) next[k] = state[k];
        }
        next[view] = slice;
        return next;
    }

    /**
     * Description: shallow-copy one view slice with fields overridden.
     * Inputs: slice (object), patch (object).
     * Output: object.
     */
    function patchSlice(slice, patch) {
        var out = {};
        for (var k in slice) {
            if (Object.prototype.hasOwnProperty.call(slice, k)) out[k] = slice[k];
        }
        for (var p in patch) {
            if (Object.prototype.hasOwnProperty.call(patch, p)) out[p] = patch[p];
        }
        return out;
    }

    /**
     * Description: pull the rows out of an envelope for a given view.
     *   Only ever called on a renderable outcome, so `result` is a real
     *   collection or a real object.
     * Inputs: envelope (object), view (string).
     * Output: Array - rows, possibly empty. A non-array `result` (the
     *   single-object routes, e.g. one transcript) is wrapped, so the
     *   row key always holds an array and no consumer has to test.
     */
    function rowsFrom(envelope, view) {
        if (!ROW_KEY[view]) return [];
        var r = envelope && envelope.result;
        if (Array.isArray(r)) return r;
        if (r === null || r === undefined) return [];
        return [r];
    }

    /**
     * Description: read `meta.scan.resume_cursor`, the only thing that
     *   makes a `partial` continuable.
     * Inputs: meta (object).
     * Output: string|null.
     */
    function resumeCursorFrom(meta) {
        var scan = meta && meta.scan;
        if (!scan || typeof scan !== 'object') return null;
        return typeof scan.resume_cursor === 'string' ? scan.resume_cursor : null;
    }

    /**
     * Description: read `meta.paging.next_cursor`.
     * Inputs: meta (object).
     * Output: string|null.
     */
    function nextCursorFrom(meta) {
        var paging = meta && meta.paging;
        if (!paging || typeof paging !== 'object') return null;
        return typeof paging.next_cursor === 'string' ? paging.next_cursor : null;
    }

    /**
     * Description: the whole transition table. Pure: same inputs, same
     *   output, no clock read and no I/O.
     * Inputs: state (object) - from initial() or a prior reduce().
     *         action (object) - one of:
     *   {type:'ROUTE', route}
     *     Replace the route. Touches no view.
     *   {type:'REQUEST', view, requestClass, at, patch}
     *     A NEW question. Clears the view's rows, sets `loading` and
     *     stamps deadlineAt = at + DEADLINES_MS[requestClass]. Legal
     *     from every token, including `partial` - asking something new
     *     is always allowed, it just discards the incomplete answer.
     *   {type:'RESUME', view, at}
     *     Continue a stopped scan. LEGAL ONLY FROM `partial`, and only
     *     when the view holds a resumeCursor. Keeps the rows already
     *     found and sets resuming=true so the next response appends.
     *   {type:'RESPONSE', view, envelope, at}
     *     Apply a server envelope. DROPPED unless the view is `loading`.
     *   {type:'TRANSPORT_ERROR', view, reason}
     *     The fetch itself failed. Always accepted; a dead network is
     *     news whatever the view was doing.
     *   {type:'TICK', view, at}
     *     Feed the clock in. Expires an overdue `loading`.
     *   {type:'RESET', view}
     *     Back to idle, rows cleared.
     * Output: object - a new state, or the SAME object when the action
     *   was rejected. Identity is the signal: `next === prev` means
     *   nothing moved, which a caller can assert on.
     * Example:
     *   var s = reduce(initial(), {type: 'REQUEST', view: 'search',
     *                              requestClass: 'search', at: 0});
     *   s.search.token       // 'loading'
     *   s.search.deadlineAt  // 45000
     */
    function reduce(state, action) {
        if (!action || typeof action !== 'object') return state;
        var type = action.type;

        if (type === 'ROUTE') {
            var next = withView(state, 'route', action.route || state.route);
            return next;
        }

        var view = action.view;
        if (!view || !state[view] || view === 'route' || view === 'liveSession') return state;
        var slice = state[view];
        var rowKey = ROW_KEY[view];

        if (type === 'RESET') {
            var cleared = patchSlice(slice, {
                token: IDLE, reasons: [], deadlineAt: null,
                requestClass: null, resuming: false
            });
            if (rowKey) cleared[rowKey] = [];
            return withView(state, view, cleared);
        }

        if (type === 'REQUEST') {
            var cls = action.requestClass;
            var ms = DEADLINES_MS[cls];
            if (typeof ms !== 'number') return state;   // an undeclared
            // request class would be a loading state with no deadline,
            // which is exactly the shape invariant 1 forbids.
            var started = patchSlice(slice, action.patch || {});
            started = patchSlice(started, {
                token: LOADING,
                reasons: [],
                requestClass: cls,
                deadlineAt: action.at + ms,
                resuming: false
            });
            if (rowKey) started[rowKey] = [];
            return withView(state, view, started);
        }

        if (type === 'RESUME') {
            // Invariant 3. `partial` is the ONLY token a resume is
            // meaningful from, and a partial the server gave no
            // resume_cursor for is not continuable at all.
            if (slice.token !== 'partial') return state;
            if (!slice.resumeCursor) return state;
            var resumed = patchSlice(slice, {
                token: LOADING,
                requestClass: slice.requestClass || 'search',
                deadlineAt: action.at + (DEADLINES_MS[slice.requestClass] || DEADLINES_MS.search),
                resuming: true
            });
            return withView(state, view, resumed);
        }

        if (type === 'TRANSPORT_ERROR') {
            return withView(state, view, patchSlice(slice, {
                token: 'transport-error',
                reasons: [{ subject: view, reason: String(action.reason || 'the request failed') }],
                deadlineAt: null,
                resuming: false
            }));
        }

        if (type === 'TICK') {
            if (slice.token !== LOADING || slice.deadlineAt === null) return state;
            if (action.at < slice.deadlineAt) return state;
            var waited = Math.round((DEADLINES_MS[slice.requestClass] || 0) / 1000);
            return withView(state, view, patchSlice(slice, {
                token: 'transport-error',
                reasons: [{ subject: view, reason: 'no response in ' + waited + 's' }],
                deadlineAt: null,
                resuming: false
            }));
        }

        if (type === 'RESPONSE') {
            // Invariant 2. A response that did not answer an outstanding
            // request is dropped, not applied.
            if (slice.token !== LOADING) return state;

            var classified = window.ArchiveOutcome.classify(action.envelope);
            var meta = classified.meta;
            var landed = patchSlice(slice, {
                token: classified.token,
                reasons: classified.reasons,
                deadlineAt: null,
                resuming: false
            });

            if (rowKey) {
                var incoming = window.ArchiveOutcome.isRenderable(classified.token)
                    ? rowsFrom(action.envelope, view) : [];
                landed[rowKey] = slice.resuming ? slice[rowKey].concat(incoming) : incoming;
            }
            if (view === 'search') {
                landed.scan = (meta && meta.scan) || null;
                landed.resumeCursor = resumeCursorFrom(meta);
            }
            if (view === 'list') {
                landed.nextCursor = nextCursorFrom(meta);
                landed.hasMore = window.ArchiveOutcome.hasMore(action.envelope);
            }
            if (view === 'reader') {
                // A spine is complete only on a plain `ok`. `partial`
                // means the far end was never read, so claiming
                // completeness there would hide the missing tail.
                landed.spineComplete = classified.token === 'ok';
            }
            return withView(state, view, landed);
        }

        return state;
    }

    window.ArchiveState = {
        initial: initial,
        reduce: reduce,
        DEADLINES_MS: DEADLINES_MS,
        ROW_KEY: ROW_KEY,
        IDLE: IDLE,
        LOADING: LOADING
    };
    console.log('[ArchiveState Module] Exported as window.ArchiveState');
})();
