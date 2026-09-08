/**
 * Archive outcome rendering - the only file that decides what a
 * `cannot_determine` LOOKS LIKE.
 *
 * WHY THIS FILE IS THE MOST IMPORTANT ONE IN THE ARCHIVE UI.
 * The archive server pays real cost to distinguish "I looked and found
 * nothing" from "I could not look" from "I ran out of budget partway".
 * archive-outcome.js carries that distinction as far as a token. THIS
 * file carries it the last step, to the pixel, where there is no outer
 * check left to catch a mistake. If an empty result and a
 * cannot_determine reach the screen looking alike, every layer of care
 * underneath is spent for nothing and the person reading the screen is
 * told a verdict nobody measured.
 *
 * FOUR INDEPENDENT CHANNELS, DELIBERATELY.
 * Every outcome differs on all four, and no single one of them is load
 * bearing on its own:
 *   1. TEXT      - the words a person reads, including the server's own
 *                  `unevaluated` reasons rendered verbatim.
 *   2. CLASS     - the styling hook, `archive-outcome--<token>`.
 *   3. ATTRIBUTE - `data-outcome`, the machine-readable token.
 *   4. ACTIONS   - which affordances exist in the subtree. This is the
 *                  hardest channel to fake, because it is a structural
 *                  fact rather than a string the renderer chose.
 * Colour is NOT a channel. Neither is border-radius: three of this
 * app's 23 themes (`terminal`, `gameboy`, `legacy_apple`) zero every
 * radius token on purpose, so a meaning carried by a rounded corner is
 * a meaning those themes cannot express.
 *
 * TWO MEASURED FACTS THAT DRIVE THE COPY (live server, 5055, 2026-08-31):
 *
 *   1. `scan.bytes_scanned` IS A CHARGE, NOT WORK DONE. Measured, it
 *      reported 551,648,566 against a `budget_bytes` of 536,870,912 -
 *      2.75% OVER its own budget - and 91,950,363 bytes in 0.0756 s,
 *      an impossible 1.22 GB/s, because a whole transcript is charged
 *      when the page limit is hit after reading a fraction of it. A
 *      quantity that exceeds its own budget cannot be a fraction of
 *      anything. Progress here is ALWAYS transcripts_scanned over
 *      transcripts_in_scope. bytes_scanned is rendered only as a raw
 *      number, labelled a charge.
 *
 *   2. `result_status: "ok"` DOES NOT MEAN THE WHOLE SCOPE WAS READ.
 *      Measured: GET /archive/search?q=restic&project_id=12&limit=3
 *      answered result_status "ok" with scan.status "limit_reached"
 *      and transcripts_not_scanned 3415 of 3416. So the `ok` block
 *      states its coverage too, whenever the server supplied the
 *      numbers. An `ok` that silently implies a complete search is the
 *      same false green in a nicer suit.
 *
 * NO innerHTML, ANYWHERE. Host display names carry real non-ASCII -
 * measured, host 2 is "Joseph’s Mac mini (2)" with a U+2019 - and
 * server-supplied `reason` strings are rendered verbatim by
 * requirement. Every string reaches the DOM through a text node.
 *
 * Depends on archive-outcome.js. Does not read `result_status`,
 * `scope_status` or `scan.status`: those are that module's exclusive
 * business. It reads only INTEGERS out of `meta`.
 *
 * NOTE FOR THE STRUCTURAL TEST that asserts `result_status` appears in
 * exactly one client file. Every occurrence of it in THIS file is prose
 * or a JSDoc example - there is no executable reference, and
 * `node -e` on this module never touches the field. That test must skip
 * comment lines, the same way deploy/lint-portability.sh does, or it
 * will report a violation about a sentence describing the rule it is
 * enforcing.
 */

console.log('[ArchiveOutcomeView Module] Loading...');

