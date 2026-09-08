/**
 * The transcript list's VOCABULARY and its PURE RENDERERS: the three
 * scheme filters, the attribution values that mean "not established",
 * and the functions that turn one server row into one list item.
 *
 * WHY THIS IS SEPARATE FROM archive-transcript-list.js. Everything here
 * is a PURE FUNCTION of its arguments - give it a document and a row
 * and it returns an element, with no fetch, no cursor, no page and no
 * state of any kind. The sibling file owns all of that. The split is
 * along that line and no other, which is why every function here can be
 * tested by calling it, with no harness beyond a document.
 *
 * ONE TABLE FOR THE FILTERS, so the button order and the order the `t`
 * key cycles through them cannot drift into disagreeing. Two lists
 * would be two declarations of one fact, and they would diverge.
 *
 * UNATTRIBUTED AND cannot_determine ARE DIFFERENT AND ARE NEVER MERGED.
 * A transcript in the unattributed listing belongs to no PROJECT; one
 * with `host_attribution: "cannot_determine"` has no established HOST.
 * Either can be true without the other, and each gets its own badge.
 * UNESTABLISHED_ATTRIBUTION is a LIST rather than a comparison so a new
 * server value cannot silently classify itself as established.
 *
 * `has_more` IS THREE-VALUED HERE TOO: describeFilter renders "more"
 * on `=== true` and nothing else, because treating null as false claims
 * the end of a list that was never read.
 *
 * No innerHTML: session_ref and source_path are file-derived strings.
 * Exports window.ArchiveTlistRow.
 */

console.log('[ArchiveTlistRow Module] Loading...');

