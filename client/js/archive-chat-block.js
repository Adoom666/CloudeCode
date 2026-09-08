/**
 * ONE CONTENT BLOCK OF ONE CHAT TURN, rendered for a person rather than
 * for a parser.
 *
 * WHY THIS IS ITS OWN FILE. It is the only place in the chat view where
 * stored bytes reach the screen, so it is the only place a credential
 * can be disclosed by this feature. Keeping it separate means the rule
 * "every renderable string goes through archive-mask.js" is checkable by
 * reading one file, and it means no other chat module has any reason to
 * touch `block.text` at all. Grep for `.text` across client/js/
 * archive-chat-*.js: it must appear here and nowhere else.
 *
 * THE MASKING CONTRACT, AND WHY IT IS DELIBERATELY PESSIMISTIC. The
 * server flags secrets and never redacts, because byte-exactness is the
 * point of the archive. archive-mask.js needs three things: the string,
 * the findings array, and an INDEPENDENT declared count. A turn carries
 * `secret_finding_count`; a block may or may not carry its own findings.
 * When a turn declares secrets and the block carries no findings array,
 * maskBody REFUSES, and this file renders the refusal. That refuses more
 * text than strictly necessary - a turn with one flagged block will
 * withhold its other blocks too - and that is the correct direction to
 * be wrong in. Half-masked output does not look like a failure; it looks
 * like a success with a short hex tail that reads as prose.
 *
 * ONLY THE UTF-16 OFFSETS ARE EVER USED, and this file never reads an
 * offset at all: it hands the findings to archive-mask.js, which is the
 * one module permitted to index into a body.
 *
 * THREE OUTCOMES PER BLOCK, NEVER TWO. A block whose text the server
 * INCLUDED renders its text. A block the server WITHHELD renders as
 * withheld, naming its length, because "too big to send" and "empty" are
 * different findings. A block whose state cannot be determined says so.
 * An empty <div> would collapse all three into "nothing here".
 *
 * NO LISTENERS. Everything interactive is a `data-action` resolved by
 * the one delegated listener in archive-chat-view.js, because rows are
 * recycled on every paint and a listener bound to one would leak.
 *
 * Depends on archive-mask.js and archive-outcome-view.js.
 * Exports window.ArchiveChatBlock.
 */

console.log('[ArchiveChatBlock Module] Loading...');

