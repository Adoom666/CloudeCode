/**
 * THE RESTART PICKER'S OPTION LIST, and the one fact that is not a choice.
 * ----------------------------------------------------------------------
 * Split out of session-restart-picker.js, which was 578 lines and past
 * this project's 500-line rule before anything below was added. The
 * picker still owns the panel, the promise and the arming gate; this file
 * owns the rows, and it is the whole of what is drawn inside
 * `.restart-picker__options`.
 *
 * A PROPERTY OF THE SESSION IS NOT A PROPERTY OF THE CHOICE, and getting
 * that wrong is what this file was extracted to fix. The owner opened
 * restart on a session whose transcript is gone and read the SAME
 * sentence five times, once under every wrapper:
 *
 *   no transcript for claude session db81f6bf-... exists under
 *   /Users/jsugamele/.claude/projects, so it cannot be resumed
 *
 * Whether a conversation is on disk cannot vary with which wrapper is
 * picked to reopen it, so printing it per option told the user nothing
 * about any of the options and buried the four words that DID differ.
 * `sharedDetail()` detects that case and `optionsHtml()` drops the line
 * from every row, leaving one statement above the list.
 *
 * IT IS DETECTED, NOT ASSUMED, and that is deliberate rather than
 * defensive. Continuity genuinely CAN differ between these rows: the
 * replay rung re-runs the command tmux recorded and resumes whatever
 * uuid THAT string carries, while the agent rung resumes the uuid on the
 * session's row. They are frequently different conversations and either
 * may be absent on its own, which is exactly why the server measures
 * presence into a `presence_by_uuid` map rather than reaching a single
 * verdict (src/core/session_manager.py, src/core/session_restart_preview.py).
 * So the hoist fires only when every row's sentence is byte-identical,
 * which is the proof that it is a session-level fact. The moment two rows
 * disagree, both sentences stay on their own rows where they mean
 * something.
 *
 * Load AFTER session-sidebar-rows.js; BEFORE session-restart-picker.js.
 */

console.log('[SessionRestartOptions Module] Loading...');

