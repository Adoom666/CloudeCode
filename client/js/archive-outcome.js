/**
 * Archive outcome classification - the ONLY interpreter of an archive
 * envelope's status fields in the entire client.
 *
 * WHY THIS IS ONE FILE AND WHY NOTHING ELSE MAY READ result_status.
 * The archive server pays real cost to distinguish "I looked and found
 * nothing" from "I could not look" from "I ran out of budget partway".
 * That distinction is only worth anything if it survives all the way to
 * the screen. The moment a second file branches on result_status, the
 * two branch sets drift, and one of them starts rendering `partial` as
 * `ok` - a search that gave up after 801 of 3,416 transcripts reported
 * to the person as a complete answer. Grep for `result_status` across
 * client/js/archive-*.js: it must appear in this file and nowhere else.
 *
 * THE THREE-OUTCOME RULE, which this module exists to enforce:
 * every response is pass, fail, or COULD NOT EVALUATE, and the third is
 * never folded into either of the other two. Collapsing it into pass
 * invents a verdict nobody measured. Collapsing it into fail burns the
 * alert's credibility until people stop reading it.
 *
 * TWO MEASURED FACTS THAT DRIVE THE IMPLEMENTATION (live server, 5055,
 * 2026-08-31):
 *
 *   1. `result` is `null` on some failures and `[]` on others, depending
 *      only on whether the route returns a collection or a single
 *      object. GET /archive/transcripts/99999 returns `result: null`
 *      with result_status `not_found`; GET /archive/projects/999999/
 *      transcripts returns `result: []` with the SAME result_status.
 *      A client that classifies on `result` being empty is wrong in both
 *      directions: it calls the second one "empty" (a positive claim
 *      that project 999999 exists and holds no transcripts) and it has
 *      no way to see the first at all. So classification reads
 *      result_status and scope_status, NEVER the shape of `result`.
 *
 *   2. HTTP status is NOT a proxy for result_status. src/api/
 *      archive_support.py::respond is explicit: 404 for not_found, 400
 *      for a cannot_determine naming a client parameter, and "200
 *      otherwise - including a cannot_determine the SERVER is
 *      responsible for". src/api/archive_export_routes.py line 454
 *      returns exactly that: status_code=200 carrying a cannot_determine
 *      envelope. Reading 200 as success is the single most damaging
 *      mistake available here, so this function never sees an HTTP
 *      status and cannot be tempted by one.
 *
 * Pure. No DOM, no fetch, no globals beyond the export.
 */

console.log('[ArchiveOutcome Module] Loading...');

