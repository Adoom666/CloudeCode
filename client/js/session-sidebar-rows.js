/**
 * Session sidebar ROW MARKUP - the HTML for one conversation row, and
 * the repaint signature that decides whether a repaint is needed at all.
 *
 * Split out of client/js/session-sidebar.js for the project's 500-line
 * rule, and along the same seam the repo already uses for row internals:
 * client/js/session-row-actions.js owns the destructive control and
 * client/js/session-status-ui.js owns the status dot. This module is the
 * row that composes them, nothing else - it holds no state and touches
 * no DOM, it only returns strings.
 *
 * THE ACTION ICONS ARE DRAWN INLINE AGAIN. "move the pin and close icons
 * back to the inline icons. remove 'add to group' / 'restart the agent'
 * and the three dots now that they're not needed." Pin and the
 * close/remove control sit on the row, built by the same two builders
 * that have always drawn them: `pinButtonHtml` here and
 * `SessionRowActions.html` next door. No kebab, no panel, no third way
 * to reach either control.
 *
 * WHAT WENT WITH THE MENU, so nobody goes looking for it: the group
 * picker ITEM (filing is still reachable by dragging onto a group
 * header, by `g` on a focused row and by Alt+Arrow across a band edge -
 * see client/js/session-sidebar-group-actions.js) and RESTART ON A LIVE
 * ROW (a DEAD row still offers it inline, and that is now the only
 * surface reaching the respawn ladder). Right-click and long-press
 * opened that menu and now open nothing.
 *
 * NO GROUP CHIP EITHER. Group membership is untouched: still DB-backed,
 * and still how the sidebar's OWN group headers file each row.
 *
 * WHAT EACH DENSITY DRAWS (see client/js/session-sidebar-density.js for
 * the modes and where the preference lives):
 *   compact   grip, dot, name, pin, close/remove
 *   cozy      the above plus the tmux/external badge  (DEFAULT)
 *   detailed  the above, with the badge moved DOWN to a second line that
 *             also carries the session's age
 *
 * NO AGENT-FAMILY PILL, AT ANY DENSITY, SINCE THIS ROUND. "i dont think
 * we need the pills in the sidebar take out for now." The pill is still
 * drawn on the HOME screen by client/js/launchpad.js, which owns its own
 * builder; nothing here feeds that one, so removing this row's pill
 * cannot change what the home screen renders.
 *
 * REMOVING IT ALSO REMOVED A REAL DEFECT whose shape recurs: this
 * builder put a literal `~` in front of a guessed family AND
 * `.family-pill--guess::before` adds another, so `~~claude` rendered
 * while every DOM assertion read a correct `~claude`. A test that reads
 * DOM text cannot see a `::before`; only a rendered pixel or a computed
 * style can, which is why the pill assertions in
 * scripts/verify_sidebar_sessions.py were the ones that could catch it.
 *
 * WHAT NOW FILLS DETAILED'S SECOND LINE: the tmux/external badge, moved
 * down off the first line, plus the age it already carried. The badge is
 * emitted exactly once per row either way - line one at cozy, line two
 * at detailed, not at all at compact.
 *
 * THE ROW HEIGHTS ARE DECLARED, NOT EMERGENT. Removing a glyph from a
 * row would otherwise shorten it by however tall that glyph happened to
 * be, so `client/css/session-sidebar-density.css` now pins a `min-height`
 * per density. The density contract is a number the stylesheet states,
 * not an accident of whichever controls currently ride the line.
 *
 * A RESTART CONTROL IS EMITTED for a row whose status is `dead`, inline
 * beside remove, and for no other status. It SUPERSEDES an older rule
 * saying the sidebar could not know a session was stopped rather than
 * unknown: `session-sidebar-fetch.js` `mergeLiveRow()` overwrites
 * `status` with the server's `activity_status`, which is
 * `resolve_pane_status()` reading tmux's own `#{pane_dead}`, so `dead` is
 * a MEASUREMENT - while a probe-only row still carries `unknown` and
 * `SessionRowActions.actionsFor` refuses to treat `unknown` as stopped.
 *
 * The destructive control (close vs remove) is unchanged and still comes
 * from SessionRowActions.
 *
 * Must load AFTER session-status-ui.js, session-row-actions.js and
 * session-listing-state.js, and BEFORE session-sidebar.js runs.
 */