(function () {
    'use strict';

    /**
     * Short badge for each predicted rung. The SENTENCE always comes from
     * the server (`detail`) so the wording lives in one place; this is
     * only the two- or three-word tag that lets the eye sort the list
     * without reading every line.
     * @type {Object<string, string>}
     */
    var KIND_BADGE = {
        agent: 'would start the agent',
        replay: 'would replay the recorded command',
        shell: 'would return a plain shell',
        not_dead: 'still running',
        transcript_missing: 'its conversation is gone',
        cannot_determine: 'cannot be determined',
    };

    /**
     * Rungs a user is allowed to commit to. Mirrors
     * ACTIONABLE_RESPAWN_KINDS in src/core/session_respawn.py. The server
     * is the authority (`actionable` / `actionable_now`); this exists so a
     * payload missing that field fails CLOSED rather than open.
     * @type {Array<string>}
     */
    var ACTIONABLE = ['agent', 'replay', 'shell'];

    /**
     * Description: HTML-escape for text and attributes. Routed through
     *   SessionSidebarRows when present so this module owns no second
     *   escaper; the inline fallback keeps the panel safe if that module
     *   has not loaded.
     * Inputs: value (any).
     * Output: string, safe inside a double-quoted attribute or as text.
     */
    function esc(value) {
        if (window.SessionSidebarRows) return window.SessionSidebarRows.esc(value);
        var div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML.replace(/"/g, '&quot;');
    }

    /**
     * Description: can this predicted rung be acted on?
     * Inputs: kind (string) - a ladder verdict.
     * Output: boolean - false for not_dead, cannot_determine and anything
     *   unrecognised. An unknown verdict is treated as NOT actionable,
     *   because a client that guesses yes about a rung it has never heard
     *   of is exactly the failure this panel exists to prevent.
     */
    function isActionable(kind) {
        return ACTIONABLE.indexOf(kind) !== -1;
    }

    /**
     * Description: the detail sentence and predicted rung every row of
     *   the panel would render, baseline row first, in display order.
     *   The single source of truth for "what does row N say", used both
     *   to build the rows and to decide whether they all say the same
     *   thing. Reading it twice from two different expressions is how the
     *   hoist would drift from the rows it hoists out of.
     * Inputs: preview (object) - the RestartPreviewResponse body.
     * Output: Array<{detail: string, kind: string}>.
     * Example: rowFacts({unchanged: {}, projected: {}, options: []})
     *   -> [{detail: 'what this would run could not be determined',
     *        kind: 'cannot_determine'}]
     */
    function rowFacts(preview) {
        var unchanged = (preview && preview.unchanged) || {};
        var projected = (preview && preview.projected) || {};
        var facts = [{
            detail: projected.detail || unchanged.detail
                || 'what this would run could not be determined',
            kind: projected.kind || unchanged.kind || 'cannot_determine',
        }];
        ((preview && preview.options) || []).forEach(function (o) {
            facts.push({
                detail: (o && (o.projected_detail || o.detail)) || '',
                kind: (o && (o.projected_kind || o.kind)) || 'cannot_determine',
            });
        });
        return facts;
    }

    /**
     * Description: the one sentence EVERY row would print, when every row
     *   would print the same one. That identity is the evidence that the
     *   sentence describes the session rather than any choice in it, so
     *   it is what licenses moving the line out of the list.
     *
     *   TWO ROWS MINIMUM. A panel with only the baseline row has no
     *   repetition to remove, and hoisting its single detail would strip
     *   the row of its only explanation to say the same words two
     *   millimetres higher.
     *
     *   AN EMPTY DETAIL IS NOT A SHARED ONE. Rows that all say nothing
     *   agree trivially, and returning '' for them is also what the
     *   caller reads as "nothing to hoist", so the two cases collapse
     *   safely.
     *
     *   THE RUNG MUST MATCH TOO, not only the sentence. The verdict badge
     *   is dropped from the rows on the strength of this answer, and a
     *   badge is the thing that stops a shell reading like an agent. Two
     *   rows that somehow shared a sentence while predicting different
     *   outcomes must keep their own badges, so the stricter test is the
     *   one that runs.
     * Inputs: preview (object) - the RestartPreviewResponse body.
     * Output: {detail: string, kind: string} - detail is '' when the rows
     *   do not agree, which is the signal to leave every line where it is.
     */
    function sharedDetail(preview) {
        var facts = rowFacts(preview);
        var none = { detail: '', kind: '' };
        if (facts.length < 2) return none;
        var first = facts[0];
        if (!first.detail) return none;
        for (var i = 1; i < facts.length; i += 1) {
            if (facts[i].detail !== first.detail) return none;
            if (facts[i].kind !== first.kind) return none;
        }
        return { detail: first.detail, kind: first.kind };
    }

    /**
     * Description: the hoisted sentence, rendered once above the list.
     *
     *   IT IS SHORTENED ONLY WHERE A SHORTER TRUE SENTENCE EXISTS. For
     *   `transcript_missing` the server's wording is a uuid and an
     *   absolute path, which is diagnostic rather than decision support:
     *   what the user has to decide with is that this session cannot come
     *   back on its old conversation. Every other shared verdict is
     *   printed verbatim, because inventing a paraphrase for a sentence
     *   this file has not seen is how a UI starts lying.
     *
     *   THE EXACT WORDING IS KEPT, NOT DELETED, and kept somewhere a
     *   PHONE can reach it. A `title` tooltip cannot be opened by a
     *   thumb, and this app is driven from a phone, so the full sentence
     *   goes in a native `<details>` that needs no script and no CSP
     *   exception to open.
     * Inputs: preview (object) - the RestartPreviewResponse body.
     * Output: string - HTML, or '' when the rows disagree and every
     *   sentence therefore belongs on its own row.
     */
    function sharedDetailHtml(preview) {
        var shared = sharedDetail(preview);
        if (!shared.detail) return '';
        if (shared.kind !== 'transcript_missing') {
            return '<div class="restart-picker__shared">'
                + esc(shared.detail) + '</div>';
        }
        return '<div class="restart-picker__shared" '
            + 'data-kind="transcript_missing">'
            + '<span class="restart-picker__shared-line">'
            + esc(KIND_BADGE.transcript_missing)
            + ', so nothing below can resume it. a restart starts a new one.'
            + '</span>'
            + '<details class="restart-picker__shared-more">'
            + '<summary>what was looked for</summary>'
            + '<span>' + esc(shared.detail) + '</span>'
            + '</details>'
            + '</div>';
    }

    /**
     * Description: build one selectable row.
     * Inputs:
     *   id (string) - DOM id for the input.
     *   value (string) - the agent_type this row stands for, '' for the
     *     "leave it as it is" row.
     *   title (string) - the row's heading.
     *   spec (object) - {kind, projectedKind, detail, actionableNow,
     *     current, checked}. `kind` is what a restart does RIGHT NOW;
     *     `projectedKind` is what this choice would come back AS.
     *   hoisted (string) - the sentence already printed above the list,
     *     or ''. A row whose detail IS that sentence renders no detail,
     *     because it has already been said once for the whole session.
     * Output: string - HTML for one <label>.
     */
    function optionHtml(id, value, title, spec, hoisted) {
        // TWO DIFFERENT FACTS, TWO DIFFERENT USES, and the server keeps
        // them apart so this can too:
        //   projectedKind  what this choice would COME BACK AS. It is the
        //                  badge and the sentence, because it is the
        //                  question the user is actually asking, and it
        //                  is the only one that says anything at all
        //                  about a session that is still running.
        //   actionableNow  whether a restart may act on it right now. It
        //                  is the ONLY thing that enables the radio. A
        //                  prediction is never a permission.
        var kind = spec.projectedKind || spec.kind || 'cannot_determine';
        // BOTH, and it is belt and braces on purpose. The server's
        // `actionable_now` is the authority; `isActionable` refuses a rung
        // this client has never heard of, so a future verdict added
        // server-side cannot be offered by an old client that has no idea
        // what it means.
        var canPick = spec.actionableNow === true && isActionable(spec.kind);
        // WHETHER THIS COULD BE PICKED IF THE USER ARMS A LIVE RESTART.
        // Carried as data, never as `disabled`: the initial render is
        // derived from `actionable_now` ALONE, so a live session paints
        // every radio disabled however good its projection is. Only
        // `sync()` may relax that, and only from a checkbox.
        var liveEligible = isActionable(spec.projectedKind || spec.kind);
        var disabled = canPick ? '' : ' disabled';
        var checked = spec.checked ? ' checked' : '';
        var current = spec.current
            ? '<span class="restart-picker__current">current</span>'
            : '';
        var detail = spec.detail || '';
        // ALREADY SAID ONCE, FOR THE WHOLE SESSION. When this row's
        // sentence IS the hoisted one, the row drops both the sentence
        // and the badge above it, because the caller only hoists when
        // every row agreed on the rung as well as the wording. A badge
        // that reads the same on every row sorts nothing, and repeating
        // it under a heading that just said those exact words is the
        // repetition this file was written to remove.
        //
        // THE ROW STILL SHOWS THAT IT CANNOT BE PICKED. `is-unavailable`
        // and the disabled radio come from `actionable_now` and are
        // untouched here, so nothing about what may be pressed depends on
        // this branch.
        var hoistedRow = !!hoisted && detail === hoisted;
        var detailHtml = (detail && !hoistedRow)
            ? '<span class="restart-picker__detail">' + esc(detail) + '</span>'
            : '';
        var badgeHtml = hoistedRow ? ''
            : '<span class="restart-picker__kind" data-kind="' + esc(kind) + '">'
                + esc(KIND_BADGE[kind] || 'cannot be determined') + '</span>';
        return (
            '<label class="restart-picker__option' + (canPick ? '' : ' is-unavailable')
            + '" for="' + esc(id) + '">'
            + '<input type="radio" name="restart-picker-choice" id="' + esc(id) + '" '
            + 'value="' + esc(value) + '" data-kind="' + esc(kind) + '" '
            + 'data-now-kind="' + esc(spec.kind || '') + '" '
            + 'data-actionable-now="' + (canPick ? '1' : '0') + '" '
            + 'data-live-eligible="' + (liveEligible ? '1' : '0') + '"'
            + disabled + checked + '>'
            + '<span class="restart-picker__body">'
            + '<span class="restart-picker__title">' + esc(title) + current + '</span>'
            + badgeHtml
            + detailHtml
            + '</span></label>'
        );
    }

    /**
     * Description: the whole option list, "leave it as it is" first.
     *
     *   THE BASELINE ROW LEADS ON PURPOSE. It is the row that exposes the
     *   shell landmine: on a pane with no recorded start command it reads
     *   "returns a plain shell", which is the fact the user most needs
     *   before deciding anything.
     * Inputs: preview (object) - the RestartPreviewResponse body.
     * Output: string - HTML.
     */
    function optionsHtml(preview) {
        var unchanged = preview.unchanged || {};
        var projected = preview.projected || {};
        // WHAT HAS ALREADY BEEN SAID ONCE, so no row says it again. The
        // panel renders `sharedDetailHtml(preview)` immediately above
        // this list from the same function, so the two cannot disagree
        // about which sentence was lifted.
        var hoisted = sharedDetail(preview).detail;
        var baselineTitle = preview.current_agent_type
            ? 'leave it on ' + preview.current_agent_type
            : 'leave it as it is';
        var out = [
            optionHtml('restart-choice-keep', '', baselineTitle, {
                kind: unchanged.kind || 'cannot_determine',
                projectedKind: projected.kind || unchanged.kind
                    || 'cannot_determine',
                detail: projected.detail || unchanged.detail
                    || 'what this would run could not be determined',
                actionableNow: unchanged.actionable === true,
                current: false,
                checked: true,
            }, hoisted),
        ];
        (preview.options || []).forEach(function (o, i) {
            out.push(optionHtml(
                'restart-choice-' + i,
                o.agent_type,
                o.label || o.agent_type,
                {
                    kind: o.kind,
                    projectedKind: o.projected_kind || o.kind,
                    detail: o.projected_detail || o.detail,
                    actionableNow: o.actionable_now === true,
                    current: !!o.is_current,
                    checked: false,
                },
                hoisted
            ));
        });
        return out.join('');
    }

    window.SessionRestartOptions = {
        KIND_BADGE: KIND_BADGE,
        esc: esc,
        isActionable: isActionable,
        rowFacts: rowFacts,
        sharedDetail: sharedDetail,
        sharedDetailHtml: sharedDetailHtml,
        optionsHtml: optionsHtml,
    };
})();

console.log('[SessionRestartOptions Module] Exported as window.SessionRestartOptions');
