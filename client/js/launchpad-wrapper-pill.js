/**
 * LaunchpadWrapperPill - names the launch WRAPPER a session started from.
 *
 * WHY A SECOND PILL. The family pill next to it answers "what kind of
 * agent is in this pane" and has five possible answers. It cannot answer
 * the question the owner actually asked on the home screen, which is
 * WHICH claude: a session started through `claude (chrome)` and one
 * started through `claude` render the identical family pill, because
 * they are the identical family. The wrapper is the user's own launch
 * choice, and it is the only thing on the row that distinguishes them.
 *
 * WHEN IT RENDERS NOTHING, AND WHY THAT IS NOT THE UNKNOWN CASE. The
 * server sends `agent_wrapper_label: null` whenever no configured
 * wrapper can be named for a session - the agent_type was fingerprinted
 * from scrollback (a scan reads a banner, and every claude wrapper
 * prints the same one), or it is a bare family name like `shell`, or it
 * names a wrapper the user has since deleted from config. This module
 * returns the EMPTY STRING for all of those.
 *
 * That is deliberate, and it is the opposite of the family pill's rule.
 * The family pill must render "unknown family" out loud, because the
 * question "what is running here" always has an answer and failing to
 * know it is information. "Which wrapper" does not always have an
 * answer: a session genuinely launched as a bare shell was launched
 * through no wrapper at all, and a pill reading "unknown wrapper" on it
 * would report a gap where there is none. Absence of a wrapper and
 * absence of knowledge about one are both rendered as nothing here, and
 * the family pill standing beside it is what tells the two apart -
 * `unknown family` means we could not tell, anything else means we
 * could.
 *
 * NEVER RENDERS THE RAW agent_type. The id (`claude-chrome`) is an
 * internal key the user never chose the spelling of and which doubles as
 * a filename; the label (`claude (chrome)`) is what they typed into
 * settings. Falling back to the id would put a different string on the
 * home screen from the one in the wrappers editor for the same wrapper.
 * The server already falls the label back to the id when a wrapper's
 * label is blank, so by the time a value reaches here it is the best
 * name that exists.
 */
(function () {
    'use strict';

    /**
     * HTML-escape a string for use in an attribute or as text.
     * @param {string} text  Raw value, possibly from config.
     * @returns {string} The escaped value.
     */
    function escAttr(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Render the wrapper pill for one session row, or nothing.
     *
     * @param {string|null|undefined} label  `agent_wrapper_label` from
     *   `/sessions/list` or `/sessions/attachable`. Null, undefined, a
     *   non-string and a blank/whitespace-only string all render nothing.
     * @returns {string} One `<span class="wrapper-pill">` element, or ''.
     * @example
     *   LaunchpadWrapperPill.html('claude (chrome)')
     *     -> '<span class="wrapper-pill" ...>claude (chrome)</span>'
     *   LaunchpadWrapperPill.html(null) -> ''
     */
    function html(label) {
        if (typeof label !== 'string') return '';
        var text = label.trim();
        if (!text) return '';
        var title = 'launch wrapper: ' + text;
        return (
            '<span class="wrapper-pill" title="' + escAttr(title) + '"' +
            ' aria-label="' + escAttr(title) + '">' + escAttr(text) + '</span>'
        );
    }

    window.LaunchpadWrapperPill = { html: html };
})();
