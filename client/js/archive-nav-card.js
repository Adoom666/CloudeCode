/**
 * THE PROJECT CARD - one project rendered as a bordered card, with the
 * two counts that project actually has and an affordance that opens the
 * rest of the detail in a modal.
 *
 * WHY THIS IS A SEPARATE FILE FROM archive-nav-row.js. That file is the
 * rail's VOCABULARY - the node kinds, the labels, the NOT KNOWN literal,
 * the refusal renderer - and it was at 387 lines against this repo's
 * 500-line cap. Everything here is about ONE kind of node, the project,
 * and it is the only kind that is a card. archive-nav-row.js still owns
 * host, corpus and unattributed rows and still owns every primitive this
 * file builds on; it delegates the project kind here and nothing else.
 *
 * ------------------------------------------------------------------
 * THE TWO COUNTS ARE NOT TWO NUMBERS. THEY ARE ONE SENTENCE.
 * ------------------------------------------------------------------
 * The rail used to render `transcript_count` and call it the project's
 * size. It is not: 19,588 of 21,039 transcripts corpus-wide (93.1%,
 * measured 2026-08-31) are AGENT SIDECHAIN files written by subagents,
 * not conversations anybody had. So a card read "718" while the middle
 * column - which defaults to `session_ref_scheme = uuid`, "My sessions"
 * - showed 27 for the same project. Two numbers that disagreed, side by
 * side, with nothing on screen explaining why.
 *
 * The owner's instruction was explicit: "so sessions i agree, we should
 * find an eloquent way to show both, but in reality its my sessions i
 * care about." So the card renders ONE SENTENCE, not two figures:
 *
 *     27 sessions   of 718 transcripts
 *
 * The word `of` is doing the whole job. It makes the total read as the
 * SET THE SESSIONS ARE DRAWN FROM rather than as a competing measure of
 * the same thing, which is the exact confusion two bare numbers create.
 * The sessions half is accent-coloured, larger and first; the total is
 * muted, smaller and second. THE TWO ARE NEVER DISTINGUISHED BY COLOUR
 * ALONE - they carry different NOUNS, and the connective sits between
 * them - because three themes zero every radius token and a fourth may
 * flatten the accent.
 *
 * ------------------------------------------------------------------
 * THE SESSION COUNT IS NOT AVAILABLE FROM THE SERVER TODAY, AND THE
 * CARD SAYS SO RATHER THAN SUBSTITUTING THE ONE THAT IS.
 * ------------------------------------------------------------------
 * Measured 2026-09-02 against the live dev instance: GET
 * /api/v1/archive/projects returns 77 nodes whose keys are exactly
 * display_name, full_path, host_count, hosts, members, observed_cwd,
 * project_id and transcript_count. There is NO per-project session
 * count anywhere in that payload or its meta.
 *
 * The number exists per-project - GET
 * /archive/projects/<id>/transcripts?session_ref_scheme=uuid&limit=1
 * answers meta.filters.matched_in_scope - but reaching it means 77
 * requests to paint one rail, which is an N+1 on the first paint of the
 * only way into the archive. It is not done, and the total is NOT shown
 * in its place under a `sessions` label, because a substituted number
 * is a verdict nobody measured. Until the merged endpoint carries the
 * field, the sessions half renders NOT KNOWN and names why in its
 * tooltip. See SESSION_COUNT_FIELDS for the exact field this consumes
 * the moment it appears.
 *
 * THREE OUTCOMES, NOT TWO, on that count: measured, never reported, and
 * reported-as-unmeasurable are three different findings. The last two
 * both render NOT KNOWN - a person cannot act differently on them - but
 * they carry different `data-session-state` values and different
 * tooltips, so the distinction survives into anything that inspects it.
 *
 * Exports window.ArchiveNavCard.
 */

console.log('[ArchiveNavCard Module] Loading...');

