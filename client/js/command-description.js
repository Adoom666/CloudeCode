/**
 * Command-description shortening for the slash-command list.
 *
 * THE LABEL IS NOT THE VALUE. There is exactly ONE description per
 * command - the full one the server sends, from scraped docs or a skill's
 * own frontmatter. This module derives a DISPLAY form from it. It never
 * replaces it, and nothing downstream may search, copy or store the
 * derived string:
 *
 *   - the full text stays on the row as `data-description`
 *   - client/js/slash-command-filter.js indexes THAT, never the rendered
 *     text, so a word that only appears in the truncated-away tail still
 *     matches
 *   - a "more" control reveals the full text in place, because a phone
 *     has no hover and a `title` attribute there is a control the user can
 *     never see
 *
 * Same rule (and the same reason) as client/js/copy-output.js's chip
 * shortening - see the "THE LABEL IS NOT THE VALUE" assertion in
 * tests/test_copy_output.node.mjs.
 *
 * WHY SHORTEN AT ALL: the list carries ~145 commands, and a scraped
 * description runs to 240 characters. At 375px that is seven wrapped lines
 * per row, so the list "goes forever" and the command names stop being
 * scannable. Desktop has the width and keeps more.
 *
 * Loaded BEFORE slash-command-filter.js and slash-commands.js, both of
 * which call window.CommandDescription.
 */

console.log('[CommandDescription Module] Loading...');

(function () {
    // Display caps, in characters of the FULL description. Phone first:
    // at 375px a description line box is about 234px, which fits roughly
    // 36 characters at the list's 0.85rem, so 90 is about two and a half
    // lines - enough to say what a command does, short enough that ten
    // rows still fit on a screen. The wide cap is the server's own
    // MAX_DESCRIPTION_LENGTH (slash_command_discovery.MAX_DESCRIPTION_LENGTH),
    // i.e. desktop truncates nothing that was not already truncated.
    const NARROW_MAX_CHARS = 90;
    const WIDE_MAX_CHARS = 240;
    // Viewport at or below which the narrow cap applies. Matches the
    // phone breakpoint slash-command-chips.css already uses.
    const NARROW_VIEWPORT_MAX_PX = 600;

    const ELLIPSIS = '...';

    /**
     * Pick the display cap for the current viewport.
     * Inputs: viewportWidth (number|undefined) - CSS px; defaults to
     *   window.innerWidth, or the wide cap when there is no window.
     * Output: number - maximum characters of the full description to show.
     */
    function maxChars(viewportWidth) {
        const w = typeof viewportWidth === 'number'
            ? viewportWidth
            : (typeof window !== 'undefined' ? window.innerWidth : 0);
        if (!w) return WIDE_MAX_CHARS;
        return w <= NARROW_VIEWPORT_MAX_PX ? NARROW_MAX_CHARS : WIDE_MAX_CHARS;
    }

    /**
     * Derive the display form of one description.
     *
     * Cuts at a WORD boundary, never mid-word, and always marks the cut
     * with an ellipsis so the reader can tell there is more. Purely
     * functional: the input string is never mutated and the result is
     * never written back anywhere the full value is expected.
     *
     * Inputs:
     *   text (string) - the full description.
     *   limit (number|undefined) - character cap; defaults to the cap for
     *     the current viewport.
     * Output: string - `text` unchanged when it already fits, otherwise a
     *   word-boundary prefix followed by "...".
     * Example: shorten('manage subagents and their tools', 20)
     *   -> 'manage subagents...'
     */
    function shorten(text, limit) {
        const full = String(text == null ? '' : text).trim();
        const cap = typeof limit === 'number' ? limit : maxChars();
        if (full.length <= cap) return full;

        // Reserve room for the ellipsis so the rendered string never
        // exceeds the cap the caller asked for.
        const budget = Math.max(1, cap - ELLIPSIS.length);
        let cut = full.slice(0, budget);
        const lastSpace = cut.lastIndexOf(' ');
        if (lastSpace > 0) cut = cut.slice(0, lastSpace);
        // Trailing punctuation before an ellipsis reads as a typo.
        cut = cut.replace(/[\s.,;:!?-]+$/, '');
        return `${cut}${ELLIPSIS}`;
    }

    /**
     * True when `shorten()` would actually drop something - i.e. when a
     * "more" control is worth rendering.
     * Inputs: text (string); limit (number|undefined) - as shorten().
     * Output: bool.
     */
    function isShortened(text, limit) {
        const full = String(text == null ? '' : text).trim();
        return shorten(full, limit) !== full;
    }

    window.CommandDescription = { shorten, isShortened, maxChars };
    console.log('[CommandDescription Module] Exported as window.CommandDescription');
})();
