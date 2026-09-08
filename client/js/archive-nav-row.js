/**
 * The nav rail's VOCABULARY and its PURE RENDERERS: the node kinds the
 * rail can hold, and the functions that turn one server row into one
 * rail row, one count into a label, and one refusal envelope into a
 * visible block.
 *
 * WHY THIS IS SEPARATE FROM archive-nav.js. Everything here is a PURE
 * FUNCTION of its arguments. The sibling file owns the rail's STATE -
 * which nodes are expanded, which rows have been loaded, which filter
 * text is live, and every fetch that produces them. Nothing here reads
 * or writes any of that, which is why each function can be tested by
 * calling it with a document and a row.
 *
 * `unattributed` IS A FIRST-CLASS KIND, not a flag on a corpus. It is a
 * selectable scope in its own right, and modelling it as a flag would
 * make "no project" unrepresentable in the tree that has to show it.
 *
 * NOT_KNOWN IS A RENDERED STRING, NOT A ZERO. renderCount() prints it
 * whenever the server sent no count, because a count of 0 and a count
 * nobody measured are different findings and a rail that shows "0" for
 * both is lying about one of them.
 *
 * A REFUSAL IS RENDERED, NEVER SWALLOWED. renderOutcomeInto() puts the
 * envelope's own reason on screen; a rail that silently shows an empty
 * branch on a refused request is indistinguishable from a branch that
 * is genuinely empty.
 *
 * Exports window.ArchiveNavRow.
 */

console.log('[ArchiveNavRow Module] Loading...');