(function () {
    'use strict';

    /** @type {string} */
    var ROOT_CLASS = 'archive-chat-block';

    /**
     * Text states this module understands. The server names them; these
     * are the values it may name. Anything else is a could-not-determine
     * rather than a guess.
     * @type {Object<string,string>}
     */
    var TEXT_STATES = {
        INCLUDED: 'included',
        WITHHELD: 'withheld',
        UNKNOWN: 'cannot-determine'
    };

    /**
     * How each block type is introduced in the header strip. A type not
     * in this table is rendered under its own raw name rather than
     * silently dropped: an unrecognised block is still content somebody
     * needs to know arrived.
     * @type {Object<string,string>}
     */
    var TYPE_LABELS = {
        text: 'Text',
        // The server's name for a message whose `content` was a bare
        // JSON string rather than a block array. It is prose, and it is
        // by far the most common shape of a user turn, so it must not
        // render under an internal-looking underscore name.
        _string_content: 'Text',
        thinking: 'Thinking',
        redacted_thinking: 'Thinking (redacted by the model)',
        tool_use: 'Tool call',
        tool_result: 'Tool result',
        image: 'Image',
        document: 'Document'
    };

    /**
     * Block types whose text is COLLAPSED by default. Prose is the thing
     * the reader came for; a tool payload and a thinking trace are the
     * envelope around it, reachable in one click and not in the way.
     * @type {Object<string,boolean>}
     */
    var COLLAPSED_BY_DEFAULT = {
        thinking: true,
        redacted_thinking: true,
        tool_use: true,
        tool_result: true
    };

    /**
     * Description: element with a class and optional text.
     * Inputs: doc (Document), tag (string), cls (string|null),
     *   text (string|null).
     * Output: Element.
     * Example: el(doc, 'p', 'x__note', 'hello')
     */
    function el(doc, tag, cls, text) {
        var n = doc.createElement(tag);
        if (cls) n.setAttribute('class', cls);
        if (text !== null && text !== undefined) n.textContent = String(text);
        return n;
    }

    /**
     * Description: decide, from whatever the server sent, whether this
     *   block's text was INCLUDED, WITHHELD, or cannot be determined.
     *   Tolerant of the field name because the contract names the field
     *   only by its meaning; intolerant of guessing, because a wrong
     *   guess here renders a withheld block as an empty one.
     * Inputs: block (object) - one entry from a turn's `blocks`.
     * Output: string - one of TEXT_STATES.
     * Example: textState({text: 'hi'}) // -> 'included'
     */
    function textState(block) {
        var raw = block.text_state || block.state || block.body_state || null;
        if (typeof raw === 'string') {
            if (raw === 'included') return TEXT_STATES.INCLUDED;
            // Every withheld_* spelling the archive uses means the same
            // thing to a reader: the server has it and chose not to send
            // it. The REASON is rendered from the raw value below.
            if (raw.indexOf('withheld') === 0) return TEXT_STATES.WITHHELD;
            if (raw === 'not_requested') return TEXT_STATES.WITHHELD;
            return TEXT_STATES.UNKNOWN;
        }
        // No state field. Derive only where the derivation is total.
        if (typeof block.text === 'string') return TEXT_STATES.INCLUDED;
        var n = block.text_length;
        if (Number.isFinite(n) && n > 0) return TEXT_STATES.WITHHELD;
        if (n === 0) return TEXT_STATES.INCLUDED;
        return TEXT_STATES.UNKNOWN;
    }

    /**
     * Description: how many secret findings the server believes are in
     *   this block's text. A block-level count wins; otherwise the
     *   TURN's count is used, which is the pessimistic choice explained
     *   in the header.
     * Inputs: block (object), turn (object|null).
     * Output: number - always an integer, never NaN.
     */
    function declaredSecrets(block, turn) {
        if (Number.isInteger(block.secret_finding_count)) {
            return block.secret_finding_count;
        }
        if (!turn || !Number.isInteger(turn.secret_finding_count) ||
                turn.secret_finding_count <= 0) {
            return 0;
        }
        // THE TURN'S COUNT ONLY POISONS BLOCKS WHOSE SECRETS ARE STILL
        // UNLOCATED. Measured live 2026-09-01: this server WITHHOLDS the
        // text of any block it flagged, reporting
        // `text_state: withheld_secret_bearing` and the full length. When
        // it has done that, it has told us exactly where the secrets are,
        // and the turn's other blocks are not carrying an unlocated one -
        // so refusing them too would withhold prose for no safety gain.
        // When NO block is withheld for that reason, the count is a
        // credential at an unknown position and every block in the turn
        // is refused, which is the conservative direction and the one to
        // fail toward.
        if (_turnWithholdsForSecrets(turn)) return 0;
        return turn.secret_finding_count;
    }

    /**
     * Description: has the server already withheld a block of this turn
     *   BECAUSE it was secret-bearing? That is the server locating the
     *   secrets for us.
     * Inputs: turn (object).
     * Output: boolean.
     */
    function _turnWithholdsForSecrets(turn) {
        var blocks = turn.blocks;
        if (!Array.isArray(blocks)) return false;
        for (var i = 0; i < blocks.length; i++) {
            var raw = blocks[i] &&
                (blocks[i].text_state || blocks[i].state || blocks[i].body_state);
            if (typeof raw === 'string' && raw.indexOf('secret') !== -1 &&
                    raw.indexOf('withheld') === 0) {
                return true;
            }
        }
        return false;
    }

    /**
     * Description: the findings array to mask this block with, if any.
     *   Block-level first, then the turn's. `null` and `[]` are NOT the
     *   same to archive-mask.js and are passed through unflattened.
     * Inputs: block (object), turn (object|null).
     * Output: Array|null.
     */
    function findingsFor(block, turn) {
        if (Array.isArray(block.secrets)) return block.secrets;
        if (turn && Array.isArray(turn.secrets)) return turn.secrets;
        return null;
    }

    /**
     * Description: render the WITHHELD state. Names the length so the
     *   reader knows the size of what they are not being shown, and
     *   names the server's own word for why.
     * Inputs: doc (Document), block (object).
     * Output: Element.
     */
    function renderWithheld(doc, block) {
        var box = el(doc, 'div', ROOT_CLASS + '__withheld', null);
        box.setAttribute('data-text-state', 'withheld');
        var n = block.text_length;
        box.appendChild(el(doc, 'p', ROOT_CLASS + '__withheld-head',
            'WITHHELD BY THE SERVER. Not empty: this block has content ' +
            'that was not sent.'));
        box.appendChild(el(doc, 'p', ROOT_CLASS + '__withheld-size',
            Number.isFinite(n)
                ? 'Length: ' + n + ' characters.'
                : 'Length: NOT KNOWN.'));
        var raw = block.text_state || block.state || block.body_state;
        box.appendChild(el(doc, 'p', ROOT_CLASS + '__withheld-why',
            'Server reason: ' + (raw ? String(raw) : 'NOT STATED')));
        return box;
    }

    /**
     * Description: render a mask REFUSAL. The body never appears. The
     *   reason carries counts and offsets only, never matched text, by
     *   construction in archive-mask.js.
     * Inputs: doc (Document), masked (object) - a maskBody refusal.
     * Output: Element.
     */
    function renderRefusal(doc, masked) {
        var box = el(doc, 'div', ROOT_CLASS + '__refused', null);
        box.setAttribute('data-text-state', 'mask-refused');
        box.setAttribute('role', 'status');
        box.appendChild(el(doc, 'p', ROOT_CLASS + '__refused-head',
            'NOT SHOWN. This text carries ' + masked.findingCount +
            ' flagged secret(s) that could not be masked safely, so none ' +
            'of it is rendered.'));
        box.appendChild(el(doc, 'p', ROOT_CLASS + '__refused-why',
            'Reason: ' + masked.reason));
        return box;
    }

    /**
     * Description: render the could-not-determine state through the
     *   shared outcome view, so it cannot drift from every other
     *   third-outcome in this screen.
     * Inputs: doc (Document), block (object).
     * Output: Element.
     */
    function renderUnknown(doc, block) {
        return window.ArchiveOutcomeView.renderOutcomeBlock({
            result: null,
            result_status: 'cannot_determine',
            scope_status: 'resolved',
            unevaluated: [{
                subject: 'block ' + (Number.isInteger(block.seq) ? block.seq : '?') +
                    ' of type ' + String(block.type),
                reason: 'the server did not say whether this block\'s text was ' +
                    'included or withheld, and the block carries no text, so ' +
                    'whether there is content here is NOT KNOWN'
            }],
            meta: {}
        }, { document: doc, omitActions: true });
    }

    /**
     * Description: the masked, safe-to-render text element, or the
     *   refusal that replaces it.
     * Inputs: doc (Document), block (object), turn (object|null).
     * Output: Element.
     */
    function renderText(doc, block, turn) {
        var masked = window.ArchiveMask.maskBody(
            block.text, findingsFor(block, turn), declaredSecrets(block, turn));
        if (masked.status === window.ArchiveMask.MASK_REFUSED) {
            return renderRefusal(doc, masked);
        }
        var pre = el(doc, 'div', ROOT_CLASS + '__text', masked.text);
        pre.setAttribute('data-text-state', 'included');
        if (masked.masked > 0) {
            pre.setAttribute('data-masked-count', String(masked.masked));
        }
        if (block.text_truncated !== true) return pre;

        // TRUNCATION IS A PARTIAL ANSWER AND MUST SAY SO. Measured live
        // 2026-09-01 on transcript 4: 143 of 400 blocks came back
        // truncated at the server's 400-character preview gate. Text that
        // simply stops, with no marker, reads as the whole thing - the
        // reader draws a conclusion from an excerpt believing they saw
        // all of it, and nothing anywhere says otherwise. So the excerpt
        // is wrapped and the shortfall is stated in characters.
        var wrap = el(doc, 'div', ROOT_CLASS + '__truncated', null);
        wrap.setAttribute('data-truncated', 'true');
        wrap.appendChild(pre);
        var shown = typeof masked.text === 'string' ? masked.text.length : null;
        var full = block.text_length;
        wrap.appendChild(el(doc, 'p', ROOT_CLASS + '__truncated-note',
            'TRUNCATED BY THE SERVER. ' +
            (Number.isFinite(full) && Number.isFinite(shown)
                ? 'This is about ' + shown + ' of ' + full + ' characters; ' +
                  'the rest was not sent.'
                : 'Part of this block was not sent; how much is NOT KNOWN.') +
            ' Open the raw view for the whole record.'));
        return wrap;
    }

    /**
     * Description: the one-line header strip that names what a block is
     *   before you open it: its type, its tool, and whether the tool
     *   errored. Rendered for EVERY block including plain text, because
     *   a reader scanning a conversation needs to tell prose from a tool
     *   payload without opening either.
     * Inputs: doc (Document), block (object).
     * Output: Element.
     */
    function renderLabel(doc, block) {
        var strip = el(doc, 'span', ROOT_CLASS + '__label', null);
        var type = String(block.type);
        strip.appendChild(el(doc, 'span', ROOT_CLASS + '__type',
            TYPE_LABELS[type] || type));
        if (block.tool_name) {
            strip.appendChild(el(doc, 'span', ROOT_CLASS + '__tool',
                String(block.tool_name)));
        }
        if (block.is_error === true) {
            // Named in TEXT, not only coloured: three themes zero every
            // radius token and colour alone is not a signal anyone can
            // rely on.
            var bad = el(doc, 'span', ROOT_CLASS + '__error', 'ERROR');
            bad.setAttribute('data-error', 'true');
            strip.appendChild(bad);
        }
        if (Number.isFinite(block.text_length)) {
            strip.appendChild(el(doc, 'span', ROOT_CLASS + '__len',
                block.text_length + ' chars'));
        }
        return strip;
    }

    /**
     * Description: render one content block whole.
     *
     *   PROSE IS OPEN, ENVELOPE IS CLOSED. A `text` block renders its
     *   text directly. Thinking, tool calls and tool results render
     *   inside a <details> that is collapsed by default, so the
     *   conversation reads as a conversation and the machinery is one
     *   click away. That is the owner's rule - not information overload,
     *   but everything reachable - expressed as markup.
     * Inputs: doc (Document) - REQUIRED.
     *         block (object) - {seq, type, text, text_length, tool_name,
     *           tool_use_id, is_error, text_state}.
     *         turn (object|null) - the owning turn, read ONLY for its
     *           secret count and findings.
     *         options (object|null) - {forceOpen: boolean} to render a
     *           normally-collapsed block already open.
     * Output: Element - a <div data-block-type=... data-block-seq=...>.
     * Example:
     *   renderBlock(doc, {seq: 0, type: 'text', text: 'hi'}, turn)
     */
    function renderBlock(doc, block, turn, options) {
        if (!doc) throw new Error('ArchiveChatBlock.renderBlock needs a document');
        var opts = options || {};
        var b = (block && typeof block === 'object') ? block : {};
        var root = el(doc, 'div', ROOT_CLASS, null);
        root.setAttribute('data-block-type', String(b.type));
        if (Number.isInteger(b.seq)) {
            root.setAttribute('data-block-seq', String(b.seq));
        }
        if (b.tool_use_id) root.setAttribute('data-tool-use-id', String(b.tool_use_id));

        var state = textState(b);
        root.setAttribute('data-text-state', state);

        var content;
        if (state === TEXT_STATES.WITHHELD) content = renderWithheld(doc, b);
        else if (state === TEXT_STATES.UNKNOWN) content = renderUnknown(doc, b);
        else content = renderText(doc, b, turn);

        var collapsed = COLLAPSED_BY_DEFAULT[String(b.type)] === true &&
            opts.forceOpen !== true;

        if (!collapsed) {
            root.appendChild(renderLabel(doc, b));
            root.appendChild(content);
            return root;
        }

        var det = el(doc, 'details', ROOT_CLASS + '__disclosure', null);
        var sum = el(doc, 'summary', ROOT_CLASS + '__summary', null);
        sum.appendChild(renderLabel(doc, b));
        det.appendChild(sum);
        det.appendChild(content);
        root.appendChild(det);
        return root;
    }

    window.ArchiveChatBlock = {
        renderBlock: renderBlock,
        textState: textState,
        declaredSecrets: declaredSecrets,
        ROOT_CLASS: ROOT_CLASS,
        TEXT_STATES: TEXT_STATES,
        TYPE_LABELS: TYPE_LABELS,
        COLLAPSED_BY_DEFAULT: COLLAPSED_BY_DEFAULT
    };
    console.log('[ArchiveChatBlock Module] Exported as window.ArchiveChatBlock');
})();
