/**
 * Search's ENVELOPE INTERPRETATION and its PURE RENDERERS: what a
 * `scan.status` means, what coverage the server actually achieved, what
 * a resumable scan is offering, and how one hit becomes one list item.
 *
 * WHY THIS IS SEPARATE FROM archive-search.js. Everything here is a
 * PURE FUNCTION of an envelope or a hit. The sibling file owns the
 * QUERY - the live cursors, the accumulated hits, the in-flight request
 * and the resume chain. Reading a server's own account of what it did
 * is a different job from deciding what to ask it next, and this is the
 * first of the two.
 *
 * A SCAN STATUS IS NEVER ASSUMED COMPLETE. SCAN_STATUSES is a
 * membership list, so a status this client has never seen renders as
 * unrecognised rather than silently as success. `budget_exhausted` and
 * `limit_reached` mean the server STOPPED LOOKING - results are a floor,
 * never a total - and saying "no matches" over a partial scan is the
 * false green this whole file exists to prevent.
 *
 * A WITHHELD HIT IS STILL A HIT. WITHHELD_STATES names the two states
 * where the server found something and deliberately did not send the
 * text. Dropping those rows would under-report the count; rendering
 * them without saying why would imply the preview was empty. Both are
 * wrong, so the row is rendered AND labelled.
 *
 * Exports window.ArchiveSearchRender.
 */

console.log('[ArchiveSearchRender Module] Loading...');

