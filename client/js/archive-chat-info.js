/**
 * THE "i" PANEL: the envelope of one chat turn, on demand.
 *
 * WHY THE ENVELOPE IS HIDDEN BY DEFAULT AND WHY IT IS NOT DELETED. The
 * complaint that produced this whole view was "im looking at a bunch of
 * raw json so it does not read properly". The fix is not to throw the
 * uuids and token counts away - they are how you correlate a turn with a
 * subagent, with a log line, or with a bug report. The fix is to move
 * them behind one affordance per turn. Not information overload, but the
 * ability to grab as much info as properly.
 *
 * EVERY FIELD IS ALWAYS RENDERED, EVEN WHEN ABSENT. A field the server
 * did not send renders as `NOT KNOWN`, never as a blank cell and never
 * by being omitted from the list. A missing row and a row with no value
 * look identical to a reader, and they mean different things: one is
 * "this turn has no model" and the other is "nobody told me". This panel
 * is the place people come to when something is confusing, so it is the
 * last place that may quietly drop a fact.
 *
 * NOTHING HERE IS MASKED, BECAUSE NOTHING HERE IS BODY TEXT. The fields
 * are identifiers, timestamps, a model name and integer counts. If the
 * server ever adds a free-text field to `info`, it must be routed
 * through archive-mask.js like every other renderable string - see the
 * UNKNOWN-KEY branch below, which renders extra keys as text and is the
 * one place that rule could be broken by a future server change.
 *
 * NO LISTENERS. The "i" button lives on the turn and is resolved by the
 * one delegated listener in archive-chat-view.js.
 *
 * Depends on nothing but a Document. Exports window.ArchiveChatInfo.
 */

