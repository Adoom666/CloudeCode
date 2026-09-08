/**
 * THE SUBAGENT LIST HANGING OFF ONE TURN, in the order they ran.
 *
 * "subagents should be listed in time order so we know which was 1st,
 * 2nd, 3rd." That sentence is the whole specification, and it contains a
 * trap: an ordinal is a CLAIM. Printing "1st" next to a row asserts that
 * this run started before the other two. The server knows on what basis
 * it ordered them and SAYS SO in `order_basis`, so this view renders
 * that word rather than inventing a confidence of its own - measured
 * live 2026-09-01, the two bases in this corpus are `start_ts` (a real
 * timestamp, so the ordinals are chronological) and `file_position` (the
 * order the spawns appear in the transcript, which is NOT the same
 * claim). A view that printed "1st, 2nd, 3rd" identically for both would
 * be upgrading a file offset into a clock.
 *
 * THREE ORDERING OUTCOMES, NEVER TWO:
 *   the server's own basis, named verbatim;
 *   this view's fallback sort by start time, labelled as DERIVED;
 *   and NOT KNOWN, where the ordinal column is left empty of any rank
 *   rather than filled with a plausible one.
 *
 * "NO SUBAGENTS", "SOME I COULD NOT IDENTIFY" AND "I COULD NOT LOOK" ARE
 * THREE DIFFERENT FINDINGS, and the server distinguishes all three, so
 * this view must not flatten them:
 *
 *   subagents_state 'none_spawned'    - it looked; this turn spawned none.
 *   subagents_state 'resolved'        - it looked and found these.
 *   subagents_state 'cannot_determine'- it could NOT look. Renders as a
 *                                       could-not-evaluate, never as
 *                                       "spawned nothing".
 *   per row, link_state != 'resolved' - THE RUN IS REAL AND UNIDENTIFIED.
 *                                       The server's own meta says so:
 *                                       "an unresolved entry means the
 *                                       run is real and unidentified,
 *                                       never that no run happened."
 *                                       So the row is LISTED, counted,
 *                                       and given a disabled control
 *                                       that names why it cannot open.
 *
 * Dropping any of those would report "this turn spawned nothing" for a
 * turn that spawned five, which is the exact false green this repo's
 * three-outcome rule exists to kill.
 *
 * NO LISTENERS. Drill-in is `data-action="open-subagent"` carrying the
 * ids on the element, resolved by archive-chat-view.js's one delegated
 * listener, because these rows are recycled on every paint.
 *
 * Depends on archive-outcome-view.js. Exports window.ArchiveChatSubagents.
 */

