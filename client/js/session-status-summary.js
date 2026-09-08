/**
 * Session status summary - roll a set of sessions up into ONE LED.
 *
 * A group header (and the launchpad's top bar, which is the same question
 * asked of every session at once) has to answer "is there anything in
 * here I need to deal with" without the user opening the group. That is a
 * pure fold over the children's states, and it lives here rather than in
 * either caller so the sidebar and the launchpad cannot drift.
 *
 * THE PRIORITY IS THE PRODUCT DECISION, so it is written down once, as
 * data, in SUMMARY_PRIORITY:
 *
 *   permission > input > working > unread > done > dead > unknown
 *
 * Read it as "what is the most interesting thing in this group".
 * `permission` is a session STOPPED on a yes/no; `input` is one that
 * wants the user's eyes (a Notification, or a startup prompt nobody has
 * answered) without being stopped. They were one `waiting` bucket until
 * the 2026-09-08 split; permission leads because it is the only state in
 * the list that is guaranteed to make no progress at all until a human
 * acts, and a group header that hoisted a chatty notification over a
 * parked one would point the user at the wrong session.
 *
 * Both outrank working because they are about the USER and will stay
 * that way until they act; working will resolve on its own. Unread
 * outranks done for the same reason. Dead sits BELOW done deliberately:
 * a dead pane in a group of live ones is not the headline, and hoisting
 * it would make a group with one corpse and nine busy sessions read as
 * dead. Unknown is last because it is the absence of a measurement, and
 * any measured state is more informative than no measurement.
 *
 * Depends on client/js/status-led.js (for the vocabularies) and nothing
 * else. No DOM, no globals beyond that one.
 */

console.log('[SessionStatusSummary Module] Loading...');

