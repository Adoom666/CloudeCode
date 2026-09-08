/**
 * One archive line to DOM, routed by record type.
 *
 * NO innerHTML, ANYWHERE. Every string in this file reaches the DOM
 * through a text node. Bodies are raw JSONL out of somebody's transcript
 * and may contain anything at all; host display names carry real
 * non-ASCII (host 2 is "Joseph's Mac mini (2)" with a U+2019, measured).
 * There is no sanitiser here because there is nothing to sanitise: text
 * nodes do not parse markup.
 *
 * A BODY WITH FINDINGS IS NEVER RENDERED UNMASKED. This file does not
 * read `body_json`, does not read `secrets`, and does not slice strings
 * by offset. It renders `entry.text`, which archive-body-cache.js
 * produced by running archive-mask.js, and which is `null` on every
 * refusal path. The only way to render text here is to hand it text that
 * a masker already approved. That is deliberate: the failure mode of
 * "mask in the renderer" is a half-masked body, and a half-masked body
 * does not look like a failure, it looks like a success with a short hex
 * tail.
 *
 * ROLE IS NULL ON 44.93% OF BODIES (measured 2026-08-31), and `ts` is
 * NULL on 33,480. A reader keyed on role blanks half its rows. The
 * fallback chain is role, then record_type, then the literal string
 * "no role recorded" - never a blank cell, because a blank cell is a
 * could-not-evaluate laundered into whitespace.
 *
 * THE MODEL COLUMN IS NOT ALL CLAUDE. Measured, the 13 values include
 * `nemotron-3-super` and a literal `<synthetic>`. Nothing here tests for
 * a "claude-" prefix, and the value is rendered as text exactly as
 * stored, angle brackets and all.
 *
 * `progress` IS 917,436 ROWS, 37.49% OF ALL BODIES. A run of them
 * collapses to one counted chip that says how many are folded and over
 * which lines. NEVER hidden and never filtered out by default: a filter
 * that silently removes 37% of a byte-exact archive is a client-side lie
 * about the file's contents.
 *
 * MEANING IS NEVER CARRIED BY COLOUR OR BY BORDER-RADIUS ALONE. Three of
 * this app's 23 themes (`terminal`, `gameboy`, `legacy_apple`) zero
 * every radius token on purpose. Every state here differs by its TEXT,
 * its `data-body-state` attribute, and which actions exist in the
 * subtree, before any styling is considered.
 *
 * Depends on archive-format.js, archive-mask.js (indirectly, through the
 * cache), archive-body-cache.js and archive-outcome-view.js.
 * Exports window.ArchiveLineRender.
 */

console.log('[ArchiveLineRender Module] Loading...');