console.log('[ArchiveChatSubagents Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-subagents';

    /** @type {string} data-action that drills into one subagent. */
    var ACTION_OPEN = 'open-subagent';

    /** Ordering bases this VIEW can produce when the server names none.
     *  A server-supplied `order_basis` is passed through as itself. */
    var ORDER_DECLARED = 'declared';
    var ORDER_DERIVED = 'derived-from-start-time';
    var ORDER_UNKNOWN = 'cannot-determine';

    /** Lookup outcomes for a turn's subagent list. */
    var LOOKUP_KNOWN = 'known';
    var LOOKUP_FAILED = 'cannot-determine';

    /**
     * How each server-declared `order_basis` is explained to a person.
     * A basis NOT in this table is still rendered, under its own raw
     * name, prefixed so nobody reads an unfamiliar word as a guarantee.
     * @type {Object<string,string>}
     */
    var BASIS_PROSE = {
        start_ts: 'ordered by START TIME, as the server recorded it',
        file_position: 'ordered by POSITION IN THE TRANSCRIPT, which is ' +
            'the order the spawns were written, NOT a measured clock',
        declared: 'in the order the server declared they ran',
        'derived-from-start-time': 'sorted by START TIME by this view. The ' +
            'server declared no order, so these ordinals are derived ' +
            'rather than stated'
    };

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
     * Description: did the server actually look for this turn's
     *   subagents? Structural first, then the server's own word.
     *
     *   AN EMPTY ARRAY IS NOT EVIDENCE ON ITS OWN. A server that sends
     *   `[]` alongside `subagents_state: 'cannot_determine'` is telling
     *   us the array means nothing, and that state wins.
     * Inputs: turn (object|null).
     * Output: string - LOOKUP_KNOWN or LOOKUP_FAILED.
     * Example: lookupState({subagents: [], subagents_state: 'none_spawned'})
     *   // -> 'known'
     */
    function lookupState(turn) {
        if (!turn || typeof turn !== 'object') return LOOKUP_FAILED;
        var st = turn.subagents_state || turn.subagent_status;
        if (typeof st === 'string' && st !== '') {
            // Allow-list the states that mean "I looked". Anything else,
            // including a value invented after this file was written,
            // fails toward the third outcome rather than toward success.
            var ok = st === 'none_spawned' || st === 'resolved' ||
                st === 'ok' || st === 'partial_resolution';
            if (!ok) return LOOKUP_FAILED;
        }
        return Array.isArray(turn.subagents) ? LOOKUP_KNOWN : LOOKUP_FAILED;
    }

    /**
     * Description: the start time of one subagent row, whatever the
     *   server called it. Returns null when there is none; null is not
     *   zero, and a row with no time must not sort as the earliest.
     * Inputs: row (object).
     * Output: string|null - the raw timestamp, uninterpreted.
     */
    function startOf(row) {
        if (!row || typeof row !== 'object') return null;
        var v = row.start_ts || row.started_at || row.start_time || row.ts || null;
        return (typeof v === 'string' && v !== '') ? v : null;
    }

    /**
     * Description: the transcripts one spawn resolved to. Always an
     *   array, possibly empty. A spawn with none is a REAL RUN THAT WAS
     *   NOT IDENTIFIED, never a spawn that did not happen.
     * Inputs: row (object).
     * Output: Array<object>.
     */
    function transcriptsOf(row) {
        if (!row || typeof row !== 'object') return [];
        if (Array.isArray(row.transcripts)) return row.transcripts;
        // Tolerate a flat shape, so a server that later sends one id per
        // row does not silently render as unlinked.
        if (row.transcript_id !== undefined && row.transcript_id !== null) {
            return [{ transcript_id: row.transcript_id }];
        }
        return [];
    }

    /**
     * Description: put the rows in the order they ran, and say on what
     *   basis. The SERVER'S basis wins when it names one; this function
     *   only decides when it does not.
     * Inputs: rows (Array) - a turn's `subagents`.
     * Output: {rows: Array, basis: string} - `rows` is a new array; the
     *   input is not mutated, because it belongs to the caller's data.
     * Example: order([{order: 2, order_basis: 'start_ts'},
     *                 {order: 1, order_basis: 'start_ts'}]).basis
     *   // -> 'start_ts'
     */
    function order(rows) {
        var list = Array.isArray(rows) ? rows.slice() : [];
        if (list.length === 0) return { rows: list, basis: ORDER_DECLARED };

        var allDeclared = list.every(function (r) {
            return r && Number.isInteger(r.order) && r.order >= 1;
        });
        if (allDeclared) {
            list.sort(function (a, b) { return a.order - b.order; });
            // ONE basis, or none. Rows that disagree about how they were
            // ordered cannot be summarised by either of their answers.
            var bases = {};
            for (var i = 0; i < list.length; i++) {
                var b = list[i].order_basis;
                if (typeof b === 'string' && b !== '') bases[b] = true;
            }
            var names = Object.keys(bases);
            return { rows: list,
                basis: names.length === 1 ? names[0] : ORDER_DECLARED };
        }

        var allTimed = list.every(function (r) { return startOf(r) !== null; });
        if (allTimed) {
            // String comparison, deliberately: these are ISO-8601
            // timestamps and ISO-8601 sorts lexicographically in the
            // same order it sorts chronologically. Parsing them into
            // Date objects would introduce a timezone interpretation
            // this view has no business making.
            list.sort(function (a, b) {
                var x = startOf(a);
                var y = startOf(b);
                return x < y ? -1 : (x > y ? 1 : 0);
            });
            return { rows: list, basis: ORDER_DERIVED };
        }

        return { rows: list, basis: ORDER_UNKNOWN };
    }

    /**
     * Description: the ordinal shown against one row. A known ordering
     *   prints a rank; an unknown one prints NOT KNOWN in the same
     *   column, so the gap sits exactly where the claim would have been.
     * Inputs: basis (string), i (number) - zero-based position.
     * Output: string.
     */
    function ordinalFor(basis, i) {
        if (basis === ORDER_UNKNOWN) return 'NOT KNOWN';
        var n = i + 1;
        var mod100 = n % 100;
        if (mod100 >= 11 && mod100 <= 13) return n + 'th';
        var mod10 = n % 10;
        if (mod10 === 1) return n + 'st';
        if (mod10 === 2) return n + 'nd';
        if (mod10 === 3) return n + 'rd';
        return n + 'th';
    }

    /**
     * Description: the sentence above the list, naming the count AND the
     *   basis of the ordinals beside it.
     * Inputs: n (number), basis (string).
     * Output: string.
     */
    function basisProse(n, basis) {
        if (basis === ORDER_UNKNOWN) {
            return n + ' subagent(s). RUN ORDER NOT KNOWN: the server ' +
                'declared neither an order nor a start time for every one, ' +
                'so these are listed as received and the ordinals are ' +
                'withheld.';
        }
        var how = BASIS_PROSE[basis] ||
            ('ordered on a basis this view does not have a name for, which ' +
             'the server calls "' + basis + '"');
        return n + ' subagent(s), ' + how + '.';
    }

    /**
     * Description: a readable name for one spawn. Built from what the
     *   server actually sent, in descending order of usefulness, and
     *   never invented: a spawn with nothing identifying is called what
     *   it is.
     * Inputs: row (object), ts (Array) - transcriptsOf(row).
     * Output: string.
     */
    function nameFor(row, ts) {
        if (ts.length === 1 && ts[0].session_ref) return String(ts[0].session_ref);
        if (Array.isArray(row.agent_ids) && row.agent_ids.length) {
            return 'agent ' + row.agent_ids.join(', ');
        }
        var by = row.spawned_by;
        if (by && by.tool_name) {
            return String(by.tool_name) + ' spawn' +
                (by.line_no !== undefined && by.line_no !== null
                    ? ' at line ' + by.line_no : '');
        }
        return 'subagent';
    }

    /**
     * Description: the control that opens ONE resolved transcript, or
     *   the disabled one that explains why a real run cannot be opened.
     * Inputs: doc, row (object), t (object|null) - one transcript entry,
     *         basis (string), i (number) - zero-based position of the
     *           SPAWN, so two transcripts of one spawn share its ordinal.
     * Output: Element.
     */
    function renderOpen(doc, row, t, basis, i) {
        var btn = el(doc, 'button', ROOT_CLASS + '__open', null);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-action', ACTION_OPEN);

        var ord = el(doc, 'span', ROOT_CLASS + '__ordinal', ordinalFor(basis, i));
        ord.setAttribute('data-ordinal', basis === ORDER_UNKNOWN
            ? 'unknown' : String(i + 1));
        btn.appendChild(ord);

        var ts = transcriptsOf(row);
        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__name',
            t && t.session_ref ? String(t.session_ref) : nameFor(row, ts)));

        var st = (t && t.start_ts) || startOf(row);
        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__started',
            st ? 'started ' + st : 'started: NOT KNOWN'));

        if (t && t.transcript_id !== undefined && t.transcript_id !== null) {
            btn.setAttribute('data-openable', 'true');
            btn.setAttribute('data-transcript-id', String(t.transcript_id));
            btn.appendChild(el(doc, 'span', ROOT_CLASS + '__tid',
                Number.isFinite(t.line_count)
                    ? t.line_count + ' lines' : 'transcript ' + t.transcript_id));
        } else {
            // THE RUN HAPPENED. It could not be linked to a stored
            // transcript, which is a different fact from it not existing,
            // and the server's own word for why is rendered verbatim.
            btn.setAttribute('disabled', 'disabled');
            btn.setAttribute('data-openable', 'false');
            btn.setAttribute('data-link-state', String(row.link_state));
            var why = 'NOT OPENABLE: this run is real and was not linked to ' +
                'a stored transcript (' + String(row.link_state) + '). That ' +
                'is not the same as no run happening.';
            btn.setAttribute('title', why);
            btn.appendChild(el(doc, 'span', ROOT_CLASS + '__tid', why));
        }

        if (Array.isArray(row.agent_ids) && row.agent_ids.length) {
            btn.setAttribute('data-agent-id', String(row.agent_ids[0]));
        }
        return btn;
    }

    /**
     * Description: one spawn's row. A spawn that resolved to SEVERAL
     *   transcripts gets one control each, under one ordinal, because
     *   collapsing them would hide runs and picking one would be a
     *   choice nobody made.
     * Inputs: doc, row (object), basis (string), i (number).
     * Output: Element.
     */
    function renderRow(doc, row, basis, i) {
        var li = el(doc, 'li', ROOT_CLASS + '__row', null);
        var ts = transcriptsOf(row);
        if (ts.length === 0) {
            li.appendChild(renderOpen(doc, row, null, basis, i));
            return li;
        }
        for (var k = 0; k < ts.length; k++) {
            li.appendChild(renderOpen(doc, row, ts[k], basis, i));
        }
        if (ts.length > 1) {
            li.appendChild(el(doc, 'p', ROOT_CLASS + '__multi',
                'This one spawn resolved to ' + ts.length + ' transcripts. ' +
                'All of them are listed; none was picked for you.'));
        }
        return li;
    }

    /**
     * Description: the whole subagent section for one turn, or null when
     *   there is nothing to render at all.
     *
     *   RETURNS null ONLY for the known-and-empty case. That is the one
     *   state with no affordance, and it is the reason a turn that
     *   spawned nothing looks like an ordinary turn while a turn whose
     *   lookup failed carries a visible could-not-evaluate.
     * Inputs: doc (Document) - REQUIRED. turn (object|null).
     * Output: Element|null.
     * Example: renderSubagents(doc, {subagents: [],
     *   subagents_state: 'none_spawned'}) // -> null
     */
    function renderSubagents(doc, turn) {
        if (!doc) throw new Error('ArchiveChatSubagents.renderSubagents needs a document');
        var state = lookupState(turn);

        if (state === LOOKUP_FAILED) {
            var box = el(doc, 'div', ROOT_CLASS + ' ' + ROOT_CLASS + '--unknown', null);
            box.setAttribute('data-subagent-state', LOOKUP_FAILED);
            box.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock({
                result: null,
                result_status: 'cannot_determine',
                scope_status: 'resolved',
                unevaluated: [{
                    subject: 'subagents of line ' +
                        (turn && turn.line_no !== undefined ? turn.line_no : '?'),
                    reason: (turn && typeof turn.subagent_reason === 'string')
                        ? turn.subagent_reason
                        : 'the server reported subagents_state=' +
                          String(turn && (turn.subagents_state ||
                              turn.subagent_status)) +
                          ' for this turn, so whether it spawned any is NOT ' +
                          'KNOWN. This is not the same as it having spawned ' +
                          'none.'
                }],
                meta: {}
            }, { document: doc, omitActions: true }));
            return box;
        }

        var rows = turn.subagents;
        if (rows.length === 0) return null;

        var ordered = order(rows);
        var root = el(doc, 'div', ROOT_CLASS, null);
        root.setAttribute('data-subagent-state', LOOKUP_KNOWN);
        root.setAttribute('data-order-basis', ordered.basis);
        root.setAttribute('data-subagent-count', String(ordered.rows.length));
        root.appendChild(el(doc, 'p', ROOT_CLASS + '__basis',
            basisProse(ordered.rows.length, ordered.basis)));

        var ol = el(doc, 'ol', ROOT_CLASS + '__list', null);
        var unlinked = 0;
        for (var i = 0; i < ordered.rows.length; i++) {
            ol.appendChild(renderRow(doc, ordered.rows[i], ordered.basis, i));
            if (transcriptsOf(ordered.rows[i]).length === 0) unlinked++;
        }
        root.appendChild(ol);

        // COUNTED OUT LOUD, not left for the reader to notice by scanning
        // for disabled buttons. A list where two of five cannot be opened
        // is a partial answer and says so.
        if (unlinked > 0) {
            root.appendChild(el(doc, 'p', ROOT_CLASS + '__unlinked',
                unlinked + ' of these ' + ordered.rows.length + ' run(s) ' +
                'could not be linked to a stored transcript and cannot be ' +
                'opened. They still ran.'));
        }
        return root;
    }

    /**
     * Description: the label for the expander on a turn, or null when a
     *   turn should carry no expander. Kept here rather than in the turn
     *   renderer so the count and the list can never disagree.
     * Inputs: turn (object|null).
     * Output: {label: string, state: string}|null.
     * Example: expanderFor({subagents: [{}, {}]})
     *   // -> {label: '2 subagents', state: 'known'}
     */
    function expanderFor(turn) {
        var state = lookupState(turn);
        if (state === LOOKUP_FAILED) {
            return { label: 'Subagents: NOT KNOWN', state: LOOKUP_FAILED };
        }
        var n = turn.subagents.length;
        if (n === 0) return null;
        return {
            label: n === 1 ? '1 subagent' : n + ' subagents',
            state: LOOKUP_KNOWN
        };
    }

    window.ArchiveChatSubagents = {
        renderSubagents: renderSubagents,
        expanderFor: expanderFor,
        lookupState: lookupState,
        order: order,
        ordinalFor: ordinalFor,
        startOf: startOf,
        transcriptsOf: transcriptsOf,
        basisProse: basisProse,
        ROOT_CLASS: ROOT_CLASS,
        ACTION_OPEN: ACTION_OPEN,
        ORDER_DECLARED: ORDER_DECLARED,
        ORDER_DERIVED: ORDER_DERIVED,
        ORDER_UNKNOWN: ORDER_UNKNOWN,
        LOOKUP_KNOWN: LOOKUP_KNOWN,
        LOOKUP_FAILED: LOOKUP_FAILED,
        BASIS_PROSE: BASIS_PROSE
    };
    console.log('[ArchiveChatSubagents Module] Exported as window.ArchiveChatSubagents');
})();