(function () {
    'use strict';

    /**
     * Root element class, shared by every outcome block.
     * @type {string}
     */
    var ROOT_CLASS = 'archive-outcome';

    /**
     * Class prefix used by renderReaderState when a caller supplies
     * none. Named rather than repeated, so the reader and this file
     * cannot drift apart on the string that hangs every style rule.
     * @type {string}
     */
    var READER_ROOT_CLASS = 'archive-reader';

    /**
     * The banner word for each token. Deliberately not sentence case:
     * this is the line a person's eye lands on first, and the six must
     * be unmistakable from across a room.
     * @type {Object<string,string>}
     */
    var LABELS = {
        'ok': 'RESULTS',
        'empty': 'NO MATCHES',
        'partial': 'INCOMPLETE - I DID NOT FINISH LOOKING',
        'cannot-determine': 'COULD NOT EVALUATE',
        'not-found': 'NOT FOUND',
        'transport-error': 'NO ANSWER FROM THE SERVER'
    };

    /**
     * The action set every block of a given token ALWAYS carries. These
     * are what makes channel 4 distinct, so they are additive-only: a
     * call site may add view-specific actions via `options.extraActions`
     * but can never remove these, which means no view can accidentally
     * erase the structural difference between two outcomes.
     *
     * Every entry is {action, label}. `action` becomes `data-action`.
     * @type {Object<string,Array<{action: string, label: string}>>}
     */
    var DEFAULT_ACTIONS = {
        'ok': [
            { action: 'new-search', label: 'New search' }
        ],
        'empty': [
            { action: 'broaden-scope', label: 'Search a wider scope' },
            { action: 'new-search', label: 'New search' }
        ],
        'partial': [
            { action: 'resume', label: 'Resume the scan' }
        ],
        'cannot-determine': [
            { action: 'retry', label: 'Try again' }
        ],
        'not-found': [
            { action: 'go-up', label: 'Go up to the containing scope' }
        ],
        'transport-error': [
            { action: 'retry', label: 'Try again' },
            { action: 'show-details', label: 'Show the raw response' }
        ]
    };

    /**
     * Description: build one element with a class and, optionally, text.
     *   Text always goes through textContent, never innerHTML.
     * Inputs: doc (Document), tag (string), cls (string), text (string|null).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: describe the scope the server said it was working in,
     *   as a phrase a person can act on. Reads only `meta.scope`, which
     *   carries ids and names, never a status.
     * Inputs: meta (object) - the envelope's meta object.
     * Output: string - e.g. "project 12" or "transcript 4", or the
     *   literal "the requested scope" when meta carried no scope at all.
     *   Never an empty string: a blank scope reads as if nothing was
     *   searched.
     * Example: describeScope({scope: {kind: 'project', project_id: 12}})
     *          // -> "project 12"
     */
    function describeScope(meta) {
        var scope = meta && meta.scope;
        if (!scope || typeof scope !== 'object') return 'the requested scope';
        var kind = typeof scope.kind === 'string' ? scope.kind : 'scope';
        var idKey = kind + '_id';
        var id = scope[idKey];
        var name = scope.display_name || scope.corpus_key || scope.slug;
        var out = kind;
        if (id !== null && id !== undefined) out += ' ' + String(id);
        if (name) out += ' (' + String(name) + ')';
        return out;
    }

    /**
     * Description: pull the three scan INTEGERS out of meta. Never reads
     *   `scan.status` (archive-outcome.js owns status interpretation)
     *   and never returns bytes as a fraction of anything.
     * Inputs: meta (object).
     * Output: {scanned: number|null, notScanned: number|null,
     *          inScope: number|null, bytesCharged: number|null,
     *          resumeCursor: string|null}
     *   Any field the server did not supply is null, and the caller
     *   renders NOT KNOWN rather than inventing a zero.
     */
    function scanNumbers(meta) {
        var scan = (meta && typeof meta.scan === 'object' && meta.scan) || {};
        var scope = (meta && typeof meta.scope === 'object' && meta.scope) || {};
        var num = function (v) { return typeof v === 'number' ? v : null; };
        return {
            scanned: num(scan.transcripts_scanned),
            notScanned: num(scan.transcripts_not_scanned),
            inScope: num(scope.transcripts_in_scope),
            bytesCharged: num(scan.bytes_scanned),
            resumeCursor: typeof scan.resume_cursor === 'string' ? scan.resume_cursor : null
        };
    }

    /**
     * Description: the one-sentence headline for a token. This is the
     *   sentence that must never be shared between two outcomes.
     * Inputs: token (string), meta (object), n (object) - scanNumbers().
     * Output: string, always non-empty.
     */
    function headlineFor(token, meta, n) {
        var scope = describeScope(meta);
        if (token === 'empty') {
            // "searched all" is load-bearing wording, not decoration:
            // tests/test_archive_search_zero_hits.node.mjs asserts on it
            // as the proof that a COMPLETE empty search makes a positive
            // claim about the whole scope, where a budget-exhausted one
            // must refuse to. Keep the phrase if you reword this.
            var searched = (n.scanned !== null && n.inScope !== null)
                ? ('searched all ' + n.scanned + ' of ' + n.inScope +
                   ' transcripts in ' + scope)
                : ('searched ' + scope + ' in full');
            return 'Search a wider scope: ' + searched + ', and none matched.';
        }
        if (token === 'partial') {
            var left = n.notScanned !== null ? String(n.notScanned) : 'an unreported number of';
            return 'Resume the scan: ' + left + ' transcripts in ' + scope +
                   ' were never read, so nothing is known about them.';
        }
        if (token === 'cannot-determine') {
            return 'Try again: the server answered but could not establish a result for ' +
                   scope + ', which is not the same as finding nothing.';
        }
        if (token === 'not-found') {
            return 'Go up to the containing scope: the server looked and established that ' +
                   scope + ' does not exist.';
        }
        if (token === 'transport-error') {
            return 'Try again: nothing at all is known about ' + scope +
                   ', because the server did not answer or answered with something' +
                   ' this client could not read.';
        }
        return 'Results from ' + scope + '.';
    }

    /**
     * Description: render the server's `unevaluated` entries verbatim.
     *   A block whose token means "something went unmeasured" and which
     *   carries no reasons still renders a NAMED line saying the reason
     *   was not supplied. A blank cell is not an answer.
     * Inputs: doc (Document), token (string),
     *         reasons (Array<{subject,reason}>).
     * Output: Element|null - null only for `ok` and `empty` with no
     *   reasons, where there is genuinely nothing unevaluated.
     */
    function renderReasons(doc, token, reasons) {
        var needsReasons = token !== 'ok' && token !== 'empty';
        if (!reasons.length && !needsReasons) return null;

        var list = el(doc, 'ul', ROOT_CLASS + '__reasons', null);
        if (!reasons.length) {
            list.appendChild(el(doc, 'li', ROOT_CLASS + '__reason',
                'The server supplied no reason for this outcome. What went unmeasured is itself NOT KNOWN.'));
            return list;
        }
        for (var i = 0; i < reasons.length; i++) {
            var r = reasons[i] || {};
            var item = el(doc, 'li', ROOT_CLASS + '__reason', null);
            item.appendChild(el(doc, 'span', ROOT_CLASS + '__reason-subject',
                String(r.subject === undefined || r.subject === null ? 'unnamed subject' : r.subject)));
            item.appendChild(el(doc, 'span', ROOT_CLASS + '__reason-text',
                String(r.reason === undefined || r.reason === null
                    ? 'no reason text supplied' : r.reason)));
            list.appendChild(item);
        }
        return list;
    }

    /**
     * Description: render scan coverage as transcript counts. NEVER as a
     *   byte fraction - see the header note, bytes_scanned overshot its
     *   own budget by 2.75% in a live measurement, so it cannot be a
     *   denominator or a numerator. bytes_scanned is emitted only as a
     *   raw number, labelled a charge, so a reader cannot mistake it for
     *   work completed.
     * Inputs: doc (Document), n (object) - scanNumbers().
     * Output: Element|null - null when the server reported no scan at all.
     */
    function renderCoverage(doc, n) {
        if (n.scanned === null && n.inScope === null && n.bytesCharged === null) return null;
        var box = el(doc, 'p', ROOT_CLASS + '__coverage', null);
        var scanned = n.scanned === null ? 'NOT KNOWN' : String(n.scanned);
        var inScope = n.inScope === null ? 'NOT KNOWN' : String(n.inScope);
        box.appendChild(el(doc, 'span', ROOT_CLASS + '__coverage-counts',
            'Transcripts read: ' + scanned + ' of ' + inScope + ' in scope.'));
        if (n.notScanned !== null) {
            box.appendChild(el(doc, 'span', ROOT_CLASS + '__coverage-gap',
                ' Not read: ' + n.notScanned + '.'));
        }
        if (n.bytesCharged !== null) {
            box.appendChild(el(doc, 'span', ROOT_CLASS + '__coverage-charge',
                ' Scan charge: ' + n.bytesCharged +
                ' bytes (a charge the server levies, not a measure of work done).'));
        }
        return box;
    }

    /**
     * Description: build the action row. `partial` is the one token whose
     *   primary action can be impossible: a partial with no
     *   `resume_cursor` cannot be resumed. The control is still emitted,
     *   because the actions channel is what keeps the outcomes
     *   structurally distinct, but it is DISABLED and carries a stated
     *   reason. A control that silently fails is worse than a stated
     *   blocker.
     * Inputs: doc (Document), token (string), n (object) - scanNumbers(),
     *         extraActions (Array<{action,label}>).
     * Output: Element.
     */
    function renderActions(doc, token, n, extraActions, omit) {
        var row = el(doc, 'div', ROOT_CLASS + '__actions', null);
        var skip = Array.isArray(omit) ? omit : [];
        var defs = (DEFAULT_ACTIONS[token] || []).concat(extraActions || []);
        for (var i = 0; i < defs.length; i++) {
            var def = defs[i];
            // TWO IDENTICAL PRIMARY BUTTONS IS A BUG, NOT REDUNDANCY.
            // A caller that renders its OWN, richer control for an action
            // says so here rather than letting both draw one. Measured
            // before this option existed: a partial search painted
            // "Resume the scan" twice, a few hundred pixels apart, from
            // two different modules that did not know about each other.
            if (skip.indexOf(def.action) !== -1) continue;
            var btn = el(doc, 'button', ROOT_CLASS + '__action', def.label);
            btn.setAttribute('type', 'button');
            btn.setAttribute('data-action', def.action);
            if (def.action === 'resume' && !n.resumeCursor) {
                btn.setAttribute('disabled', '');
                btn.setAttribute('data-blocked-reason',
                    'the server returned no resume_cursor, so this scan cannot be continued');
            }
            row.appendChild(btn);
        }
        return row;
    }

    /**
     * Description: render one outcome token as a DOM subtree. The single
     *   entry point of this module.
     * Inputs: envelope (object|null) - a parsed archive response body, or
     *           null when the fetch produced none.
     *         options (object) - {document: Document} to render into a
     *           harness rather than the page; {extraActions:
     *           Array<{action,label}>} to ADD view-specific affordances
     *           (never to remove the token's own).
     * Output: Element - a <section> carrying `data-outcome`, the
     *   `archive-outcome--<token>` class, the label, the headline, the
     *   verbatim reasons, the coverage line and the action row.
     * Example:
     *   renderOutcomeBlock({result: null, result_status: 'not_found',
     *                       scope_status: 'not_found',
     *                       unevaluated: [{subject: 'transcript:99999',
     *                         reason: 'no row in message_transcripts'}],
     *                       meta: {}});
     *   // -> <section data-outcome="not-found" class="archive-outcome
     *   //     archive-outcome--not-found"> ... </section>
     */
    function renderOutcomeBlock(envelope, options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('renderOutcomeBlock needs a document');

        var classified = window.ArchiveOutcome.classify(envelope);
        var token = classified.token;
        var meta = classified.meta;
        var n = scanNumbers(meta);

        var root = el(doc, 'section', ROOT_CLASS + ' ' + ROOT_CLASS + '--' + token, null);
        root.setAttribute('data-outcome', token);
        // Politeness, not assertiveness: these blocks replace a region a
        // person is already looking at, and an assertive live region
        // would interrupt a screen reader mid-sentence on every paging
        // step.
        root.setAttribute('role', 'status');

        root.appendChild(el(doc, 'p', ROOT_CLASS + '__label', LABELS[token] || String(token)));
        root.appendChild(el(doc, 'p', ROOT_CLASS + '__headline', headlineFor(token, meta, n)));

        // THE AUDIT TRAIL GOES BEHIND A DISCLOSURE, AND NOTHING IS
        // DROPPED. The word count was the bug, never the facts. The
        // partial state used to state one fact - 801 of 3,416 read - in
        // five registers one after another, and volunteer the server's
        // internal byte charge in the middle of a result panel. Every
        // one of those lines is still built, still exact, and still in
        // this block's textContent for any test, screen reader or
        // Ctrl-F that walks it. It is one click away instead of being
        // the fourth restatement of the sentence above it.
        //
        // The summary NAMES what is inside rather than saying
        // "details", so nobody has to open it to find out whether it is
        // worth opening. The disclosure is appended only when it would
        // hold something: an empty <details> is a control that does
        // nothing.
        var audit = el(doc, 'details', ROOT_CLASS + '__audit', null);
        audit.appendChild(el(doc, 'summary', ROOT_CLASS + '__audit-summary',
            "Audit trail: coverage, counts and the server's own reasons"));
        var auditItems = 0;

        var reasons = renderReasons(doc, token, classified.reasons);
        if (reasons) { audit.appendChild(reasons); auditItems++; }

        var coverage = renderCoverage(doc, n);
        if (coverage) { audit.appendChild(coverage); auditItems++; }

        // has_more is a three-outcome field of its own: the server
        // returns null on every failure path, and null is not false.
        // Saying so out loud beats hiding a load-more control as if the
        // end of the list had been reached.
        if (window.ArchiveOutcome.hasMore(envelope) === null &&
                (token === 'partial' || token === 'cannot-determine')) {
            audit.appendChild(el(doc, 'p', ROOT_CLASS + '__has-more',
                'Whether there is more beyond this: NOT KNOWN.'));
            auditItems++;
        }

        if (auditItems > 0) root.appendChild(audit);

        root.appendChild(renderActions(doc, token, n, opts.extraActions,
                                       opts.omitActions));
        return root;
    }

    /**
     * Description: build the children for the reader's NON-`ok` state.
     *   EVERY READER STATE IS ONE OF THE SIX OUTCOME TOKENS OR ONE OF
     *   THE TWO NON-OUTCOME STATES. There is no hand-rolled empty state
     *   and no hand-rolled error state: `idle` renders a prompt,
     *   `loading` renders a stated DEADLINE, and everything else is
     *   handed whole to renderOutcomeBlock. A spinner with no terminal
     *   condition is a state that can never fail, which is a false green
     *   at the pixel level.
     *   The CALLER keeps ownership of its status element and of setting
     *   `data-reader-state`; this function only builds children.
     * Inputs: token (string) - 'idle', 'loading', or an outcome token.
     *         envelope (object|null) - backs a failure token.
     *         options (object|null) - {document, rootClass, deadlineMs}.
     * Output: Element - a container holding the state's children.
     * Example:
     *   renderReaderState('loading', null, {document: doc,
     *     rootClass: 'archive-reader', deadlineMs: 15000});
     */
    function renderReaderState(token, envelope, options) {
        var opts = options || {};
        var doc = opts.document ||
            (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('renderReaderState needs a document');
        var rc = opts.rootClass || READER_ROOT_CLASS;
        var box = el(doc, 'div', rc + '__state', null);

        if (token === 'idle') {
            // NORMATIVE COPY. "You have not asked" and "your question
            // found nothing" are different findings and must not look
            // alike. `Live session: NOT CHECKED` is rendered verbatim:
            // this build does not correlate archived transcripts with
            // live terminal sessions, and it is not asserting that
            // there are none.
            box.appendChild(el(doc, 'p', rc + '__idle-head',
                'Pick a transcript.'));
            box.appendChild(el(doc, 'p', rc + '__idle-live',
                'Live session: NOT CHECKED'));
            box.appendChild(el(doc, 'p', rc + '__idle-note',
                'This build does not correlate archived transcripts with ' +
                'live terminal sessions. It is not asserting that there ' +
                'are none.'));
            return box;
        }
        if (token === 'loading') {
            var p = el(doc, 'p', rc + '__loading', 'Loading the line index...');
            p.setAttribute('role', 'status');
            box.appendChild(p);
            // The deadline is surfaced so the state is visibly terminal
            // rather than an open-ended spinner. A deadline nobody
            // supplied is NOT a deadline of zero, so it is stated as an
            // unknown rather than rendered as a number nobody measured.
            var ms = opts.deadlineMs;
            box.appendChild(el(doc, 'p', rc + '__deadline',
                Number.isFinite(ms)
                    ? 'If there is no answer within ' + Math.round(ms / 1000) +
                      's this becomes NO ANSWER FROM THE SERVER.'
                    : 'The deadline for this request: NOT KNOWN.'));
            return box;
        }
        box.appendChild(renderOutcomeBlock(envelope, { document: doc }));
        return box;
    }

    window.ArchiveOutcomeView = {
        renderOutcomeBlock: renderOutcomeBlock,
        renderReaderState: renderReaderState,
        describeScope: describeScope,
        scanNumbers: scanNumbers,
        headlineFor: headlineFor,
        ROOT_CLASS: ROOT_CLASS,
        LABELS: LABELS,
        DEFAULT_ACTIONS: DEFAULT_ACTIONS
    };
    console.log('[ArchiveOutcomeView Module] Exported as window.ArchiveOutcomeView');
})();
