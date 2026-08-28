/**
 * Session Status UI - shared status-dot rendering for the launchpad's
 * "Running Sessions" list and the in-terminal session sidebar.
 *
 * Single source of truth so the two call sites never drift on colors,
 * labels, or the accessibility wiring (title + aria-label, never
 * color-only - a user who can't distinguish the dot colors still needs
 * to know what state a session is in).
 *
 * feat/hook-driven-status: states mirror the backend's UNIFIED vocabulary
 * (src/core/session_status.py's ALL_ACTIVITY_STATUSES), driven by Claude
 * Code's own lifecycle hooks where available and gracefully falling back
 * to tmux-only classification otherwise. Listed in the exact display
 * priority the states are meant to be noticed in:
 *   dead             - the pane's process exited; tmux is only holding
 *                       the corpse open. Loudest treatment on purpose
 *                       (see CLAUDE.md hazard: a dead pane that "looked"
 *                       fine).
 *   question         - Claude is waiting on YOU (a Notification or
 *                       PermissionRequest hook fired and nothing has
 *                       resolved it yet). The state this whole feature
 *                       exists to surface alongside finished_unread.
 *   working_subagent - the agent is actively working INSIDE a spawned
 *                       subagent (SubagentStart/Stop heartbeat).
 *   working          - the agent is actively doing tool work at the top
 *                       level (PreToolUse/PostToolUse heartbeat), or (no
 *                       hook signal at all) tmux reports a non-shell
 *                       foreground process - the old "running".
 *   finished_unread  - a Stop hook landed and nobody has looked since, OR
 *                       the user manually pinned this session unread for
 *                       followup.
 *   idle             - alive, nothing pending, already seen.
 *   unknown          - status could not be determined (non-tmux backend,
 *                       tmux query failed, or hooks not installed AND
 *                       tmux itself can't classify the pane). Never
 *                       guessed.
 *
 * Must load AFTER no other module (no dependencies) and BEFORE
 * launchpad.js / session-sidebar.js, both of which call into it.
 */

console.log('[SessionStatusUI Module] Loading...');

