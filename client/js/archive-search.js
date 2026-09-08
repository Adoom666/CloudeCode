/**
 * Archive search - scoped search, its coverage statement, and its TWO
 * distinct resume affordances.
 *
 * THE PROPERTY THIS FILE EXISTS TO PROTECT.
 * `meta.scan.status` has FOUR values and a zero-hit result means a
 * completely different thing under two of them:
 *
 *   complete          I read the whole scope. There is nothing there.
 *   budget_exhausted  I stopped partway. I do not know what is in the
 *                     2,615 transcripts I never opened.
 *   limit_reached     I filled the page. There are more MATCHES.
 *   not_run           No scan happened at all. Every count is null.
 *
 * A zero-hit `budget_exhausted` rendered like a zero-hit `complete` tells
 * a person the archive contains no occurrence of their term when 76
 * percent of the scope was never read. That is the false green this
 * whole screen exists to prevent, and this is the file where it would be
 * manufactured.
 *
 * TWO CURSORS, TWO MEANINGS, NEVER CROSSED. Measured live 2026-08-31 and
 * they are exactly complementary, which is precisely what makes reading
 * the wrong one so easy to do and so hard to notice:
 *
 *   limit_reached     meta.paging.next_cursor SET, meta.scan.resume_cursor NULL
 *   budget_exhausted  meta.paging.next_cursor NULL, meta.scan.resume_cursor SET
 *
 * Read the wrong field and you get null, which looks like "cannot
 * resume" - a plausible, quiet, wrong answer. So `resumeAffordance()`
 * below reads ONE named field per scan status and there is no fallback
 * between them: a missing cursor is reported as a BLOCKED affordance
 * naming the field that was absent, never silently swapped for the other
 * one.
 *
 * `result_status: "ok"` DOES NOT MEAN THE SCOPE WAS READ. Measured:
 * q=restic&project_id=12&limit=3 answered `ok` with `limit_reached`,
 * `transcripts_scanned: 1`, `transcripts_not_scanned: 3415`. So the
 * coverage line renders on EVERY outcome including `ok`.
 *
 * NEVER A BYTE PROGRESS BAR. `scan.bytes_scanned` is a CHARGE: measured
 * at 551,648,566 against a budget_bytes of 536,870,912, 2.75 percent
 * OVER its own budget. A quantity that exceeds its own budget cannot be
 * a fraction of anything. Progress is transcripts_scanned over
 * transcripts_in_scope, both integers, both monotone.
 *
 * A WITHHELD SNIPPET IS A REAL HIT. Secret-bearing matches arrive with
 * `snippet: null` and a `snippet_state`; the server says so itself with
 * `withholding_never_suppresses_a_hit: true`. The row still renders with
 * its transcript, line and offset, reading as "preview withheld", never
 * as an error and never as a missing result.
 *
 * This file is the only interpreter of `meta.scan.status`, for the same
 * reason archive-outcome.js is the only interpreter of `result_status`:
 * two branch sets on one field drift, and one starts calling a
 * budget_exhausted a complete.
 *
 * No innerHTML. Snippet masking is archive-mask.js's concern, not this
 * file's.
 */

console.log('[ArchiveSearch Module] Loading...');

