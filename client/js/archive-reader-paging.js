/**
 * The reader's PAGER: the single entry point for "load the next window"
 * of transcript lines, the button that offers it, and the in-flight
 * guard that keeps the two from racing.
 *
 * WHY THIS IS ITS OWN FILE. The paging guard is the one piece of reader
 * state with two independent callers - the pager button and the `m`
 * key - and a second parallel path would let a key double-fetch while
 * the button is mid-flight and append two pages out of order. Keeping
 * the guard, the button's three visible states and the settle-on-reject
 * behaviour in ONE file makes that invariant readable in one screen
 * instead of spread across a 700-line shell.
 *
 * THREE OUTCOMES THAT ARE NOT A FETCH, and they are NAMED STRINGS, never
 * null: PAGE_NO_PAGER ("nobody wired a callback"), PAGE_COMPLETE
 * ("there is nothing left to load") and PAGE_FAILED ("the page was
 * asked for and did not come back") are three different findings, and a
 * caller that cannot tell them apart cannot report honestly.
 *
 * A REJECTION CLEARS THE IN-FLIGHT STATE EXACTLY LIKE A RESOLUTION. A
 * failed page that left the button disabled would be a dead end that
 * looks like the end of the transcript - the worst shape this failure
 * can take, because it is indistinguishable from success. The refusal
 * itself is rendered by the fetch owner into the pane, not by the
 * button, so the button always returns to idle.
 *
 * Exports window.ArchiveReaderPaging.
 */

console.log('[ArchiveReaderPaging Module] Loading...');

(function () {
    'use strict';

    /**
     * Rows one paging request asks for - the server's own page size for
     * the line index. The pager label states it, so nobody has to guess
     * how far one click moves.
     * @type {number}
     */
    var DEFAULT_PAGE_ROWS = 500;

    /** @type {string} data-action on the pager button. */
    var ACTION_LOAD_MORE = 'load-more-lines';

    // requestMoreLines() outcomes that are NOT a fetch. Named strings,
    // never null: "no pager was wired", "there is nothing left to load"
    // and "the page came back" are three different findings.
    /** @type {string} */ var PAGE_NO_PAGER = 'no-pager';
    /** @type {string} */ var PAGE_COMPLETE = 'complete';
    /** @type {string} */ var PAGE_FAILED = 'failed';

    /**
     * Build the pager for one reader instance. The in-flight promise is
     * held HERE, so there is exactly one of it per reader.
     *
     * @param {object} ctx the reader's own handles:
     *   `el(tag, cls, text)` the reader's element helper;
     *   `rootClass` the reader's BEM root class;
     *   `pageRows` rows per request, for the button label;
     *   `onLoadMore()` returns the current callback, or null - a GETTER
     *     because setOnLoadMore() can replace it after construction;
     *   `spineComplete()` returns whether the last page has arrived;
     *   `render()` repaints, so the button's busy state becomes visible.
     * @returns {object} {requestMoreLines, pagerButton, isLoadingMore}
     * @example
     *   var pager = ArchiveReaderPaging.createPager(ctx);
     *   pager.requestMoreLines();       // the `m` key or the button
     */
    function createPager(ctx) {
        /** @type {?Promise} the live paging promise, or null. */
        var inFlight = null;

        /**
         * Build the pager control. THREE STATES, all visible: idle is
         * enabled and names the page size; in-flight is `disabled` plus
         * `aria-busy` and says so in the label; a REFUSAL is not
         * rendered here at all - the fetch owner renders the outcome
         * block into the pane and this control returns to idle, so a
         * failed page is never a permanently dead button.
         * @returns {Element}
         */
        function pagerButton() {
            var busy = inFlight !== null;
            var b = ctx.el('button', ctx.rootClass + '__more', busy
                ? 'Loading ' + ctx.pageRows + ' more lines...'
                : 'Load ' + ctx.pageRows + ' more lines');
            b.setAttribute('type', 'button');
            b.setAttribute('data-action', ACTION_LOAD_MORE);
            if (busy) {
                b.setAttribute('disabled', 'disabled');
                b.setAttribute('aria-busy', 'true');
            }
            return b;
        }

        /**
         * THE SINGLE ENTRY POINT FOR "load the next window". The pager
         * button and every external caller (the `m` key) go through it,
         * so the in-flight guard, the disabled/aria-busy button state
         * and the clear-on-settle behaviour are identical on both paths.
         * A second parallel path would let a key double-fetch while the
         * button is mid-flight and append two pages out of order.
         * A rejection clears the in-flight state exactly like a
         * resolution: a failed page that left the button disabled would
         * be a dead end that looks like the end of the transcript. The
         * refusal itself is rendered by the fetch owner, not the pager.
         * @returns {Promise<*>} the callback's value, or a named string
         *   (PAGE_NO_PAGER / PAGE_COMPLETE / PAGE_FAILED)
         */
        function requestMoreLines() {
            if (inFlight) return inFlight;
            if (ctx.spineComplete()) return Promise.resolve(PAGE_COMPLETE);
            var onLoadMore = ctx.onLoadMore();
            if (!onLoadMore) {
                console.warn('[ArchiveReader] requestMoreLines called with no ' +
                    'pager wired. Call setOnLoadMore(fn) first; nothing was ' +
                    'fetched and no page is in flight.');
                return Promise.resolve(PAGE_NO_PAGER);
            }
            var settle = function () { inFlight = null; ctx.render(); };
            inFlight = Promise.resolve()
                .then(function () { return onLoadMore(); })
                .then(function (v) { settle(); return v; },
                    function (err) {
                        settle();
                        console.error('[ArchiveReader] the pager callback ' +
                            'rejected; no rows were appended: ', err);
                        return PAGE_FAILED;
                    });
            ctx.render();
            return inFlight;
        }

        return {
            requestMoreLines: requestMoreLines,
            pagerButton: pagerButton,
            /** Is a page in flight. @returns {boolean} */
            isLoadingMore: function () { return inFlight !== null; }
        };
    }

    window.ArchiveReaderPaging = {
        createPager: createPager,
        DEFAULT_PAGE_ROWS: DEFAULT_PAGE_ROWS,
        ACTION_LOAD_MORE: ACTION_LOAD_MORE,
        PAGE_NO_PAGER: PAGE_NO_PAGER,
        PAGE_COMPLETE: PAGE_COMPLETE,
        PAGE_FAILED: PAGE_FAILED
    };
    console.log('[ArchiveReaderPaging Module] Exported as window.ArchiveReaderPaging');
})();