(function () {
    /**
     * Human-readable label per status. Used for both the visible badge
     * text (uppercased by CSS) and the title/aria-label pair on the dot,
     * so the meaning is never conveyed by color alone.
     *
     * @type {Object<string, string>}
     */
    const STATUS_LABELS = {
        dead: 'dead - process exited',
        question: 'your turn - claude is waiting on you',
        working_subagent: 'working - a subagent is active',
        working: 'working - agent active',
        finished_unread: 'finished - unread',
        idle: 'idle - waiting at the shell',
        // feat/ended-sessions-visibility. NOT a synonym for `dead`, and
        // the difference is the whole reason it earns a key: `dead` is a
        // tmux session that still EXISTS holding an exited process, so it
        // can be attached to and cleared up. `stopped` is the word
        // sessions.lifecycle already uses for a tmux instance that is
        // GONE - there is nothing to attach to and nothing to kill, only
        // a stored record. Reusing `dead` for it would have told the user
        // to go clear up a pane that does not exist.
        stopped: 'ended - the session is no longer running',
        unknown: 'status unknown',
        // Back-compat: a stale cached response (pre feat/hook-driven-status
        // server, or a browser tab that hasn't reloaded yet) may still send
        // the old tmux-only 'running' string. Map it onto 'working' rather
        // than falling through to 'unknown' so a half-upgraded deployment
        // still renders something meaningful.
        running: 'working - agent active',
    };

    /**
     * CSS modifier class per status - kept separate from STATUS_LABELS so
     * the 'running' back-compat alias can share the 'working' dot style
     * without duplicating a color rule.
     * @type {Object<string, string>}
     */
    const STATUS_DOT_CLASS = {
        dead: 'dead',
        question: 'question',
        working_subagent: 'working-subagent',
        working: 'working',
        finished_unread: 'finished-unread',
        idle: 'idle',
        stopped: 'stopped',
        unknown: 'unknown',
        running: 'working',
    };

    /**
     * Escape a value for interpolation into a double-quoted HTML attribute.
     *
     * Description: The ONE escaper this module uses. `&` must be replaced
     *   first or the later replacements would be re-escaped ("<" becoming
     *   "&amp;lt;"). All five characters are handled so the result is
     *   correct inside either quoting style and survives a round trip
     *   through `element.dataset`, which is what the mark-unread handlers
     *   in launchpad.js and session-sidebar.js read back.
     *
     *   Deliberately string-based rather than the
     *   `div.textContent = s; return div.innerHTML` trick used elsewhere
     *   in the client: that idiom does NOT escape quote characters, so it
     *   is wrong for an attribute value and right only for text content.
     *   It is also why this module keeps its own helper instead of reusing
     *   App._escapeHtml, on top of this file having no dependencies by
     *   design (it loads before everything that calls it).
     * Inputs:
     *   value (any) - stringified first; null/undefined become ''.
     * Output:
     *   string - safe to place between the quotes of an attribute.
     * Example:
     *   escapeAttr('a"b\'c<d>') -> 'a&quot;b&#39;c&lt;d&gt;'
     */
    function escapeAttr(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /**
     * Normalize any input into one of the known status keys.
     *
     * Description: Defensive normalizer so a missing/unexpected value from
     *   the API (older cached response, non-tmux backend) never produces
     *   an unstyled dot or an empty aria-label.
     * Inputs:
     *   status (string|null|undefined) - raw value from the API payload.
     * Output:
     *   string - one of the STATUS_LABELS keys.
     * Example:
     *   normalizeStatus('working') -> 'working'
     *   normalizeStatus(undefined) -> 'unknown'
     */
    function normalizeStatus(status) {
        return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status)
            ? status
            : 'unknown';
    }

    /**
     * Build the status-dot markup for a session row.
     *
     * Description: Returns a `<span>` styled by a `status-dot--<state>`
     *   CSS class (see client/css/session-sidebar.css), carrying both
     *   `title` (desktop hover tooltip) and `aria-label` (screen readers)
     *   set to the same human-readable label - the accessibility
     *   requirement this feature exists for. `role="img"` marks it as a
     *   meaningful glyph rather than decoration (unlike the old
     *   `aria-hidden="true"` dot it replaces).
     * Inputs:
     *   status (string|null|undefined) - raw activity_status/status value.
     * Output:
     *   string - HTML for a single inline `<span>` element.
     * Example:
     *   dotHtml('dead') ->
     *     '<span class="status-dot status-dot--dead" role="img"
     *        title="dead - process exited"
     *        aria-label="dead - process exited"></span>'
     */
    function dotHtml(status) {
        const key = normalizeStatus(status);
        const label = STATUS_LABELS[key];
        const cssClass = STATUS_DOT_CLASS[key];
        // label/cssClass come from the frozen tables above via
        // normalizeStatus, so they cannot currently carry a special
        // character. Escaped anyway: the audit that added escapeAttr found
        // exactly this shape (raw interpolation into an attribute) already
        // shipped once with a user-controlled value, and a future label
        // edit must not be able to reintroduce it.
        return (
            `<span class="status-dot status-dot--${escapeAttr(cssClass)}" role="img" ` +
            `title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}"></span>`
        );
    }

    /**
     * Look up the human-readable label alone (no markup) - used where a
     * caller wants plain text, e.g. a badge or a screen-reader-only note.
     *
     * Inputs:
     *   status (string|null|undefined) - raw activity_status/status value.
     * Output:
     *   string - human-readable label.
     * Example:
     *   labelFor('idle') -> 'idle - waiting at the shell'
     */
    function labelFor(status) {
        return STATUS_LABELS[normalizeStatus(status)];
    }

    /**
     * Build the markup for the manual "mark unread for followup" toggle.
     *
     * Description: A small button, distinct from the status dot, so the
     *   user can flag a session for later attention regardless of its
     *   current live activity state. Carries `aria-pressed` (not just a
     *   CSS class) so the toggled state is exposed to assistive tech, and
     *   `title`/`aria-label` name the action in words. The caller wires
     *   the click handler (this module only builds markup); `data-*`
     *   attributes carry what the handler needs to know which row was
     *   clicked and its CURRENT state, so the handler can send the
     *   opposite value without re-querying the DOM.
     * Inputs:
     *   tmuxName (string) - literal tmux session name (unread is keyed by
     *     name server-side, not session_id - see PATCH
     *     /sessions/{name}/unread).
     *   unread (boolean) - current unread state for this row.
     * Output:
     *   string - HTML for a single inline `<span role="button">`.
     * Example:
     *   markUnreadHtml('cloude_myproj', false) ->
     *     '<span class="mark-unread-toggle" role="button" ...>...</span>'
     */
    function markUnreadHtml(tmuxName, unread) {
        const label = unread
            ? 'clear unread flag'
            : 'mark unread for followup';
        const pressed = unread ? 'true' : 'false';
        // tmuxName is the only user-controlled value in this module. A
        // session name is free text, so it can hold a quote, an angle
        // bracket, or an ampersand; interpolating it raw put arbitrary
        // markup into the attribute list. The previous quote-only replace
        // left `&` alone, which silently corrupted any name containing an
        // entity-shaped substring on the way back out through
        // `dataset.markUnread`.
        const safeName = escapeAttr(tmuxName);
        return (
            `<span class="mark-unread-toggle${unread ? ' mark-unread-toggle--active' : ''}" ` +
            `role="button" tabindex="0" aria-pressed="${pressed}" ` +
            `title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}" ` +
            `data-mark-unread="${safeName}" data-unread-current="${pressed}">` +
            `${unread ? envelopeFilledSvg() : envelopeOutlineSvg()}</span>`
        );
    }

    /**
     * Envelope glyph, "not flagged unread" state - a plain outline, same
     * family as trashIconSvg (16x16 viewBox, stroke="currentColor",
     * fill="none", stroke-width 1.5). No `stroke` color set on the paths
     * themselves; the caller's CSS `color` drives the stroke via
     * currentColor so the icon recolors with the row/theme like every
     * other control in the app.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   envelopeOutlineSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function envelopeOutlineSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<rect x="2" y="3.5" width="12" height="9" rx="1.25" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="M2.5 4.25L8 8.5L13.5 4.25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '</svg>'
        );
    }

    /**
     * Envelope glyph, "flagged unread" state - the same envelope outline
     * plus a solid notification dot in the top-right corner, so the two
     * states are distinguishable by shape (not color alone).
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   envelopeFilledSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function envelopeFilledSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<rect x="2" y="3.5" width="12" height="9" rx="1.25" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="M2.5 4.25L8 8.5L13.5 4.25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<circle cx="12.5" cy="3.5" r="2.5" fill="currentColor" stroke="var(--color-bg, #000)" stroke-width="0.75"/>' +
            '</svg>'
        );
    }

    /**
     * Shared trash-can glyph for every delete control in the app (launcher
     * project rows, in-terminal conversation sidebar rows, the running-
     * sessions kill button). Single source of truth so the three call
     * sites can never draw three slightly different trash cans - one SVG,
     * one definition. No `stroke` color is set on the paths themselves;
     * callers wrap this in a button whose CSS `color` (default + :hover)
     * drives the stroke via `currentColor`, matching every other icon
     * button in the app.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   trashIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function trashIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M3 4.5H13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M5.5 4.5V3.25C5.5 2.83579 5.83579 2.5 6.25 2.5H9.75C10.1642 2.5 10.5 2.83579 10.5 3.25V4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M4.5 4.5L5 12.75C5 13.1642 5.33579 13.5 5.75 13.5H10.25C10.6642 13.5 11 13.1642 11 12.75L11.5 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M6.5 6.75V11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M9.5 6.75V11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '</svg>'
        );
    }

    /**
     * Shared X (close) glyph for every "close this running session"
     * control in the app (launcher running-session rows, the conversation
     * sidebar). Deliberately NOT the trash can: in this app an X means
     * "stop the running process, keep the record", a trash can means
     * "forget the record, stop nothing" - see client/js/session-row-actions.js
     * for the semantics both glyphs are bound to. Same family as
     * trashIconSvg/pencilIconSvg: 16x16 viewBox, stroke="currentColor",
     * fill="none", stroke-width 1.5, no color set on the paths so the
     * caller's CSS `color` drives it via currentColor.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   closeIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function closeIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M4 4L12 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M12 4L4 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '</svg>'
        );
    }

    /**
     * Shared restart glyph - a circular arrow - for the "start the agent
     * again in this session" control on a dead row
     * (client/js/session-row-actions.js).
     *
     * A CIRCULAR ARROW, deliberately, not a play triangle. Play reads as
     * "begin something new"; this control puts a process back into a pane
     * that already exists and keeps its scrollback, its name and its
     * place in the list. The shape has to say "again", not "new", because
     * the row it sits on already offers a destructive neighbour and the
     * two must not be confusable at a glance.
     *
     * Same family as trashIconSvg / closeIconSvg / pencilIconSvg: 16x16
     * viewBox, stroke="currentColor", fill="none" for the arc and an
     * explicit currentColor fill on the solid arrowhead, stroke-width
     * 1.5, no color set on the element so the caller's CSS `color` drives
     * it via currentColor.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   restartIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function restartIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M13 8A5 5 0 1 1 11.4 4.3" stroke="currentColor" ' +
            'stroke-width="1.5" stroke-linecap="round"/>' +
            '<path d="M12.9 1.9V5.1H9.7L12.9 1.9Z" fill="currentColor"/>' +
            '</svg>'
        );
    }

    /**
     * Shared pencil (edit) glyph for every inline-rename/edit control in
     * the app (launcher project rows, running-session rename). Same
     * family as trashIconSvg: 16x16 viewBox, stroke="currentColor",
     * fill="none", stroke-width 1.5, no color set on the paths - the
     * caller's CSS `color` drives it via currentColor.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   pencilIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function pencilIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M10.5 2.5L13.5 5.5L5.5 13.5H2.5V10.5L10.5 2.5Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
            '<path d="M9 4L12 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '</svg>'
        );
    }

    /**
     * Folder glyph for directory rows in the config editor tree. Same
     * family as trashIconSvg/pencilIconSvg (16x16, stroke=currentColor,
     * fill=none, stroke-width 1.5) so it recolors with the row via CSS.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   folderIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function folderIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M2 4.25C2 3.69772 2.44772 3.25 3 3.25H6.5L7.75 4.75H13C13.5523 4.75 14 5.19772 14 5.75V11.25C14 11.8023 13.5523 12.25 13 12.25H3C2.44772 12.25 2 11.8023 2 11.25V4.25Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
            '</svg>'
        );
    }

    /**
     * Plain-document glyph for file rows in the config editor tree -
     * deliberately distinct in silhouette from folderIconSvg (a folded
     * corner, no tab) so directory vs. file is legible even to a user who
     * can't tell fill from stroke color at a glance. Same 16x16/stroke
     * family as the rest of this module.
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   fileIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function fileIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<path d="M4.5 2.5H9L11.5 5V13C11.5 13.2761 11.2761 13.5 11 13.5H4.5C4.22386 13.5 4 13.2761 4 13V3C4 2.72386 4.22386 2.5 4.5 2.5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
            '<path d="M9 2.5V5H11.5" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>' +
            '</svg>'
        );
    }

    /**
     * Padlock glyph marking a sensitive (credentials/secret/key-shaped)
     * file in the config editor tree and modal - see config_files.py's
     * SENSITIVE_* constants. Same family as fileIconSvg/folderIconSvg
     * (16x16, stroke=currentColor, fill=none, stroke-width 1.5).
     * Inputs: none.
     * Output: string - a self-contained `<svg>` element, 16x16 viewBox.
     * Example:
     *   lockIconSvg() -> '<svg width="16" height="16" ...>...</svg>'
     */
    function lockIconSvg() {
        return (
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
            '<rect x="3.5" y="7.25" width="9" height="6.25" rx="1" stroke="currentColor" stroke-width="1.5"/>' +
            '<path d="M5.5 7.25V5A2.5 2.5 0 0 1 10.5 5V7.25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>' +
            '</svg>'
        );
    }

    window.SessionStatusUI = {
        escapeAttr,
        normalizeStatus,
        dotHtml,
        labelFor,
        markUnreadHtml,
        trashIconSvg,
        closeIconSvg,
        restartIconSvg,
        pencilIconSvg,
        envelopeOutlineSvg,
        envelopeFilledSvg,
        folderIconSvg,
        fileIconSvg,
        lockIconSvg,
    };
    console.log('[SessionStatusUI Module] Exported as window.SessionStatusUI');
})();
