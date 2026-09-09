/**
 * Session status summary - roll a set of sessions up into ONE LED.
 *
 * ONE COMPONENT, TWO PLACES. The roll-up is not a header-shaped dot: it
 * is `StatusLed.ledHtml` with an (inner, outer) pair this module folds
 * out of the children, so a group header and a row cannot draw two
 * different vocabularies. The finished-turn ring in particular is the
 * SAME ring on both.
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
        // `inner` here is the STOPPED half of this bucket. A group whose
        // only members are `notice` renders light blue instead - see
        // summarizeStates.
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
        // `notice` joins `waiting-input` in the SAME bucket even though
        // the five-colour pass gave it its own hue. The bucket answers
        // "what is the most interesting thing in this group", and both
        // of these are one answer: a session that wants the user without
        // being stopped by a yes/no. The colour split is a rendering
        // decision on the ROW; hoisting it into the fold would change the
        // documented priority (permission > input > working > unread >
        // done > dead > unknown), which it must not.
        if (l.inner === 'waiting-input' || l.inner === 'notice') return 'input';
        if (l.inner === 'working') return 'working';
        if (l.outer === 'unread') return 'unread';
        if (l.inner === 'done') return 'done';
        // `disconnected` is a transport fact and no group feeds one in
        // today - children come from a REST listing, which has no socket.
        // It buckets with `dead` rather than adding an eighth bucket
        // because they paint the same red and rank the same way: neither
        // is the headline for a group that also holds live sessions.
        if (l.inner === 'dead' || l.inner === 'disconnected') return 'dead';
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
        // See the `input` bucket note in the loop below.
        let inputIsStopped = false;

        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            if (!row || typeof row !== 'object') continue;
            total += 1;
            const led = globalThis.StatusLed.ledStateFor(row);
            const bucket = bucketFor(led);
            present[bucket] = true;
            // ONE BUCKET, TWO HUES. The `input` bucket holds both
            // `waiting-input` (stopped on a startup prompt, yellow) and
            // `notice` (still working, wants a look, light blue). The
            // bucket's RANK is the same for both - that is the product
            // decision and it does not move - but the header still has
            // to paint one of them, and a header that disagrees with its
            // only child is a bug this suite already guards. Yellow wins
            // inside the bucket, because a stopped session is the one
            // that will not move until someone goes to it.
            if (bucket === 'input' && led.inner === 'waiting-input') {
                inputIsStopped = true;
            }
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
                const inner =
                    entry.key === 'input' && !inputIsStopped
                        ? 'notice'
                        : entry.inner;
                return {
                    inner: inner,
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
     * The summary LED, as one HTML string.
     *
     * Description: What a group header and the launchpad top bar both
     *   render, and it is EXACTLY the component the rows render - this
     *   function only picks the (inner, outer) pair and hands it to
     *   `StatusLed.ledHtml`. A roll-up therefore takes every treatment a
     *   row LED takes, including the green-ring-with-grey-centre that
     *   says a turn finished in here and nobody has looked. Building a
     *   second dot for headers is what would let the two drift.
     *
     *   THERE IS NO LONGER A NUMERIC BADGE BESIDE IT. A yellow "(n)" pill
     *   used to carry the unread count; it was removed on 2026-09-09 at
     *   the owner's request. The count is not replaced by anything,
     *   deliberately: the ring already says "there is something here for
     *   you", and a second indicator for one fact is how two indicators
     *   end up disagreeing. `summarizeStates` still RETURNS
     *   `unreadCount` - it is a measured property of the fold and cheap
     *   to keep - but nothing renders it.
     * Inputs:
     *   children (Array|null) - as summarizeStates.
     *   opts (Object|null) - `{size}` forwarded to ledHtml.
     * Output:
     *   string - HTML for one `.status-led`.
     * Example:
     *   summaryHtml([{activity_status: 'idle', unread: true}])
     *   // '<span class="status-led" data-inner="done" data-outer="unread"
     *   //   ...></span>'
     */
    function summaryHtml(children, opts) {
        const o = opts || {};
        const s = summarizeStates(children);
        const label =
            s.total === 0
                ? 'no sessions'
                : s.bucket + ' - ' + s.total + ' session' + (s.total === 1 ? '' : 's');
        return globalThis.StatusLed.ledHtml({
            inner: s.inner,
            outer: s.outer,
            size: o.size,
            title: label,
        });
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