(function () {
    'use strict';

    /** Root element class. @type {string} */
    var ROOT_CLASS = 'archive-tlist';

    /** Rows requested per page. The server's own default is 50. @type {number} */
    var PAGE_SIZE = 50;

    /**
     * The three scheme filters this list offers.
     * @type {Object<string,string>}
     */
    var SCHEME_FILTERS = {
        ALL: 'all',
        CONVERSATIONS: 'uuid',
        SIDECHAINS: 'agent'
    };

    /**
     * THE DEFAULT IS THE OWNER'S OWN SESSIONS, NOT EVERYTHING.
     *
     * Measured on the live corpus 2026-08-31: 19,588 of 21,039
     * transcripts (93.1%) are `agent`-scheme sidechain FILES written by
     * subagents, and only 1,451 are `uuid`-scheme top-level sessions.
     * Defaulting to `all` therefore opened this list on 93 percent noise
     * and buried the 7 percent anybody was looking for.
     *
     * `uuid` is the right default because that set is exactly "the
     * top-level sessions", named or not - an older session with no title
     * is still one the owner started, so the default must not be
     * narrowed further to "titled" rows. `all` remains one click away and
     * is still the only view that hides nothing, which is why it is
     * listed first below rather than being removed.
     * @type {string}
     */
    var DEFAULT_SCHEME = SCHEME_FILTERS.CONVERSATIONS;

    /**
     * The three filters in the order the options are drawn AND the order
     * `t` cycles through them. One table, so the control order and the
     * keyboard order cannot drift into disagreeing. `hint` is the option's
     * `title`, because a compact chooser has no room to explain itself in
     * the label.
     * @type {Array<{v: string, label: string, hint: string}>}
     */
    var SCHEME_DEFS = [
        { v: SCHEME_FILTERS.ALL, label: 'Everything',
          hint: 'Every transcript in this project, sessions and agent ' +
                'sidechains together. The only view that hides nothing.' },
        { v: SCHEME_FILTERS.CONVERSATIONS, label: 'My sessions',
          hint: 'Top-level sessions (session_ref_scheme = uuid), named or ' +
                'not. About 7 percent of the corpus; the default.' },
        { v: SCHEME_FILTERS.SIDECHAINS, label: 'Agent sidechains',
          hint: 'Transcript files written by subagents ' +
                '(session_ref_scheme = agent). About 93 percent of the corpus.' }
    ];

    /**
     * What each `title_source` means, and how it must LOOK.
     *
     * A HUMAN-CHOSEN NAME AND A MACHINE GUESS MUST NOT RENDER ALIKE.
     * `custom-title` is a name somebody typed; `last-prompt` is an
     * excerpt the ingest lifted off the session's last prompt, which is
     * frequently a fragment of a sentence and is sometimes actively
     * misleading about what the session was for. Showing both as plain
     * bold text would present a guess with the authority of a name.
     *
     * A LIST, NOT A COMPARISON, for the same reason UNESTABLISHED_-
     * ATTRIBUTION is: a `title_source` this client has never heard of
     * must classify as NOT KNOWN rather than defaulting into whichever
     * branch an if-chain happened to end on.
     * @type {Object<string,{label: string, kind: string, hint: string}>}
     */
    var TITLE_SOURCES = {
        'custom-title': {
            label: 'NAMED', kind: 'human', mod: '__source--human',
            hint: 'A name a person chose for this session. Nothing ' +
                  'outranks it.'
        },
        'ai-title': {
            label: 'AI-NAMED', kind: 'derived', mod: '__source--derived',
            hint: 'Generated, but generated ABOUT the session as a whole, ' +
                  'and stable. Not a name anybody chose.'
        },
        'summary': {
            label: 'FROM SUMMARY', kind: 'derived', mod: '__source--derived',
            hint: 'Generated to describe the session, but written for ' +
                  'compaction rather than for naming.'
        },
        'last-prompt': {
            label: 'LAST PROMPT, NOT A NAME', kind: 'weak', mod: '__source--weak',
            hint: 'NOT a title. It is the text of the last thing typed in ' +
                  'this session - measured values include "yes" and ' +
                  '"exirt". Shown only because a bad name beats a UUID.'
        },
        'cannot_determine': {
            label: 'NAME LOOKUP FAILED', kind: 'cannot-determine',
            mod: '__source--cannot-determine',
            hint: 'The server could not read this session’s title records. ' +
                  'This is NOT the same as the session having no name - ' +
                  'nobody has established either way.'
        }
    };

    /** How an ABSENT title_source renders. Distinct from an unrecognised
     *  one: "there is no name" is a measurement, "I do not know what
     *  produced this name" is not. @type {object} */
    var TITLE_SOURCE_NONE = {
        label: 'NOT NAMED', kind: 'none', mod: '__source--none',
        hint: 'This session has no title from any source. The reference ' +
              'below is shown in its place; it is not a name.'
    };

    /** How an UNRECOGNISED or failed title_source renders. @type {object} */
    var TITLE_SOURCE_UNKNOWN = {
        label: 'NAME SOURCE NOT KNOWN', kind: 'cannot-determine',
        mod: '__source--cannot-determine',
        hint: 'A title was supplied but this client cannot tell what ' +
              'produced it, so it cannot say whether it is a chosen name ' +
              'or a derived guess.'
    };

    /**
     * Attribution values that mean "not established". Kept as a list so
     * a new server value does not silently classify as established.
     * @type {string[]}
     */
    var UNESTABLISHED_ATTRIBUTION = ['cannot_determine', 'unknown'];

    /**
     * Description: build an element with a class and optional text.
     * Inputs: doc (Document), tag (string), cls (string|null), text (any).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: translate a UI filter choice into the wire value the
     *   server takes, or null for "do not send the parameter". Pure.
     *
     *   `null` and `'all'` are not interchangeable on the wire: sending
     *   `session_ref_scheme=all` would be an UNKNOWN scheme and answer
     *   400, so "no filter" has to be an omitted parameter.
     * Inputs: scheme (string) - a SCHEME_FILTERS value.
     * Output: string|null.
     * Example: wireScheme('all') // -> null
     */
    function wireScheme(scheme) {
        if (!scheme || scheme === SCHEME_FILTERS.ALL) return null;
        return scheme;
    }

    /**
     * Description: the sentence under the list, built from what the
     *   SERVER reported rather than from what this file counted. Pure.
     *
     *   It states three separate things and does not merge them: how
     *   many rows are on screen, how many the whole scope holds under
     *   this filter, and what the filter is actually matching on. The
     *   third comes from the server's own `session_ref_scheme_means`, so
     *   there is one wording and it cannot drift from the API's.
     * Inputs: loaded (number) - rows on screen, filters (object|null) -
     *         the server's `meta.filters` block, or null when the last
     *         response carried none.
     * Output: string - '' when no filter is applied and nothing is hidden.
     * Example: describeFilter(50, {applied: true, matched_in_scope: 77,
     *          scope_total_before_filter: 3416, session_ref_scheme: 'uuid'})
     */
    function describeFilter(loaded, filters) {
        if (!filters || filters.applied !== true) return '';
        var noun = filters.session_ref_scheme === SCHEME_FILTERS.CONVERSATIONS
            ? 'conversations (uuid scheme)' : 'agent sidechains';
        var line = 'Showing ' + fmtCount(loaded) + ' ' + noun + '.';
        if (typeof filters.matched_in_scope === 'number') {
            line += ' The server filtered the WHOLE scope, which holds ' +
                    fmtCount(filters.matched_in_scope) + ' rows with this scheme';
            if (typeof filters.scope_total_before_filter === 'number') {
                line += ' out of ' + fmtCount(filters.scope_total_before_filter);
            }
            line += '.';
        } else {
            line += ' The server did not report how many rows in this scope' +
                    ' carry this scheme, so that number is NOT KNOWN.';
        }
        if (typeof filters.session_ref_scheme_means === 'string') {
            line += ' Caveat from the server: ' + filters.session_ref_scheme_means;
        }
        return line;
    }

    /**
     * Description: format a count, deferring to archive-format.js when
     *   it is loaded so grouping is identical everywhere.
     * Inputs: n (number|null). Output: string.
     */
    function fmtCount(n) {
        return window.ArchiveFormat ? window.ArchiveFormat.formatCount(n) : String(n);
    }

    /**
     * Description: whether an attribution field states a real finding or
     *   states that nothing was established. Pure.
     * Inputs: value (string|null).
     * Output: boolean - true when the link was NOT established.
     * Example: isUnestablished('cannot_determine')  // -> true
     */
    function isUnestablished(value) {
        return UNESTABLISHED_ATTRIBUTION.indexOf(String(value)) !== -1;
    }

    /**
     * Description: classify a row's `title_source` into how it renders.
     *   Pure, and total: every input lands on exactly one of the three
     *   descriptors, so an unrecognised value can never be presented as
     *   a chosen name.
     * Inputs: row (object).
     * Output: {label, kind, hint} - one of TITLE_SOURCES' entries,
     *   TITLE_SOURCE_NONE or TITLE_SOURCE_UNKNOWN.
     * Example: titleSource({title: 'x', title_source: 'last-prompt'}).kind
     *          // -> 'derived'
     */
    function titleSource(row) {
        var r = row || {};
        var src = String(r.title_source);
        // THE FAILED LOOKUP IS CHECKED FIRST, AND THAT ORDER IS THE WHOLE
        // POINT. The server ships `title: null, title_source:
        // "cannot_determine"` when it could not READ the title records,
        // and `title: null, title_source: null` when it looked and there
        // is genuinely no name. Testing the empty title first collapses
        // the two into "NOT NAMED" - reporting a measurement the server
        // explicitly declined to make. Same shape as the attribution
        // fields: an untitled transcript and a transcript whose title
        // could not be looked up are different findings, and only one of
        // them is good news.
        if (Object.prototype.hasOwnProperty.call(TITLE_SOURCES, src) &&
                src === 'cannot_determine') {
            return TITLE_SOURCES[src];
        }
        var t = r.title;
        if (typeof t !== 'string' || t.length === 0) return TITLE_SOURCE_NONE;
        if (Object.prototype.hasOwnProperty.call(TITLE_SOURCES, src)) {
            return TITLE_SOURCES[src];
        }
        return TITLE_SOURCE_UNKNOWN;
    }

    /**
     * Description: the text that LEADS the row, and whether it is a name.
     *
     *   THE FALLBACK MUST NOT IMPLY A NAME EXISTS. When there is no
     *   title the row leads with the session_ref, which is a file-derived
     *   reference and not a name - so `isTitle` comes back false and the
     *   caller styles it as the reference it is, beside a NOT NAMED
     *   marker. Rendering the ref in the title's own treatment would
     *   present every unnamed session as though somebody had named it
     *   after its UUID.
     *
     *   The three-outcome shape is in `isTitle` and `text` together:
     *   a real name, a reference standing in for one, or - when the row
     *   carries neither - a stated absence rather than an empty cell.
     * Inputs: row (object).
     * Output: {text: string, isTitle: boolean}.
     * Example: displayTitle({session_ref: 'journal'})
     *          // -> {text: 'journal', isTitle: false}
     */
    function displayTitle(row) {
        var r = row || {};
        if (typeof r.title === 'string' && r.title.length > 0) {
            return { text: r.title, isTitle: true };
        }
        if (typeof r.session_ref === 'string' && r.session_ref.length > 0) {
            return { text: r.session_ref, isTitle: false };
        }
        return { text: 'no name and no session_ref recorded', isTitle: false };
    }

    /**
     * Description: read one FUZZY-FILTERABLE column out of a row, as the
     *   string that is actually on screen. Filtering a value the person
     *   cannot see - a raw epoch behind a formatted date - makes a
     *   filter that fails for reasons nobody can inspect.
     * Inputs: row (object), key (string) - 'title', 'ref' or 'date'.
     * Output: string.
     */
    function rowValue(row, key) {
        var r = row || {};
        if (key === 'title') return displayTitle(r).text;
        if (key === 'ref') {
            return typeof r.session_ref === 'string' ? r.session_ref : '';
        }
        if (key === 'date') return fmtStamp(r.ingested_at);
        return '';
    }

    /** Description: format a timestamp through archive-format.js when it
     *  is loaded. Inputs: v. Output: string. */
    function fmtStamp(v) {
        return window.ArchiveFormat
            ? window.ArchiveFormat.formatTimestamp(v) : String(v);
    }

    /**
     * Description: fill a node with text, highlighting the fuzzy-matched
     *   characters, and ALWAYS set `title` to the full text.
     *
     *   THE `title` IS THE OTHER HALF OF TRUNCATION. Every label in this
     *   pane is clipped with an ellipsis rather than wrapped, so without
     *   the attribute a long name becomes unreadable with no way back.
     *   It is written here, in the one function that writes label text,
     *   so a new field cannot be added without it.
     * Inputs: doc (Document), node (Element), text (string),
     *         spans (Array<[number,number]>|null|undefined).
     * Output: Element - node.
     */
    function fillLabel(doc, node, text, spans) {
        var s = String(text === null || text === undefined ? '' : text);
        node.setAttribute('title', s);
        node.textContent = '';
        var segs = (spans && spans.length && window.ArchiveFuzzy)
            ? window.ArchiveFuzzy.segments(s, spans)
            : [{ text: s, hit: false }];
        for (var i = 0; i < segs.length; i++) {
            if (!segs[i].hit) {
                node.appendChild(doc.createTextNode(segs[i].text));
                continue;
            }
            var mark = el(doc, 'mark', ROOT_CLASS + '__hit', segs[i].text);
            node.appendChild(mark);
        }
        return node;
    }

    /**
     * Description: render one transcript row. Keyed and linked on
     *   transcript_id only; session_ref appears as text and never as an
     *   identity.
     * Inputs: doc (Document), row (object),
     *         opts (object) - {onSelect: function(transcriptId, row),
     *                          unattributed: boolean,
     *                          spans: Object<string,Array>|null - fuzzy
     *                            match spans keyed by rowValue column}
     * Output: Element - the <li>.
     */
    function renderRow(doc, row, opts) {
        var options = opts || {};
        var spans = options.spans || {};
        var r = row || {};
        var li = el(doc, 'li', ROOT_CLASS + '__row', null);
        li.setAttribute('data-transcript-id', String(r.transcript_id));
        li.setAttribute('data-scheme', String(r.session_ref_scheme || 'unknown'));

        var btn = el(doc, 'button', ROOT_CLASS + '__open', null);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-action', 'open-transcript');
        // The id is the addressable thing and it goes on the control, so
        // no click path can end up keyed on the label.
        btn.setAttribute('data-transcript-id', String(r.transcript_id));

        // THE NAME LEADS. The row used to lead with a bare UUID, which is
        // the one field on it nobody recognises.
        var shown = displayTitle(r);
        var src = titleSource(r);
        var head = el(doc, 'span', ROOT_CLASS + '__title', null);
        head.setAttribute('data-is-title', shown.isTitle ? 'true' : 'false');
        fillLabel(doc, head, shown.text, spans.title);
        btn.appendChild(head);

        // The modifier is a LITERAL from the table, not `'__source--' +
        // src.kind`. A computed class name cannot be recovered from the
        // source by the stylesheet antijoin in
        // tests/test_archive_tlist_styled.node.mjs, so a modifier with no
        // rule anywhere would render in user-agent defaults and no test
        // could see it - which is the exact defect that test exists for.
        var badge = el(doc, 'span',
            ROOT_CLASS + '__source ' + ROOT_CLASS + src.mod, src.label);
        badge.setAttribute('data-title-source', String(
            r.title_source === null || r.title_source === undefined
                ? 'none' : r.title_source));
        badge.setAttribute('title', src.hint);
        btn.appendChild(badge);

        // The ref is DEMOTED to the metadata line once a name exists, but
        // it never disappears: it is what the export filename and the raw
        // file on disk are called.
        var ref = el(doc, 'span', ROOT_CLASS + '__ref', null);
        fillLabel(doc, ref, String(
            r.session_ref === null || r.session_ref === undefined
                ? 'no session_ref recorded' : r.session_ref), spans.ref);
        btn.appendChild(ref);

        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__id', 'transcript ' + r.transcript_id));
        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__lines',
            fmtCount(r.line_count) + ' lines'));
        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__bytes',
            window.ArchiveFormat ? window.ArchiveFormat.formatBytes(r.raw_byte_length)
                                 : String(r.raw_byte_length)));
        var ing = el(doc, 'span', ROOT_CLASS + '__ingested', null);
        fillLabel(doc, ing, fmtStamp(r.ingested_at), spans.date);
        btn.appendChild(ing);

        // The two attribution conditions, side by side and never merged.
        if (options.unattributed) {
            btn.appendChild(el(doc, 'span',
                ROOT_CLASS + '__badge ' + ROOT_CLASS + '__badge--no-project',
                'NO PROJECT: this transcript is attributed to no project'));
        }
        if (isUnestablished(r.host_attribution)) {
            btn.appendChild(el(doc, 'span',
                ROOT_CLASS + '__badge ' + ROOT_CLASS + '__badge--host-unknown',
                'HOST NOT ESTABLISHED: host_attribution is ' + String(r.host_attribution) +
                '. This is a separate condition from having no project.'));
        }
        if (typeof options.onSelect === 'function') {
            btn.addEventListener('click', function () {
                options.onSelect(r.transcript_id, r);
            });
        }
        li.appendChild(btn);
        return li;
    }

    window.ArchiveTlistRow = {
        el: el,
        wireScheme: wireScheme,
        describeFilter: describeFilter,
        fmtCount: fmtCount,
        isUnestablished: isUnestablished,
        renderRow: renderRow,
        titleSource: titleSource,
        displayTitle: displayTitle,
        rowValue: rowValue,
        fillLabel: fillLabel,
        ROOT_CLASS: ROOT_CLASS,
        PAGE_SIZE: PAGE_SIZE,
        SCHEME_FILTERS: SCHEME_FILTERS,
        SCHEME_DEFS: SCHEME_DEFS,
        DEFAULT_SCHEME: DEFAULT_SCHEME,
        TITLE_SOURCES: TITLE_SOURCES,
        TITLE_SOURCE_NONE: TITLE_SOURCE_NONE,
        TITLE_SOURCE_UNKNOWN: TITLE_SOURCE_UNKNOWN,
        UNESTABLISHED_ATTRIBUTION: UNESTABLISHED_ATTRIBUTION
    };
    console.log('[ArchiveTlistRow Module] Exported as window.ArchiveTlistRow');
})();