console.log('[SessionSidebarRows Module] Loading...');

(function () {
    /**
     * Description: HTML-escape a value for safe interpolation.
     * Inputs: value (any). Output: string.
     */
    function esc(value) {
        const div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML;
    }

    /**
     * Description: whether this row's session can be renamed, as THREE
     *   states rather than a boolean, plus the sentence that says why.
     *
     *   This mirrors LaunchpadController._renderRenamePencilHtml exactly,
     *   on the same two fields, because a session must not be renameable
     *   on one surface and not on the other. The two fields answer
     *   DIFFERENT questions and neither one alone is the answer:
     *   `session_id` is only populated by the /sessions/list merge, so it
     *   really means "is there a live backend for this right now", while
     *   `created_by_cloude` is about ORIGIN and is genuinely NULLABLE -
     *   the server fills it from an ownership map that can simply have no
     *   entry for a name.
     *
     *     'renameable'  a session id is known. The rename endpoint is
     *                   keyed on it, so the edit can actually be sent.
     *     'unavailable' no session id, but ownership IS known, so the
     *                   precondition can be stated precisely: open it
     *                   (ours) or adopt it (external).
     *     'unknown'     no session id AND ownership is null. CANNOT
     *                   DETERMINE. `== null` catches null and undefined
     *                   and nothing else, deliberately - `!r.x` would
     *                   fold the genuine unknown into "external" and
     *                   invent an answer nobody measured.
     *
     *   The state is stamped on the row as `data-rename-state`, which is
     *   what client/js/session-sidebar-rename.js gates the inline editor
     *   on. A row that cannot be renamed must not silently accept an edit
     *   that is going to fail.
     * Inputs: r (object) - one merged session row.
     * Output: object - {state (string), reason (string)}.
     * Example: renameState({session_id: null, created_by_cloude: null})
     *   // {state: 'unknown', reason: 'CANNOT DETERMINE ...'}
     */
    function renameState(r) {
        if (r && r.session_id) {
            return { state: 'renameable', reason: 'double-click to rename (or F2)' };
        }
        if (!r || r.created_by_cloude == null) {
            return {
                state: 'unknown',
                reason: 'cannot rename: CANNOT DETERMINE whether this session is yours,'
                    + ' so whether it can be renamed is unknown',
            };
        }
        return {
            state: 'unavailable',
            reason: r.created_by_cloude
                ? 'cannot rename until this session is open - click the row to open it'
                : 'cannot rename until this session is adopted - click the row to adopt it',
        };
    }

    /**
     * Description: the drag grip. Its own control rather than the whole
     *   row, because the row's click already means "switch to this
     *   conversation" - see client/js/session-sidebar-reorder.js.
     * Inputs: name (string). Output: string - HTML.
     */
    function gripHtml(name) {
        return (
            `<span class="session-sidebar-row-grip" data-grip-session="${esc(name)}" ` +
            'aria-hidden="true" title="drag to reorder (or Alt+Up / Alt+Down)">' +
            '<svg width="10" height="14" viewBox="0 0 10 14" aria-hidden="true">' +
            '<circle cx="2.5" cy="3" r="1.2" fill="currentColor"/>' +
            '<circle cx="7.5" cy="3" r="1.2" fill="currentColor"/>' +
            '<circle cx="2.5" cy="7" r="1.2" fill="currentColor"/>' +
            '<circle cx="7.5" cy="7" r="1.2" fill="currentColor"/>' +
            '<circle cx="2.5" cy="11" r="1.2" fill="currentColor"/>' +
            '<circle cx="7.5" cy="11" r="1.2" fill="currentColor"/>' +
            '</svg></span>'
        );
    }

    /**
     * Description: the per-row pin toggle. `aria-pressed` carries the real
     *   state; the glyph is presentational, so the state is never
     *   shape-only.
     * Inputs: name (string), pinned (boolean). Output: string - HTML.
     */
    function pinButtonHtml(name, pinned) {
        const label = pinned ? `Unpin ${name}` : `Pin ${name} to the top`;
        return (
            `<button type="button" class="session-sidebar-row-pin" data-pin-session="${esc(name)}" ` +
            `aria-pressed="${pinned ? 'true' : 'false'}" tabindex="-1" ` +
            `aria-label="${esc(label)}" title="${pinned ? 'unpin' : 'pin to top'}">` +
            '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            '<line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14l-2-4V4H7v9l-2 4z"/>' +
            '</svg></button>'
        );
    }

    /**
     * Description: stable fingerprint of everything the rendered rows
     *   actually show, so the 5s poll tick can skip a DOM rewrite that
     *   would thrash focus and scroll position. Density, pin state and
     *   position are all things the row SHOWS, so all three are in here -
     *   leaving any of them out means a change the user just made does
     *   not paint until something unrelated happens to move.
     * Inputs: rows (Array<object>), density (string), listing (object|null),
     *   missing (Array<string>), groups (object|null) - {collapsed
     *   (Array<string>), dragging (boolean)}, both of which change what
     *   is on screen and therefore both of which must be in here.
     * Output: string.
     */
    function signature(rows, density, listing, missing, groups) {
        return JSON.stringify({
            density: density || 'cozy',
            // A FOLDED SECTION DRAWS NO ROWS AT ALL, so folding one is
            // the largest change this list can make to itself. It has to
            // be in the signature or the fold does not paint until a poll
            // tick happens to differ for some other reason. Same for the
            // drag flag, which is what makes an empty pinned group appear
            // as a drop target.
            collapsed: (groups && Array.isArray(groups.collapsed))
                ? groups.collapsed.slice()
                : [],
            dragging: !!(groups && groups.dragging),
            listing: listing && !listing.ok
                ? ['unavailable', listing.reason || '', listing.detail || '']
                : ['ok'],
            missing: (missing || []).slice(),
            // NO EXPLICIT INDEX HERE, AND THAT IS DELIBERATE. An earlier
            // version carried the array index as a field to make the
            // signature position-sensitive. It is provably redundant:
            // this maps in order and JSON.stringify preserves array
            // order, so two different orderings of the same rows already
            // serialise differently. The index is fully determined by the
            // position it was meant to witness, so it could never change
            // a comparison - and a mutation that deleted it was
            // unkillable by construction. What position-sensitivity
            // actually requires is that this never sorts or normalises
            // the row order before serialising it.
            rows: (rows || []).map((r) => ({
                name: r.name,
                // THE ROW'S TEXT IS THE LABEL, so it must be here or a
                // rename repaints nothing. `name` cannot stand in: a
                // rename moves ONLY the label and leaves the handle put,
                // so the field the diff watched is the one a rename no
                // longer touches. The editor forces a repaint by clearing
                // `_lastSig` - but the 5s poller and another tab's
                // `session.renamed` come through this diff.
                label: r.label || null,
                status: r.status || 'unknown',
                active: !!r.is_active,
                thisTab: !!r.is_this_tab,
                unread: !!r.unread,
                pinned: !!r.is_pinned,
                // The row DRAWS its rename state, in the title on the
                // name and in a data attribute the editor gates on, so a
                // session gaining or losing a live backend has to repaint
                // the row. Leaving it out meant a session that had just
                // opened kept telling the user it could not be renamed
                // until something unrelated happened to change.
                rename: renameState(r).state,
                // The BADGE is drawn at cozy and detailed, so a change of
                // ownership has to repaint. It used to be implied by
                // fields the family pill carried; those are gone.
                badge: !!r.created_by_cloude,
                // The age is only DRAWN at detailed density, and what is
                // drawn is the coarse label ("3h"), not the epoch. Keying
                // on the epoch would repaint every poll tick for a field
                // nobody is looking at; keying on the label at every
                // density would do the same at cozy, where there is no
                // age on screen at all. The signature must track what the
                // row SHOWS, which is why this is conditional.
                age: (density === 'detailed') ? ageLabel(r.created_at_epoch) : null,
                theme: r.pinned_theme || null,
                // punchlist 19 - the "needs a keypress" badge appears and
                // disappears on its own, without any other field on the
                // row changing: a session parked on its trust prompt has
                // the same name, label, status and ownership before and
                // after somebody answers it. Leaving this out would mean
                // the badge painted on whichever poll tick happened to
                // differ for an unrelated reason, and then stayed on
                // screen after the prompt was answered. Normalized, so an
                // absent field and an explicit 'unknown' are one value.
                startup: window.SessionStartupGate
                    ? window.SessionStartupGate.normalize(r.startup_gate)
                    : 'unknown',
                // Same trap as `startup`: a dropped socket moves
                // nothing else that the server reports.
                transport: window.SessionTransport
                    ? window.SessionTransport.stateFor(r.name) : 'unknown',
            })),
        });
    }

    /**
     * Description: a short relative age, e.g. "3h". Empty string when the
     *   row carries no creation epoch - an unknown age renders as nothing
     *   rather than as "0s", which would be a number nobody measured.
     * Inputs: epoch (number|null|undefined) - seconds since the epoch.
     * Output: string.
     */
    function ageLabel(epoch) {
        if (!epoch || typeof epoch !== 'number') return '';
        const secs = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
        if (secs < 60) return `${secs}s`;
        if (secs < 3600) return `${Math.floor(secs / 60)}m`;
        if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
        return `${Math.floor(secs / 86400)}d`;
    }

    /**
     * Description: the foot of the list - the status-light key, then the
     *   app's version (client/js/version-footer.js's sidebar placement).
     * Inputs: none. Output: string - HTML. Either half is '' with no module.
     */
    function footerHtml() {
        const key = window.SessionStatusKey ? window.SessionStatusKey.keyHtml() : '';
        const version = window.VersionFooter ? window.VersionFooter.sidebarFooterHtml() : '';
        return key + version;
    }

    /**
     * Description: the notice shown when a stored arrangement existed and
     *   could not be read. The list falls back to its default order, and
     *   this says so - presenting the default silently would tell the user
     *   it is the arrangement he chose.
     * Inputs: arrangement (object|null) - {status, reason}.
     * Output: string - HTML, or ''.
     */
    function arrangementNoticeHtml(arrangement) {
        if (!arrangement || arrangement.status !== 'unreadable') return '';
        const reason = esc(arrangement.reason || 'reason unknown');
        return (
            '<div class="session-sidebar-notice" role="status" data-arrangement-state="unreadable">' +
            '<div class="session-sidebar-notice__title">CANNOT LOAD your saved order</div>' +
            `<div class="session-sidebar-notice__detail">${reason}. showing the default order ` +
            'until you pin or move something.</div>' +
            '</div>'
        );
    }

    /**
     * Description: build the full list markup for the sidebar body.
     *
     *   AN EMPTY LIST AND AN UNREADABLE ONE MUST NOT LOOK THE SAME. With a
     *   listing that answered, zero rows means the user has no other
     *   conversations and the list says so. With a listing that did NOT
     *   answer, zero rows means nothing at all, and rendering the
     *   confident empty state would be a claim the app cannot support -
     *   and would contradict the CANNOT DETERMINE block the home screen is
     *   rendering from the same failed probe at the same moment.
     *   The remembered-position count is NOT a parameter any more (see
     *   footerHtml); it still reaches `signature()`.
     * Inputs: rows (Array<object>), density (string), listing (object|null)
     *   - {ok, reason, detail},
     *   arrangement (object|null) - {status, reason, collapsed},
     *   opts (object|null) - {dragging (boolean)}, passed straight
     *   through to client/js/session-sidebar-groups.js, which is the only
     *   thing that reads it.
     * Output: string - HTML.
     */
    function listHtml(rows, density, listing, arrangement, opts) {
        const attention = window.SessionListingState
            ? window.SessionListingState.attentionHtml(listing)
            : '';
        const notice = arrangementNoticeHtml(arrangement);
        // THE KEY RIDES EVERY BRANCH, including the two that draw no
        // rows: it explains lights the user has seen on other surfaces
        // too, so it is not conditional on this list holding anything.
        const footer = footerHtml();
        if (!rows || rows.length === 0) {
            if (listing && !listing.ok) return notice + attention + footer;
            const empty = '<div class="session-sidebar-empty">no other conversations</div>';
            return notice + empty + footer;
        }
        const body = window.SessionSidebarGroups
            ? window.SessionSidebarGroups.bodyHtml(rows, density, arrangement, opts)
            : rows.map((r) => rowHtml(r, density)).join('');
        return notice + attention + body + footer;
    }

    /**
     * Description: build one row at the given density. The dot, the theme
     *   swatch, the pin and the close/restart/remove control all come
     *   from shared modules, so this row and the launcher's
     *   running-session row are the same controls with the same tooltips
     *   and confirm copy.
     *
     *   `status` is stamped on the ROW as `data-row-status`, because the
     *   restart picker needs to know what state the row was painted in
     *   and the row is now the only element built from the whole payload.
     *   `is_pinned` needs no attribute of its own: the pin button's
     *   `aria-pressed` carries it. `unread` is not drawn as a glyph at
     *   all but IS still in ``signature``, because the LIGHT renders it.
     * Inputs: r (object) - one merged session row.
     *   density (string) - 'compact' | 'cozy' | 'detailed'.
     * Output: string - HTML.
     */
    function rowHtml(r, density) {
        const mode = density || 'cozy';
        // THE LIGHT CARRIES THE UNREAD FLAG NOW - the envelope is gone.
        const signals = { unread: !!r.unread, startup_gate: r.startup_gate,
            transport: window.SessionTransport
                ? window.SessionTransport.stateFor(r.name) : 'unknown' };
        const dot = window.SessionStatusUI
            ? window.SessionStatusUI.dotHtml(r.status, signals) : '';
        // punchlist 19 - "needs a keypress". Empty string for both 'ready'
        // and 'unknown', so this adds nothing to a normal row. It rides
        // at EVERY density including compact, unlike the tmux/external
        // badge: that badge is redundant with the row's own styling,
        // while this one is the only thing on screen saying the session
        // has not started. A density setting must not be able to hide it.
        const startupGate = window.SessionStartupGate
            ? window.SessionStartupGate.indicatorHtml(r.startup_gate)
            : '';
        // TWO STRINGS, NOT INTERCHANGEABLE. `name` is the tmux handle,
        // for the ATTRIBUTES - grip, pin, group filing, delete and
        // reorder all key on it, so it must never be a label. `display` is what
        // a HUMAN reads, from the one resolver in session-label.js. This
        // row rendered the handle over a `label` its payload has carried
        // since the feature landed, so "Media Compression" showed as
        // "cloude_Media". Outcome 3 (null) falls back to the handle: a
        // blank where a name goes is worse than either.
        const name = esc(r.name);
        const resolved = window.SessionLabel
            ? window.SessionLabel.resolve(r)
            : null;
        const display = resolved === null ? name : esc(resolved);
        const badge = r.created_by_cloude ? 'tmux' : 'external';
        const sidAttr = r.session_id ? ` data-session-id="${esc(r.session_id)}"` : '';
        const themeAttrs = window.SessionThemeTint
            ? window.SessionThemeTint.attrs(r.pinned_theme)
            : '';
        // The session-theme cue, and the only thing that carries it. It
        // used to be an accent ring on the row's own box, which is the
        // box `[data-active="1"]` uses for selection - so a session
        // pinned to the host theme read as the selected row. See
        // client/js/session-theme-tint.js. Empty for all three
        // not-themed cases.
        const themeSwatch = window.SessionThemeTint
            ? window.SessionThemeTint.swatchHtml(r.pinned_theme)
            : '';
        // THE TWO INLINE CONTROLS. Pin toggles the row to the top; the
        // action is close on a live row, restart plus remove on a dead
        // one. Both come from the builders that own them, so this row and
        // the launcher's card draw the same glyphs, tooltips and confirm
        // copy.
        const pin = pinButtonHtml(r.name, !!r.is_pinned);
        const rowAction = window.SessionRowActions
            ? window.SessionRowActions.html(
                r.status, r.name, 'session-sidebar-row-delete')
            : '';
        const rename = renameState(r);
        // The badge is the first thing to go when the user asks for thin
        // rows: "tmux" vs "external" is already carried by the row's
        // ownership styling, so at compact it is the most redundant glyph
        // on the row. At DETAILED it moves to the second line rather than
        // being dropped, which is what keeps that line about something -
        // the family pill used to sit there and no longer exists.
        const badgeHtml = `<span class="session-sidebar-row-badge">${badge}</span>`;
        const age = ageLabel(r.created_at_epoch);
        const secondLine = mode === 'detailed'
            ? ('<div class="session-sidebar-row-meta">'
                + badgeHtml
                + (age ? `<span class="session-sidebar-row-age">${esc(age)}</span>` : '')
                + '</div>')
            : '';
        const inlineBadge = (mode === 'cozy') ? badgeHtml : '';
        return (
            `<div class="session-sidebar-row" data-name="${name}" ` +
            `data-active="${r.is_this_tab ? '1' : '0'}" ` +
            `data-pinned="${r.is_pinned ? '1' : '0'}" ` +
            `data-rename-state="${rename.state}" ` +
            // THE ROW CARRIES ITS OWN STATUS NOW. It used to live on the
            // kebab; with that gone the row is the only element built
            // from the whole payload, and session-sidebar-clicks.js reads
            // it from here to tell the restart picker what state the
            // session was painted in.
            `data-row-status="${esc(r.status || 'unknown')}" ` +
            (window.SessionSidebarFetch ? window.SessionSidebarFetch.workAttr(r) : '') +
            `role="option" aria-selected="${r.is_this_tab ? 'true' : 'false'}" ` +
            `tabindex="-1"${sidAttr}${themeAttrs}>` +
            '<div class="session-sidebar-row-main">' +
            gripHtml(r.name) +
            dot +
            // `data-row-name` stays the HANDLE: nothing reads its value
            // (it is the rename module's `closest()` target), and an
            // identity-shaped attribute should not carry a label. The
            // TEXT is the display value; they differ on purpose.
            `<span class="session-sidebar-row-name" data-row-name="${name}" ` +
            `title="${esc(rename.reason)}">${display}</span>` +
            // After the name, not before it: the name column starts at
            // the same x on every row whether or not it is themed, so a
            // themed row does not make the list ragged. It is also the
            // full width of the name away from the status dot, which is
            // the other coloured mark on the row.
            themeSwatch +
            startupGate +
            inlineBadge +
            pin +
            rowAction +
            '</div>' +
            secondLine +
            '</div>'
        );
    }

    window.SessionSidebarRows = {
        listHtml, rowHtml, signature, esc, gripHtml, renameState,
        pinButtonHtml, ageLabel, footerHtml, arrangementNoticeHtml,
    };
    console.log('[SessionSidebarRows Module] Exported as window.SessionSidebarRows');
})();
