/**
 * Variable-height windowing for the archive reader.
 *
 * WHY THIS IS A SCROLL HANDLER AND NOT AN IntersectionObserver PER ROW.
 * The largest transcript in this corpus is 30,805 lines (id 5767,
 * measured 2026-08-31). One observer per row is 30,805 live compositor
 * registrations, each with its own root-margin geometry, and creating
 * them all blocks the main thread on the exact transcript the feature
 * exists to open. The rejection is structural, not a preference. This
 * engine is a binary search over a Float64Array plus a forward walk:
 * O(log N) to find the window and O(visible) to fill it, and neither
 * term depends on N.
 *
 * THE SCROLLBAR IS HONEST. Total height starts as the sum of estimates
 * derived from `body_chars` on the spine and converges as real rows are
 * measured. It is never rounded to a pretty number and no page count is
 * published, because a page count over variable-height rows is a number
 * nobody computed.
 *
 * THE ANTI-JUMP RULE IS THE HARD PART. Every estimate is wrong. When a
 * row ABOVE the viewport turns out to be taller or shorter than its
 * estimate, every row below it moves, and the content under the reader's
 * eyes leaps. On a 30,805-line document that loses their place
 * permanently. So `applyMeasurements()` returns the exact pixel delta
 * contributed by rows strictly above the first visible row, and the
 * caller MUST add it to scrollTop in the SAME frame, before paint.
 * Deferring it to a later frame is worse than not compensating at all,
 * because a one-frame leap reads as a bug rather than as scrolling.
 *
 * NO DOM IN THIS FILE. It is pure geometry over typed arrays, so the
 * whole windowing algorithm is testable under plain node with no
 * harness, and no rendering path can reach around it.
 *
 * Depends on nothing. Exports window.ArchiveVirtualList.
 */

console.log('[ArchiveVirtualList Module] Loading...');

