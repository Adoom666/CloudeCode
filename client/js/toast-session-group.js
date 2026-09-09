/**
 * One card per session - which toast a session's card is showing.
 *
 * THE DEFECT THIS CLOSES. client/js/toast.js coalesced on (kind,
 * session): twelve `Stop` events for one session became one "Your turn"
 * card, and five `Notification` events became one "wants your attention"
 * card. Two kinds, so two cards, for ONE session. Measured on the
 * owner's screen 2026-09-09: four cards for two sessions, plus a
 * "Dismiss all (9)" row and an overflow row, filling the right half of
 * a phone. The pile the coalescing was written to stop had simply moved
 * up a level.
 *
 * SO A SESSION GETS EXACTLY ONE CARD, and this module answers the only
 * question that creates: which of the session's pending events does that
 * card show.
 *
 * THE ANSWER IS NOT A NEW RANKING. The project already writes its
 * attention order down once, as data, in
 * client/js/session-status-summary.js:
 *
 *   permission > input > working > unread > done > dead > unknown
 *
 * That fold already drives the sidebar group headers and the launchpad
 * top bar. A toast stack that disagreed with the LED beside it would be
 * two indicators for one fact, which is how two indicators end up
 * contradicting each other. So SUMMARY_PRIORITY is READ from that module
 * at call time and never copied; the only thing declared here is which
 * bucket each toast KIND lands in, because the toast kinds are hook
 * event names and the buckets are LED states, and nothing else in the
 * app has had to join those two vocabularies.
 *
 * THE JOIN, and why each one:
 *
 *   PermissionRequest -> permission. Claude is STOPPED on a yes/no. It
 *     leads for the same reason it leads in the fold: it is the only
 *     state guaranteed to make no progress at all until a human acts.
 *   StartupPrompt     -> input. A pane parked on its folder-trust
 *     dialog. `session-startup-gate.js` already renders it as the
 *     `waiting-input` LED, so this is the same bucket that LED folds
 *     into, not a second opinion about it.
 *   Notification      -> input. Claude wants a look and is not blocked.
 *     `bucketFor` in the summary module puts the `notice` LED in the
 *     `input` bucket for exactly this reason; this agrees with it by
 *     construction.
 *   Stop              -> unread. "Your turn": a finished turn nobody has
 *     looked at. That IS the `unread` bucket - a Stop is what sets the
 *     unread flag the LED reads.
 *
 * `working`, `done`, `dead` and `unknown` have no toast kind, and that
 * is expected rather than a gap: nothing fires a hook to announce that
 * work is proceeding normally.
 *
 * AN UNKNOWN KIND IS TREATED AS A NOTIFICATION, never as the least
 * interesting thing in the group. toast.js already refuses to assume a
 * future kind is harmless (`SEVERITY_DEFAULT = 2`, which is
 * Notification's severity); DEFAULT_BUCKET is the same refusal spelled
 * in the same terms, so the two defaults cannot drift into disagreeing
 * about the same unknown card.
 *
 * SEVERITY BREAKS A TIE INSIDE A BUCKET, and it has to. `input` holds
 * both StartupPrompt and Notification, and only one of those is
 * blocking - toast.js marks StartupPrompt severity 3 and cap-exempt for
 * that reason. Picking newest-first inside the bucket would let a chatty
 * Notification take the card away from a session parked on an unanswered
 * trust dialog, dropping the card's severity from 3 to 2 and with it its
 * cap exemption and its `role="alert"`. So the pick is (bucket, then
 * severity, then arrival), and the severity table is passed IN by
 * toast.js rather than copied here - that table is toast.js's, and a
 * second copy is a second thing to forget to update.
 *
 * WHAT THIS BUYS FOR FREE. The pick is a PURE FOLD over whatever the
 * manager currently holds; it keeps no state of its own. So an upgrade
 * (a permission prompt landing on a session already showing "your
 * turn") is just the fold answering differently on the next render, on
 * the SAME card, and a downgrade is impossible by construction - a
 * lesser event cannot win while a higher one is still pending. Hook
 * events arrive unordered, duplicated and droppable; a fold over the
 * live set is idempotent against all three, where an incremental
 * "current worst" variable would not be.
 *
 * Depends on client/js/session-status-summary.js and nothing else. It is
 * read through `globalThis` at CALL time, so script order does not
 * matter. If it is absent this module GROUPS NOTHING - toast.js then
 * falls back to its per-kind coalescing, which is noisier than intended
 * and never wrong. Inventing a local copy of the order would be the one
 * unacceptable answer.
 */

console.log('[ToastSessionGroup Module] Loading...');

