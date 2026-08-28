/**
 * Session sidebar ARRANGEMENT - the ordering algebra. How a list of
 * session rows is sorted into the user's arrangement, and the four
 * operations that change it: pin, move, place, and merge.
 *
 * Everything about WHAT IS REMEMBERED lives one file down, in
 * client/js/session-sidebar-store.js: the localStorage envelope, its
 * three load outcomes, and the pin membership. This file was 524 lines
 * with both jobs in it, over the project's 500-line budget, and they are
 * genuinely different jobs. The dependency runs one way - this reads and
 * writes through the store, the store knows nothing about ordering.
 *
 * THE PUBLIC SURFACE IS UNCHANGED. `window.SessionSidebarArrangement`
 * still carries every name it did before the split, including the ones
 * that now live in the store, because a refactor that makes every caller
 * change is not a refactor. Callers and tests use the same API.
 *
 * A REMEMBERED NAME WHOSE SESSION IS GONE KEEPS ITS SLOT. It is not an
 * error and it is not dropped: the entry stays in storage at the same
 * index, renders no row, and is counted out loud by `arrange()`. So a
 * conversation that comes back lands back where the user left it,
 * instead of at the bottom as if it were new.
 *
 * THE INCOMING ORDER IS ONLY A FALLBACK. `arrange()` receives the
 * sidebar's built-in sort (this tab, then live, then newest) and uses it
 * ONLY for names the user has never arranged. A user-defined position is
 * never silently overridden by it, which is the whole requirement.
 *
 * Pure functions over plain objects, so it is testable without a
 * browser.
 *
 * Must load AFTER session-sidebar-store.js and BEFORE session-sidebar.js.
 */

console.log('[SessionSidebarArrangement Module] Loading...');