(function () {
    'use strict';

    var ROW_CLASS = 'archive-row';

    /**
     * Rendering family per record type. The 26 record types measured in
     * this corpus 2026-08-31 collapse to five families; anything not
     * listed renders as `meta`, which is a plain, honest row rather than
     * a crash. Adding a 27th record type upstream must not break the
     * reader, and must not silently look like a conversation turn.
     * @type {Object<string,string>}
     */
    var FAMILY = (function () {
        var m = {};
        var groups = {
            turn: ['user', 'assistant'],
            tool: ['tool_use_summary', 'result'],
            progress: ['progress'],
            note: ['summary', 'system', 'ai-title', 'custom-title', 'last-prompt']
        };
        Object.keys(groups).forEach(function (fam) {
            groups[fam].forEach(function (rt) { m[rt] = fam; });
        });
        return m;
    })();

    /**
     * Rendered when a line has neither a role nor a record type. The
     * literal words are NORMATIVE: see the file header.
     * @type {string}
     */
    var NO_ROLE_TEXT = 'no role recorded';

    /**
     * Rendered in place of a timestamp on the 33,480 bodies whose `ts`
     * is NULL. Reuses archive-format.js's own not-known token so the
     * reader says the same thing everywhere.
     * @type {string}
     */
    function _notKnown() {
        return (window.ArchiveFormat && window.ArchiveFormat.NOT_KNOWN) ||
               'NOT KNOWN';
    }

    /**
     * Description: create an element with an optional class and text.
     *   The single choke point through which text reaches the DOM.
     * Inputs: doc (Document), tag (string), cls (string|null),
     *         text (string|null)
     * Output: Element
     */
    function el(doc, tag, cls, text) {
        var e = doc.createElement(tag);
        if (cls) e.className = cls;
        if (text !== null && text !== undefined) {
            e.appendChild(doc.createTextNode(String(text)));
        }
        return e;
    }

    /**
     * Description: a button carrying a data-action, which is how the
     *   reader routes clicks and how a test asserts channel 4 (which
     *   affordances exist).
     * Inputs: doc (Document), action (string), label (string)
     * Output: Element
     */
    function actionButton(doc, action, label) {
        var b = el(doc, 'button', ROW_CLASS + '__action', label);
        b.setAttribute('type', 'button');
        b.setAttribute('data-action', action);
        return b;
    }

    /**
     * Description: the family a record type renders as.
     * Inputs: recordType (string|null)
     * Output: string - one of turn|tool|progress|note|meta.
     * Example: familyFor('assistant') // -> 'turn'
     */
    function familyFor(recordType) {
        if (typeof recordType !== 'string') return 'meta';
        return FAMILY[recordType] || 'meta';
    }

    /**
     * Description: what goes in the row's second column. NORMATIVE
     *   fallback chain, see the file header.
     * Inputs: row (object) - spine row.
     * Output: {text: string, source: string} - source is 'role',
     *   'record_type' or 'none', so a test can assert WHICH rung of the
     *   chain produced the label rather than just its text.
     * Example: roleLabel({role: null, record_type: 'progress'})
     *   // -> {text: 'progress', source: 'record_type'}
     */
    function roleLabel(row) {
        if (row && typeof row.role === 'string' && row.role.length) {
            return { text: row.role, source: 'role' };
        }
        if (row && typeof row.record_type === 'string' && row.record_type.length) {
            return { text: row.record_type, source: 'record_type' };
        }
        return { text: NO_ROLE_TEXT, source: 'none' };
    }

    /**
     * Description: the row's metadata line - timestamp, model, size, and
     *   the lineage badges. Rendered for every family.
     * Inputs: doc (Document), row (object)
     * Output: Element
     */
    function renderMeta(doc, row) {
        var wrap = el(doc, 'div', ROW_CLASS + '__meta', null);
        var fmt = window.ArchiveFormat;

        var ts = (row && row.ts) ? fmt.formatTimestamp(row.ts) : _notKnown();
        wrap.appendChild(el(doc, 'span', ROW_CLASS + '__ts', ts));

        // Rendered verbatim: `<synthetic>` is a real stored value and
        // must not be mistaken for markup or dropped as a placeholder.
        if (row && typeof row.model === 'string' && row.model.length) {
            wrap.appendChild(el(doc, 'span', ROW_CLASS + '__model', row.model));
        }
        if (row && Number.isFinite(row.body_chars)) {
            wrap.appendChild(el(doc, 'span', ROW_CLASS + '__size',
                fmt.formatChars(row.body_chars)));
        }

        // Subagent lineage. A sidechain line came from a spawned agent's
        // own file, and conflating it with the main thread is how a
        // transcript reads as if the operator said something an agent
        // said. Both badges are text, not colour.
        if (row && row.is_sidechain) {
            var sc = el(doc, 'span', ROW_CLASS + '__badge ' + ROW_CLASS + '__badge--sidechain',
                'sidechain');
            sc.setAttribute('data-badge', 'sidechain');
            wrap.appendChild(sc);
        }
        if (row && row.agent_id !== null && row.agent_id !== undefined &&
                String(row.agent_id).length) {
            var ag = el(doc, 'span', ROW_CLASS + '__badge ' + ROW_CLASS + '__badge--agent',
                'agent ' + String(row.agent_id));
            ag.setAttribute('data-badge', 'agent');
            wrap.appendChild(ag);
        }
        if (row && row.is_compact_boundary) {
            // The point in the file where context was compacted away.
            // Everything before it survives only as whatever the summary
            // kept, which is a fact about the transcript's fidelity and
            // belongs on the row, not in a footnote.
            var cb = el(doc, 'span', ROW_CLASS + '__badge ' + ROW_CLASS + '__badge--compact',
                'compact boundary' + (row.compact_subtype
                    ? ' (' + String(row.compact_subtype) + ')' : ''));
            cb.setAttribute('data-badge', 'compact-boundary');
            wrap.appendChild(cb);
        }
        return wrap;
    }

    /**
     * Description: build a synthetic cannot_determine envelope so that a
     *   per-row failure renders through archive-outcome-view.js like
     *   every other failure in this screen, instead of through a
     *   hand-rolled error state that would drift away from it.
     * Inputs: subject (string), reason (string)
     * Output: object - an envelope archive-outcome.js classifies as
     *   'cannot-determine'.
     */
    function _cannotDetermineEnvelope(subject, reason) {
        return {
            result: null,
            result_status: 'cannot_determine',
            scope_status: 'resolved',
            unevaluated: [{ subject: subject, reason: reason }],
            meta: {}
        };
    }

    /**
     * Description: the body region of a row, chosen by the cache entry's
     *   state. This function is the whole reason a 54 MB body cannot
     *   reach the DOM: there is no branch here that renders anything
     *   except `entry.text`, and `entry.text` is null in every state
     *   except `included`.
     * Inputs: doc (Document), row (object), entry (object|null) - a
     *   cache entry, or null meaning not requested.
     * Output: Element
     */
    function renderBody(doc, row, entry) {
        var C = window.ArchiveBodyCache;
        var fmt = window.ArchiveFormat;
        var box = el(doc, 'div', ROW_CLASS + '__body', null);

        // NOT REQUESTED. A sized placeholder, never a spinner: nothing
        // has been asked for, so there is nothing to wait on.
        if (!entry) {
            box.setAttribute('data-body-state', 'not-requested');
            box.appendChild(el(doc, 'p', ROW_CLASS + '__placeholder',
                Number.isFinite(row && row.body_chars)
                    ? fmt.formatChars(row.body_chars) + ' not loaded yet'
                    : 'body not loaded yet'));
            return box;
        }

        box.setAttribute('data-body-state', entry.state);

        if (entry.state === C.STATE_OK) {
            var pre = el(doc, 'pre', ROW_CLASS + '__text', entry.text);
            box.appendChild(pre);
            if (entry.masked > 0) {
                // Say that a lens was applied. The archive is byte-exact
                // on disk; this view is not, and a reader who is not told
                // will read the marker as stored content.
                var n = el(doc, 'p', ROW_CLASS + '__masked-note',
                    entry.masked + ' secret' + (entry.masked === 1 ? '' : 's') +
                    ' masked in this view. The archived bytes are unchanged.');
                n.setAttribute('data-masked-count', String(entry.masked));
                box.appendChild(n);
            }
            return box;
        }

        if (entry.state === C.STATE_MASK_REFUSED) {
            // NORMATIVE: the body is NOT rendered. Not truncated, not
            // partially masked, not shown behind a warning. A body with a
            // credential at an unknown position has no safe rendering.
            box.appendChild(el(doc, 'p', ROW_CLASS + '__refusal-label',
                'BODY WITHHELD BY THIS VIEW'));
            box.appendChild(el(doc, 'p', ROW_CLASS + '__refusal',
                'This body declares ' + entry.findingCount +
                ' secret finding(s) that could not be located, so it cannot be ' +
                'masked and is not shown. Reason: ' + String(entry.reason)));
            box.appendChild(actionButton(doc, 'retry-body', 'Try loading it again'));
            return box;
        }

        if (entry.state === C.STATE_GATED_SOFT) {
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate-label', 'LARGE BODY'));
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate', String(entry.reason)));
            box.appendChild(actionButton(doc, 'render-anyway', 'Render anyway'));
            box.appendChild(actionButton(doc, 'download-body', 'Download this body'));
            return box;
        }

        if (entry.state === C.STATE_GATED_HARD) {
            // NORMATIVE: no render action exists here at all. Its
            // ABSENCE is the guarantee, which is why it is a structural
            // fact rather than a disabled attribute somebody can flip.
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate-label',
                'TOO LARGE TO RENDER'));
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate', String(entry.reason)));
            box.appendChild(actionButton(doc, 'download-body', 'Download this body'));
            return box;
        }

        if (entry.state === C.STATE_WITHHELD) {
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate-label',
                'WITHHELD BY THE SERVER'));
            box.appendChild(el(doc, 'p', ROW_CLASS + '__gate', String(entry.reason)));
            box.appendChild(actionButton(doc, 'download-body', 'Download this body'));
            return box;
        }

        if (entry.state === C.STATE_NO_BODY) {
            // A real, measured shape: an appearance row with a null
            // body_id. It is a fact about the file, not a failure.
            box.appendChild(el(doc, 'p', ROW_CLASS + '__placeholder',
                'This line has no body row in the archive.'));
            return box;
        }

        if (entry.state === C.STATE_LOADING) {
            box.appendChild(el(doc, 'p', ROW_CLASS + '__loading', 'Loading body...'));
            return box;
        }

        // Everything else is a could-not-evaluate, and it goes through
        // the shared outcome renderer so it cannot drift into looking
        // like an empty row.
        box.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock(
            _cannotDetermineEnvelope('body:' + String(row && row.body_id),
                String(entry.reason || 'the body could not be read')),
            { document: doc, extraActions: [
                { action: 'retry-body', label: 'Try loading it again' }
            ] }
        ));
        return box;
    }

    /**
     * Description: render one archive line as a row element.
     * Inputs: doc (Document), row (object) - spine row.
     *         entry (object|null) - the body cache entry, or null for
     *           not-requested.
     *         options (object|null) - {index: number} for the reader's
     *           own bookkeeping.
     * Output: Element - carries data-line-no, data-record-type,
     *   data-family and data-role-source.
     * Example: renderLine(document, {line_no: 7111, record_type:
     *   'assistant', role: 'assistant', body_chars: 5501}, entry)
     */
    function renderLine(doc, row, entry, options) {
        var opts = options || {};
        var family = familyFor(row && row.record_type);
        var label = roleLabel(row);

        var el0 = el(doc, 'article',
            ROW_CLASS + ' ' + ROW_CLASS + '--' + family, null);
        el0.setAttribute('data-line-no', String(row && row.line_no));
        el0.setAttribute('data-record-type', String((row && row.record_type) || ''));
        el0.setAttribute('data-family', family);
        el0.setAttribute('data-role-source', label.source);
        if (Number.isFinite(opts.index)) {
            el0.setAttribute('data-index', String(opts.index));
        }

        var head = el(doc, 'div', ROW_CLASS + '__head', null);
        head.appendChild(el(doc, 'span', ROW_CLASS + '__lineno',
            String(row && row.line_no)));
        head.appendChild(el(doc, 'span', ROW_CLASS + '__role', label.text));
        // The record type is always shown, even when it also supplied the
        // role label. Two columns saying the same thing is cheaper than a
        // reader guessing which one they are looking at.
        head.appendChild(el(doc, 'span', ROW_CLASS + '__type',
            String((row && row.record_type) || 'no record type')));
        el0.appendChild(head);
        el0.appendChild(renderMeta(doc, row));
        el0.appendChild(renderBody(doc, row, entry));
        return el0;
    }

    /**
     * Description: render a collapsed run of consecutive `progress`
     *   lines as ONE counted row. NORMATIVE: the count and the line
     *   range are both stated, and the run is one action away from being
     *   expanded. Nothing is hidden.
     * Inputs: doc (Document), run (object) - {kind: 'progress-run',
     *           from, to, count, rows}
     *         options (object|null) - {expanded: boolean, index: number}
     * Output: Element
     * Example: renderProgressRun(document,
     *   {kind: 'progress-run', from: 7110, to: 7123, count: 14, rows: [...]})
     */
    function renderProgressRun(doc, run, options) {
        var opts = options || {};
        var el0 = el(doc, 'article', ROW_CLASS + ' ' + ROW_CLASS + '--progress-run', null);
        el0.setAttribute('data-record-type', 'progress');
        el0.setAttribute('data-family', 'progress');
        el0.setAttribute('data-progress-count', String(run.count));
        el0.setAttribute('data-expanded', opts.expanded ? 'true' : 'false');
        el0.setAttribute('data-line-no', String(run.from));
        if (Number.isFinite(opts.index)) {
            el0.setAttribute('data-index', String(opts.index));
        }

        var head = el(doc, 'div', ROW_CLASS + '__head', null);
        head.appendChild(el(doc, 'span', ROW_CLASS + '__lineno', String(run.from)));
        head.appendChild(el(doc, 'span', ROW_CLASS + '__chip',
            'progress x ' + run.count));
        head.appendChild(el(doc, 'span', ROW_CLASS + '__range',
            run.from === run.to
                ? 'line ' + run.from
                : 'lines ' + run.from + '-' + run.to));
        head.appendChild(actionButton(doc,
            opts.expanded ? 'collapse-progress' : 'expand-progress',
            opts.expanded ? 'Collapse' : 'Expand'));
        el0.appendChild(head);

        if (opts.expanded) {
            var body = el(doc, 'div', ROW_CLASS + '__progress-children', null);
            for (var i = 0; i < run.rows.length; i++) {
                body.appendChild(renderLine(doc, run.rows[i],
                    opts.entryFor ? opts.entryFor(run.rows[i]) : null, null));
            }
            el0.appendChild(body);
        }
        return el0;
    }

    /**
     * Description: fold consecutive `progress` lines into runs, leaving
     *   every other line alone. Runs of ONE are left as ordinary lines:
     *   a chip reading "progress x 1" costs a row and says less than the
     *   row it replaced.
     * Inputs: spine (Array<object>)
     * Output: Array<object> - items are either the original row objects
     *   or {kind: 'progress-run', from, to, count, rows}.
     * Example: groupRows([a, p1, p2, p3, b])
     *   // -> [a, {kind:'progress-run', count: 3, ...}, b]
     */
    function groupRows(spine) {
        var out = [];
        if (!Array.isArray(spine)) return out;
        var i = 0;
        while (i < spine.length) {
            if (spine[i] && spine[i].record_type === 'progress') {
                var j = i;
                while (j + 1 < spine.length &&
                       spine[j + 1] && spine[j + 1].record_type === 'progress') {
                    j++;
                }
                if (j > i) {
                    out.push({
                        kind: 'progress-run',
                        from: spine[i].line_no,
                        to: spine[j].line_no,
                        count: (j - i) + 1,
                        rows: spine.slice(i, j + 1)
                    });
                    i = j + 1;
                    continue;
                }
            }
            out.push(spine[i]);
            i++;
        }
        return out;
    }

    /**
     * Description: render any grouped item, line or progress run.
     * Inputs: doc (Document), item (object), entry (object|null),
     *         options (object|null)
     * Output: Element
     */
    function renderItem(doc, item, entry, options) {
        if (item && item.kind === 'progress-run') {
            return renderProgressRun(doc, item, options);
        }
        return renderLine(doc, item, entry, options);
    }

    window.ArchiveLineRender = {
        renderLine: renderLine,
        renderProgressRun: renderProgressRun,
        renderItem: renderItem,
        renderBody: renderBody,
        groupRows: groupRows,
        familyFor: familyFor,
        roleLabel: roleLabel,
        ROW_CLASS: ROW_CLASS,
        NO_ROLE_TEXT: NO_ROLE_TEXT,
        FAMILY: FAMILY
    };
    console.log('[ArchiveLineRender Module] Exported as window.ArchiveLineRender');
})();