(function () {
    /**
     * Toast kind -> a bucket key in SessionStatusSummary.SUMMARY_PRIORITY.
     * @type {Object<string, string>}
     */
    const KIND_BUCKET = {
        PermissionRequest: 'permission',
        StartupPrompt: 'input',
        Notification: 'input',
        Stop: 'unread',
    };

    /** The bucket an unrecognised kind lands in. See the header. */
    const DEFAULT_BUCKET = 'input';

    /** Suffix on the group key. See `groupKey`. */
    const KEY_SUFFIX = '|session';

    let warned = false;

    /**
     * The fold order, as bucket keys, read live from the summary module.
     *
     * Description: Returns null when that module has not loaded, which
     *   is the ONLY honest answer - a local copy of the order would be
     *   a second source of truth for the one decision this file exists
     *   to avoid duplicating.
     * Inputs: None.
     * Output: Array<string>|null - highest priority first.
     * Example: priorityKeys() -> ['permission', 'input', 'working', ...]
     */
    function priorityKeys() {
        const summary = globalThis.SessionStatusSummary;
        const fold = summary && summary.SUMMARY_PRIORITY;
        if (!Array.isArray(fold) || fold.length === 0) {
            if (!warned) {
                warned = true;
                console.error(
                    '[ToastSessionGroup] session-status-summary.js has not '
                    + 'loaded, so the attention order is unavailable. Toasts '
                    + 'fall back to per-kind coalescing.');
            }
            return null;
        }
        return fold.map(function (entry) { return entry && entry.key; });
    }

    /**
     * Which summary bucket one toast kind falls into.
     *
     * Inputs: kind (string|null|undefined) - a toast kind.
     * Output: string - a SUMMARY_PRIORITY bucket key.
     * Example: bucketForKind('PermissionRequest') -> 'permission'
     */
    function bucketForKind(kind) {
        const bucket = KIND_BUCKET[kind];
        return bucket || DEFAULT_BUCKET;
    }

    /**
     * The coalesce key that puts every one of a session's status toasts
     * on one card.
     *
     * Description: Keyed on the session ALONE - not on (kind, session),
     *   which is the change. A toast with no session id gets null: the
     *   whole point of the key is "these belong to the same session",
     *   and an absent id would merge unrelated records under one
     *   `undefined`. Null is also returned when the attention order is
     *   unavailable, because a card that had to pick a winner without it
     *   could only guess.
     * Inputs: toast (object) - server-shape toast.
     * Output: string|null - the group key, or null for "do not group".
     * Example: groupKey({session_id: 'ses_1', kind: 'Stop'})
     *          -> 'ses_1|session'
     */
    function groupKey(toast) {
        if (!toast || !toast.session_id) return null;
        if (!priorityKeys()) return null;
        return String(toast.session_id) + KEY_SUFFIX;
    }

    /**
     * Choose the one toast a session's card shows, and how many of its
     * kind are behind it.
     *
     * Description: PURE. (bucket rank, then severity, then arrival) as
     *   described in the header. `toasts` is arrival ordered, so a `<=`
     *   comparison keeps the LATEST member of a tie, which is what a
     *   single-kind group has always shown.
     *
     *   `count` deliberately counts only the members whose KIND matches
     *   the winner. The badge sits beside the winner's title, so it is
     *   read as "this, that many times"; counting the session's whole
     *   pile there would put a 7 next to a sentence that happened six
     *   times. How many records the card would CLEAR is a different
     *   number and toast.js states it separately, on the dismiss
     *   control.
     * Inputs: toasts (Array) - one group's members, arrival ordered;
     *   severityOf (function) - kind -> number, from toast.js.
     * Output: {winner, count}|null - null for an empty list.
     * Example: pick([stopA, permB], sevOf) -> {winner: permB, count: 1}
     */
    function pick(toasts, severityOf) {
        if (!Array.isArray(toasts) || toasts.length === 0) return null;
        const order = priorityKeys();
        const rankOf = function (toast) {
            if (!order) return 0;
            const idx = order.indexOf(bucketForKind(toast && toast.kind));
            // A bucket the fold does not carry is LAST, never first: an
            // unplaceable state must not outrank a measured one.
            return idx === -1 ? order.length : idx;
        };
        const sevOf = typeof severityOf === 'function'
            ? function (toast) { return severityOf(toast && toast.kind); }
            : function () { return 0; };

        let winner = null;
        let winnerRank = Infinity;
        let winnerSev = -Infinity;
        for (let i = 0; i < toasts.length; i++) {
            const toast = toasts[i];
            if (!toast) continue;
            const rank = rankOf(toast);
            if (rank > winnerRank) continue;
            const sev = sevOf(toast);
            if (rank === winnerRank && sev < winnerSev) continue;
            winner = toast;
            winnerRank = rank;
            winnerSev = sev;
        }
        if (!winner) return null;

        let count = 0;
        for (let i = 0; i < toasts.length; i++) {
            if (toasts[i] && toasts[i].kind === winner.kind) count += 1;
        }
        return { winner: winner, count: count };
    }

    const api = {
        KIND_BUCKET: Object.assign({}, KIND_BUCKET),
        DEFAULT_BUCKET: DEFAULT_BUCKET,
        bucketForKind: bucketForKind,
        groupKey: groupKey,
        pick: pick,
    };

    globalThis.ToastSessionGroup = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }

    console.log('[ToastSessionGroup Module] Loaded');
})();