(function () {
    /**
     * The persistence layer. Aliased once rather than reached through
     * `window` at every call site, so the dependency is stated at the top
     * of the file instead of being scattered through it.
     * @type {object}
     */
    const S = window.SessionSidebarStore;

    /**
     * Description: sort rows into the user's arrangement: the pinned band
     *   first, then the rest, each band following the stored order, with
     *   any name the arrangement has never seen appended to the end of its
     *   band in the caller's incoming (default-sorted) order. Names the
     *   arrangement remembers but that have no row are reported, never
     *   dropped.
     *
     *   The incoming order is the sidebar's built-in sort (this tab, then
     *   live, then newest). It is used ONLY for names the user has not
     *   arranged - a user-defined position is never silently overridden by
     *   it, which is the whole requirement.
     * Inputs: rows (Array<object>) - merged session rows, each with `name`.
     * Output: object - {rows (Array<object>), missing (Array<string>)}.
     * Example: SessionSidebarArrangement.arrange(rows).missing // ['cloude_old']
     */
    function arrange(rows) {
        const list = Array.isArray(rows) ? rows.slice() : [];
        const byName = new Map();
        for (const row of list) {
            if (row && typeof row.name === 'string') byName.set(row.name, row);
        }

        const known = new Set(S.current().order);
        const ordered = [];
        for (const name of S.current().order) {
            const row = byName.get(name);
            if (row) ordered.push(row);
        }
        for (const row of list) {
            if (!known.has(row.name)) ordered.push(row);
        }

        // PARTITION BY BAND, not by the pin flag alone. `bands()` is
        // PINNED, then each user group in its stored order, then OTHER -
        // and with the group model unknown or unreadable it is exactly
        // the pinned/rest pair this function has always produced, so an
        // install with no groups (or one that could not read them) gets
        // byte-identical output to the pre-groups build.
        //
        // Relative order INSIDE a band is preserved from `ordered`, which
        // is the user's own manual order. That is what lets the group
        // dimension be added without touching the ordering algebra: a
        // group changes which band a row is in, never where it sits
        // within one.
        const G = groupStore();
        const keys = G ? G.bandOrder() : ['pinned', 'other'];
        const buckets = new Map(keys.map((k) => [k, []]));
        for (const row of ordered) {
            row.is_pinned = S.isPinned(row.name);
            row.band_key = G
                ? G.bandOf(row.name, row.is_pinned)
                : (row.is_pinned ? 'pinned' : 'other');
            // A row whose band is not in the current order can only mean
            // the group model changed between the membership read and
            // this paint. It goes to OTHER rather than being DROPPED -
            // a row that renders nowhere is a conversation the user
            // cannot reach, which is far worse than one in the wrong
            // section for a single frame.
            if (!buckets.has(row.band_key)) row.band_key = 'other';
            buckets.get(row.band_key).push(row);
        }
        let out = [];
        for (const key of keys) out = out.concat(buckets.get(key));

        const remembered = S.dedupe(S.current().order.concat(S.current().pinned));
        const missing = remembered.filter((n) => !byName.has(n));
        return { rows: out, missing };
    }

    /**
     * Description: the group store, or null when it has not loaded. Read
     *   through a function rather than aliased at module load because
     *   this file may be parsed before it - and a null here is a
     *   MEANINGFUL state (draw the pre-groups list), not an error.
     * Inputs: none. Output: object|null.
     */
    function groupStore() {
        const G = window.SessionSidebarGroupStore;
        return (G && G.isUsable()) ? G : null;
    }

    /**
     * Description: rewrite the stored order from the currently VISIBLE
     *   sequence while keeping every remembered-but-absent name in the
     *   slot it already occupies. Walking the old order and substituting
     *   the new visible sequence into the slots the visible names held is
     *   what makes a stopped session come back to where the user left it,
     *   rather than to the bottom of the list.
     * Inputs: visibleNames (Array<string>) - the new top-to-bottom order
     *   of the names that currently have rows.
     * Output: Array<string> - the merged order to persist.
     */
    function mergeOrder(visibleNames) {
        const incoming = S.dedupe(visibleNames);
        const visible = new Set(incoming);
        const out = [];
        let i = 0;
        for (const name of S.current().order) {
            if (visible.has(name)) {
                if (i < incoming.length) out.push(incoming[i++]);
            } else {
                out.push(name);
            }
        }
        while (i < incoming.length) out.push(incoming[i++]);
        return S.dedupe(out).slice(0, S.MAX_REMEMBERED);
    }

    /**
     * Description: flip one session's pin and persist the result. The row
     *   keeps its relative position inside whichever band it lands in.
     * Inputs: name (string), visibleNames (Array<string>) - current
     *   top-to-bottom visible order, so the write records the list as the
     *   user currently sees it.
     * Output: boolean - the new pinned state of `name`.
     */
    function togglePin(name, visibleNames) {
        const next = !S.isPinned(name);
        const pinned = next
            ? S.current().pinned.concat([name])
            : S.current().pinned.filter((n) => n !== name);
        S.save(pinned, mergeOrder(visibleNames));
        return next;
    }

    /**
     * Description: move one session one slot up or down, CROSSING THE
     *   PINNED BOUNDARY when it reaches its band's edge - which pins or
     *   unpins it. This is the keyboard twin of dragging a row into or out
     *   of the pinned group, and it exists so that capability is not
     *   pointer-only.
     *
     *   THIS REVERSES AN EARLIER RULE ON PURPOSE. The band edge used to be
     *   a hard refusal, on the reasoning that a move must never change a
     *   pin the user could not see themselves perform. The user has since
     *   asked for exactly that crossing, so the protection moves rather
     *   than disappearing: a crossing move RETURNS `crossed: true`, and
     *   the caller announces the pin change in words (see
     *   client/js/session-sidebar-reorder.js). An invisible unpin was the
     *   thing being prevented, not the unpin itself.
     *
     *   THE LIST EDGES ARE STILL HARD REFUSALS. Alt+Up on the very first
     *   row has no row above it to cross past, so it does nothing rather
     *   than pinning into an empty band - `p` is the gesture for
     *   "pin where you are", and overloading Alt+Up with it would make the
     *   top row's behaviour depend on invisible state.
     * Inputs: name (string), delta (number) - -1 up, +1 down.
     *   visibleNames (Array<string>) - current top-to-bottom visible order.
     * Output: object|null - {order (Array<string>), crossed (boolean),
     *   pinned (boolean) - the row's pin state AFTER the move}, or null
     *   when the move was refused (row not found, or at a list edge).
     */
    function move(name, delta, visibleNames) {
        const names = S.dedupe(visibleNames);
        const idx = names.indexOf(name);
        if (idx === -1) return null;
        const step = delta < 0 ? -1 : 1;
        const target = idx + step;
        if (target < 0 || target >= names.length) return null;
        const band = S.isPinned(name);
        const neighbourBand = S.isPinned(names[target]);
        const next = names.slice();
        next[idx] = names[target];
        next[target] = name;
        // Crossing means adopting the neighbour's band. The swap above
        // already puts the row on the far side of the boundary in the
        // visible sequence; arrange() re-partitions from the pin set, so
        // the pin has to move with it or the row snaps straight back.
        const crossed = neighbourBand !== band;
        const nowPinned = crossed ? neighbourBand : band;
        const pinnedSet = crossed
            ? (nowPinned ? S.current().pinned.concat([name]) : S.current().pinned.filter((n) => n !== name))
            : S.current().pinned;
        S.save(pinnedSet, mergeOrder(next));
        return { order: next, crossed, pinned: nowPinned };
    }

    /**
     * Description: place `name` immediately before `beforeName` (or at the
     *   end of the list when `beforeName` is null) AND set which band it
     *   lands in, then persist. This is the pointer-drag commit, and the
     *   one function that can pin or unpin by position.
     *
     *   The caller decides the band from what the pointer is OVER (the
     *   group container, not the neighbouring row), because a drop into an
     *   empty pinned group has no neighbouring row to infer a band from
     *   and inferring one from the nearest row would make the empty group
     *   undroppable - which is exactly the case the feature is for.
     *
     *   `next` is not required to be band-contiguous. `arrange()`
     *   re-partitions it into the two bands and preserves relative order
     *   inside each, so this only has to get the within-band sequence
     *   right, which is why there is no boundary arithmetic left here.
     * Inputs: name (string) - the row being placed.
     *   targetPinned (boolean) - the band it should end up in.
     *   beforeName (string|null) - the visible row to sit above, or null
     *   for the end.
     *   visibleNames (Array<string>) - current top-to-bottom visible order.
     * Output: object|null - {order (Array<string>), crossed (boolean),
     *   pinned (boolean)}, or null when the placement is a no-op or the
     *   row is not visible.
     * Example: placeAt('cloude_fs2', true, 'cloude_asd', names).crossed
     */
    function placeAt(name, targetPinned, beforeName, visibleNames) {
        const names = S.dedupe(visibleNames);
        if (names.indexOf(name) === -1) return null;
        if (beforeName === name) return null;
        if (beforeName !== null && beforeName !== undefined
            && names.indexOf(beforeName) === -1) return null;
        const want = !!targetPinned;
        const rest = names.filter((n) => n !== name);
        let at = (beforeName === null || beforeName === undefined)
            ? rest.length
            : rest.indexOf(beforeName);
        if (at === -1) at = rest.length;
        const next = rest.slice(0, at).concat([name], rest.slice(at));
        const wasPinned = S.isPinned(name);
        const crossed = wasPinned !== want;
        // A drop that changes neither the band nor the sequence is not a
        // move. Returning null for it keeps the live drag from writing
        // storage and repainting on every pointer sample.
        if (!crossed && sameOrder(next, names)) return null;
        const pinnedSet = want
            ? S.current().pinned.concat([name])
            : S.current().pinned.filter((n) => n !== name);
        S.save(pinnedSet, mergeOrder(next));
        return { order: next, crossed, pinned: want };
    }

    /**
     * Description: true when two name lists are identical, same order.
     * Inputs: a (Array<string>), b (Array<string>).
     * Output: boolean.
     */
    function sameOrder(a, b) {
        return a.length === b.length && a.every((n, i) => n === b[i]);
    }

    // The store's own surface is re-exported here so
    // `window.SessionSidebarArrangement` still answers every call it did
    // before the split. See the file docblock: the split is internal, the
    // API is not.
    window.SessionSidebarArrangement = {
        load: S.load,
        current: S.current,
        save: S.save,
        isPinned: S.isPinned,
        isCollapsed: S.isCollapsed,
        toggleCollapsed: S.toggleCollapsed,
        readCollapsed: S.readCollapsed,
        STORAGE_KEY: S.STORAGE_KEY,
        VERSION: S.VERSION,
        MAX_REMEMBERED: S.MAX_REMEMBERED,
        GROUP_KEYS: S.GROUP_KEYS,
        arrange, mergeOrder, togglePin, move, placeAt, sameOrder, groupStore,
    };
    console.log('[SessionSidebarArrangement Module] Exported as window.SessionSidebarArrangement');
})();
