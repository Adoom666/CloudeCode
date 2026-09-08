/**
 * ONE CHAT BUBBLE: a turn, its blocks, its "i", and its subagents.
 *
 * THE BRIEF, VERBATIM: "in the conversation view it should read like a
 * chat, and each bubble should have icons for like an i for info about
 * the chat message, if it has sub agents it should be able to be opened
 * to see the run subagents in the particular message and then you can
 * drill down into and view the subagent the same way. not information
 * overload, but the ability to grab as much info as properly."
 *
 * SO THE DEFAULT STATE OF A BUBBLE IS PROSE AND NOTHING ELSE. The
 * envelope is behind the "i". Thinking, tool calls and tool results are
 * behind their own disclosures (archive-chat-block.js). Subagents are
 * behind a counted expander. Everything is reachable in one click from
 * where you already are; nothing is in your way before you click.
 *
 * ROLE IS NEVER SIGNALLED BY COLOUR ALONE, and this is not a
 * nice-to-have here. Three of this app's 23 themes - terminal, gameboy,
 * legacy_apple - deliberately zero every radius token, so a bubble in
 * those themes is a rectangle and the chat metaphor cannot lean on
 * rounded corners either. Every turn therefore carries the role as
 * TEXT in its header, plus a `data-role` attribute for the stylesheet to
 * hang a border or an indent on. Strip the CSS entirely and the
 * transcript still reads as a conversation.
 *
 * A ROW IS A PURE FUNCTION OF (turn, viewState). It holds no state of
 * its own and registers no listeners: rows are recycled on every paint
 * by the virtual list, so a bound listener would leak and a captured
 * flag would render the wrong turn's open panel. Which panels are open
 * lives in archive-chat-view.js and arrives as an argument.
 *
 * PROGRESS RECORDS ARE NOT TURNS. They are 917,436 rows, 37.5 percent of
 * every body in the corpus, and they stay collapsed behind a count chip
 * exactly as the raw reader already does it. This file renders that chip
 * when handed a `progress-run` item; it never expands one inline.
 *
 * Depends on archive-chat-block.js, archive-chat-info.js,
 * archive-chat-subagents.js. Exports window.ArchiveChatTurn.
 */

