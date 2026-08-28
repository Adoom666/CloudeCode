/**
 * Session sidebar GROUP STORE - the user's named groups, read from and
 * written to the database, and the band algebra that turns them plus the
 * pin set into the list's top-to-bottom sections.
 *
 * THE DATABASE IS THE ONLY SOURCE. There is no localStorage mirror of a
 * group, deliberately. `client/js/session-sidebar-store.js` keeps the
 * pin set and the manual order because those are per-device VIEW state;
 * a group is a FACT about the conversation, and the moment it exists in
 * two places the sidebar has to decide which of two disagreeing lists to
 * draw. That is the defect that moved projects to DB-only, and it would
 * land in this same panel.
 *
 * THREE OUTCOMES, AND THE THIRD ONE CHANGES WHAT IS DRAWN:
 *
 *   'unknown'      no read has completed yet. Draw the list exactly as
 *                  it was drawn before groups existed.
 *   'ok'           the groups are known. An empty list genuinely means
 *                  the user has made none.
 *   'unavailable'  CANNOT DETERMINE. The datastore is absent, predates
 *                  v8, or would not open.
 *
 * 'unavailable' AND 'ok WITH NO GROUPS' RENDER IDENTICALLY - ungrouped -
 * BUT THEY ARE NOT THE SAME STATE, and the difference is visible: the
 * panel says so in words (see `noticeText`), and every group-creating
 * control is disabled rather than silently failing. Drawing "you have no
 * groups" over a table we could not read would be a verdict nobody
 * measured, and letting the user make a group that cannot be saved is
 * worse than telling them why they cannot.
 *
 * BANDS. The list's sections, top to bottom, are:
 *
 *   PINNED        every pinned row, whatever group it belongs to
 *   <group>...    each user group, in its stored position order
 *   OTHER         everything else
 *
 * PINNED IS NOT A GROUP - see the long argument in
 * src/core/session_group_store.py. The short form: a group is a bucket
 * (exactly one holds you) and a pin is a flag (orthogonal to which
 * bucket you are in), so folding them together would make pinning a
 * conversation EJECT it from the group the user filed it in. Instead a
 * pinned row renders in PINNED and carries a chip naming its group, so
 * the filing stays visible while the pin decides where it sits. Unpin it
 * and it drops back into its group.
 *
 * A DROP TARGET IS THEREFORE TWO FACTS, not one, and `bandIntent()` is
 * the single place that maps a band to them:
 *
 *   pinned      -> pin it, LEAVE its group alone
 *   a group     -> file it there, and unpin it
 *   other       -> ungroup it, and unpin it
 *
 * The unpin on the last two is not a policy choice, it is arithmetic: a
 * row that stayed pinned would be re-partitioned straight back into
 * PINNED by `arrange()` on the very next paint, so the drop would
 * visibly snap back. This is the same reasoning that already makes a
 * cross-boundary drag pin or unpin.
 *
 * Must load BEFORE session-sidebar-arrangement.js.
 */

console.log('[SessionSidebarGroupStore Module] Loading...');