console.log('[ArchiveChatInfo Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-info';

    /** The string rendered wherever a fact was not supplied. Uppercase
     *  and unmistakable, so a scan of the panel finds the gaps.
     *  @type {string} */
    var NOT_KNOWN = 'NOT KNOWN';

    /**
     * The envelope fields, in the order a person reads them: what this
     * turn IS, then where it sits, then what it cost. `key` is looked up
     * on the turn first and on `turn.info` second, so the server may put
     * a field in either place without this file caring.
     * @type {Array<{key: string, label: string}>}
     */
    var FIELDS = [
        { key: 'message_uuid', label: 'uuid' },
        { key: 'parent_uuid', label: 'parent uuid' },
        { key: 'role', label: 'role' },
        // WHETHER THE ROLE IS A ROLE. Measured live 2026-09-01: this
        // server reports `role_state: 'record_type_fallback'` for records
        // that carry no role of their own, and it fills the role in from
        // the record type. A reader who thinks "system" was declared
        // when it was inferred is being told something nobody measured,
        // so the derivation is stated beside the value.
        { key: 'role_state', label: 'role came from' },
        { key: 'record_type', label: 'record type' },
        { key: 'model', label: 'model' },
        { key: 'ts', label: 'timestamp' },
        { key: 'line_no', label: 'line number' },
        { key: 'seq_in_file', label: 'sequence in file' },
        { key: 'body_id', label: 'body id' },
        { key: 'body_chars', label: 'body characters' },
        { key: 'line_status', label: 'line status' },
        { key: 'fidelity_outcome', label: 'fidelity' },
        { key: 'origin_session_ref', label: 'origin session' },
        { key: 'agent_id', label: 'agent id' },
        { key: 'is_sidechain', label: 'is a sidechain' },
        { key: 'blocks_state', label: 'blocks came from' },
        { key: 'subagents_state', label: 'subagent lookup' },
        { key: 'secret_finding_count', label: 'flagged secrets' }
    ];

    /**
     * Token accounting, kept as its own group because it answers a
     * different question from the identifiers above it and because a
     * cache-read count sitting in a list of uuids reads as noise.
     * @type {Array<{key: string, label: string}>}
     */
    var USAGE_FIELDS = [
        // `state` and `reason` FIRST, because they are what makes a row
        // of nulls readable. The server reports
        // `usage.state: 'not_recorded'` with a reason in words for every
        // message that carries no usage object, and four blank counts
        // with no explanation would read as a bug in this panel.
        { key: 'state', label: 'usage recorded' },
        { key: 'reason', label: 'why not' },
        { key: 'input_tokens', label: 'input tokens' },
        { key: 'output_tokens', label: 'output tokens' },
        { key: 'cache_creation_input_tokens', label: 'cache write tokens' },
        { key: 'cache_read_input_tokens', label: 'cache read tokens' }
    ];

    /**
     * Description: element with a class and optional text.
     * Inputs: doc (Document), tag (string), cls (string|null), text.
     * Output: Element.
     */
    function el(doc, tag, cls, text) {
        var n = doc.createElement(tag);
        if (cls) n.setAttribute('class', cls);
        if (text !== null && text !== undefined) n.textContent = String(text);
        return n;
    }

    /**
     * Description: read one field off the turn, then off `turn.info`.
     *   Two lookups rather than one so the server contract can put a
     *   field in either place; `undefined` from both means NOT KNOWN.
     * Inputs: turn (object), key (string).
     * Output: * - the raw value, or undefined.
     * Example: pick({info: {model: 'x'}}, 'model') // -> 'x'
     */
    function pick(turn, key) {
        if (!turn || typeof turn !== 'object') return undefined;
        if (turn[key] !== undefined && turn[key] !== null) return turn[key];
        var info = turn.info;
        if (info && typeof info === 'object' && info[key] !== undefined &&
                info[key] !== null) {
            return info[key];
        }
        // THE ENVELOPE IS NESTED, AND EVERY NEST IS SEARCHED. Measured
        // live 2026-09-01, this server groups the fields under
        // `info.usage`, `info.line` and `info.body`. A lookup that only
        // read the top level would render two thirds of the envelope as
        // NOT KNOWN while the server had sent every one of them - a gap
        // manufactured by this file, reported as a gap in the data.
        var nests = ['usage', 'line', 'body'];
        for (var i = 0; i < nests.length; i++) {
            var nest = info && typeof info === 'object' ? info[nests[i]] : null;
            if (nest && typeof nest === 'object' && nest[key] !== undefined &&
                    nest[key] !== null) {
                return nest[key];
            }
        }
        return undefined;
    }

    /**
     * Description: render a value for display. An object or array is
     *   JSON, because a `[object Object]` in an info panel is a fact
     *   destroyed rather than shown; everything else is its string.
     * Inputs: v (*) - any value.
     * Output: string.
     */
    function display(v) {
        if (v === undefined || v === null || v === '') return NOT_KNOWN;
        if (typeof v === 'object') {
            try { return JSON.stringify(v); }
            catch (e) { return NOT_KNOWN + ' (value could not be serialised)'; }
        }
        return String(v);
    }

    /**
     * Description: one definition-list row. Marked `data-known` so a
     *   test, and a stylesheet, can tell a fact from a gap without
     *   parsing the copy.
     * Inputs: doc (Document), label (string), value (*).
     * Output: DocumentFragment-less pair appended by the caller.
     */
    function appendRow(doc, list, label, value) {
        var dt = el(doc, 'dt', ROOT_CLASS + '__key', label);
        var known = value !== undefined && value !== null && value !== '';
        var dd = el(doc, 'dd', ROOT_CLASS + '__value', display(value));
        dd.setAttribute('data-known', known ? 'true' : 'false');
        dd.setAttribute('data-field', label);
        list.appendChild(dt);
        list.appendChild(dd);
    }

    /**
     * Description: build the envelope panel for one turn.
     * Inputs: doc (Document) - REQUIRED.
     *         turn (object|null) - the turn. `null` is a real input: it
     *           means the panel was asked for and there is no turn, and
     *           it renders as such rather than as an empty box.
     * Output: Element - a <div class="archive-chat-info" role="group">.
     * Example: renderInfo(doc, {uuid: 'a', model: 'claude-opus-5'})
     */
    function renderInfo(doc, turn) {
        if (!doc) throw new Error('ArchiveChatInfo.renderInfo needs a document');
        var root = el(doc, 'div', ROOT_CLASS, null);
        root.setAttribute('role', 'group');
        root.setAttribute('aria-label', 'Message envelope detail');

        if (!turn || typeof turn !== 'object') {
            root.appendChild(el(doc, 'p', ROOT_CLASS + '__none',
                'NOT KNOWN. The envelope was requested for a turn this view ' +
                'does not hold.'));
            return root;
        }

        root.appendChild(el(doc, 'p', ROOT_CLASS + '__head',
            'Envelope detail. Every field the server sent, and every ' +
            'field it did not.'));

        var list = el(doc, 'dl', ROOT_CLASS + '__list', null);
        var i;
        for (i = 0; i < FIELDS.length; i++) {
            appendRow(doc, list, FIELDS[i].label, pick(turn, FIELDS[i].key));
        }
        root.appendChild(list);

        var usage = el(doc, 'dl', ROOT_CLASS + '__usage', null);
        for (i = 0; i < USAGE_FIELDS.length; i++) {
            appendRow(doc, usage, USAGE_FIELDS[i].label,
                pick(turn, USAGE_FIELDS[i].key));
        }
        root.appendChild(el(doc, 'p', ROOT_CLASS + '__usage-head', 'Token usage'));
        root.appendChild(usage);

        var extra = extraKeys(turn);
        if (extra.length) {
            root.appendChild(el(doc, 'p', ROOT_CLASS + '__extra-head',
                'Other fields this view does not have a name for'));
            var more = el(doc, 'dl', ROOT_CLASS + '__extra', null);
            for (i = 0; i < extra.length; i++) {
                appendRow(doc, more, extra[i], turn.info[extra[i]]);
            }
            root.appendChild(more);
        }
        return root;
    }

    /**
     * Description: keys on `turn.info` this file has no row for. Shown
     *   rather than dropped: a server that grows a field must not have
     *   it silently disappear, which is how a fact becomes invisible for
     *   a year. `usage` is excluded because its contents are rendered
     *   above under their own names.
     * Inputs: turn (object).
     * Output: Array<string> - sorted, so the panel is stable.
     */
    function extraKeys(turn) {
        var info = turn.info;
        if (!info || typeof info !== 'object' || Array.isArray(info)) return [];
        var named = {};
        var i;
        for (i = 0; i < FIELDS.length; i++) named[FIELDS[i].key] = true;
        for (i = 0; i < USAGE_FIELDS.length; i++) named[USAGE_FIELDS[i].key] = true;
        // The three nests are rendered above under their own names, so
        // listing them again as raw JSON would be the same facts twice.
        named.usage = true;
        named.line = true;
        named.body = true;
        return Object.keys(info).filter(function (k) {
            return named[k] !== true;
        }).sort();
    }

    window.ArchiveChatInfo = {
        renderInfo: renderInfo,
        pick: pick,
        display: display,
        extraKeys: extraKeys,
        ROOT_CLASS: ROOT_CLASS,
        NOT_KNOWN: NOT_KNOWN,
        FIELDS: FIELDS,
        USAGE_FIELDS: USAGE_FIELDS
    };
    console.log('[ArchiveChatInfo Module] Exported as window.ArchiveChatInfo');
})();