(function () {
    /**
     * The fold order. Index 0 wins. Each entry names a summary bucket and
     * the (inner, outer) LED pair that renders it.
     * @type {Array<{key: string, inner: string, outer: string}>}
     */
    const SUMMARY_PRIORITY = [
        { key: 'permission', inner: 'waiting-permission', outer: 'active' },
        { key: 'input', inner: 'waiting-input', outer: 'active' },
        { key: 'working', inner: 'working', outer: 'active' },
        { key: 'unread', inner: 'done', outer: 'unread' },
        { key: 'done', inner: 'done', outer: 'steady' },
        { key: 'dead', inner: 'dead', outer: 'off' },
        { key: 'unknown', inner: 'unknown', outer: 'dim' },
    ];

    /**
     * Which summary bucket one child's (inner, outer) pair falls into.
     *
     * Description: The bucket is derived from the LED state the child
     *   ALREADY resolved to, not from its raw `activity_status`. That is
     *   deliberate: the header then cannot disagree with the rows under
     *   it, because both are reading the same value - and it is why the
     *   startup gate lands in `input` for free, without this function
     *   knowing the gate exists. Note the halo is checked before the dot
     *   for `unread` - a working session with an unread Stop counts as
     *   working (it is moving), but a resting one with the same flag
     *   counts as unread.
     * Inputs:
     *   led (Object|null) - `{inner, outer}` from StatusLed.ledStateFor.
     * Output:
     *   string - one of the SUMMARY_PRIORITY keys.
     * Example:
     *   bucketFor({inner: 'done', outer: 'unread'}) -> 'unread'
     */
    function bucketFor(led) {
        const l = led || {};
        if (l.inner === 'waiting-permission') return 'permission';
        if (l.inner === 'waiting-input') return 'input';
        if (l.inner === 'working') return 'working';
        if (l.outer === 'unread') return 'unread';
        if (l.inner === 'done') return 'done';
        if (l.inner === 'dead') return 'dead';
        return 'unknown';
    }

    /**
     * Summarise a set of sessions into one LED state plus a count.
     *
     * Description: PURE. Takes the raw server rows (each carrying
     *   `activity_status`, `unread` and optionally `startup_gate`),
     *   resolves each through the single mapping in StatusLed, buckets
     *   them, and returns the highest-priority bucket present along with
     *   how many children are unread.
     *
     *   AN EMPTY GROUP IS `unknown`, NOT `done`. A group with no children
     *   has not been measured as quiet; there is simply nothing in it,
     *   and rendering that as a calm green light would be the same false
     *   green this project keeps paying for elsewhere.
     * Inputs:
     *   children (Array|null) - session rows. Each may be
     *     `{activity_status, unread, startup_gate}`. Non-objects are
     *     skipped rather than throwing, so one malformed row cannot blank
     *     a whole header.
     * Output:
     *   Object - `{inner, outer, bucket, unreadCount, total}`.
     * Example:
     *   summarizeStates([{activity_status: 'idle', unread: true},
     *                    {activity_status: 'working'}])
     *   // {inner: 'working', outer: 'active', bucket: 'working',
     *   //  unreadCount: 1, total: 2}
     */
    function summarizeStates(children) {
        const rows = Array.isArray(children) ? children : [];
        const present = Object.create(null);
        let unreadCount = 0;
        let total = 0;

        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            if (!row || typeof row !== 'object') continue;
            total += 1;
            const led = globalThis.StatusLed.ledStateFor(row);
            const bucket = bucketFor(led);
            present[bucket] = true;
            // Counted off the ROW's flag, not off the halo: a working
            // session with an unread Stop buckets as `working` but is
            // still one unread thing waiting for the user, and the badge
            // is a count of those, not a count of unread-coloured lights.
            if (row.unread) unreadCount += 1;
        }

        if (total === 0) {
            return {
                inner: 'unknown',
                outer: 'dim',
                bucket: 'unknown',
                unreadCount: 0,
                total: 0,
            };
        }

        for (let i = 0; i < SUMMARY_PRIORITY.length; i++) {
            const entry = SUMMARY_PRIORITY[i];
            if (present[entry.key]) {
                return {
                    inner: entry.inner,
                    outer: entry.outer,
                    bucket: entry.key,
                    unreadCount: unreadCount,
                    total: total,
                };
            }
        }

        // Unreachable: bucketFor always returns one of the keys above.
        // Returned rather than thrown so a future bucket added to one
        // table and not the other degrades to not-measured instead of
        // taking down the sidebar render.
        return {
            inner: 'unknown',
            outer: 'dim',
            bucket: 'unknown',
            unreadCount: unreadCount,
            total: total,
        };
    }

    /**
     * The summary LED plus its unread-count badge, as one HTML string.
     *
     * Description: What a group header and the launchpad top bar both
     *   render. The badge is omitted entirely at zero rather than shown
     *   as "0" - an empty badge is noise, and its absence is already the
     *   signal. Copy is lowercase and plain, per the project's voice.
     * Inputs:
     *   children (Array|null) - as summarizeStates.
     *   opts (Object|null) - `{size}` forwarded to ledHtml.
     * Output:
     *   string - HTML: one `.status-led` and, when non-zero, one
     *     `.status-summary-badge`.
     * Example:
     *   summaryHtml([{activity_status: 'idle', unread: true}])
     *   // '<span class="status-led" ...></span>
     *   //  <span class="status-summary-badge" ...>1</span>'
     */
    function summaryHtml(children, opts) {
        const o = opts || {};
        const s = summarizeStates(children);
        const label =
            s.total === 0
                ? 'no sessions'
                : s.bucket + ' - ' + s.total + ' session' + (s.total === 1 ? '' : 's');
        let html = globalThis.StatusLed.ledHtml({
            inner: s.inner,
            outer: s.outer,
            size: o.size,
            title: label,
        });
        if (s.unreadCount > 0) {
            const badgeLabel = s.unreadCount + ' unread';
            html +=
                '<span class="status-summary-badge" role="status" ' +
                'title="' +
                badgeLabel +
                '" aria-label="' +
                badgeLabel +
                '">' +
                s.unreadCount +
                '</span>';
        }
        return html;
    }

    const api = {
        SUMMARY_PRIORITY: SUMMARY_PRIORITY.slice(),
        summarizeStates: summarizeStates,
        summaryHtml: summaryHtml,
        bucketFor: bucketFor,
    };

    globalThis.SessionStatusSummary = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }

    console.log('[SessionStatusSummary Module] Loaded');
})();
