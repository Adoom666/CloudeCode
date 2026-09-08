/**
 * HEIGHT GUESSES FOR CHAT BUBBLES, before any of them have been drawn.
 *
 * WHY THE RAW READER'S ESTIMATOR COULD NOT BE REUSED. A raw line is one
 * monospace blob whose height is a function of one number, `body_chars`.
 * A chat turn is a header, plus N blocks each with its own disclosure
 * state, plus optionally an envelope panel and a subagent list. Two
 * turns with identical total character counts differ by hundreds of
 * pixels depending on how many of those characters are inside a
 * collapsed <details>. Feeding chat turns to estimateHeight() would have
 * produced a scrollbar whose length was wrong by an order of magnitude
 * on any tool-heavy transcript, and the anti-jump compensation would
 * have spent the whole scroll paying off that error.
 *
 * THE ESTIMATE ONLY HAS TO BE CLOSE, AND THIS IS THE PART THAT IS EASY
 * TO GET WRONG BY OVERTHINKING. archive-virtual-list.js measures every
 * row it actually paints and pays the pixel delta owed by rows above the
 * viewport IN THE SAME FRAME. So an estimate's job is not to be right;
 * it is to be (a) finite, (b) positive, and (c) not so far off that the
 * scrollbar thumb is a lie before you have scrolled anywhere. Precision
 * beyond that buys nothing, because the measurement supersedes it the
 * moment the row is on screen.
 *
 * THE CAP IS LOAD-BEARING, NOT A TIDINESS RULE. The corpus holds a
 * single line of 37 MB. Estimated at one pixel per wrapped line that is
 * roughly three million pixels for ONE row, which makes every other row
 * in the transcript unreachable by dragging the scrollbar, because the
 * thumb resolves to nothing. Every turn is therefore capped, exactly as
 * the raw reader caps a collapsed body, and the cap is what keeps a
 * pathological row from eating the whole document's geometry.
 *
 * NO DOM. Pure arithmetic over the turn shape, so the whole thing is
 * testable under plain node.
 *
 * Exports window.ArchiveChatEstimate.
 */

console.log('[ArchiveChatEstimate Module] Loading...');