(function () {
    'use strict';

    /** Root element class. @type {string} */
    var ROOT_CLASS = 'archive-nav';

    /**
     * Node kinds this rail can render. `unattributed` is a first-class
     * kind, not a flag on a corpus, because it is a selectable scope
     * with its own listing endpoint.
     * @type {Object<string,string>}
     */
    var NODE_KINDS = {
        HOST: 'host',
        CORPUS: 'corpus',
        PROJECT: 'project',
        UNATTRIBUTED: 'unattributed'
    };

    /**
     * The literal rendered wherever a count was not supplied. Never a
     * zero: "the server did not tell me" and "there are none" are
     * different findings.
     * @type {string}
     */
    var NOT_KNOWN = 'NOT KNOWN';

    /**
     * Description: build one element with a class and optional text.
     *   Text always reaches the DOM as a text node.
     * Inputs: doc (Document), tag (string), cls (string|null),
     *   text (string|number|null).
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var node = doc.createElement(tag);
        if (cls) node.setAttribute('class', cls);
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    /**
     * Description: render a count that may be absent. A missing number
     *   renders as NOT KNOWN, never as 0.
     * Inputs: n (number|null|undefined).
     * Output: string.
     * Example: renderCount(null) // -> 'NOT KNOWN'
     */
    function renderCount(n) {
        if (typeof n !== 'number' || !isFinite(n)) return NOT_KNOWN;
        return window.ArchiveFormat
            ? window.ArchiveFormat.formatCount(n)
            : String(n);
    }

    /**
     * Description: case-insensitive substring filter over already-loaded
     *   rows. Pure, exported so a test can assert it without a DOM.
     * Inputs: rows (Array<object>) - nav rows.
     *         needle (string) - the filter text; '' matches everything.
     *         fields (Array<string>) - row properties to search.
     * Output: Array<object> - the matching subset, order preserved.
     * Example: filterRows([{slug:'abc'}], 'B', ['slug'])  // -> [{slug:'abc'}]
     */
    function filterRows(rows, needle, fields) {
        var list = Array.isArray(rows) ? rows : [];
        var q = String(needle === null || needle === undefined ? '' : needle).toLowerCase();
        if (!q) return list.slice();
        var keys = Array.isArray(fields) && fields.length ? fields : ['slug'];
        return list.filter(function (row) {
            for (var i = 0; i < keys.length; i++) {
                var v = row && row[keys[i]];
                if (typeof v === 'string' && v.toLowerCase().indexOf(q) !== -1) return true;
            }
            return false;
        });
    }

    /**
     * The unattributed node's name. It says WHAT THESE TRANSCRIPTS ARE,
     * not merely what they lack: their source path has no
     * `.claude/projects` layer in it, so the slug deriver returned "none
     * declared" rather than failing to derive one. A bare count next to
     * the word "unattributed" reads as an attribution failure somebody
     * ought to chase; this reads as an answer.
     *
     * It deliberately does NOT say "audit logs". Every one of the five
     * such transcripts measured on 2026-09-02 was a local-agent sandbox
     * audit log, and that is a fact about today's corpus, not about the
     * category - the day a different kind of project-less transcript is
     * ingested, a label asserting "audit logs" would be lying while
     * looking authoritative. The dated measurement goes in the tooltip
     * (see titleFor), where it can be read as the dated observation it
     * is. @type {string}
     */
    var UNATTRIBUTED_LABEL = 'transcripts with no project layer';

    /**
     * Description: the note under the unattributed node, DERIVED from the
     *   same shouldShowUnattributed() verdict that decided to show it, so
     *   the words and the reason cannot disagree. Three outcomes, because
     *   the node is shown for three different reasons and only one of
     *   them is settled: a measured non-zero count is ANSWERED, while a
     *   count that could not be established or was never reported is
     *   genuinely open and must not be dressed up as answered. Pure.
     * Inputs: row (object) - the same row shouldShowUnattributed reads.
     * Output: {text: string, answered: boolean}.
     * Example: unattributedNote({unattributed_transcript_count: 5})
     *   // -> {text: 'belongs to no project ...', answered: true}
     */
    function unattributedNote(row) {
        var verdict = shouldShowUnattributed(row);
        if (verdict.reason === 'count could not be determined' ||
            verdict.reason === 'no count was reported') {
            return {
                answered: false,
                text: 'belongs to no project, invisible from the project ' +
                      'tree - and how many there are is not established, ' +
                      'so this node is shown rather than hidden'
            };
        }
        return {
            answered: true,
            text: 'belongs to no project because its path has no project ' +
                  'layer to declare one - a complete answer, not a failed ' +
                  'attribution. It is invisible from the project tree, ' +
                  'which is why it is listed here.'
        };
    }

    /**
     * Description: the sentence a filter must always carry, naming what
     *   it did and did NOT look at. Pure.
     * Inputs: matched (number) - rows the filter kept.
     *         loaded (number) - rows this rail has fetched.
     *         total (number|null) - rows the server says exist, or null.
     *         noun (string) - e.g. 'projects'.
     * Output: string - empty only when the filter is inactive.
     * Example: describeFilter(3, 71, 3416, 'projects')
     *   // -> 'filter matches 3 of 71 loaded projects. 3,416 exist; this
     *   //     filters rows already fetched, not the whole corpus.'
     */
    function describeFilter(matched, loaded, total, noun) {
        var line = 'filter matches ' + renderCount(matched) + ' of ' +
                   renderCount(loaded) + ' loaded ' + noun + '.';
        if (typeof total === 'number' && total > loaded) {
            line += ' ' + renderCount(total) + ' exist;';
        }
        return line + ' This filters rows already fetched, not the whole corpus.';
    }

    /**
     * Description: the label for one nav row, by kind. Pure.
     * Inputs: kind (string) - a NODE_KINDS value. row (object).
     * Output: string, never empty. A row with no name at all renders its
     *   id rather than a blank, because a blank row cannot be clicked
     *   with intent.
     */
    function labelFor(kind, row) {
        var r = row || {};
        if (kind === NODE_KINDS.HOST) {
            return String(r.display_name || r.hostname || ('host ' + r.host_id));
        }
        if (kind === NODE_KINDS.CORPUS) {
            return String(r.corpus_key || r.root_path || ('corpus ' + r.corpus_id));
        }
        if (kind === NODE_KINDS.UNATTRIBUTED) {
            return UNATTRIBUTED_LABEL;
        }
        // display_name is the FOLDER NAME the server derived from
        // observed_cwd, already widened where it would collide. It is
        // preferred over the slug because the slug middle-truncates to
        // '-Users-jsugamele-Develo...t-Assistants-Developer' in a 272px
        // rail, which removes precisely the segment that identifies it.
        // No shortenSlug here: the rail truncates with CSS and puts the
        // full text in a title attribute, so nothing is destroyed.
        if (typeof r.display_name === 'string' && r.display_name) {
            return r.display_name;
        }
        // display_name null means the server had no observed_cwd and
        // refused to guess a folder name out of a slug it cannot invert.
        // Showing the slug is honest; showing a fabricated leaf is not.
        var slug = r.full_path || r.slug || r.observed_cwd || ('project ' + r.project_id);
        return String(slug);
    }

    /**
     * Description: the full, untruncated text for a row's `title`
     *   attribute, so hovering a truncated label reveals everything the
     *   rail could not fit. Pure.
     * Inputs: kind (string), row (object).
     * Output: string - '' when there is nothing more to say than the
     *   label already says.
     * Example: titleFor('project', {display_name:'Media', full_path:'-Users-x-Media'})
     */
    function titleFor(kind, row) {
        var r = row || {};
        if (kind === NODE_KINDS.UNATTRIBUTED) {
            // A DATED OBSERVATION, which is why it lives here and not in
            // the label: it says what these were when someone last looked,
            // and stays true afterwards even if the mix changes.
            return 'Their source path has no .claude/projects layer, so no ' +
                   'project could be declared for them.\nMeasured ' +
                   '2026-09-02: all 5 were local-agent sandbox audit logs ' +
                   '(permission and decision trails), not conversations.';
        }
        if (kind === NODE_KINDS.PROJECT) {
            var parts = [];
            if (typeof r.observed_cwd === 'string' && r.observed_cwd) {
                parts.push(r.observed_cwd);
            }
            var slug = r.full_path || r.slug;
            if (typeof slug === 'string' && slug && slug !== parts[0]) {
                parts.push('slug: ' + slug);
            }
            if (Array.isArray(r.hosts) && r.hosts.length) {
                parts.push('on: ' + r.hosts.join(', '));
            }
            return parts.join('\n');
        }
        if (kind === NODE_KINDS.HOST) {
            return String(r.hostname || r.display_name || '');
        }
        if (kind === NODE_KINDS.CORPUS) {
            return String(r.root_path || r.corpus_key || '');
        }
        return '';
    }

    /**
     * Description: decide whether the unattributed node is rendered.
     *   THE ONLY CASE THAT HIDES IT is a count the server measured and
     *   reported as 0.
     *
     *   A count that is missing, null, or flagged `counted: false` keeps
     *   the node ON SCREEN. Hiding on an unmeasured count would be
     *   indistinguishable from hiding real transcripts, and these are
     *   exactly the transcripts that are invisible from the project tree
     *   by construction - the one population where a wrong hide is
     *   permanent. Measured 2026-09-01: corpus 1 has 0, corpus 2 has 5.
     * Inputs: row (object|null) - the corpus row carrying the count.
     * Output: {show: boolean, reason: string} - reason is for tests and
     *   for the node's own tooltip, so the decision is inspectable.
     * Example: shouldShowUnattributed({unattributed_transcript_count: 0})
     *   // -> {show: false, reason: 'known zero'}
     */
    function shouldShowUnattributed(row) {
        var r = row || {};
        if (r.counted === false) {
            return { show: true, reason: 'count could not be determined' };
        }
        var n = r.unattributed_transcript_count;
        if (typeof n !== 'number' || !isFinite(n)) {
            return { show: true, reason: 'no count was reported' };
        }
        if (n === 0) return { show: false, reason: 'known zero' };
        return { show: true, reason: 'holds ' + n };
    }

    /**
     * Description: render the host badges a merged project node carries,
     *   or null when there is nothing to say. The machine was a LEVEL in
     *   the old tree and is a badge here; dropping it entirely would
     *   discard information the merge is only allowed to demote.
     * Inputs: doc (Document), row (object).
     * Output: Element|null.
     */
    function renderHostBadges(doc, row) {
        var hosts = row && row.hosts;
        if (!Array.isArray(hosts) || hosts.length === 0) return null;
        var wrap = el(doc, 'span', ROOT_CLASS + '__hosts', null);
        for (var i = 0; i < hosts.length; i++) {
            var badge = el(doc, 'span', ROOT_CLASS + '__host-badge', String(hosts[i]));
            badge.setAttribute('title', 'also collected from ' + hosts[i]);
            wrap.appendChild(badge);
        }
        if (hosts.length > 1) {
            wrap.setAttribute('data-multi-host', 'true');
        }
        return wrap;
    }

    /**
     * Description: the id a row is addressed by, per kind. Pure.
     * Inputs: kind (string), row (object).
     * Output: number|string|null.
     */
    function idFor(kind, row) {
        var r = row || {};
        if (kind === NODE_KINDS.HOST) return r.host_id;
        if (kind === NODE_KINDS.CORPUS) return r.corpus_id;
        if (kind === NODE_KINDS.PROJECT) return r.project_id;
        return r.corpus_id;
    }

    /**
     * Description: the transcript count a row advertises, per kind.
     * Inputs: kind (string), row (object).
     * Output: number|null.
     */
    function countFor(kind, row) {
        var r = row || {};
        var v = kind === NODE_KINDS.PROJECT ? r.transcript_count
              : kind === NODE_KINDS.CORPUS ? r.transcript_count
              : kind === NODE_KINDS.UNATTRIBUTED ? r.unattributed_transcript_count
              : r.transcript_count;
        return typeof v === 'number' ? v : null;
    }

    /**
     * Description: render one row as a <li> carrying a selectable button
     *   and, for expandable kinds, an empty child slot.
     * Inputs: doc (Document), kind (string), row (object),
     *         opts (object) - {expandable: boolean, onActivate: function}
     * Output: Element - the <li>.
     */
    function renderRow(doc, kind, row, opts) {
        var options = opts || {};
        // A PROJECT IS A CARD, and the card lives in archive-nav-card.js.
        // Delegated at CALL time rather than by loading that file first,
        // so this module keeps no dependency on it and stays the thing
        // every other kind is built from. A build that forgot the card
        // file still renders projects - as the plain rows below, which
        // is degraded rather than blank.
        if (kind === NODE_KINDS.PROJECT && window.ArchiveNavCard) {
            return window.ArchiveNavCard.renderCard(doc, row, options);
        }
        var id = idFor(kind, row);
        var li = el(doc, 'li', ROOT_CLASS + '__node ' + ROOT_CLASS + '__node--' + kind, null);
        li.setAttribute('data-node-kind', kind);
        li.setAttribute('data-node-id', String(id === null || id === undefined ? '' : id));

        var btn = el(doc, 'button', ROOT_CLASS + '__row', null);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-action', options.expandable ? 'expand' : 'select');
        if (options.expandable) btn.setAttribute('aria-expanded', 'false');

        var text = labelFor(kind, row);
        var label = el(doc, 'span', ROOT_CLASS + '__label', null);
        // Labels TRUNCATE (CSS ellipsis), never wrap - a wrapped row in a
        // 272px rail pushes every sibling down and makes the tree
        // unscannable. The full text goes in `title` so nothing is lost,
        // and a parallel change makes the pane resizable, so truncation +
        // resize + tooltip is the whole answer.
        if (options.positions && options.positions.length &&
            options.matchField && window.ArchiveNavFuzzy &&
            String(row && row[options.matchField]) === text) {
            label.appendChild(window.ArchiveNavFuzzy.highlight(doc, text, options.positions));
        } else {
            label.textContent = text;
        }
        btn.appendChild(label);

        var badges = renderHostBadges(doc, row);
        if (kind === NODE_KINDS.PROJECT && badges) btn.appendChild(badges);

        btn.appendChild(el(doc, 'span', ROOT_CLASS + '__count', renderCount(countFor(kind, row))));

        // The tooltip carries the full path, the raw slug and the hosts.
        // `text` is always included so a truncated LABEL is still
        // readable on hover even when there is no extra detail.
        var tip = titleFor(kind, row);
        btn.setAttribute('title', tip ? text + '\n' + tip : text);
        if (kind === NODE_KINDS.UNATTRIBUTED) {
            // Named in words as well as by class, because this node's
            // whole purpose is that it is easy to not notice. The tone
            // follows the verdict: an ANSWERED bucket must not be painted
            // in the warning colour, or the rail keeps asking a question
            // that has already been answered and the colour stops meaning
            // anything on the nodes that are genuinely open.
            var note = unattributedNote(row);
            var cls = ROOT_CLASS + '__note' +
                      (note.answered ? ' ' + ROOT_CLASS + '__note--answered' : '');
            btn.appendChild(el(doc, 'span', cls, note.text));
        }
        if (typeof options.onActivate === 'function') {
            btn.addEventListener('click', function () { options.onActivate(kind, id, row); });
        }
        li.appendChild(btn);
        if (options.expandable) {
            li.appendChild(el(doc, 'ul', ROOT_CLASS + '__children', null));
        }
        return li;
    }


    /**
     * Description: replace a node's child slot with a rendered outcome
     *   block. Used for every non-renderable envelope at every level, so
     *   a failed expand shows the reason where the person clicked rather
     *   than looking like an empty branch.
     * Inputs: doc (Document), slot (Element), envelope (object|null),
     *         transportError (string|null).
     * Output: void.
     */
    function renderOutcomeInto(doc, slot, envelope, transportError) {
        slot.textContent = '';
        var payload = envelope;
        if (transportError) {
            // callEnvelope never rejects; it reports the failure here.
            // Handing archive-outcome.js a null envelope is exactly how
            // it produces `transport-error`, which is the correct token.
            payload = null;
        }
        var host = el(doc, 'li', ROOT_CLASS + '__outcome', null);
        var block = window.ArchiveOutcomeView.renderOutcomeBlock(payload, { document: doc });
        if (transportError) {
            block.appendChild(el(doc, 'p', ROOT_CLASS + '__transport-reason', transportError));
        }
        host.appendChild(block);
        slot.appendChild(host);
    }

    window.ArchiveNavRow = {
        el: el,
        titleFor: titleFor,
        shouldShowUnattributed: shouldShowUnattributed,
        unattributedNote: unattributedNote,
        UNATTRIBUTED_LABEL: UNATTRIBUTED_LABEL,
        renderHostBadges: renderHostBadges,
        renderCount: renderCount,
        filterRows: filterRows,
        describeFilter: describeFilter,
        labelFor: labelFor,
        idFor: idFor,
        countFor: countFor,
        renderRow: renderRow,
        renderOutcomeInto: renderOutcomeInto,
        ROOT_CLASS: ROOT_CLASS,
        NODE_KINDS: NODE_KINDS,
        NOT_KNOWN: NOT_KNOWN
    };
    console.log('[ArchiveNavRow Module] Exported as window.ArchiveNavRow');
})();