(function () {
    'use strict';

    var ROW = window.ArchiveNavRow;
    if (!ROW) {
        console.error('[ArchiveNavCard] MISSING DEPENDENCY: window.ArchiveNavRow. ' +
            'Load client/js/archive-nav-row.js BEFORE this file.');
    }

    /** Root element class, shared with the rest of the rail. @type {string} */
    var ROOT_CLASS = ROW ? ROW.ROOT_CLASS : 'archive-nav';

    /**
     * The field names a merged project node may carry its OWN-SESSION
     * count under, strongest first. This is the ONE place a server field
     * name for that count lands, so wiring the server up is an edit to
     * this array and to nothing else.
     *
     * `session_count` is the canonical name requested of the server.
     * `session_transcript_count` is accepted as an alias only because it
     * is the name that follows the existing `transcript_count` /
     * `unattributed_transcript_count` convention, and guessing wrong
     * between the two would otherwise cost a rewrite rather than a
     * one-line change.
     * @type {Array<string>}
     */
    var SESSION_COUNT_FIELDS = ['session_count', 'session_transcript_count'];

    /**
     * The field a server sets FALSE to say it tried and could not count.
     * Mirrors `counted` on the unattributed rows, where the same
     * distinction already exists.
     * @type {string}
     */
    var SESSION_COUNTED_FIELD = 'session_counted';

    /**
     * The three states a session count can be in. Rendered identically
     * for the middle two (a person cannot act differently on them) but
     * kept apart in the DOM and in the tooltip, because "nobody asked"
     * and "I asked and could not tell" are different findings.
     * @type {Object<string,string>}
     */
    var SESSION_STATES = {
        KNOWN: 'known',
        NOT_REPORTED: 'not-reported',
        CANNOT_DETERMINE: 'cannot-determine'
    };

    /** What each non-known state says in the card's tooltip. */
    var SESSION_REASONS = {};
    SESSION_REASONS[SESSION_STATES.NOT_REPORTED] =
        'The server did not report a session count for this project. ' +
        'The total beside it counts EVERY transcript, including agent ' +
        'sidechains, so it is not a stand-in for this number.';
    SESSION_REASONS[SESSION_STATES.CANNOT_DETERMINE] =
        'The server reported that it could not count the sessions in ' +
        'this project. That is not the same as there being none.';

    /**
     * Description: resolve a project's own-session count into one of
     *   three outcomes. Pure, and the only reader of the server's field
     *   names for this count.
     * Inputs: row (object|null) - a merged project node.
     * Output: {state: string, value: number|null} - `value` is a number
     *   ONLY in the `known` state; it is null in both others so a caller
     *   cannot accidentally render a substitute.
     * Example: sessionCountFor({session_count: 27})
     *   // -> {state: 'known', value: 27}
     * Example: sessionCountFor({transcript_count: 718})
     *   // -> {state: 'not-reported', value: null}
     */
    function sessionCountFor(row) {
        var r = row || {};
        if (r[SESSION_COUNTED_FIELD] === false) {
            return { state: SESSION_STATES.CANNOT_DETERMINE, value: null };
        }
        for (var i = 0; i < SESSION_COUNT_FIELDS.length; i++) {
            var v = r[SESSION_COUNT_FIELDS[i]];
            if (typeof v === 'number' && isFinite(v)) {
                return { state: SESSION_STATES.KNOWN, value: v };
            }
            // A field that is PRESENT and explicitly null is the server
            // saying it has the slot and no value for it. That is a
            // could-not-determine, not an absence - and it must not fall
            // through to the next alias, which would let a stale second
            // field answer for a first one that said "I do not know".
            if (v === null) {
                return { state: SESSION_STATES.CANNOT_DETERMINE, value: null };
            }
        }
        return { state: SESSION_STATES.NOT_REPORTED, value: null };
    }

    /**
     * Description: the presentation facts a project card renders. Pure.
     *
     *   THE OVERLAY ARRIVES ON THE ROW, not from a client-side store.
     *   `GET /api/v1/archive/overlay/projects` (src/api/archive_overlay_routes.py,
     *   landed 2026-09-02) returns the same 77 merged nodes with
     *   `display_name` ALREADY overridden, the archive's own derived name
     *   preserved beside it as `archive_display_name`, and an `overlay`
     *   block carrying {status, group, hidden, applied}. Nothing here
     *   invents that storage and nothing here writes it.
     *
     *   `status` HAS THREE VALUES AND THE THIRD IS NOT A FLAVOUR OF THE
     *   FIRST. 'none' means the owner has said nothing; 'applied' means a
     *   row was found; 'cannot_determine' means the project is not
     *   addressable, so nothing CAN be said about it. The server's own
     *   contract note is explicit that a client must not infer 'none'
     *   from an unchanged name, because renaming a project to its own
     *   folder name is a thing a person may do - so `renamed` is read off
     *   `applied`, never off a string comparison.
     *
     *   A row with NO overlay block at all is the third case again, one
     *   level up: this build is talking to `/archive/projects`, which
     *   does not carry one. That is `absent`, not 'none'.
     * Inputs: row (object) - the project node.
     *         overlay (object|null) - a client-side fallback, kept for
     *           tests and for a build with no overlay endpoint. Consulted
     *           ONLY when the row carries no overlay block.
     * Output: {name, serverName, group, hidden, renamed, overlayStatus}.
     */
    function presentationFor(row, overlay) {
        var r = row || {};
        var block = (r.overlay && typeof r.overlay === 'object' &&
                     !Array.isArray(r.overlay)) ? r.overlay : null;
        var archiveName = (typeof r.archive_display_name === 'string' &&
                           r.archive_display_name)
            ? r.archive_display_name
            : ROW.labelFor(ROW.NODE_KINDS.PROJECT, r);

        if (block) {
            var applied = Array.isArray(block.applied) ? block.applied : [];
            return {
                name: ROW.labelFor(ROW.NODE_KINDS.PROJECT, r),
                serverName: archiveName,
                group: (typeof block.group === 'string' && block.group) ? block.group : null,
                hidden: block.hidden === true,
                renamed: applied.indexOf('display_name') !== -1,
                overlayStatus: String(block.status || 'cannot_determine')
            };
        }

        var patch = null;
        if (overlay && typeof overlay.for === 'function') {
            patch = overlay.for(r);
        } else if (overlay && typeof overlay === 'object') {
            patch = overlay[String(r.project_id)] || null;
        }
        patch = patch || {};
        var name = (typeof patch.display_name === 'string' && patch.display_name)
            ? patch.display_name : archiveName;
        return {
            name: name,
            serverName: archiveName,
            group: (typeof patch.group === 'string' && patch.group) ? patch.group : null,
            hidden: patch.hidden === true,
            renamed: name !== archiveName,
            overlayStatus: 'absent'
        };
    }

    /**
     * Description: render the counts block - the sessions figure and,
     *   after it, the total it is drawn from.
     * Inputs: doc (Document), row (object).
     * Output: Element - a <span> carrying both halves.
     */
    function renderCounts(doc, row) {
        var el = ROW.el;
        var session = sessionCountFor(row);
        var total = ROW.countFor(ROW.NODE_KINDS.PROJECT, row);

        var wrap = el(doc, 'span', ROOT_CLASS + '__counts', null);

        // ---- primary: the sessions the owner actually cares about ----
        var primary = el(doc, 'span',
            ROOT_CLASS + '__count ' + ROOT_CLASS + '__count--sessions', null);
        primary.setAttribute('data-count', 'sessions');
        primary.setAttribute('data-session-state', session.state);
        primary.appendChild(el(doc, 'span', ROOT_CLASS + '__count-value',
            session.state === SESSION_STATES.KNOWN
                ? ROW.renderCount(session.value)
                : ROW.NOT_KNOWN));
        primary.appendChild(el(doc, 'span', ROOT_CLASS + '__count-noun', 'sessions'));
        wrap.appendChild(primary);

        // ---- secondary: the set those sessions come out of ----------
        // `of` is a separate element rather than part of the number so
        // it can be styled down without touching the figure, and so a
        // test can assert the connective is present. It is the whole
        // reason these read as one sentence instead of two rival counts.
        var secondary = el(doc, 'span',
            ROOT_CLASS + '__count ' + ROOT_CLASS + '__count--total', null);
        secondary.setAttribute('data-count', 'transcripts');
        secondary.appendChild(el(doc, 'span', ROOT_CLASS + '__count-of', 'of'));
        secondary.appendChild(el(doc, 'span', ROOT_CLASS + '__count-value',
            ROW.renderCount(total)));
        // `total`, not `transcripts`, and the choice is a measurement.
        // The rail gives the counts line about 198px. "NOT KNOWN sessions
        // of 718 transcripts" needs ~207px and ellipsised the noun away
        // on every one of the 77 cards, so the word that named what the
        // total counts was never actually readable. "of 718 total" fits,
        // and it states the SUBSET relation more plainly than the longer
        // word did. Nothing is lost: this element's own title spells out
        // "transcripts", says they include agent sidechains, and the info
        // modal carries the full "All transcripts" row.
        secondary.appendChild(el(doc, 'span', ROOT_CLASS + '__count-noun', 'total'));
        wrap.appendChild(secondary);

        wrap.setAttribute('title', countsTitle(session, total));
        return wrap;
    }

    /**
     * Description: the sentence the counts block carries on hover, which
     *   is the only place there is room to say what the two numbers are.
     *   Pure, and exported so a test can assert the wording without a
     *   DOM.
     * Inputs: session ({state, value}), total (number|null).
     * Output: string.
     */
    function countsTitle(session, total) {
        var head;
        if (session.state === SESSION_STATES.KNOWN) {
            head = ROW.renderCount(session.value) + ' top-level sessions ' +
                   '(session_ref_scheme = uuid), out of ' +
                   ROW.renderCount(total) + ' transcripts in total.';
        } else {
            head = 'Sessions: ' + ROW.NOT_KNOWN + '. ' +
                   SESSION_REASONS[session.state] + ' Total transcripts: ' +
                   ROW.renderCount(total) + '.';
        }
        return head + '\nThe total includes agent sidechain files, which are ' +
               'about 93 percent of this archive and are not conversations ' +
               'anybody had.';
    }

    /**
     * Description: render one project as a card. Same contract as
     *   ArchiveNavRow.renderRow - a <li> the rail can append - so the
     *   caller does not branch on kind.
     * Inputs: doc (Document), row (object), opts (object) -
     *   {onActivate, onInfo, positions, matchField, overlay}.
     * Output: Element - the <li>.
     */
    function renderCard(doc, row, opts) {
        var el = ROW.el;
        var options = opts || {};
        var kind = ROW.NODE_KINDS.PROJECT;
        var id = ROW.idFor(kind, row);
        var pres = presentationFor(row, options.overlay);

        var li = el(doc, 'li', ROOT_CLASS + '__node ' + ROOT_CLASS + '__node--' + kind, null);
        li.setAttribute('data-node-kind', kind);
        li.setAttribute('data-node-id', String(id === null || id === undefined ? '' : id));
        // The seam Task 4 asks for, made visible in the DOM rather than
        // only in a closure: a group renders as a data attribute today
        // and can grow a heading later without the card changing shape,
        // and a hidden project is MARKED, never dropped, because a card
        // that vanishes and a card that was never built look identical.
        if (pres.group) li.setAttribute('data-project-group', pres.group);
        if (pres.hidden) li.setAttribute('data-project-hidden', 'true');
        if (pres.renamed) li.setAttribute('data-project-renamed', 'true');

        var card = el(doc, 'div', ROOT_CLASS + '__card', null);

        var btn = el(doc, 'button', ROOT_CLASS + '__row ' + ROOT_CLASS + '__card-main', null);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-action', 'select');

        // TRUNCATE, NEVER WRAP - the rail is resizable and 272px at its
        // narrowest, and a wrapped name pushes 76 siblings down. The full
        // text is in `title` on the button, and the modal behind the info
        // affordance carries the path the face has no room for.
        var label = el(doc, 'span', ROOT_CLASS + '__label', null);
        if (options.positions && options.positions.length &&
            options.matchField && window.ArchiveNavFuzzy &&
            String(row && row[options.matchField]) === pres.name) {
            label.appendChild(window.ArchiveNavFuzzy.highlight(doc, pres.name, options.positions));
        } else {
            label.textContent = pres.name;
        }
        btn.appendChild(label);
        btn.appendChild(renderCounts(doc, row));

        // WHEN, beside HOW MUCH. This is the value the rail's default
        // order is built on, and a card ordered by a number it does not
        // show asks the reader to take the ordering on trust. Three
        // outcomes: a real day, a MEASURED 'undated', and a
        // 'no date' that means nobody could establish one - the last two
        // are different findings and read differently on the face.
        var when = window.ArchiveNavOrder
            ? window.ArchiveNavOrder.activityCell(row) : null;
        if (when) {
            var whenEl = el(doc, 'span', ROOT_CLASS + '__when' +
                (when.known ? '' : ' ' + ROOT_CLASS + '__when--unknown'),
                when.text);
            whenEl.setAttribute('title', when.title);
            btn.appendChild(whenEl);
        }

        // NO HOST PILLS ON THE FACE. They were removed at the owner's
        // instruction - "the machine pills are probably not necessary to
        // display, but should fold into an info button" - and they are
        // not deleted, they MOVED: the modal names every machine and
        // links back to that machine's list. Dropping them entirely would
        // discard the one fact the project merge is only allowed to
        // demote.
        var tip = ROW.titleFor(kind, row);
        btn.setAttribute('title', tip ? pres.name + '\n' + tip : pres.name);
        if (typeof options.onActivate === 'function') {
            btn.addEventListener('click', function () { options.onActivate(kind, id, row); });
        }
        card.appendChild(btn);

        var info = el(doc, 'button', ROOT_CLASS + '__info-btn', 'i');
        info.setAttribute('type', 'button');
        info.setAttribute('data-action', 'info');
        info.setAttribute('aria-label', 'Details for ' + pres.name);
        info.setAttribute('title', 'Machines, full path and details for ' + pres.name);
        if (typeof options.onInfo === 'function') {
            info.addEventListener('click', function (e) {
                // The card face selects the project; this control must
                // not ALSO select it on the way to opening the modal, or
                // the middle column reloads behind a dialog nobody asked
                // it to load behind.
                if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
                if (e && typeof e.preventDefault === 'function') e.preventDefault();
                options.onInfo(row, pres);
            });
        }
        card.appendChild(info);

        // PARKED, and it says so. A card sitting at the end of a
        // most-recent-first list looks like the oldest project in the
        // archive; this marker is the only thing that distinguishes "we
        // could not place it" from "it really is the oldest". Marked as
        // an attribute as well as in words, so a test asserts the
        // classification rather than the wording.
        if (options.unsorted) {
            li.setAttribute('data-unsorted', options.unsorted.short);
            var mark = el(doc, 'span', ROOT_CLASS + '__unsorted',
                          'not in this order: ' + options.unsorted.short);
            mark.setAttribute('title', options.unsorted.title);
            card.appendChild(mark);
        }

        li.appendChild(card);
        return li;
    }

    window.ArchiveNavCard = {
        renderCard: renderCard,
        renderCounts: renderCounts,
        countsTitle: countsTitle,
        sessionCountFor: sessionCountFor,
        presentationFor: presentationFor,
        SESSION_STATES: SESSION_STATES,
        SESSION_REASONS: SESSION_REASONS,
        SESSION_COUNT_FIELDS: SESSION_COUNT_FIELDS,
        SESSION_COUNTED_FIELD: SESSION_COUNTED_FIELD
    };
    console.log('[ArchiveNavCard Module] Exported as window.ArchiveNavCard');
})();
