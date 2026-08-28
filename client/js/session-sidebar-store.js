/**
 * Session sidebar arrangement STORE - the localStorage envelope, how it
 * is parsed, how a bad one is graded, and the pin membership that rides
 * in it. Everything about WHAT IS REMEMBERED. Nothing about ordering.
 *
 * WHY THIS IS ITS OWN FILE. session-sidebar-arrangement.js reached 524
 * lines, over the project's 500-line budget, and it had two genuinely
 * different jobs inside it: persistence and grading on one side, the
 * ordering algebra on the other. The seam was already there; this only
 * cuts along it. The dependency runs ONE WAY - arrangement reads and
 * writes through this store, and this store knows nothing about
 * ordering - so there is no cycle to reason about.
 *
 * THE THREE OUTCOMES OF A LOAD, which is the whole reason this half is
 * worth isolating:
 *
 *   'default'    nothing stored. Not an error, announces nothing.
 *   'ok'         parsed and applied.
 *   'unreadable' something IS stored and it could not be understood.
 *                CANNOT DETERMINE, said out loud in the UI.
 *
 * The third is never collapsed into the first. Silently treating an
 * unreadable arrangement as "no arrangement" would throw away the
 * user's own ordering and report nothing, which is the exact false
 * green this project keeps removing. AND THE BAD BYTES ARE NEVER
 * OVERWRITTEN: an unreadable value stays on disk, inspectable, until
 * the user's next deliberate arrangement change replaces it.
 *
 * `collapsed` IS GRADED DIFFERENTLY FROM `pinned` AND `order`, on
 * purpose. A malformed pin set or order is 'unreadable', because that is
 * the user's own arrangement and losing it silently would be a lie. A
 * malformed `collapsed` warns to the console and reads as "nothing
 * collapsed", because a fold is a preference and not data: there is
 * nothing of the user's to lose and nothing to announce.
 *
 * USER GROUP FOLDS RIDE THE SAME KEY, ALSO WITHOUT BUMPING VERSION.
 * A folded user group is stored as `g:<uuid>` beside `pinned` and
 * `other`. `isFoldKey` therefore validates the SHAPE of a key rather
 * than its membership in a fixed list - it cannot ask whether the group
 * still exists, because the groups are read asynchronously from the
 * database and this parse runs first. An OLDER build reading an envelope
 * containing `g:<uuid>` drops it and warns, which unfolds that section
 * and loses nothing: a fold is graded as a preference here precisely so
 * that this is survivable. A NEWER build reading an older envelope is
 * unchanged.
 *
 * It also rides the SAME envelope without bumping VERSION. Bumping to 2
 * would have declared every arrangement already on disk 'unreadable',
 * so every existing user would have opened the bar to a CANNOT LOAD
 * notice and a default order, breaking the exact thing this module
 * exists to protect in order to store a preference. It is additive and
 * optional: absent reads as "nothing collapsed", and an older build
 * parses the envelope unchanged and ignores the key.
 *
 * Pure functions over a plain object plus localStorage, so it is
 * testable without a browser.
 *
 * Must load BEFORE session-sidebar-arrangement.js.
 */

console.log('[SessionSidebarStore Module] Loading...');