console.log('[ArchiveChatTurn Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-turn';

    /** @type {string} toggles the envelope panel for one turn. */
    var ACTION_INFO = 'toggle-turn-info';
    /** @type {string} toggles the subagent list for one turn. */
    var ACTION_SUBAGENTS = 'toggle-turn-subagents';
    /** @type {string} expands a collapsed run of progress records. */
    var ACTION_EXPAND_PROGRESS = 'expand-progress';
    /** @type {string} re-collapses one. */
    var ACTION_COLLAPSE_PROGRESS = 'collapse-progress';

    /**
     * How each role is announced. The KEY is what the server sends; the
     * value is what a person reads. A role not in this table is rendered
     * under its own raw name - an unknown speaker is still a speaker, and
     * folding it into "assistant" would attribute somebody's words to
     * the wrong party.
     * @type {Object<string,string>}
     */
    var ROLE_LABELS = {
        user: 'You',
        assistant: 'Claude',
        system: 'System',
        tool: 'Tool'
    };

    /** Rendered wherever a fact was not supplied. @type {string} */
    var NOT_KNOWN = 'NOT KNOWN';

    /**
     * Description: element with a class and optional text.
     * Inputs: doc, tag, cls, text. Output: Element.
     */
    function el(doc, tag, cls, text) {
        var n = doc.createElement(tag);
        if (cls) n.setAttribute('class', cls);
        if (text !== null && text !== undefined) n.textContent = String(text);
        return n;
    }

    /**
     * Description: the speaker's display name. Falls back to the raw
     *   role, then to an explicit unknown, never to a default party.
     * Inputs: turn (object).
     * Output: string.
     * Example: roleLabel({role: 'user'}) // -> 'You'
     */
    function roleLabel(turn) {
        var r = turn && turn.role;
        if (typeof r !== 'string' || r === '') return 'SPEAKER ' + NOT_KNOWN;
        return ROLE_LABELS[r] || r;
    }

    /**
     * Description: the small button that opens a panel. A real <button>
     *   with a real accessible name, never a clickable <span>: this is
     *   the affordance the whole feature hangs off and it has to be
     *   reachable by keyboard and by a screen reader.
     * Inputs: doc (Document), action (string), label (string),
     *         text (string), open (boolean), extra (object|null) - more
     *           attributes to set.
     * Output: Element.
     */
    function toggleButton(doc, action, label, text, open, extra) {
        var b = el(doc, 'button', ROOT_CLASS + '__toggle', text);
        b.setAttribute('type', 'button');
        b.setAttribute('data-action', action);
        b.setAttribute('aria-label', label);
        b.setAttribute('aria-expanded', open ? 'true' : 'false');
        b.setAttribute('data-open', open ? 'true' : 'false');
        if (extra) {
            var keys = Object.keys(extra);
            for (var i = 0; i < keys.length; i++) {
                b.setAttribute(keys[i], String(extra[keys[i]]));
            }
        }
        return b;
    }

    /**
     * Description: the header strip: who spoke, when, on what model, and
     *   the two affordances. Compact by design; the detail is one click
     *   below it.
     * Inputs: doc, turn (object), state (object) - {infoOpen, subOpen}.
     * Output: Element.
     */
    function renderHeader(doc, turn, state) {
        var head = el(doc, 'header', ROOT_CLASS + '__head', null);

        var who = el(doc, 'span', ROOT_CLASS + '__who', roleLabel(turn));
        who.setAttribute('data-role', String(turn.role));
        // A role the server INFERRED from the record type is marked, in
        // text, so nobody reads a derivation as a declaration. The full
        // explanation is one click away in the "i".
        if (turn.role_state && turn.role_state !== 'role') {
            who.setAttribute('data-role-state', String(turn.role_state));
            who.appendChild(doc.createTextNode(' (inferred)'));
        }
        head.appendChild(who);

        head.appendChild(el(doc, 'time', ROOT_CLASS + '__ts',
            turn.ts ? String(turn.ts) : NOT_KNOWN));

        // The model is shown inline only when there is one. An
        // "unknown model" chip on every user turn is noise, and a user
        // turn genuinely has no model rather than an unmeasured one.
        // The "i" panel states it either way, which is where the
        // question actually gets asked.
        if (turn.model) {
            head.appendChild(el(doc, 'span', ROOT_CLASS + '__model',
                String(turn.model)));
        }

        if (Number.isInteger(turn.secret_finding_count) &&
                turn.secret_finding_count > 0) {
            var s = el(doc, 'span', ROOT_CLASS + '__secrets',
                turn.secret_finding_count + ' flagged secret(s)');
            s.setAttribute('data-secrets', String(turn.secret_finding_count));
            head.appendChild(s);
        }

        // The "i". Its accessible name is a sentence, because "i" is not
        // one, and it is the control the brief named explicitly.
        head.appendChild(toggleButton(doc, ACTION_INFO,
            'Envelope detail for this message', 'i', !!state.infoOpen, null));

        var exp = window.ArchiveChatSubagents.expanderFor(turn);
        if (exp) {
            head.appendChild(toggleButton(doc, ACTION_SUBAGENTS,
                exp.label + ' spawned by this message', exp.label,
                !!state.subOpen, { 'data-subagent-state': exp.state }));
        }
        return head;
    }

    /**
     * Description: the blocks of one turn, in `seq` order.
     *
     *   A TURN WITH NO BLOCKS IS TWO DIFFERENT FINDINGS and they are
     *   rendered as two different sentences. An empty ARRAY is the
     *   server saying this turn carried no content blocks - a real,
     *   ordinary state for some record types. A MISSING array is the
     *   server not having told us, which is a could-not-evaluate.
     * Inputs: doc, turn (object).
     * Output: Element.
     */
    function renderBody(doc, turn) {
        var body = el(doc, 'div', ROOT_CLASS + '__body', null);
        var blocks = turn.blocks;

        // THE SERVER'S OWN WORD FOR WHERE THE BLOCKS CAME FROM, checked
        // BEFORE the array is trusted. Measured live 2026-09-01 the
        // three states are `extracted` (a real content array),
        // `content_string` (the content was a bare string, which is most
        // user turns) and `no_message_content` (it looked; there is
        // none). Any other value - including one invented after this
        // file was written - means this view cannot say what the empty
        // array in front of it represents, so it says that instead of
        // rendering "no content blocks" over an unevaluated lookup.
        var bs = turn.blocks_state;
        var bsKnown = bs === undefined || bs === null || bs === '' ||
            bs === 'extracted' || bs === 'content_string' ||
            bs === 'no_message_content';

        if (!Array.isArray(blocks) || !bsKnown) {
            body.setAttribute('data-blocks', 'cannot-determine');
            body.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock({
                result: null, result_status: 'cannot_determine',
                scope_status: 'resolved',
                unevaluated: [{
                    subject: 'blocks of line ' +
                        (turn.line_no !== undefined ? turn.line_no : '?'),
                    reason: (bsKnown
                        ? 'the server sent no content blocks array for this turn'
                        : 'the server reported blocks_state=' + String(bs) +
                          ', which this view cannot interpret') +
                        ', so what this turn said is NOT KNOWN. That is not ' +
                        'the same as it having said nothing.'
                }],
                meta: {}
            }, { document: doc, omitActions: true }));
            return body;
        }

        body.setAttribute('data-blocks', String(blocks.length));
        if (blocks.length === 0) {
            body.appendChild(el(doc, 'p', ROOT_CLASS + '__no-blocks',
                'No content blocks. The server looked' +
                (bs ? ' (' + String(bs) + ')' : '') +
                ' and this turn carries none.'));
            return body;
        }

        // Sorted on a COPY. `seq` is the server's declared order; rows
        // without one keep their received position rather than being
        // shuffled to the front by a NaN comparison.
        var ordered = blocks.slice().sort(function (a, b) {
            var x = (a && Number.isInteger(a.seq)) ? a.seq : 0;
            var y = (b && Number.isInteger(b.seq)) ? b.seq : 0;
            return x - y;
        });
        for (var i = 0; i < ordered.length; i++) {
            body.appendChild(window.ArchiveChatBlock.renderBlock(
                doc, ordered[i], turn, null));
        }
        return body;
    }

    /**
     * Description: the collapsed `progress` chip. Progress records are
     *   37.5 percent of every body in this corpus; rendering them as
     *   bubbles would bury the conversation in its own telemetry. The
     *   chip states the count and the line range, which is what somebody
     *   actually needs from them.
     * Inputs: doc, item (object) - {from, to, count}, expanded (boolean).
     * Output: Element.
     */
    function renderProgressRun(doc, item, expanded) {
        var root = el(doc, 'div', ROOT_CLASS + ' ' + ROOT_CLASS + '--progress', null);
        root.setAttribute('data-kind', 'progress-run');
        root.setAttribute('data-from', String(item.from));
        var n = Number.isFinite(item.count) ? item.count : null;
        var b = el(doc, 'button', ROOT_CLASS + '__progress-chip',
            (n === null ? 'progress records' : n + ' progress records') +
            ' (lines ' + item.from + ' to ' + item.to + ')' +
            (expanded ? ' - hide' : ' - show'));
        b.setAttribute('type', 'button');
        b.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        b.setAttribute('data-action',
            expanded ? ACTION_COLLAPSE_PROGRESS : ACTION_EXPAND_PROGRESS);
        root.appendChild(b);
        return root;
    }

    /**
     * Description: render one turn whole.
     * Inputs: doc (Document) - REQUIRED.
     *         turn (object) - the server's turn shape, or a
     *           {kind: 'progress-run'} item.
     *         state (object|null) - {index, infoOpen, subOpen,
     *           progressExpanded}. Held by the view, not by the row.
     * Output: Element - an <article data-index=... data-role=...>.
     * Example: renderTurn(doc, {role: 'user', blocks: []}, {index: 0})
     */
    function renderTurn(doc, turn, state) {
        if (!doc) throw new Error('ArchiveChatTurn.renderTurn needs a document');
        var s = state || {};
        var t = (turn && typeof turn === 'object') ? turn : {};

        if (t.kind === 'progress-run') {
            var chip = renderProgressRun(doc, t, !!s.progressExpanded);
            if (Number.isInteger(s.index)) {
                chip.setAttribute('data-index', String(s.index));
            }
            return chip;
        }

        var root = el(doc, 'article', ROOT_CLASS, null);
        root.setAttribute('data-role', typeof t.role === 'string' && t.role
            ? t.role : 'unknown');
        root.setAttribute('data-record-type', String(t.record_type));
        if (Number.isInteger(s.index)) root.setAttribute('data-index', String(s.index));
        if (t.line_no !== undefined && t.line_no !== null) {
            root.setAttribute('data-line-no', String(t.line_no));
        }
        if (t.body_id !== undefined && t.body_id !== null) {
            root.setAttribute('data-body-id', String(t.body_id));
        }

        root.appendChild(renderHeader(doc, t, s));

        if (s.infoOpen) {
            var panel = window.ArchiveChatInfo.renderInfo(doc, t);
            panel.setAttribute('data-panel', 'info');
            root.appendChild(panel);
        }

        root.appendChild(renderBody(doc, t));

        if (s.subOpen) {
            var subs = window.ArchiveChatSubagents.renderSubagents(doc, t);
            // null means known-and-empty, and a turn with no subagents
            // carries no expander, so this branch is unreachable through
            // the UI. It is guarded anyway: a caller that forces the
            // state open must not produce an appendChild(null) crash that
            // takes the whole conversation down.
            if (subs) {
                subs.setAttribute('data-panel', 'subagents');
                root.appendChild(subs);
            }
        }
        return root;
    }

    window.ArchiveChatTurn = {
        renderTurn: renderTurn,
        roleLabel: roleLabel,
        ROOT_CLASS: ROOT_CLASS,
        ROLE_LABELS: ROLE_LABELS,
        ACTION_INFO: ACTION_INFO,
        ACTION_SUBAGENTS: ACTION_SUBAGENTS,
        ACTION_EXPAND_PROGRESS: ACTION_EXPAND_PROGRESS,
        ACTION_COLLAPSE_PROGRESS: ACTION_COLLAPSE_PROGRESS
    };
    console.log('[ArchiveChatTurn Module] Exported as window.ArchiveChatTurn');
})();