(function () {
    'use strict';

    /**
     * Every `result_status` this client recognises. Membership is
     * checked, not equality against 'ok', so an absent or newly-invented
     * value fails toward `transport-error` rather than toward success.
     * @type {string[]}
     */
    var RESULT_STATUSES = ['ok', 'partial', 'cannot_determine', 'not_found'];

    /**
     * Every `scope_status` this client recognises. Same reasoning.
     * @type {string[]}
     */
    var SCOPE_STATUSES = ['resolved', 'not_found', 'cannot_determine'];

    /**
     * The complete outcome vocabulary of the archive UI. Exported so a
     * view can assert it handles all six without re-listing them, and so
     * a test can prove no seventh token leaks out of classify().
     * @type {string[]}
     */
    var TOKENS = [
        'ok',
        'empty',
        'partial',
        'cannot-determine',
        'not-found',
        'transport-error'
    ];

    /**
     * Description: reduce an API envelope to exactly one outcome token.
     *   This is the ONLY function in the client permitted to read
     *   result_status or scope_status.
     * Inputs: envelope (object|null|undefined) - a parsed response body,
     *   or null/undefined if the fetch never produced one. An HTTP status
     *   code is deliberately NOT a parameter: see the header note, a 200
     *   can carry a cannot_determine.
     * Output: {token: string, reasons: Array<{subject: string, reason: string}>,
     *          meta: object}
     *   `token` is one of ok|empty|partial|cannot-determine|not-found|
     *   transport-error. `reasons` is the envelope's `unevaluated` array,
     *   surfaced so a view can render WHY something could not be
     *   evaluated - a blank cell is not an answer. `meta` is the
     *   envelope's meta object, or {}.
     * Example:
     *   classify({result: [], result_status: 'ok',
     *             scope_status: 'resolved', unevaluated: [], meta: {}})
     *   // -> {token: 'empty', reasons: [], meta: {}}
     */
    function classify(envelope) {
        if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
            return {
                token: 'transport-error',
                reasons: [{ subject: 'response', reason: 'no parsable response body' }],
                meta: {}
            };
        }

        var rs = envelope.result_status;
        var ss = envelope.scope_status;
        var reasons = Array.isArray(envelope.unevaluated) ? envelope.unevaluated : [];
        var meta = (envelope.meta && typeof envelope.meta === 'object' &&
                    !Array.isArray(envelope.meta)) ? envelope.meta : {};

        // An unrecognised or absent status is NOT ok. Fail toward the
        // third outcome, never toward the first. Measured shape this
        // catches: a FastAPI validation error body,
        // {"detail": [{"type": "missing", "loc": ["query", "q"], ...}]},
        // returned by GET /archive/search with no q (HTTP 400,
        // 2026-08-31). It carries no result_status at all, and treating
        // an absent field as 'ok' would render it as a successful empty
        // search.
        if (RESULT_STATUSES.indexOf(rs) === -1 || SCOPE_STATUSES.indexOf(ss) === -1) {
            return {
                token: 'transport-error',
                reasons: reasons.length ? reasons : [{
                    subject: 'envelope',
                    reason: 'result_status=' + String(rs) +
                            ' scope_status=' + String(ss) +
                            ' is not a value this client recognises'
                }],
                meta: meta
            };
        }

        // not_found before cannot_determine: "there is no such thing" is
        // a MEASUREMENT and is strictly more informative than "I could
        // not tell". Reporting it as cannot-determine would throw away a
        // fact the server established.
        if (ss === 'not_found' || rs === 'not_found') {
            return { token: 'not-found', reasons: reasons, meta: meta };
        }
        if (ss === 'cannot_determine' || rs === 'cannot_determine') {
            return { token: 'cannot-determine', reasons: reasons, meta: meta };
        }

        // `partial` is its own outcome and is checked BEFORE the
        // empty/non-empty split. It means "I did not finish looking". It
        // is not a success and it is not an error, and a partial that
        // returned zero rows is emphatically NOT `empty`: measured live
        // 2026-08-31, GET /archive/search?q=zzzqqqxyznotfound&project_id=12
        // answered result_status 'partial' with `result: []` after
        // scanning 801 of 3,416 transcripts. Rendering that as "no
        // matches" claims 2,615 unscanned transcripts contain nothing.
        if (rs === 'partial') {
            return { token: 'partial', reasons: reasons, meta: meta };
        }

        // rs === 'ok'. Only HERE does the shape of `result` mean
        // anything, because only here has the server asserted it looked
        // at everything it was asked to look at.
        var r = envelope.result;
        var isEmpty = r === null || r === undefined ||
                      (Array.isArray(r) && r.length === 0);
        return { token: isEmpty ? 'empty' : 'ok', reasons: reasons, meta: meta };
    }

    /**
     * Description: read `meta.paging.has_more` as the three-outcome field
     *   it actually is. Lives here rather than in the list view because
     *   it is the same class of interpretation as result_status, and the
     *   same drift applies if two files decide it independently.
     *
     *   NORMATIVE (design doc section D.3): the server returns
     *   `has_more: null` on every failure path - verified live, the
     *   budget_exhausted search returned "has_more": null. `null` is not
     *   `false`. Treating it as `false` is a claim that the end of the
     *   list was reached when no list was read, and a client that paged
     *   3,416 transcripts on that claim would stop early and say so
     *   confidently.
     * Inputs: envelope (object|null) - a parsed response body.
     * Output: true | false | null. `null` means NOT KNOWN and the caller
     *   must render it as such rather than hiding the load-more control
     *   as if the end had been reached.
     * Example:
     *   hasMore({meta: {paging: {has_more: null}}})  // -> null
     *   hasMore({meta: {paging: {has_more: true}}})  // -> true
     */
    function hasMore(envelope) {
        if (!envelope || typeof envelope !== 'object') return null;
        var meta = envelope.meta;
        if (!meta || typeof meta !== 'object') return null;
        var paging = meta.paging;
        if (!paging || typeof paging !== 'object') return null;
        var v = paging.has_more;
        // Strict identity on both sides. Anything that is not exactly
        // the boolean true or the boolean false is NOT KNOWN.
        if (v === true) return true;
        if (v === false) return false;
        return null;
    }

    /**
     * Description: whether a token means the view may render content.
     *   `partial` counts, because a partial answer has real rows in it
     *   that the person should see, alongside the banner naming what was
     *   not reached.
     * Inputs: token (string) - one of TOKENS.
     * Output: boolean.
     */
    function isRenderable(token) {
        return token === 'ok' || token === 'partial';
    }

    window.ArchiveOutcome = {
        classify: classify,
        hasMore: hasMore,
        isRenderable: isRenderable,
        TOKENS: TOKENS,
        RESULT_STATUSES: RESULT_STATUSES,
        SCOPE_STATUSES: SCOPE_STATUSES
    };
    console.log('[ArchiveOutcome Module] Exported as window.ArchiveOutcome');
})();