(function () {
    /** No read has completed yet. Draw the pre-groups list. */
    const STATUS_UNKNOWN = 'unknown';

    /** The groups are known. An empty list means the user made none. */
    const STATUS_OK = 'ok';

    /** CANNOT DETERMINE. Never collapsed into "no groups". */
    const STATUS_UNAVAILABLE = 'unavailable';

    /** The pinned band's reserved key. Never a user group's key. */
    const BAND_PINNED = 'pinned';

    /** The ungrouped remainder's reserved key. Also never a user group. */
    const BAND_OTHER = 'other';

    /**
     * Prefix that makes a user group's band key impossible to confuse
     * with a reserved one. A group named literally "pinned" is fine and
     * gets key `g:<uuid>`; only the PREFIX decides.
     * @type {string}
     */
    const BAND_GROUP_PREFIX = 'g:';

    /**
     * Live state, replaced wholesale by `apply()`.
     * @type {{status: string, groups: Array<object>, detail: (string|null)}}
     */
    let state = { status: STATUS_UNKNOWN, groups: [], detail: null };

    /**
     * tmux name -> group uuid, rebuilt whenever `apply()` runs so a
     * membership lookup is O(1) rather than a scan per row per paint.
     * @type {Map<string, string>}
     */
    let membership = new Map();

    /**
     * Description: the band key for one user group's uuid.
     * Inputs: groupUuid (string).
     * Output: string - e.g. 'g:3f2a...'.
     */
    function bandKeyFor(groupUuid) { return BAND_GROUP_PREFIX + groupUuid; }

    /**
     * Description: the group uuid inside a band key, or null when the key
     *   is a reserved band rather than a user group.
     * Inputs: key (string).
     * Output: string|null.
     * Example: groupUuidOf('g:abc') // 'abc'; groupUuidOf('pinned') // null
     */
    function groupUuidOf(key) {
        if (typeof key !== 'string') return null;
        if (key.indexOf(BAND_GROUP_PREFIX) !== 0) return null;
        return key.slice(BAND_GROUP_PREFIX.length) || null;
    }

    /**
     * Description: replace the live state from a `GET /session-groups`
     *   body, rebuilding the membership index. Anything that is not a
     *   recognisable body is treated as 'unavailable' rather than as an
     *   empty group list, for the reason in the file docblock.
     * Inputs: body (object|null) - {status, groups, detail}.
     * Output: object - the new state.
     * Example: apply({status: 'ok', groups: []}).status // 'ok'
     */
    function apply(body) {
        const ok = body && typeof body === 'object'
            && body.status === STATUS_OK && Array.isArray(body.groups);
        if (!ok) {
            const detail = (body && typeof body === 'object' && body.detail)
                ? String(body.detail)
                : 'the groups could not be read';
            state = { status: STATUS_UNAVAILABLE, groups: [], detail };
            membership = new Map();
            return state;
        }
        const groups = body.groups
            .filter((g) => g && typeof g.group_uuid === 'string' && g.group_uuid)
            .map((g) => ({
                group_uuid: g.group_uuid,
                name: typeof g.name === 'string' ? g.name : '',
                position: Number.isFinite(g.position) ? g.position : 0,
                members: Array.isArray(g.members) ? g.members.filter((n) => !!n) : [],
            }))
            .sort((a, b) => (a.position - b.position)
                || (a.group_uuid < b.group_uuid ? -1 : 1));
        membership = new Map();
        for (const g of groups) {
            for (const name of g.members) membership.set(name, g.group_uuid);
        }
        state = { status: STATUS_OK, groups, detail: null };
        return state;
    }

    /**
     * Description: mark the groups unreadable, with a reason. Used when
     *   the fetch itself threw, where there is no body to grade.
     * Inputs: detail (string).
     * Output: object - the new state.
     */
    function markUnavailable(detail) {
        state = {
            status: STATUS_UNAVAILABLE,
            groups: [],
            detail: detail ? String(detail) : 'the groups could not be read',
        };
        membership = new Map();
        return state;
    }

    /**
     * Description: the last applied state, without re-reading anything.
     * Inputs: none. Output: object - {status, groups, detail}.
     */
    function current() { return state; }

    /**
     * Description: true when the group model is known well enough to draw
     *   group chrome. False for BOTH 'unknown' and 'unavailable', which
     *   is what makes an unreadable table render as the pre-groups list
     *   rather than as a confident "no groups".
     * Inputs: none. Output: boolean.
     */
    function isUsable() { return state.status === STATUS_OK; }

    /**
     * Description: the sentence the panel shows about the group model, or
     *   null when there is nothing to say. A blank cell is not an
     *   outcome - if the groups could not be read, the panel says so.
     * Inputs: none. Output: string|null.
     */
    function noticeText() {
        if (state.status !== STATUS_UNAVAILABLE) return null;
        return `groups unavailable: ${state.detail}`;
    }

    /**
     * Description: which group a session is filed in.
     * Inputs: name (string) - tmux name.
     * Output: string|null - group uuid, or null for ungrouped.
     */
    function groupOf(name) {
        const uuid = membership.get(name);
        return uuid === undefined ? null : uuid;
    }

    /**
     * Description: the group record for a uuid, or null.
     * Inputs: groupUuid (string|null).
     * Output: object|null.
     */
    function groupByUuid(groupUuid) {
        if (!groupUuid) return null;
        return state.groups.find((g) => g.group_uuid === groupUuid) || null;
    }

    /**
     * Description: move a session between groups LOCALLY, before the
     *   server has confirmed it.
     *
     *   WHY OPTIMISTIC AT ALL. A drag has to reorder under the user's
     *   finger; waiting for a round trip on every pointer sample would
     *   make the row lag the pointer, and firing a request per sample
     *   would hammer the API. So the drag moves the row here and the
     *   commit happens once, on pointer up.
     *
     *   WHAT THIS IS NOT. It does NOT mark the state 'ok' and it does not
     *   touch `groups[].members`. Only the membership index moves, so a
     *   subsequent authoritative read overwrites it wholesale with no
     *   merge step to get wrong. If the commit fails, the caller re-reads
     *   from the server rather than trying to invert this - an invented
     *   undo is a second write path that can itself be wrong, and the
     *   server already holds the answer.
     * Inputs: name (string) - tmux name. groupUuid (string|null) - target
     *   group, or null for ungrouped.
     * Output: string|null - the membership this REPLACED, so a caller
     *   that wants to describe the change can.
     * Example: setOptimistic('cloude_a', 'u1')
     */
    function setOptimistic(name, groupUuid) {
        const previous = groupOf(name);
        if (groupUuid) membership.set(name, groupUuid);
        else membership.delete(name);
        return previous;
    }

    /**
     * Description: the band a row belongs in RIGHT NOW - pinned wins over
     *   its group, because the pinned band is above every group and a row
     *   can only be drawn once.
     * Inputs: name (string), isPinned (boolean).
     * Output: string - a band key.
     */
    function bandOf(name, isPinned) {
        if (isPinned) return BAND_PINNED;
        if (!isUsable()) return BAND_OTHER;
        const uuid = groupOf(name);
        return uuid ? bandKeyFor(uuid) : BAND_OTHER;
    }

    /**
     * Description: every band key the list can draw, top to bottom.
     *   PINNED first, then the user's groups in their stored order, then
     *   OTHER. OTHER is last and not sortable: it is the remainder, and a
     *   remainder that floated between named groups would read as one.
     * Inputs: none.
     * Output: Array<string>.
     */
    function bandOrder() {
        const keys = [BAND_PINNED];
        if (isUsable()) {
            for (const g of state.groups) keys.push(bandKeyFor(g.group_uuid));
        }
        keys.push(BAND_OTHER);
        return keys;
    }

    /**
     * Description: the human label for a band key.
     * Inputs: key (string).
     * Output: string.
     */
    function labelFor(key) {
        if (key === BAND_PINNED) return 'pinned';
        if (key === BAND_OTHER) return 'other';
        const group = groupByUuid(groupUuidOf(key));
        return group ? group.name : 'unknown group';
    }

    /**
     * Description: WHAT A DROP INTO THIS BAND MEANS, as the two facts a
     *   move actually changes. The single place that mapping lives, so
     *   the pointer drag, the row menu and the keyboard picker cannot
     *   come to disagree about it.
     *
     *   `group` is `undefined` for the pinned band, which is not the same
     *   as `null`: undefined means LEAVE THE FILING ALONE, null means
     *   REMOVE IT. Collapsing the two would make pinning a conversation
     *   quietly empty the group the user put it in.
     *
     *   The unpin on the other two bands is arithmetic, not policy: a row
     *   that stayed pinned would be re-partitioned back into PINNED on
     *   the next paint and the drop would visibly snap back.
     * Inputs: key (string) - a band key.
     * Output: object - {pinned (boolean), group (string|null|undefined)}.
     * Example: bandIntent('other') // {pinned: false, group: null}
     */
    function bandIntent(key) {
        if (key === BAND_PINNED) return { pinned: true, group: undefined };
        const uuid = groupUuidOf(key);
        if (uuid) return { pinned: false, group: uuid };
        return { pinned: false, group: null };
    }

    window.SessionSidebarGroupStore = {
        apply, markUnavailable, current, isUsable, noticeText, setOptimistic,
        groupOf, groupByUuid, bandOf, bandOrder, labelFor, bandIntent,
        bandKeyFor, groupUuidOf,
        STATUS_UNKNOWN, STATUS_OK, STATUS_UNAVAILABLE,
        BAND_PINNED, BAND_OTHER, BAND_GROUP_PREFIX,
    };
    console.log('[SessionSidebarGroupStore Module] Exported as window.SessionSidebarGroupStore');
})();