(function () {
    'use strict';

    /** Root element class. @type {string} */
    var ROOT_CLASS = 'archive-search';

    /**
     * Every `scan.status` this client recognises. Membership is checked,
     * not equality, so a value the server invents later reports as
     * unknown rather than as a completed scan.
     * @type {string[]}
     */
    var SCAN_STATUSES = ['complete', 'budget_exhausted', 'limit_reached', 'not_run'];

    /**
     * The resume kinds. These are what the two cursors MEAN, kept apart
     * by name so no call site can confuse "more matches on this page
     * boundary" with "more of the scope was never opened".
     * @type {Object<string,string>}
     */
    var RESUME_KINDS = {
        NONE: 'none',
        MORE_HITS: 'more-hits',
        MORE_SCOPE: 'more-scope',
        NOT_RUN: 'not-run',
        UNKNOWN: 'unknown'
    };

    /**
     * Snippet states that mean the preview was deliberately withheld.
     * The HIT is still real under every one of them.
     * @type {string[]}
     */
    var WITHHELD_STATES = ['withheld_secret_bearing', 'withheld_known_secret_value'];

    /**
     * Description: build an element with a class and optional text.
     * Inputs: doc (Document), tag (string), cls (string|null), text (any).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: read `meta.scan.status`, checked for membership.
     * Inputs: envelope (object|null).
     * Output: string - a SCAN_STATUSES value, or 'unknown' when the
     *   server reported no scan block or a value this client does not
     *   recognise. Never defaults to 'complete'.
     * Example: scanStatus({meta:{scan:{status:'budget_exhausted'}}})
     *   // -> 'budget_exhausted'
     */
    function scanStatus(envelope) {
        var meta = envelope && envelope.meta;
        var scan = meta && meta.scan;
        var s = scan && scan.status;
        return SCAN_STATUSES.indexOf(s) === -1 ? 'unknown' : s;
    }

    /**
     * Description: decide how, and whether, this response can be
     *   continued - and from WHICH cursor.
     *
     *   THE ONE RULE: each scan status reads exactly ONE named cursor
     *   field and there is no fallback to the other. `limit_reached`
     *   reads `meta.paging.next_cursor` and nothing else.
     *   `budget_exhausted` reads `meta.scan.resume_cursor` and nothing
     *   else. Falling back would produce a control that resumes the
     *   wrong dimension: continuing the SCAN when the person asked for
     *   more MATCHES silently re-reads scope they have already seen, and
     *   continuing the PAGE when the scan is unfinished skips the
     *   2,615 transcripts nobody opened while looking like it did not.
     *
     *   A missing cursor is reported as blocked, with the field named.
     *   It is never swapped.
     *
     * Inputs: envelope (object|null) - a parsed search response.
     * Output: {kind: string, cursor: string|null, field: string|null,
     *          label: string, blocked: boolean, reason: string}
     *   kind is a RESUME_KINDS value. `field` names the ONLY meta field
     *   consulted, so a test can assert the wiring rather than the text.
     * Example:
     *   resumeAffordance({meta:{scan:{status:'limit_reached'},
     *                           paging:{next_cursor:'abc'}}})
     *   // -> {kind:'more-hits', cursor:'abc',
     *   //     field:'meta.paging.next_cursor', blocked:false, ...}
     */
    function resumeAffordance(envelope) {
        var status = scanStatus(envelope);
        var meta = (envelope && envelope.meta) || {};

        if (status === 'complete') {
            return {
                kind: RESUME_KINDS.NONE, cursor: null, field: null, blocked: false,
                label: 'Nothing to resume',
                reason: 'The scan reported complete: every transcript in scope was read.'
            };
        }
        if (status === 'not_run') {
            return {
                kind: RESUME_KINDS.NOT_RUN, cursor: null, field: null, blocked: true,
                label: 'Nothing to resume',
                reason: 'No scan ran, so there is no position to resume from. ' +
                        'Every count on this response is null, not zero: nothing was measured.'
            };
        }
        if (status === 'limit_reached') {
            var paging = (meta.paging && typeof meta.paging === 'object') ? meta.paging : {};
            var next = typeof paging.next_cursor === 'string' ? paging.next_cursor : null;
            return {
                kind: RESUME_KINDS.MORE_HITS,
                cursor: next,
                field: 'meta.paging.next_cursor',
                blocked: next === null,
                label: 'Load more matches',
                reason: next === null
                    ? 'The page limit was reached but meta.paging.next_cursor is absent, ' +
                      'so the next page cannot be requested.'
                    : 'The page limit was reached. There are more MATCHES beyond this page. ' +
                      'This does not say the whole scope was read.'
            };
        }
        if (status === 'budget_exhausted') {
            var scan = (meta.scan && typeof meta.scan === 'object') ? meta.scan : {};
            var resume = typeof scan.resume_cursor === 'string' ? scan.resume_cursor : null;
            return {
                kind: RESUME_KINDS.MORE_SCOPE,
                cursor: resume,
                field: 'meta.scan.resume_cursor',
                blocked: resume === null,
                label: 'Resume the scan',
                reason: resume === null
                    ? 'The scan budget was spent but meta.scan.resume_cursor is absent, ' +
                      'so the unread part of the scope cannot be reached from here.'
                    : 'The scan budget was spent before the scope was finished. ' +
                      'Resuming reads MORE OF THE SCOPE, not more matches from what was read.'
            };
        }
        return {
            kind: RESUME_KINDS.UNKNOWN, cursor: null, field: null, blocked: true,
            label: 'Nothing to resume',
            reason: 'The server reported no scan status this client recognises, ' +
                    'so whether anything remains unread is NOT KNOWN.'
        };
    }

    /**
     * Description: the coverage sentence, rendered on EVERY outcome
     *   including `ok`, because `ok` is compatible with having read one
     *   transcript out of 3,416. Pure.
     * Inputs: envelope (object|null).
     * Output: string, never empty.
     * Example: coverageSentence(limitReachedEnvelope)
     *   // -> 'Coverage: 1 of 3416 transcripts in scope were read...'
     */
    function coverageSentence(envelope) {
        var meta = (envelope && envelope.meta) || {};
        var scan = (meta.scan && typeof meta.scan === 'object') ? meta.scan : {};
        var scope = (meta.scope && typeof meta.scope === 'object') ? meta.scope : {};
        var scanned = typeof scan.transcripts_scanned === 'number'
            ? scan.transcripts_scanned : null;
        var inScope = typeof scope.transcripts_in_scope === 'number'
            ? scope.transcripts_in_scope : null;
        if (scanned === null || inScope === null) {
            return 'Coverage: NOT KNOWN. The server did not report how much of the scope ' +
                   'it read, so this result says nothing about what was not read.';
        }
        var line = 'Coverage: ' + scanned + ' of ' + inScope +
                   ' transcripts in scope were read.';
        if (scanned < inScope) {
            line += ' ' + (inScope - scanned) + ' were NOT read; nothing is known about them.';
        }
        return line;
    }

    /**
     * Description: scan progress as a fraction of TRANSCRIPTS. Never
     *   bytes - see the header note on bytes_scanned overshooting its own
     *   budget. Pure.
     * Inputs: envelope (object|null).
     * Output: {scanned: number|null, inScope: number|null,
     *          fraction: number|null} - fraction is null whenever either
     *   integer is missing, so a caller cannot render a bar over a guess.
     */
    function scanProgress(envelope) {
        var meta = (envelope && envelope.meta) || {};
        var scan = (meta.scan && typeof meta.scan === 'object') ? meta.scan : {};
        var scope = (meta.scope && typeof meta.scope === 'object') ? meta.scope : {};
        var scanned = typeof scan.transcripts_scanned === 'number'
            ? scan.transcripts_scanned : null;
        var inScope = typeof scope.transcripts_in_scope === 'number'
            ? scope.transcripts_in_scope : null;
        var fraction = (scanned !== null && inScope !== null && inScope > 0)
            ? scanned / inScope : null;
        return { scanned: scanned, inScope: inScope, fraction: fraction };
    }

    /**
     * Description: whether a hit's preview was withheld on purpose. Pure.
     * Inputs: hit (object).
     * Output: boolean.
     * Example: isWithheld({snippet_state: 'withheld_secret_bearing'}) // -> true
     */
    function isWithheld(hit) {
        return !!hit && WITHHELD_STATES.indexOf(String(hit.snippet_state)) !== -1;
    }

    /**
     * Description: render one search hit. A withheld-preview hit renders
     *   with the SAME structure and the SAME locating facts as any other
     *   hit - transcript, line, offset - because the hit is real. Only
     *   the preview cell differs, and it says in words that the preview
     *   was withheld and why. It is not an error row and it is not an
     *   absent row.
     * Inputs: doc (Document), hit (object),
     *         opts (object) - {onOpen: function(transcriptId, lineNo)}.
     * Output: Element - the <li>.
     */
    function renderHit(doc, hit, opts) {
        var options = opts || {};
        var h = hit || {};
        var withheld = isWithheld(h);
        var li = el(doc, 'li', ROOT_CLASS + '__hit', null);
        li.setAttribute('data-transcript-id', String(h.transcript_id));
        li.setAttribute('data-line-no', String(h.line_no));
        li.setAttribute('data-snippet-state', String(h.snippet_state || 'unknown'));
        if (withheld) li.setAttribute('data-preview', 'withheld');

        var loc = el(doc, 'button', ROOT_CLASS + '__hit-loc', null);
        loc.setAttribute('type', 'button');
        loc.setAttribute('data-action', 'open-hit');
        // Routing is on transcript_id ONLY. session_ref names 14
        // different transcripts in this corpus and is a label, not an id.
        loc.setAttribute('data-transcript-id', String(h.transcript_id));
        loc.appendChild(el(doc, 'span', ROOT_CLASS + '__hit-transcript',
            'transcript ' + h.transcript_id));
        loc.appendChild(el(doc, 'span', ROOT_CLASS + '__hit-ref',
            String(h.session_ref === null || h.session_ref === undefined
                ? 'no session_ref recorded' : h.session_ref)));
        loc.appendChild(el(doc, 'span', ROOT_CLASS + '__hit-line', 'line ' + h.line_no));
        loc.appendChild(el(doc, 'span', ROOT_CLASS + '__hit-offset',
            'offset ' + h.match_offset + ', length ' + h.match_length));
        if (typeof options.onOpen === 'function') {
            loc.addEventListener('click', function () {
                options.onOpen(h.transcript_id, h.line_no);
            });
        }
        li.appendChild(loc);

        if (withheld) {
            var box = el(doc, 'div', ROOT_CLASS + '__preview ' + ROOT_CLASS + '__preview--withheld', null);
            box.appendChild(el(doc, 'span', ROOT_CLASS + '__preview-label', 'PREVIEW WITHHELD'));
            box.appendChild(el(doc, 'span', ROOT_CLASS + '__preview-note',
                'This match IS real and IS at the position named above. The server ' +
                'declined to send preview text because this body carries ' +
                (typeof h.secret_finding_count === 'number'
                    ? h.secret_finding_count + ' secret finding(s)' : 'secret findings') +
                ' (' + String(h.snippet_state) + '). Open the line to read it with masking applied.'));
            li.appendChild(box);
        } else {
            li.appendChild(el(doc, 'div', ROOT_CLASS + '__preview',
                h.snippet === null || h.snippet === undefined
                    ? 'no preview text supplied' : String(h.snippet)));
        }
        return li;
    }

    window.ArchiveSearchRender = {
        el: el,
        scanStatus: scanStatus,
        resumeAffordance: resumeAffordance,
        coverageSentence: coverageSentence,
        scanProgress: scanProgress,
        isWithheld: isWithheld,
        renderHit: renderHit,
        ROOT_CLASS: ROOT_CLASS,
        SCAN_STATUSES: SCAN_STATUSES,
        RESUME_KINDS: RESUME_KINDS,
        WITHHELD_STATES: WITHHELD_STATES
    };
    console.log('[ArchiveSearchRender Module] Exported as window.ArchiveSearchRender');
})();