(function () {
    /**
     * localStorage key for the pinned set + user order. Follows the app's
     * `cloude.*` convention, and sits beside the two flags the same panel
     * already owns (`cloude.session.sidebar`, `...sidebar.pinned`).
     * @type {string}
     */
    const STORAGE_KEY = 'cloude.session.sidebar.arrangement';

    /**
     * Schema version stamped into the stored envelope. A value carrying
     * any other version is treated as unreadable rather than guessed at.
     * @type {number}
     */
    const VERSION = 1;

    /**
     * Most remembered names kept across saves. Slots for sessions that no
     * longer exist are retained on purpose (see the file docblock); this
     * is the bound that stops "retained on purpose" becoming "forever".
     * @type {number}
     */
    const MAX_REMEMBERED = 200;

    /**
     * The two section keys the list can fold. Not free-form strings: a
     * stored key that is not one of these is discarded on load, so a
     * renamed or removed section cannot leave a fold nothing can reopen.
     * @type {Array<string>}
     */
    const GROUP_KEYS = ['pinned', 'other'];

    /**
     * Prefix of a USER GROUP's fold key, mirroring
     * `client/js/session-sidebar-group-store.js`'s `BAND_GROUP_PREFIX`.
     * Duplicated as a literal on purpose: this file must parse and grade
     * a stored envelope whether or not the group store ever loads, so it
     * cannot take a load-order dependency on it.
     * @type {string}
     */
    const GROUP_FOLD_PREFIX = 'g:';

    /**
     * Most folded sections remembered. A fold key for a group that was
     * deleted is harmless - it names a section that no longer renders -
     * but without a bound a long-lived install would accumulate one per
     * group it ever had.
     * @type {number}
     */
    const MAX_FOLDS = 100;

    /**
     * Description: true when a stored fold key names a section this build
     *   can reopen: one of the two reserved bands, or a user group.
     *
     *   WHY THIS IS A SHAPE TEST AND NOT A MEMBERSHIP TEST. It cannot ask
     *   whether the group still exists, because the groups are read
     *   asynchronously from the database and this parse runs before that
     *   read completes. Rejecting an unrecognised group key here would
     *   silently unfold every section on every reload, which is worse
     *   than keeping a key that names nothing - a fold for a group that
     *   no longer exists simply never matches a rendered section.
     * Inputs: key (any).
     * Output: boolean.
     * Example: isFoldKey('g:3f2a') // true
     */
    function isFoldKey(key) {
        if (typeof key !== 'string' || !key) return false;
        if (GROUP_KEYS.indexOf(key) !== -1) return true;
        return key.indexOf(GROUP_FOLD_PREFIX) === 0
            && key.length > GROUP_FOLD_PREFIX.length;
    }

    /** Live state, replaced wholesale by load(). @type {object} */
    let state = {
        status: 'default', reason: null, pinned: [], order: [], collapsed: [],
    };
    /**
     * Description: true when `value` is an array of non-empty strings.
     * Inputs: value (any).
     * Output: boolean.
     */
    function isNameArray(value) {
        return Array.isArray(value)
            && value.every((n) => typeof n === 'string' && n.length > 0);
    }

    /**
     * Description: drop duplicates from a name list, keeping first wins.
     * Inputs: names (Array<string>).
     * Output: Array<string>.
     */
    function dedupe(names) {
        const seen = new Set();
        const out = [];
        for (const n of names) {
            if (seen.has(n)) continue;
            seen.add(n);
            out.push(n);
        }
        return out;
    }

    /**
     * Description: read the optional `collapsed` list out of a stored
     *   envelope. Anything that is not a list of known GROUP_KEYS reads
     *   as "nothing collapsed" and warns, rather than failing the whole
     *   parse - see the file docblock for why a fold is graded as a
     *   preference and the order is graded as data.
     * Inputs: value (any) - whatever the envelope carried, possibly
     *   undefined.
     * Output: Array<string> - a subset of GROUP_KEYS, never anything else.
     * Example: readCollapsed(['pinned', 'nope']) // ['pinned']
     */
    function readCollapsed(value) {
        if (value === undefined || value === null) return [];
        if (!isNameArray(value)) {
            console.warn('SessionSidebarArrangement: stored collapsed list is not a list of'
                + ' section names, treating every section as open');
            return [];
        }
        const kept = dedupe(value).filter(isFoldKey).slice(0, MAX_FOLDS);
        if (kept.length !== dedupe(value).length) {
            console.warn('SessionSidebarArrangement: dropped unknown collapsed section key(s)');
        }
        return kept;
    }

    /**
     * Description: read the stored arrangement and classify the result as
     *   one of the three outcomes described in the file docblock. Never
     *   throws; a storage backend that refuses to answer is 'unreadable',
     *   which is a verdict, not a crash.
     * Inputs: none (reads localStorage).
     * Output: object - {status, reason, pinned, order}. Also replaces the
     *   module's live state.
     * Example: SessionSidebarArrangement.load().status // 'default'
     */
    function load() {
        let raw = null;
        try {
            raw = localStorage.getItem(STORAGE_KEY);
        } catch (err) {
            state = {
                status: 'unreadable',
                reason: 'storage unavailable',
                pinned: [],
                order: [],
                collapsed: [],
            };
            return state;
        }
        if (raw === null || raw === undefined || raw === '') {
            state = {
                status: 'default', reason: null, pinned: [], order: [], collapsed: [],
            };
            return state;
        }
        let parsed = null;
        try {
            parsed = JSON.parse(raw);
        } catch (err) {
            state = {
                status: 'unreadable',
                reason: 'stored value is not valid JSON',
                pinned: [],
                order: [],
                collapsed: [],
            };
            return state;
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            state = {
                status: 'unreadable',
                reason: 'stored value is not an arrangement object',
                pinned: [],
                order: [],
                collapsed: [],
            };
            return state;
        }
        if (parsed.v !== VERSION) {
            state = {
                status: 'unreadable',
                reason: `stored arrangement is version ${JSON.stringify(parsed.v)}, this app writes version ${VERSION}`,
                pinned: [],
                order: [],
                collapsed: [],
            };
            return state;
        }
        if (!isNameArray(parsed.pinned) || !isNameArray(parsed.order)) {
            state = {
                status: 'unreadable',
                reason: 'stored pin or order list is not a list of session names',
                pinned: [],
                order: [],
                collapsed: [],
            };
            return state;
        }
        state = {
            status: 'ok',
            reason: null,
            pinned: dedupe(parsed.pinned).slice(0, MAX_REMEMBERED),
            order: dedupe(parsed.order).slice(0, MAX_REMEMBERED),
            collapsed: readCollapsed(parsed.collapsed),
        };
        return state;
    }

    /**
     * Description: the last loaded/updated arrangement, without re-reading
     *   storage.
     * Inputs: none.
     * Output: object - {status, reason, pinned, order}.
     */
    function current() { return state; }

    /**
     * Description: persist a pin set + order, and mark the live state 'ok'.
     *   A deliberate write is what clears an 'unreadable' verdict: the
     *   user has now told us an arrangement, so there is no longer a lost
     *   one to warn about.
     * Inputs: pinned (Array<string>), order (Array<string>).
     * Output: boolean - true when the write landed, false when storage
     *   refused it (the in-memory arrangement still applies for this page).
     */
    function save(pinned, order, collapsed) {
        const nextPinned = dedupe(pinned).slice(0, MAX_REMEMBERED);
        const nextOrder = dedupe(order).slice(0, MAX_REMEMBERED);
        // `undefined` means "leave the folds alone", which is what every
        // caller that only touches pins or order wants. Passing the live
        // value through rather than defaulting to [] is what stops a
        // reorder from silently reopening a section the user folded.
        const nextCollapsed = readCollapsed(
            collapsed === undefined ? state.collapsed : collapsed,
        );
        state = {
            status: 'ok',
            reason: null,
            pinned: nextPinned,
            order: nextOrder,
            collapsed: nextCollapsed,
        };
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify({
                v: VERSION,
                pinned: nextPinned,
                order: nextOrder,
                collapsed: nextCollapsed,
            }));
            return true;
        } catch (err) {
            console.warn('SessionSidebarArrangement: could not persist arrangement:', err);
            return false;
        }
    }

    /**
     * Description: true when a section is folded shut.
     * Inputs: key (string) - one of GROUP_KEYS.
     * Output: boolean.
     */
    function isCollapsed(key) { return state.collapsed.indexOf(key) !== -1; }

    /**
     * Description: fold or unfold one section and persist it, leaving the
     *   pins and the order exactly as they were.
     * Inputs: key (string) - one of GROUP_KEYS. Anything else is ignored.
     * Output: boolean - the section's new collapsed state.
     */
    function toggleCollapsed(key) {
        if (!isFoldKey(key)) return false;
        const next = !isCollapsed(key);
        const collapsed = next
            ? state.collapsed.concat([key])
            : state.collapsed.filter((k) => k !== key);
        save(state.pinned, state.order, collapsed);
        return next;
    }

    /**
     * Description: true when this session name is pinned to the top band.
     * Inputs: name (string).
     * Output: boolean.
     */
    function isPinned(name) { return state.pinned.indexOf(name) !== -1; }

    window.SessionSidebarStore = {
        load, current, save, isPinned, isCollapsed, toggleCollapsed,
        readCollapsed, dedupe, isNameArray, isFoldKey,
        STORAGE_KEY, VERSION, MAX_REMEMBERED, GROUP_KEYS,
        GROUP_FOLD_PREFIX, MAX_FOLDS,
    };
    console.log('[SessionSidebarStore Module] Exported as window.SessionSidebarStore');
})();