(function () {
    'use strict';

    /**
     * Gutter plus the role/timestamp line that every row carries even
     * when its body is a placeholder. Pixels.
     * @type {number}
     */
    var ROW_CHROME_PX = 34;

    /**
     * Characters that fit on one wrapped line at the reader's monospace
     * measure. Used only to turn `body_chars` into a height guess.
     * @type {number}
     */
    var CHARS_PER_LINE = 96;

    /**
     * Rendered line box height in the reader's monospace stack. Pixels.
     * @type {number}
     */
    var LINE_HEIGHT_PX = 18;

    /**
     * A body is collapsed to at most this height until the reader opens
     * it. This is what keeps the estimate honest at the extremes:
     * without it, line 62 of transcript 19243 (54,376,859 chars,
     * measured) would estimate at roughly 10 million pixels and the
     * scrollbar would stop resolving every other line in the file.
     * @type {number}
     */
    var COLLAPSED_MAX_PX = 240;

    /**
     * Extra height, on top of ROW_CHROME_PX, for a row whose body was
     * not fetched: the size line plus whatever action the gate offers.
     * Fixed, because there is no body to measure.
     * @type {number}
     */
    var PLACEHOLDER_EXTRA_PX = 44;

    /**
     * Rows rendered above and below the visible window. Bodies are
     * requested for the visible window plus this margin.
     * @type {number}
     */
    var OVERSCAN_ROWS = 12;

    /**
     * A measured height must differ from the current estimate by more
     * than this before it counts as a correction. Sub-pixel churn from
     * fractional line boxes is not a correction, and treating it as one
     * produces an endless reconcile loop.
     * @type {number}
     */
    var HEIGHT_EPSILON_PX = 0.5;

    /**
     * Fixed height of a collapsed `progress` run row. Collapsed runs
     * participate in the offset table as ordinary single rows, so
     * expanding one is just a height correction (see applyMeasurements).
     * @type {number}
     */
    var PROGRESS_ROW_PX = 30;

    /**
     * Description: height guess for one spine row, before its body has
     *   ever been fetched or measured. Driven by `body_chars`, which is
     *   present on the spine even with include_bodies=false (verified
     *   live 2026-08-31), so no fetch is needed to lay out the file.
     * Inputs: row (object|null) - a spine row. Reads `body_chars` and
     *   `body_state` only.
     * Output: number - pixels, always finite and >= ROW_CHROME_PX.
     * Example: estimateHeight({body_state: 'included', body_chars: 960})
     *   // -> 34 + 18 * ceil(960/96) = 214
     */
    function estimateHeight(row) {
        if (!row || typeof row !== 'object') {
            return ROW_CHROME_PX + PLACEHOLDER_EXTRA_PX;
        }
        if (row.kind === 'progress-run') return PROGRESS_ROW_PX;

        var chars = row.body_chars;
        // A row with no usable char count is a placeholder, not a zero
        // height row. A zero height row is invisible and unclickable,
        // which is a could-not-evaluate rendered as nothing.
        if (!Number.isFinite(chars) || chars < 0) {
            return ROW_CHROME_PX + PLACEHOLDER_EXTRA_PX;
        }
        // Anything the reader will not render inline gets the fixed
        // placeholder height regardless of how enormous it is.
        if (row.body_state === 'withheld_too_large') {
            return ROW_CHROME_PX + PLACEHOLDER_EXTRA_PX;
        }
        var lines = Math.ceil(chars / CHARS_PER_LINE);
        return Math.min(COLLAPSED_MAX_PX, ROW_CHROME_PX + LINE_HEIGHT_PX * lines);
    }

    /**
     * Description: index of the last row whose top is <= y. Binary
     *   search over a monotonically non-decreasing offset table.
     * Inputs: offsets (Float64Array) - length N+1, offsets[N] is the
     *           total content height.
     *         y (number) - a pixel position in content space.
     * Output: number - row index clamped to [0, N-1].
     * Example: rowAt(Float64Array.of(0, 10, 30, 60), 25) // -> 1
     */
    function rowAt(offsets, y) {
        var lo = 0;
        var hi = offsets.length - 2;
        if (hi < 0) return 0;
        while (lo < hi) {
            var mid = (lo + hi + 1) >> 1;
            if (offsets[mid] <= y) lo = mid; else hi = mid - 1;
        }
        return lo;
    }

    /**
     * Description: build the windowing engine for one list of rows.
     *   Holds two Float64Arrays and nothing else; it does not know what
     *   a row looks like and never touches the DOM.
     * Inputs: options (object)
     *   - count (number): how many rows.
     *   - estimate (function(index): number): initial height per row.
     *       Defaults to a constant placeholder height, which is correct
     *       for the fixed-height transcript list caller.
     *   - overscan (number): rows above and below the viewport.
     * Output: object - the engine, see methods below.
     * Example:
     *   const vl = createList({count: 30805,
     *       estimate: i => estimateHeight(spine[i])});
     *   vl.windowFor(120000, 800);   // -> {first, last, ...}
     */
    function createList(options) {
        var opts = options || {};
        var overscan = Number.isFinite(opts.overscan) ? opts.overscan : OVERSCAN_ROWS;

        var heights = new Float64Array(0);
        var offsets = new Float64Array(1);
        /** index -> measured height, waiting for the next flush. */
        var pending = new Map();

        /**
         * Description: rebuild `offsets` from index `from` forward. O(N-k)
         *   over a Float64Array, which is microseconds at N = 30,805. A
         *   Fenwick tree would be asymptotically nicer and would make the
         *   anti-jump arithmetic considerably harder to get right; revisit
         *   past roughly 500,000 rows.
         * Inputs: from (number) - lowest index whose height changed.
         * Output: void.
         */
        function rebuild(from) {
            var i = from < 0 ? 0 : from;
            for (; i < heights.length; i++) offsets[i + 1] = offsets[i] + heights[i];
        }

        /**
         * Description: (re)declare how many rows there are and seed every
         *   height from the estimator. Clears pending measurements,
         *   because they refer to indices that may no longer mean the
         *   same row.
         * Inputs: count (number), estimate (function(index): number|null)
         * Output: void.
         */
        function setCount(count, estimate) {
            var n = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
            var est = typeof estimate === 'function'
                ? estimate
                : (typeof opts.estimate === 'function' ? opts.estimate : null);
            heights = new Float64Array(n);
            offsets = new Float64Array(n + 1);
            pending.clear();
            for (var i = 0; i < n; i++) {
                var h = est ? est(i) : (ROW_CHROME_PX + PLACEHOLDER_EXTRA_PX);
                // A non-finite or negative estimate would corrupt every
                // offset after it and silently break the binary search's
                // monotonicity precondition. Fall back rather than store it.
                heights[i] = Number.isFinite(h) && h > 0
                    ? h : (ROW_CHROME_PX + PLACEHOLDER_EXTRA_PX);
            }
            rebuild(0);
        }

        /**
         * Description: which rows to render for a given scroll position.
         *   `first`/`last` include the overscan; `firstVisible`/
         *   `lastVisible` do not. Both are needed: the render window is
         *   the wide one, and the anti-jump pivot is the narrow one.
         * Inputs: scrollTop (number), viewportHeight (number)
         * Output: {first, last, firstVisible, lastVisible, offsetTop,
         *          totalHeight} - all numbers. `last` is INCLUSIVE.
         *   On an empty list every index is 0 and offsetTop is 0.
         * Example: windowFor(0, 800) // -> {first: 0, last: 15, ...}
         */
        function windowFor(scrollTop, viewportHeight) {
            var n = heights.length;
            if (n === 0) {
                return { first: 0, last: -1, firstVisible: 0, lastVisible: -1,
                    offsetTop: 0, totalHeight: 0 };
            }
            var top = Number.isFinite(scrollTop) && scrollTop > 0 ? scrollTop : 0;
            var vh = Number.isFinite(viewportHeight) && viewportHeight > 0
                ? viewportHeight : 0;

            var firstVisible = rowAt(offsets, top);
            var lastVisible = firstVisible;
            // Forward walk until the accumulated height covers the
            // viewport. Bounded by the viewport, never by N.
            while (lastVisible + 1 < n && offsets[lastVisible + 1] < top + vh) {
                lastVisible++;
            }
            var first = Math.max(0, firstVisible - overscan);
            var last = Math.min(n - 1, lastVisible + overscan);
            return {
                first: first,
                last: last,
                firstVisible: firstVisible,
                lastVisible: lastVisible,
                offsetTop: offsets[first],
                totalHeight: offsets[n]
            };
        }

        /**
         * Description: record a real measured height for one row. Does
         *   NOT rebuild anything; corrections accumulate and are applied
         *   once per animation frame by applyMeasurements(). Batching is
         *   what keeps a ResizeObserver callback storm from thrashing
         *   layout once per row.
         * Inputs: index (number), px (number)
         * Output: boolean - true if the value was accepted as a pending
         *   correction, false if it was out of range, not finite, or
         *   within HEIGHT_EPSILON_PX of the current height.
         */
        function measure(index, px) {
            if (!Number.isInteger(index) || index < 0 || index >= heights.length) {
                return false;
            }
            if (!Number.isFinite(px) || px < 0) return false;
            if (Math.abs(px - heights[index]) <= HEIGHT_EPSILON_PX) return false;
            pending.set(index, px);
            return true;
        }

        /**
         * Description: apply every pending measurement and report the
         *   scroll compensation the caller owes.
         *
         *   NORMATIVE: if the returned `delta` is non-zero the caller
         *   MUST do `scroller.scrollTop += delta` in the SAME frame,
         *   before paint. `delta` is the sum of corrections for rows
         *   strictly ABOVE `firstVisibleIndex`; those rows move every
         *   subsequent row by exactly that much, and adding it back is
         *   what stops the content leaping under the reader's eyes.
         * Inputs: firstVisibleIndex (number) - the pivot. Rows at or
         *   below it are under the reader's eyes and their corrections
         *   must NOT be compensated, because those rows are supposed to
         *   grow downward.
         * Output: {delta, applied, lowestChanged, totalHeight}
         *   delta (number) pixels to add to scrollTop;
         *   applied (number) how many rows changed;
         *   lowestChanged (number) lowest index rebuilt, -1 if none;
         *   totalHeight (number) the new content height.
         * Example: const r = vl.applyMeasurements(win.firstVisible);
         *          if (r.delta) scroller.scrollTop += r.delta;
         */
        function applyMeasurements(firstVisibleIndex) {
            var n = heights.length;
            if (pending.size === 0) {
                return { delta: 0, applied: 0, lowestChanged: -1,
                    totalHeight: n ? offsets[n] : 0 };
            }
            var pivot = Number.isInteger(firstVisibleIndex) ? firstVisibleIndex : 0;
            var delta = 0;
            var lowest = Infinity;
            var applied = 0;

            pending.forEach(function (px, index) {
                var was = heights[index];
                if (Math.abs(px - was) <= HEIGHT_EPSILON_PX) return;
                heights[index] = px;
                applied++;
                if (index < lowest) lowest = index;
                if (index < pivot) delta += (px - was);
            });
            pending.clear();

            if (applied === 0) {
                return { delta: 0, applied: 0, lowestChanged: -1,
                    totalHeight: n ? offsets[n] : 0 };
            }
            rebuild(lowest);
            return {
                delta: delta,
                applied: applied,
                lowestChanged: lowest,
                totalHeight: offsets[n]
            };
        }

        /**
         * Description: current content height in pixels. This is a sum of
         *   real measurements and honest estimates, never a round number
         *   chosen to look tidy.
         * Inputs: none.
         * Output: number.
         */
        function totalHeight() {
            return heights.length ? offsets[heights.length] : 0;
        }

        /**
         * Description: pixel top of one row.
         * Inputs: index (number)
         * Output: number - 0 for an out-of-range index.
         */
        function offsetOf(index) {
            if (!Number.isInteger(index) || index < 0 || index >= offsets.length) {
                return 0;
            }
            return offsets[index];
        }

        /**
         * Description: current best height for one row.
         * Inputs: index (number)
         * Output: number - 0 for an out-of-range index.
         */
        function heightOf(index) {
            if (!Number.isInteger(index) || index < 0 || index >= heights.length) {
                return 0;
            }
            return heights[index];
        }

        return {
            setCount: setCount,
            windowFor: windowFor,
            measure: measure,
            applyMeasurements: applyMeasurements,
            totalHeight: totalHeight,
            offsetOf: offsetOf,
            heightOf: heightOf,
            /** Row count. @returns {number} */
            count: function () { return heights.length; },
            /** Pending, unapplied corrections. @returns {number} */
            pendingCount: function () { return pending.size; },
            /** The live offset table, for assertions. @returns {Float64Array} */
            offsets: function () { return offsets; }
        };
    }

    /**
     * Description: a rendered row's height, or null when it cannot be
     *   measured. NULL IS NOT ZERO: a zero would be written into the
     *   offset table as a real correction and collapse the row to
     *   invisible. An unmeasurable row keeps its estimate instead.
     * Inputs: node (Element) - a painted row.
     * Output: number|null - pixels, or null for could-not-measure.
     * Example: measuredHeight(rowEl) // -> 219.23
     */
    function measuredHeight(node) {
        if (!node) return null;
        if (typeof node.getBoundingClientRect === 'function') {
            var r = node.getBoundingClientRect();
            if (r && Number.isFinite(r.height) && r.height > 0) return r.height;
        }
        if (Number.isFinite(node.offsetHeight) && node.offsetHeight > 0) {
            return node.offsetHeight;
        }
        return null;
    }

    /**
     * Description: read the painted rows' real heights into the list,
     *   apply them, and pay the anti-jump debt. NORMATIVE: the scrollTop
     *   compensation happens HERE, in the SAME CALL as the height write,
     *   before paint. Deferring it produces a visible one-frame leap,
     *   which reads as a bug rather than as scrolling. The caller still
     *   owns the spacer height; this only reports the new total.
     * Inputs: list (object) - a createList() result.
     *         windowEl (Element) - holds the painted rows.
     *         win (object) - the windowFor() result just painted.
     *         scroller (Element|null) - the scrolling ancestor; null
     *           leaves the returned delta unpaid.
     * Output: {delta: number, applied: number, totalHeight: number}
     * Example: reconcileMeasured(list, windowEl, win, scroller)
     *   // -> {delta: -12.5, applied: 3, totalHeight: 109574.06}
     */
    function reconcileMeasured(list, windowEl, win, scroller) {
        var kids = windowEl ? windowEl.childNodes : [];
        for (var k = 0; k < kids.length; k++) {
            var node = kids[k];
            if (!node || !node.getAttribute) continue;
            var idx = parseInt(node.getAttribute('data-index'), 10);
            if (!Number.isInteger(idx)) continue;
            var h = measuredHeight(node);
            if (h !== null) list.measure(idx, h);
        }
        var r = list.applyMeasurements(win.firstVisible);
        if (r.delta !== 0 && scroller) {
            var top = scroller.scrollTop;
            scroller.scrollTop =
                (Number.isFinite(top) && top > 0 ? top : 0) + r.delta;
        }
        return r;
    }

    /**
     * Description: scroll just enough to bring one row fully into view.
     *   It does NOTHING when the row is already visible, because
     *   scrolling on every keypress fights the person reading.
     * Inputs: list (object) - a createList() result.
     *         scroller (Element|null) - the scrolling ancestor.
     *         index (number) - the row to reveal; a non-integer or
     *           negative index means nothing is selected, and is a no-op.
     *         viewportPx (number) - the scroller's usable height.
     * Output: boolean - true when scrollTop was written, false when the
     *   row was already visible or could not be evaluated.
     * Example: scrollRowIntoView(list, scroller, 412, 800) // -> true
     */
    function scrollRowIntoView(list, scroller, index, viewportPx) {
        if (!scroller || !Number.isInteger(index) || index < 0) return false;
        if (!Number.isFinite(viewportPx) || viewportPx <= 0) return false;
        var top = list.offsetOf(index);
        var bottom = top + list.heightOf(index);
        var rawTop = scroller.scrollTop;
        var viewTop = Number.isFinite(rawTop) && rawTop > 0 ? rawTop : 0;
        if (top < viewTop) { scroller.scrollTop = top; return true; }
        if (bottom > viewTop + viewportPx) {
            scroller.scrollTop = bottom - viewportPx;
            return true;
        }
        return false;
    }

    window.ArchiveVirtualList = {
        createList: createList,
        scrollRowIntoView: scrollRowIntoView,
        estimateHeight: estimateHeight,
        measuredHeight: measuredHeight,
        reconcileMeasured: reconcileMeasured,
        rowAt: rowAt,
        ROW_CHROME_PX: ROW_CHROME_PX,
        CHARS_PER_LINE: CHARS_PER_LINE,
        LINE_HEIGHT_PX: LINE_HEIGHT_PX,
        COLLAPSED_MAX_PX: COLLAPSED_MAX_PX,
        PLACEHOLDER_EXTRA_PX: PLACEHOLDER_EXTRA_PX,
        OVERSCAN_ROWS: OVERSCAN_ROWS,
        HEIGHT_EPSILON_PX: HEIGHT_EPSILON_PX,
        PROGRESS_ROW_PX: PROGRESS_ROW_PX
    };
    console.log('[ArchiveVirtualList Module] Exported as window.ArchiveVirtualList');
})();