(function () {
    'use strict';

    // The envelope interpretation and the pure renderers live in
    // archive-search-render.js, which MUST load before this file. They
    // are re-exported below so ArchiveSearch stays the one name a
    // caller has to know.
    var R = window.ArchiveSearchRender;
    if (!R) {
        console.error('[ArchiveSearch] MISSING DEPENDENCY: ' +
            'window.ArchiveSearchRender. Load ' +
            'client/js/archive-search-render.js BEFORE this file; without ' +
            'it no hit and no scan verdict can be rendered at all.');
    }
    var ROOT_CLASS = R.ROOT_CLASS;
    var SCAN_STATUSES = R.SCAN_STATUSES;
    var RESUME_KINDS = R.RESUME_KINDS;
    var WITHHELD_STATES = R.WITHHELD_STATES;
    var el = R.el;
    var scanStatus = R.scanStatus;
    var resumeAffordance = R.resumeAffordance;
    var coverageSentence = R.coverageSentence;
    var scanProgress = R.scanProgress;
    var isWithheld = R.isWithheld;
    var renderHit = R.renderHit;

    /**
     * Description: build the search view.
     * Inputs: options (object) - {document, api, onOpenHit}.
     * Output: {element, run, resume, lastAffordance}
     * Example:
     *   var s = ArchiveSearch.create({api: window.API});
     *   await s.run({q: 'restic', projectId: 12});
     */
    function create(options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveSearch.create needs a document');
        var api = opts.api;
        var onOpenHit = typeof opts.onOpenHit === 'function' ? opts.onOpenHit : function () {};

        var root = el(doc, 'section', ROOT_CLASS, null);
        var coverage = el(doc, 'p', ROOT_CLASS + '__coverage', null);
        var hitList = el(doc, 'ul', ROOT_CLASS + '__hits', null);
        var footer = el(doc, 'div', ROOT_CLASS + '__footer', null);
        root.appendChild(coverage);
        root.appendChild(hitList);
        root.appendChild(footer);

        var query = null;
        var hits = [];
        var affordance = null;

        /**
         * Description: render the resume control for the CURRENT
         *   affordance. The two kinds get different labels, different
         *   data-resume-kind values and different explanatory text, so
         *   they are distinguishable on structure and not only on prose.
         * Inputs: none. Output: void.
         */
        function paintResume() {
            if (!affordance) return;
            var box = el(doc, 'div', ROOT_CLASS + '__resume', null);
            box.setAttribute('data-resume-kind', affordance.kind);
            box.appendChild(el(doc, 'p', ROOT_CLASS + '__resume-reason', affordance.reason));
            if (affordance.kind === RESUME_KINDS.MORE_HITS ||
                    affordance.kind === RESUME_KINDS.MORE_SCOPE) {
                var btn = el(doc, 'button', ROOT_CLASS + '__resume-btn', affordance.label);
                btn.setAttribute('type', 'button');
                btn.setAttribute('data-action',
                    affordance.kind === RESUME_KINDS.MORE_HITS ? 'load-more-hits' : 'resume-scan');
                btn.setAttribute('data-cursor-field', affordance.field);
                if (affordance.blocked) {
                    // A control that silently fails is worse than a
                    // stated blocker, so it is emitted and disabled with
                    // the reason attached rather than dropped.
                    btn.setAttribute('disabled', '');
                    btn.setAttribute('data-blocked-reason', affordance.reason);
                } else {
                    btn.addEventListener('click', function () { resume(); });
                }
                box.appendChild(btn);
            }
            footer.appendChild(box);
        }

        /**
         * Description: apply one search response.
         * Inputs: r (object) - a callEnvelope result. append (boolean).
         * Output: string - the outcome token.
         */
        function apply(r, append) {
            var envelope = r.transportError ? null : r.envelope;
            var classified = window.ArchiveOutcome.classify(envelope);
            affordance = resumeAffordance(envelope);
            coverage.textContent = coverageSentence(envelope);
            footer.textContent = '';
            if (!append) hitList.textContent = '';

            var rows = (envelope && Array.isArray(envelope.result)) ? envelope.result : [];
            if (append) { hits = hits.concat(rows); } else { hits = rows.slice(); }
            for (var i = 0; i < rows.length; i++) {
                hitList.appendChild(renderHit(doc, rows[i], { onOpen: onOpenHit }));
            }

            // The outcome block carries the empty/partial/cannot-determine
            // distinction. It is rendered for every token that is not a
            // plain `ok`, and it sits ALONGSIDE any hits that did arrive
            // rather than replacing them.
            if (classified.token !== 'ok') {
                // `omitActions: ['resume']` IS THE FIX FOR TWO IDENTICAL
                // PRIMARY BUTTONS. This view renders its OWN resume
                // control below (paintResume), and the generic outcome
                // block used to render one too - so a partial search
                // painted "Resume the scan" twice, a few hundred pixels
                // apart, from two modules that did not know about each
                // other. THIS one wins because it is the kind-aware one:
                // `data-resume-kind` distinguishes more-hits from
                // more-scope from nothing-to-resume, and states in words
                // why a blocked resume is blocked. The generic block
                // cannot make that distinction and would offer a single
                // undifferentiated button for three different situations.
                var block = window.ArchiveOutcomeView.renderOutcomeBlock(
                    r.transportError ? null : r.envelope,
                    { document: doc, omitActions: ['resume'] });
                if (r.transportError) {
                    block.appendChild(el(doc, 'p', ROOT_CLASS + '__transport-reason',
                        r.transportError));
                }
                footer.appendChild(block);
            }
            paintResume();
            return classified.token;
        }

        /**
         * Description: run a NEW search. Discards prior hits: a new
         *   question does not inherit an old answer's coverage.
         * Inputs: q (object) - {q, transcriptId, projectId, corpusId,
         *   hostId, limit, caseSensitive}.
         * Output: Promise<string> - the outcome token.
         */
        function run(q) {
            query = q || {};
            hits = [];
            affordance = null;
            hitList.textContent = '';
            footer.textContent = '';
            coverage.textContent = 'Scanning. Coverage so far: NOT KNOWN.';
            return Promise.resolve(api.searchArchive(query)).then(function (r) {
                return apply(r, false);
            });
        }

        /**
         * Description: continue the previous response along the ONE
         *   dimension its scan status named. Reads `affordance.cursor`,
         *   which was resolved from exactly one meta field; this function
         *   never touches meta itself, so it cannot pick the wrong one.
         * Inputs: none.
         * Output: Promise<string> - the outcome token, or 'blocked'.
         */
        function resume() {
            if (!affordance || !affordance.cursor) return Promise.resolve('blocked');
            var next = {};
            for (var k in query) { if (Object.prototype.hasOwnProperty.call(query, k)) next[k] = query[k]; }
            next.cursor = affordance.cursor;
            return Promise.resolve(api.searchArchive(next)).then(function (r) {
                // Both kinds APPEND: more matches add rows, and more
                // scope adds whatever the newly-read transcripts hold.
                // Neither discards what was already found.
                return apply(r, true);
            });
        }

        return {
            element: root,
            run: run,
            resume: resume,
            /** Description: the current affordance, for tests. Output: object|null. */
            lastAffordance: function () { return affordance; },
            /** Description: accumulated hits, for tests. Output: Array. */
            hits: function () { return hits.slice(); }
        };
    }

    window.ArchiveSearch = {
        create: create,
        resumeAffordance: resumeAffordance,
        coverageSentence: coverageSentence,
        scanProgress: scanProgress,
        scanStatus: scanStatus,
        isWithheld: isWithheld,
        renderHit: renderHit,
        RESUME_KINDS: RESUME_KINDS,
        SCAN_STATUSES: SCAN_STATUSES,
        WITHHELD_STATES: WITHHELD_STATES,
        ROOT_CLASS: ROOT_CLASS
    };
    console.log('[ArchiveSearch Module] Exported as window.ArchiveSearch');
})();