(function () {
    'use strict';

    /** Header strip (speaker, time, model, the two buttons). Pixels.
     *  @type {number} */
    var TURN_CHROME_PX = 46;

    /** Characters on one wrapped line of PROSE at the reader's measure.
     *  Wider than the raw reader's 96 because this text is proportional
     *  and the column is not gutter-indented. @type {number} */
    var CHARS_PER_LINE = 110;

    /** Rendered line box for prose. Pixels. @type {number} */
    var LINE_HEIGHT_PX = 21;

    /** A block that renders only its <summary> until opened: the label
     *  strip and nothing else. Pixels. @type {number} */
    var COLLAPSED_BLOCK_PX = 28;

    /** A block whose text was withheld or could not be evaluated: three
     *  short lines of stated reason. Pixels. @type {number} */
    var STATED_BLOCK_PX = 66;

    /** Ceiling for one turn before it is measured. Pixels. @type {number} */
    var TURN_MAX_PX = 420;

    /** The collapsed progress chip, which is a single control.
     *  @type {number} */
    var PROGRESS_ROW_PX = 30;

    /** The envelope panel, when open: sixteen definition rows and three
     *  headings. Pixels. @type {number} */
    var INFO_PANEL_PX = 380;

    /** One subagent row in the expanded list. Pixels. @type {number} */
    var SUBAGENT_ROW_PX = 38;

    /** The subagent list's own heading line. Pixels. @type {number} */
    var SUBAGENT_HEAD_PX = 40;

    /**
     * Description: pixels for one content block, given how this view
     *   will render it.
     * Inputs: block (object|null).
     * Output: number - pixels, always finite and positive.
     * Example: blockHeight({type: 'tool_use', text_length: 9000}) // -> 28
     */
    function blockHeight(block) {
        if (!block || typeof block !== 'object') return STATED_BLOCK_PX;
        // A collapsed block costs its summary line and nothing more,
        // however enormous its payload. This is the single biggest
        // source of error in a naive character-count estimate: a
        // 200 KB tool result is 28 pixels tall until somebody opens it.
        if (window.ArchiveChatBlock.COLLAPSED_BY_DEFAULT[String(block.type)] === true) {
            return COLLAPSED_BLOCK_PX;
        }
        var state = window.ArchiveChatBlock.textState(block);
        if (state !== window.ArchiveChatBlock.TEXT_STATES.INCLUDED) {
            return STATED_BLOCK_PX;
        }
        var n = block.text_length;
        if (!Number.isFinite(n) || n < 0) {
            n = typeof block.text === 'string' ? block.text.length : 0;
        }
        var lines = Math.ceil(n / CHARS_PER_LINE);
        if (lines < 1) lines = 1;
        return COLLAPSED_BLOCK_PX + LINE_HEIGHT_PX * lines;
    }

    /**
     * Description: pixels for one turn, before it is drawn.
     * Inputs: turn (object|null) - a turn, or a progress-run item.
     *         state (object|null) - {infoOpen, subOpen}. The open panels
     *           are part of the geometry, so a turn whose "i" is open is
     *           estimated taller rather than being corrected by a jump
     *           on the next frame.
     * Output: number - pixels, always finite and >= TURN_CHROME_PX.
     * Example: estimateTurn({role: 'user', blocks: [{type: 'text',
     *   text_length: 220}]}, null) // -> 46 + 28 + 21*2 = 116
     */
    function estimateTurn(turn, state) {
        if (!turn || typeof turn !== 'object') return TURN_CHROME_PX;
        if (turn.kind === 'progress-run') return PROGRESS_ROW_PX;
        var s = state || {};

        var h = TURN_CHROME_PX;
        var blocks = turn.blocks;
        if (!Array.isArray(blocks)) {
            // A could-not-evaluate renders an outcome block, which is
            // taller than a line of prose and shorter than a payload.
            h += STATED_BLOCK_PX * 2;
        } else if (blocks.length === 0) {
            h += STATED_BLOCK_PX;
        } else {
            for (var i = 0; i < blocks.length; i++) h += blockHeight(blocks[i]);
        }

        // The cap applies to the CONTENT, before the open panels are
        // added. A panel the reader deliberately opened must not be
        // squeezed out of the estimate by a huge block above it: the
        // whole point of the cap is that unopened content does not
        // dominate the geometry.
        if (h > TURN_MAX_PX) h = TURN_MAX_PX;

        if (s.infoOpen) h += INFO_PANEL_PX;
        if (s.subOpen) {
            var subs = Array.isArray(turn.subagents) ? turn.subagents.length : 0;
            h += SUBAGENT_HEAD_PX + SUBAGENT_ROW_PX * (subs > 0 ? subs : 2);
        }
        return h;
    }

    /**
     * Description: an estimator function bound to a list of turns and a
     *   map of per-index open state, in the shape archive-virtual-list.js
     *   wants. Built as a closure rather than passed two arguments
     *   because createList() calls `estimate(index)` and nothing else.
     * Inputs: turns (Array), stateAt (function(index): object|null).
     * Output: function(index): number.
     * Example: createList({count: n, estimate: estimator(turns, stateAt)})
     */
    function estimator(turns, stateAt) {
        return function (index) {
            var t = turns ? turns[index] : null;
            var s = typeof stateAt === 'function' ? stateAt(index) : null;
            return estimateTurn(t, s);
        };
    }

    window.ArchiveChatEstimate = {
        estimateTurn: estimateTurn,
        blockHeight: blockHeight,
        estimator: estimator,
        TURN_CHROME_PX: TURN_CHROME_PX,
        CHARS_PER_LINE: CHARS_PER_LINE,
        LINE_HEIGHT_PX: LINE_HEIGHT_PX,
        COLLAPSED_BLOCK_PX: COLLAPSED_BLOCK_PX,
        STATED_BLOCK_PX: STATED_BLOCK_PX,
        TURN_MAX_PX: TURN_MAX_PX,
        PROGRESS_ROW_PX: PROGRESS_ROW_PX,
        INFO_PANEL_PX: INFO_PANEL_PX,
        SUBAGENT_ROW_PX: SUBAGENT_ROW_PX,
        SUBAGENT_HEAD_PX: SUBAGENT_HEAD_PX
    };
    console.log('[ArchiveChatEstimate Module] Exported as window.ArchiveChatEstimate');
})();
