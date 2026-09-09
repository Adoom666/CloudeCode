/**
 * The app's own build number, wherever this app shows it.
 *
 * WHY IT EXISTS. The request behind this file, verbatim: "The current
 * version number should be in small grey text in the slide out menu at
 * the bottom and at the bottom of the main screen so i know which one
 * i'm on." Two placements, one answer - so this module is the ONE place
 * that decides what that answer is, and every caller renders exactly
 * what it returns rather than reading the version itself.
 *
 * WHERE THE VALUE COMES FROM, AND WHY THIS FILE DOES NOT FETCH IT. The
 * server has exactly one version resolver, `src/core/version.py`'s
 * `resolve_version()`. `src/main.py` calls it once at process start and
 * stamps the result into `<meta name="cloude-app-version">` in
 * client/index.html (see that tag's own comment for the substitution
 * mechanics) - the same value `GET /api/v1/version` reports, because
 * both read the one resolver. Reading the meta tag costs no network
 * request, cannot race the page's first paint, and cannot disagree with
 * the endpoint. A second module that instead called `GET /api/v1/version`
 * would be a SECOND way for this page to learn its own version - exactly
 * the two-implementations-of-one-fact failure class this codebase keeps
 * paying for (see CLAUDE.md's account of the project-attribution pair
 * and the two-stylesheets-painting-one-dot LED bug). The Electron tray
 * already polls that endpoint every 20 seconds for its own reason (an
 * orphaned old server answering under a new bundle); this module adds no
 * second poll and no first one, because the value cannot change while
 * this page is open without a full reload re-stamping the tag.
 *
 * THE UNRESOLVED CASE IS NAMED, NOT BLANK. `resolve_version()` returns
 * "" when nothing resolved (no env var, no VERSION file, no git tag, no
 * package.json - a server started outside Electron with a checkout that
 * carries no tag), and until this file existed the home bar's chip
 * quietly disappeared in that case (`.home-bar__version:empty`). That
 * reads as "the control is missing", not "the version is unknown", and
 * it defeats the one thing this whole feature is for - the user cannot
 * tell which build they are on when it matters most, which is the one
 * case the resolver could not pin it down. `UNKNOWN_TEXT` renders in the
 * exact same small grey style as a real version, so it reads as a quiet
 * fact, not an error banner.
 *
 * ONE FUNCTION, TWO CALLERS. `versionSpanHtml()` is the whole component -
 * the text and the honest-fallback decision - and both placements call
 * it: `client/js/launchpad.js` `renderHomeBarVersion()` writes it
 * straight into the home bar's chip, and `client/js/session-sidebar-rows.js`
 * `footerHtml()` gets it wrapped in the sidebar's own footer chrome via
 * `sidebarFooterHtml()` below. Neither caller formats a version string of
 * its own.
 *
 * Depends on nothing. Must load before launchpad.js and
 * session-sidebar-rows.js, its two callers.
 */

console.log('[VersionFooter Module] Loading...');

(function () {
    /** @type {string} The meta tag src/main.py stamps the version into. */
    const META_NAME = 'cloude-app-version';

    /**
     * The honest label for "the resolver could not determine a version".
     * Lowercase, plain, matching this app's UI voice - it is a fact being
     * reported, not a warning.
     * @type {string}
     */
    const UNKNOWN_TEXT = 'version unknown';

    /**
     * The meta tag's content, read once and kept.
     *
     * `null` until the first read; after that, always a string (possibly
     * ""). The tag is stamped server-side before any script runs and
     * never changes for the life of the page, so re-querying the DOM on
     * every render would cost real work for an answer that cannot move.
     * @type {string|null}
     */
    let cachedVersion = null;

    /**
     * HTML-escape a value for safe interpolation.
     *
     * Deliberately string-only, with no DOM dependency, so the node test
     * exercises the real function rather than a stand-in - the same
     * reasoning `client/js/server-status-format.js` gives for its own
     * copy of this helper. This file does not import that one because
     * nothing in this codebase does; there is no bundler, and every
     * IIFE here is self-contained by construction.
     * @param {*} value - anything; null and undefined become ''.
     * @returns {string} escaped text.
     */
    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Read the resolved version text off the page's meta tag, once.
     *
     * A missing tag, a missing `document`, or a blocked DOM read all
     * degrade to "" - the same empty answer `resolve_version()` itself
     * gives when it cannot determine a version - rather than throwing.
     * @returns {string} "v1.0.35"-shaped text, or "" when unresolved.
     * @example
     *   readVersionText() // -> "v1.0.35"
     */
    function readVersionText() {
        if (cachedVersion !== null) return cachedVersion;
        let content = '';
        try {
            const meta = document.querySelector(`meta[name="${META_NAME}"]`);
            content = meta ? (meta.getAttribute('content') || '').trim() : '';
        } catch (_) {
            content = '';
        }
        cachedVersion = content;
        return cachedVersion;
    }

    /**
     * Clear the cached read. Tests only - production code never calls
     * this, because the whole point of the cache is that the answer
     * cannot move for the life of the page.
     * @returns {void}
     */
    function resetCache() {
        cachedVersion = null;
    }

    /**
     * The version, as one small span - THE component both placements use.
     *
     * Selectable, plain text: no button role, no tab stop, no click
     * handler, so a screen reader reads it as static content and Tab
     * never lands on it. It carries no `user-select: none` of its own
     * (nor does any CSS this module ships), so a reader can select and
     * copy the string into a bug report.
     * @param {string} [extraClass] - an additional class to join onto the
     *   shared `version` class, for a placement's own spacing. Omit for
     *   a placement that already owns its layout (the home bar's own
     *   `.home-bar__version` chip wraps this with no extra class needed).
     * @returns {string} HTML for one `<span>`. Never empty - the
     *   unresolved case renders `UNKNOWN_TEXT` rather than nothing.
     * @example
     *   versionSpanHtml() // -> '<span class="version">v1.0.35</span>'
     */
    function versionSpanHtml(extraClass) {
        const resolved = readVersionText();
        const text = resolved || UNKNOWN_TEXT;
        const cls = extraClass ? `version ${extraClass}` : 'version';
        return `<span class="${cls}">${esc(text)}</span>`;
    }

    /**
     * The sidebar placement: the version span inside its own footer
     * block, sitting below the status-light key with a hairline of its
     * own - see client/css/version-footer.css for the quiet treatment it
     * matches. `session-sidebar-rows.js` appends this after
     * `SessionStatusKey.keyHtml()`.
     * @returns {string} HTML for the sidebar's version footer.
     * @example
     *   sidebarFooterHtml()
     *   // -> '<div class="version-footer">
     *   //       <span class="version">v1.0.35</span></div>'
     */
    function sidebarFooterHtml() {
        return `<div class="version-footer">${versionSpanHtml()}</div>`;
    }

    globalThis.VersionFooter = {
        META_NAME,
        UNKNOWN_TEXT,
        readVersionText,
        resetCache,
        versionSpanHtml,
        sidebarFooterHtml,
    };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = globalThis.VersionFooter;
    }

    console.log('[VersionFooter Module] Exported as window.VersionFooter');
})();
