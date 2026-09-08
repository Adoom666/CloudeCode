/**
 * Archive reader FETCH OWNER.
 *
 * WHY THIS IS NOT PART OF `archive-reader.js`. That file renders: it
 * takes a spine, an outcome token and a header, and paints them. It does
 * no fetching at all, deliberately, which is what makes it testable
 * under a bare DOM with no network. Something still has to ask the server
 * for a transcript's header and its first page of lines, and that
 * something is here rather than in `archive-screen.js` only because the
 * composition root was over the repo's 500-line file cap with it inline.
 * The seam is honest: this is the one place that turns a transcript id
 * into the three inputs the reader needs.
 *
 * IT DOES NOT INTERPRET `result_status`. `ArchiveOutcome.classify()` is
 * the only interpreter (design doc B.3); this reads the TOKEN that
 * function returns, which is a different thing.
 */

console.log('[ArchiveScreenReader Module] Loading...');

(function () {
    'use strict';

    /**
     * Marks the notes this module inserts into the reader pane, so a
     * later load can remove its own leftovers without touching anything
     * else in the pane.
     * @type {string}
     */
    var NOTE_ATTR = 'data-archive-screen-note';

    /**
     * Marker value for the note the deep-link path leaves behind.
     * @type {string}
     */
    var NOTE_MARK_LINE_NOT_REACHED = 'line-not-reached';

    /**
     * Marker value for every note `loadMoreLines` leaves behind. It is
     * DELIBERATELY distinct from NOTE_MARK_LINE_NOT_REACHED so a forward
     * page's refusal can be cleared, and asserted on, without touching
     * the deep link's own note - two different questions were asked and
     * they get two different answers.
     * @type {string}
     */
    var NOTE_MARK_LOAD_MORE = 'load-more';

    /**
     * The outcome TOKEN returned when this module could not evaluate
     * something. HYPHENATED, and that is not a local choice: it is
     * `archive-outcome.js`'s `TOKENS` vocabulary, which every caller
     * switching on a token compares against. Spelled any other way it
     * would silently match no branch anywhere.
     * @type {string}
     */
    var TOKEN_CANNOT_DETERMINE = 'cannot-determine';

    /**
     * The `result_status` VALUE the SERVER uses on the wire, which is
     * UNDERSCORED. Deliberately not the same string as
     * TOKEN_CANNOT_DETERMINE: different alphabets for different layers.
     * `classify()` matches against its own `RESULT_STATUSES`, which holds
     * `cannot_determine`, so a synthesised envelope must use this
     * spelling or classify() calls it unrecognised and answers
     * `transport-error`.
     * @type {string}
     */
    var WIRE_STATUS_CANNOT_DETERMINE = 'cannot_determine';

    /**
     * The outcome token returned when the request never produced a body.
     * @type {string}
     */
    var TOKEN_TRANSPORT_ERROR = 'transport-error';

    /**
     * Distance from the last line the reader holds to the first line the
     * next page must start at. `start_line` is a LINE NUMBER and is
     * inclusive (src/core/archive_start_line.py: `start_line=7111`
     * returns line 7111 as the FIRST row), so the next window begins one
     * past the last row already held. Named rather than written as a bare
     * 1 so the inclusiveness is stated where the arithmetic happens.
     * @type {number}
     */
    var NEXT_LINE_STEP = 1;

    /**
     * In-flight forward-paging requests, keyed by transcript id. Exists
     * only to make a second `loadMoreLines` for the same transcript join
     * the first rather than start a second fetch and append its rows
     * twice. Entries are removed on settle, success and failure alike.
     * @type {Object<string, Promise<string>>}
     */
    var inFlight = {};

    /**
     * Description: remove notes a previous load left behind. A stale
     *   could-not-evaluate about a different transcript's line is worse
     *   than no note at all.
     * Inputs: pane (Element), mark (string|undefined) - when given, only
     *   notes carrying that marker value are removed; when omitted, every
     *   note this module has inserted is removed.
     * Output: void.
     * Example: clearNotes(pane, 'load-more')
     */
    function clearNotes(pane, mark) {
        var selector = (mark === undefined || mark === null)
            ? '[' + NOTE_ATTR + ']'
            : '[' + NOTE_ATTR + '="' + mark + '"]';
        var stale = pane.querySelectorAll(selector);
        for (var i = 0; i < stale.length; i++) {
            stale[i].parentNode.removeChild(stale[i]);
        }
    }

    /**
     * Description: put an outcome envelope into the pane as a note.
     *
     *   EVERY block this module shows goes through here, and here goes
     *   through `archive-outcome-view.js`. That file is the only one that
     *   decides what an outcome block looks like; a second hand-rolled
     *   block would be exactly the drift that rule exists to stop.
     * Inputs: ctx (object) - {pane}. envelope (object) - a real API
     *   envelope, either one the server sent or one synthesised here.
     *   mark (string) - the NOTE_ATTR value to tag it with.
     * Output: Element - the inserted note.
     */
    function insertOutcomeNote(ctx, envelope, mark) {
        var note = window.ArchiveOutcomeView.renderOutcomeBlock(
            envelope, { document: ctx.pane.ownerDocument });
        note.setAttribute(NOTE_ATTR, mark);
        ctx.pane.insertBefore(note, ctx.pane.firstChild);
        return note;
    }

    /**
     * Description: synthesise a CLIENT-SIDE could-not-evaluate and show
     *   it. The envelope is real, not a special case: it carries the same
     *   fields the server would send, so the view cannot tell the
     *   difference and no second rendering path exists.
     * Inputs: ctx (object) - {pane}. subject (string) - what could not be
     *   evaluated. reason (string) - why, in words a person can act on.
     *   mark (string) - the NOTE_ATTR value.
     * Output: Element - the inserted note.
     * Example: insertCannotDetermine(ctx, 'transcript:5767',
     *              'the reader holds no rows', 'load-more')
     */
    function insertCannotDetermine(ctx, subject, reason, mark) {
        return insertOutcomeNote(ctx, {
            result: null,
            result_status: WIRE_STATUS_CANNOT_DETERMINE,
            scope_status: 'resolved',
            unevaluated: [{ subject: subject, reason: reason }],
            meta: {}
        }, mark);
    }

    /**
     * Description: find which laid-out item holds a line number.
     *
     *   TWO SHAPES, AND THE OLD CODE KNEW NEITHER. `reader.items()` is
     *   `archive-line-render.js::groupRows(spine)`, which returns the
     *   ORIGINAL ROW OBJECTS for ordinary lines and a
     *   `{kind:'progress-run', from, to, count, rows}` wrapper for a run
     *   of consecutive `record_type === 'progress'` lines. There is no
     *   `.row` property anywhere in that list. The previous
     *   implementation searched `items[i].row.line_no`, which is
     *   `undefined` for EVERY item, so it could never match anything -
     *   a lookup that always failed, hidden because until `start_line`
     *   shipped the deep link never reached a page where the answer
     *   would have been yes.
     *
     *   Both shapes are handled here, and the run case matters on real
     *   data: transcript 5767's line 7,111 sits inside the run
     *   7110..7123, so even a correct plain-row search would miss it.
     * Inputs: items (Array) - from reader.items(), lineNo (number).
     * Output: {item: number, inRun: boolean} | null when not present.
     * Example: indexOfLine(reader.items(), 7111) // -> {item: 9, inRun: true}
     */
    function indexOfLine(items, lineNo) {
        for (var i = 0; i < items.length; i++) {
            var it = items[i];
            if (!it) continue;
            if (it.kind !== 'progress-run' && it.line_no === lineNo) {
                return { item: i, inRun: false };
            }
            if (it.kind === 'progress-run' && Array.isArray(it.rows)) {
                for (var j = 0; j < it.rows.length; j++) {
                    if (it.rows[j] && it.rows[j].line_no === lineNo) {
                        return { item: i, inRun: true };
                    }
                }
            }
        }
        return null;
    }

    /**
     * Description: put a deep-linked line on screen.
     *
     *   THE ENDPOINT NOW TAKES A LINE OFFSET, so the normal path is that
     *   the server was ASKED for the page beginning at this line and the
     *   line is the first row. `start_line` shipped 2026-08-31; before it
     *   the only page reachable was the first one and this function's
     *   fallback fired for every deep link past it.
     *
     *   THE FALLBACK IS STILL HERE AND IS STILL A cannot_determine,
     *   because "the server was asked for line N and line N is not in
     *   what came back" is a state nobody has measured and it must not
     *   render as "the reader is showing line N". The two reasons that
     *   can produce it are named rather than merged: the request may not
     *   have carried the offset at all (an older build, or a caller that
     *   passed rows from a different fetch), or it carried it and the
     *   rows disagree. Out-of-range and start_line-plus-cursor never
     *   reach here at all - the SERVER answers those, 404 and 400, with
     *   its own named envelope, which is strictly better than a
     *   client-side guess.
     * Inputs: ctx (object) - {reader, pane, transcriptId}.
     *         lineNo (number), rows (Array) - the spine page loaded,
     *         offsetRequested (boolean) - whether this page was fetched
     *         with start_line set to lineNo.
     * Output: boolean - true when the line was found and scrolled to.
     */
    function scrollToLine(ctx, lineNo, rows, offsetRequested) {
        var index = indexOfLine(ctx.reader.items(), lineNo);
        if (index !== null) {
            // A line inside a COLLAPSED progress run is in the spine and
            // is not on screen. Scrolling to the run without expanding it
            // would put the reader in the right place and show the person
            // a collapsed block instead of the line they asked for - a
            // landing that measures as a success and is not one.
            if (index.inRun) {
                ctx.reader.setProgressExpanded(index.item, true);
                var again = indexOfLine(ctx.reader.items(), lineNo);
                if (again !== null) index = again;
            }
            var root = ctx.reader.root();
            var scroller = root && root.querySelector('.archive-reader__scroller');
            if (scroller) scroller.scrollTop = ctx.reader.list.offsetOf(index.item);
            ctx.reader.schedule();
            ctx.pane.setAttribute('data-highlight-line', String(lineNo));
            return true;
        }
        ctx.pane.removeAttribute('data-highlight-line');
        // A CLIENT-SIDE could-not-evaluate, synthesised as a real
        // envelope and handed to archive-outcome-view.js rather than
        // hand-built here.
        insertCannotDetermine(
            ctx,
            'transcript:' + ctx.transcriptId + ' line:' + lineNo,
            'line ' + lineNo + ' is not in the ' +
                (rows ? rows.length : 0) + ' rows that were loaded. ' +
                (offsetRequested === true
                    ? 'The page WAS requested with start_line=' + lineNo +
                      ', so the server and these rows disagree and neither ' +
                      'has been shown to be right here.'
                    : 'This page was NOT requested with a start_line, so ' +
                      'the line was never asked for.') +
                ' The reader is NOT showing line ' + lineNo + ' and is NOT ' +
                'asserting that the line does not exist.',
            NOTE_MARK_LINE_NOT_REACHED);
        return false;
    }

    /**
     * Description: fetch a transcript's header and first spine page and
     *   hand both to the reader.
     *
     *   THREE OUTCOMES, EXPLICITLY, TWICE. A transport failure and an
     *   envelope the server could not evaluate are different from an
     *   empty transcript, and each reaches the reader as its own token
     *   rather than as an empty spine. And `has_more` is itself
     *   true|false|null: only an explicit `false` proves the spine is
     *   complete, so `null` renders the reader's incomplete-spine
     *   sentinel rather than an "end of transcript" nobody measured.
     * Inputs: ctx (object) - {reader, pane, api, transcriptId,
     *   spinePageRows}. lineNo (number|null).
     * Output: Promise<string> - the outcome token for the spine request.
     * Example: load({reader, pane, api: window.API, transcriptId: 5767,
     *                spinePageRows: 500}, null)  // -> 'ok'
     */
    function load(ctx, lineNo) {
        var reader = ctx.reader;
        clearNotes(ctx.pane);
        reader.setToken('loading', null);

        // A deep link asks the SERVER for the window it wants. Before
        // start_line existed this fetched page one and then reported it
        // could not reach the line, which was honest and useless.
        var wantsLine = (lineNo !== null && lineNo !== undefined);
        var lineOpts = { limit: ctx.spinePageRows };
        if (wantsLine) lineOpts.startLine = lineNo;

        return Promise.all([
            ctx.api.getArchiveTranscript(ctx.transcriptId),
            ctx.api.listArchiveLines(ctx.transcriptId, lineOpts)
        ]).then(function (both) {
            var head = both[0];
            var page = both[1];

            if (head.transportError) {
                reader.setHeader(null);
            } else {
                var headTok = window.ArchiveOutcome.classify(head.envelope);
                reader.setHeader(window.ArchiveOutcome.isRenderable(headTok.token)
                    ? head.envelope.result : null);
            }

            if (page.transportError) {
                reader.setToken('transport-error', null);
                return 'transport-error';
            }
            var classified = window.ArchiveOutcome.classify(page.envelope);
            if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                reader.setToken(classified.token, page.envelope);
                return classified.token;
            }
            var rows = page.envelope.result || [];
            // A spine fetched with start_line is a WINDOW. Even when the
            // server says has_more is false, every line before the
            // offset is missing, so this spine is not complete and must
            // not render the reader's end-of-transcript state. Claiming
            // otherwise would be a false "you have seen it all" produced
            // by the deep link itself.
            var complete = window.ArchiveOutcome.hasMore(page.envelope) === false
                && !wantsLine;
            reader.setSpine(rows, complete);
            reader.setToken(classified.token, page.envelope);
            if (wantsLine) {
                scrollToLine(ctx, lineNo, rows, true);
            }
            return classified.token;
        });
    }

    /**
     * Description: decide where the next forward page starts, from what
     *   the reader already holds.
     * Inputs: reader (object) - must expose spine().
     * Output: {next: number} when a position exists, or {reason: string}
     *   naming precisely why there is none. Never a number and a reason.
     * Example: nextStartLine(reader) // -> {next: 500}
     */
    function nextStartLine(reader) {
        if (typeof reader.spine !== 'function') {
            return { reason: 'the reader does not expose spine(), so there ' +
                'is no way to read the position this page would continue from' };
        }
        var rows = reader.spine();
        if (!Array.isArray(rows) || rows.length === 0) {
            return { reason: 'the reader holds no rows, so there is no ' +
                'position to page from' };
        }
        var last = rows[rows.length - 1];
        var lineNo = last ? last.line_no : undefined;
        if (typeof lineNo !== 'number' || !isFinite(lineNo)) {
            return { reason: 'the last row the reader holds carries no finite ' +
                'line_no (' + String(lineNo) + '), so the line number this ' +
                'page would continue from cannot be read' };
        }
        return { next: lineNo + NEXT_LINE_STEP };
    }

    /**
     * Description: fetch the NEXT window of spine rows and append it.
     *
     *   WHAT THIS FIXES. The reader loaded one page and had no forward
     *   paging at all, so transcript 5767's 30,805 lines were a 500-line
     *   transcript as far as anyone could see. `start_line` shipped on
     *   the server 2026-08-31 and until now only the deep link used it.
     *
     *   THREE OUTCOMES, ALL THROUGH THE EXISTING MACHINERY. A transport
     *   failure, an envelope the server itself named a refusal, and a
     *   renderable page are three different answers. The server's own
     *   named refusal - `past_last_line` for a start_line past the end,
     *   for instance - is rendered AS THE SERVER SENT IT, because a
     *   measurement the server made beats anything guessed here. The
     *   fourth case, a renderable `ok` carrying zero rows, is a state
     *   nobody measured: it is neither a success to swallow nor an end of
     *   transcript to claim, so it is named as a could-not-evaluate.
     *
     *   IT NEVER SENDS A CURSOR. `start_line` and `cursor` together is a
     *   client error the server refuses by name under subject
     *   `start_line` (HTTP 400). Only `limit` and `startLine` go out.
     *
     *   IT NEVER TOUCHES A BODY, AND THAT IS A SECURITY PROPERTY, not an
     *   accident of scope. Only raw spine rows are appended; bodies are
     *   fetched later by the reader's own cache path, which is what runs
     *   them through `archive-mask.js`. This function therefore cannot
     *   bypass secret masking. Any future change here that fetches a body
     *   directly, or that renders text off one, would be a
     *   credential-disclosure path and must not be made in this file.
     * Inputs: ctx (object) - {reader, pane, api, transcriptId,
     *   spinePageRows}, the same shape load() takes.
     * Output: Promise<string> - an outcome token. Concurrent calls for
     *   the same transcript receive the SAME promise.
     * Example: loadMoreLines({reader, pane, api: window.API,
     *              transcriptId: 5767, spinePageRows: 500})  // -> 'ok'
     */
    function loadMoreLines(ctx) {
        var key = String(ctx.transcriptId);
        // REENTRANCY. A second call while one is in flight joins the
        // first. Starting a second fetch would append the same window
        // twice, and a duplicated spine is not visibly wrong.
        if (Object.prototype.hasOwnProperty.call(inFlight, key)) {
            return inFlight[key];
        }

        // Only this module's own notes are cleared, so a stale refusal
        // about a previous page cannot sit above a page that worked,
        // while the deep link's note is left alone.
        clearNotes(ctx.pane, NOTE_MARK_LOAD_MORE);

        var subject = 'transcript:' + ctx.transcriptId + ' load-more';
        var start = nextStartLine(ctx.reader);
        if (start.reason !== undefined) {
            // COULD NOT EVALUATE, and no network call. Asking the server
            // for a window we cannot name would be a guess.
            insertCannotDetermine(ctx, subject, start.reason +
                '. No request was sent and no claim is being made about ' +
                'whether more lines exist.', NOTE_MARK_LOAD_MORE);
            return Promise.resolve(TOKEN_CANNOT_DETERMINE);
        }
        var next = start.next;

        var request = ctx.api.listArchiveLines(ctx.transcriptId, {
            limit: ctx.spinePageRows,
            startLine: next
        }).then(function (page) {
            if (page.transportError) {
                insertCannotDetermine(ctx, subject,
                    'the request for the page beginning at line ' + next +
                    ' produced no response body, so whether more lines ' +
                    'exist was not measured.', NOTE_MARK_LOAD_MORE);
                return TOKEN_TRANSPORT_ERROR;
            }
            var classified = window.ArchiveOutcome.classify(page.envelope);
            if (!window.ArchiveOutcome.isRenderable(classified.token)) {
                // The SERVER's envelope, unaltered. This is the path that
                // carries its named out-of-range answer; swallowing it and
                // substituting a client-side guess would throw away the
                // one measurement that was actually taken.
                insertOutcomeNote(ctx, page.envelope, NOTE_MARK_LOAD_MORE);
                return classified.token;
            }
            var rows = page.envelope.result || [];
            if (rows.length === 0) {
                insertCannotDetermine(ctx, subject,
                    'the server answered ' + classified.token + ' for the ' +
                    'page beginning at line ' + next + ' and returned zero ' +
                    'rows without refusing. That is neither an end of ' +
                    'transcript nor a page, and nothing here measured which ' +
                    'it is.', NOTE_MARK_LOAD_MORE);
                return TOKEN_CANNOT_DETERMINE;
            }
            // ONLY an explicit boolean false proves the spine is complete.
            // `null` means the server did not answer has_more and must not
            // render an end-of-transcript state.
            //
            // NOTE THE ASYMMETRY WITH load(), WHICH LOOKS LIKE A BUG AND IS
            // NOT. load() forces `complete` to false whenever a start_line
            // was used, because a DEEP LINK jumps into the middle of a
            // transcript and every line BEFORE the offset is missing - so
            // even a truthful `has_more: false` leaves an incomplete spine.
            // Here the offset is the line immediately after the end of a
            // CONTIGUOUS spine the reader already holds, so nothing before
            // it is missing, and `has_more === false` genuinely does mean
            // the whole transcript is now loaded.
            var complete = window.ArchiveOutcome.hasMore(page.envelope) === false;
            ctx.reader.appendSpine(rows, complete);
            return classified.token;
        });

        // Clear the in-flight entry on SETTLE, both ways. A failure that
        // left the entry behind would wedge this transcript's paging for
        // the life of the page, and the rethrow keeps the rejection
        // visible rather than swallowing it here.
        var tracked = request.then(function (token) {
            delete inFlight[key];
            return token;
        }, function (err) {
            delete inFlight[key];
            throw err;
        });
        inFlight[key] = tracked;
        return tracked;
    }

    window.ArchiveScreenReader = {
        load: load,
        loadMoreLines: loadMoreLines,
        scrollToLine: scrollToLine,
        clearNotes: clearNotes,
        NOTE_ATTR: NOTE_ATTR,
        NOTE_MARK_LINE_NOT_REACHED: NOTE_MARK_LINE_NOT_REACHED,
        NOTE_MARK_LOAD_MORE: NOTE_MARK_LOAD_MORE
    };
    console.log('[ArchiveScreenReader Module] Exported as window.ArchiveScreenReader');
})();
