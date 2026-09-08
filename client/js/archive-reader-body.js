/**
 * The reader's BODY-REQUEST POLICY: which cache entry a row renders
 * with, which bodies the reader is allowed to fetch on its own, and
 * what happens when a person asks for one the auto path refused.
 *
 * WHY THIS IS ITS OWN FILE. The reader shell owns the scroller, the rAF
 * loop and the click listener. This is the one part of it that owns a
 * POLICY rather than a mechanism, and the policy is the security-
 * relevant part: every body that reaches a row passes through
 * entryFor(), which is what makes archive-mask.js's secret masking
 * unavoidable. Any future shortcut that renders a body WITHOUT going
 * through this file is a credential-disclosure path. It is much easier
 * to hold that invariant in a 90-line file than in a 700-line shell.
 *
 * FOUR CASES, NOT TWO. entryFor() distinguishes cached, in flight,
 * refused by a gate, and NOT REQUESTED. The fourth is the one that
 * matters: a row outside the fetch window returns null and renders as a
 * sized placeholder, NEVER as a spinner, because a spinner that can
 * never resolve is a false "working on it" for a request nobody made.
 *
 * THE GATE IS EVALUATED BEFORE ANY NETWORK HAPPENS. requestBodies()
 * auto-fetches ONLY the freely renderable class; a 54 MB body is never
 * pulled by scrolling past it. The hard gate is unreachable even from
 * the explicit path - cache.request() refuses it whatever `force` says
 * - so renderAnyway() can only ever lift a SOFT gate.
 *
 * Depends on archive-body-cache.js. Exports window.ArchiveReaderBody.
 */

console.log('[ArchiveReaderBody Module] Loading...');

(function () {
    'use strict';

    /**
     * Build the body-request policy for one reader instance.
     *
     * @param {object} ctx the reader's own handles:
     *   `cache` the body cache, or null when no api was supplied;
     *   `items()` returns the current grouped item array;
     *   `schedule()` queues one repaint on the next animation frame.
     * @returns {object} {entryFor, requestBodies, renderAnyway}
     * @example
     *   var body = ArchiveReaderBody.createBodyPolicy(ctx);
     *   var entry = body.entryFor(item);   // may be null: not requested
     */
    function createBodyPolicy(ctx) {
        /**
         * The cache entry a row should render with. Distinguishes all
         * four cases: cached, in flight, refused by a gate, and NOT
         * REQUESTED (null), which renders as a sized placeholder so a
         * row outside the fetch window never shows a spinner that
         * cannot resolve.
         * @param {?object} it a spine row @returns {?object} cache entry
         */
        function entryFor(it) {
            var C = window.ArchiveBodyCache;
            var cache = ctx.cache;
            if (!cache || !it || it.kind === 'progress-run') return null;
            if (it.body_id === null || it.body_id === undefined) return null;
            var hit = cache.get(it.body_id);
            if (hit) return hit;
            if (cache.isLoading(it.body_id)) {
                return { state: C.STATE_LOADING, text: null, chars: 0, masked: 0 };
            }
            var gate = cache.gateFor(it);
            if (gate.state === C.STATE_OK) return null;
            return { state: gate.state, text: null, chars: 0, masked: 0,
                reason: gate.reason, findingCount: 0,
                bodyHref: it.body_href || null, gated: true };
        }

        /**
         * Ask the cache for the bodies in the render window, honouring
         * the gates. NORMATIVE: cache.request() evaluates gateFor() from
         * the spine BEFORE any network happens, so a 54 MB body is never
         * fetched by the auto path.
         * @param {object} win a windowFor() result @returns {void}
         */
        function requestBodies(win) {
            var cache = ctx.cache;
            if (!cache) return;
            var items = ctx.items();
            for (var i = win.first; i <= win.last; i++) {
                var it = items[i];
                if (!it || it.kind === 'progress-run') continue;
                if (it.body_id === null || it.body_id === undefined) continue;
                if (cache.get(it.body_id) || cache.isLoading(it.body_id)) continue;
                var gate = cache.gateFor(it);
                // Only the freely renderable class is auto-fetched.
                // Everything else waits for the reader to ask, or can
                // never be asked for at all.
                if (gate.state !== window.ArchiveBodyCache.STATE_OK) continue;
                cache.request(it).then(ctx.schedule);
            }
        }

        /**
         * Fetch a soft-gated body because the reader asked. The hard gate
         * is unreachable from here: cache.request() refuses it whatever
         * `force` says.
         * @param {number} index item index
         * @returns {Promise<?object>} the cache entry
         */
        function renderAnyway(index) {
            var it = ctx.items()[index];
            if (!it || it.kind === 'progress-run' || !ctx.cache) {
                return Promise.resolve(null);
            }
            return ctx.cache.request(it, true).then(function (e) {
                ctx.schedule();
                return e;
            });
        }

        return {
            entryFor: entryFor,
            requestBodies: requestBodies,
            renderAnyway: renderAnyway
        };
    }

    window.ArchiveReaderBody = { createBodyPolicy: createBodyPolicy };
    console.log('[ArchiveReaderBody Module] Exported as window.ArchiveReaderBody');
})();
