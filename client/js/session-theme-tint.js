/**
 * SessionThemeTint - paints a session row in the colours of that
 * session's own theme.
 *
 * WHY. Per-session themes exist so you can tell your sessions apart at a
 * glance once you are INSIDE one (see session-theme-menu.js). Outside
 * one - in the sidebar list and on the home screen - every row looked
 * identical, so the whole point of picking a theme evaporated exactly
 * where you are choosing which session to enter. This module carries the
 * identity out to the list.
 *
 * WHAT IT PAINTS, AND WHAT IT DELIBERATELY DOES NOT. One cue, drawn
 * from the theme's OWN `--color-accent`: a small SWATCH element,
 * rendered inside the row, on both surfaces, identically.
 *
 * WHY NOT THE ROW'S BOX ANY MORE. It used to be an inset 1px ring plus
 * (sidebar only) a low-alpha background wash. The row's border and
 * background already carry SELECTION - `[data-active="1"]` is an accent
 * background, an accent border and a bold accent name - so a session
 * pinned to the host theme drew an accent edge on a row that was not
 * selected and read as the selected one. Measured live: selection border
 * rgba(215, 119, 87, 0.3) against a themed row's ring
 * rgba(215, 119, 87, 0.45). One fact per channel; the box is
 * selection's, and the theme gets its own mark.
 *
 * WHY A NEW ELEMENT RATHER THAN AN EXISTING ONE. Everything else on the
 * row is spoken for: the status dot carries seven activity states, the
 * pin button carries pinned, the name's colour and weight carry
 * selection, the badge carries tmux/external and is dropped at compact
 * density. The drag grip is the only glyph with no colour meaning, and
 * it is a functional affordance - a session accent close to the row
 * background would render an invisible drag handle. The swatch is
 * emitted ONLY on themed rows, so an unthemed list is unchanged, and it
 * is the first version of this cue that is not colour-only: it carries
 * an accessible name, which no border ever could.
 *
 * It still does NOT touch the row's text colour, its badges, its status
 * dot or its icons. Those keep the palette of the theme the USER IS
 * LOOKING AT, which is the only palette whose contrast against the
 * surrounding page anyone has reasoned about.
 *
 * NO THEME, UNKNOWN THEME, REGISTRY NOT READY: all three render exactly
 * as the row does today. `attrs()` and `swatchHtml()` both return an
 * empty string, the row carries no `data-session-theme` and no swatch.
 * Three outcomes, and the two that are not "themed" are the same one:
 * leave it alone. In particular the registry is populated asynchronously
 * by `Themes.init()`, so an early paint must degrade to today's row
 * rather than to a default colour.
 *
 * Loads BEFORE session-sidebar-rows.js and launchpad.js.
 */
(function () {
    'use strict';

    /** Cache of theme id -> {accent, label}, or null for "not themeable". */
    var cache = new Map();

    /**
     * Parse a `#rgb` or `#rrggbb` colour into its channels.
     *
     * @param {string} hex - colour text from a theme manifest.
     * @returns {?{r: number, g: number, b: number}} null for anything
     *   that is not a plain hex colour, which is the honest answer for a
     *   theme that expresses its accent some other way.
     */
    function parseHex(hex) {
        if (typeof hex !== 'string') return null;
        var h = hex.trim();
        if (h.charAt(0) !== '#') return null;
        h = h.slice(1);
        if (h.length === 3) {
            h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        }
        if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
        var n = parseInt(h, 16);
        return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
    }

    /**
     * The manifest for one theme id, from the client-side registry.
     *
     * @param {string} themeId - a theme id such as 'dracula'.
     * @returns {?object} the manifest, or null when the registry has not
     *   loaded yet or does not know the id.
     */
    function manifestFor(themeId) {
        if (!window.Themes || typeof window.Themes.listAll !== 'function') return null;
        var all;
        try {
            all = window.Themes.listAll();
        } catch (_) {
            return null;
        }
        if (!Array.isArray(all)) return null;
        for (var i = 0; i < all.length; i++) {
            if (all[i] && all[i].id === themeId) return all[i];
        }
        return null;
    }

    /**
     * What a themed row is painted with, and what the swatch is called.
     *
     * @param {?string} themeId - the session's pinned theme id, or null.
     * @returns {?{accent: string, label: string}} null whenever the row
     *   must render as an unthemed row does.
     */
    function colorsFor(themeId) {
        if (!themeId) return null;
        if (cache.has(themeId)) return cache.get(themeId);
        var manifest = manifestFor(themeId);
        // Not cached: the registry fills in asynchronously, so "unknown
        // right now" must stay askable on the next paint.
        if (!manifest || !manifest.cssVars) return null;
        var rgb = parseHex(manifest.cssVars['--color-accent']);
        if (!rgb) return null;
        var value = {
            accent: 'rgb(' + rgb.r + ', ' + rgb.g + ', ' + rgb.b + ')',
            // The manifest's own display name where it has one, so the
            // accessible name reads "session theme: Dracula" rather than
            // repeating an id. The id is the honest fallback.
            label: (typeof manifest.name === 'string' && manifest.name.trim())
                ? manifest.name.trim()
                : String(themeId),
        };
        cache.set(themeId, value);
        return value;
    }

    /**
     * Scrub a theme id down to what is safe to write into markup.
     *
     * @param {?string} themeId - the session's pinned theme id.
     * @returns {string} the id reduced to [A-Za-z0-9_-], or '' when
     *   nothing survives - which yields an untinted row rather than a
     *   partially-escaped one.
     */
    function safeThemeId(themeId) {
        return String(themeId).replace(/[^a-zA-Z0-9_-]/g, '');
    }

    /**
     * Escape text for an HTML attribute value.
     *
     * @param {string} text - untrusted text, e.g. a manifest display name.
     * @returns {string} the same text with the five markup-significant
     *   characters replaced by entities.
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
     * The HTML attributes that mark a session row as themed.
     *
     * Returns a leading-space-prefixed attribute fragment for splicing
     * straight into a row template, or `''` when the row must look
     * exactly as an unthemed one does.
     *
     * Nothing in session-theme-tint.css matches the row itself any more -
     * the attribute is what `swatchHtml()` is kept in step with, and what
     * a future per-surface exception would hook on.
     *
     * @param {?string} themeId - the session's pinned theme id, or null.
     * @returns {string} e.g. ` data-session-theme="dracula" style="..."`.
     */
    function attrs(themeId) {
        var colors = colorsFor(themeId);
        if (!colors) return '';
        var safeId = safeThemeId(themeId);
        if (!safeId) return '';
        return (
            ' data-session-theme="' + safeId + '"' +
            ' style="--session-theme-accent: ' + colors.accent + ';"'
        );
    }

    /**
     * The swatch: the whole of what a themed row shows, on both surfaces.
     *
     * A dedicated element rather than a property of the row's box,
     * because the box means selection - see the module docstring. It is
     * `role="img"` with a name rather than `aria-hidden`, so this is the
     * first version of the cue that is not colour-only.
     *
     * @param {?string} themeId - the session's pinned theme id, or null.
     * @returns {string} the swatch markup, or `''` for all three
     *   not-themed cases.
     */
    function swatchHtml(themeId) {
        var colors = colorsFor(themeId);
        if (!colors) return '';
        if (!safeThemeId(themeId)) return '';
        var label = 'session theme: ' + colors.label;
        return (
            '<span class="session-theme-swatch" role="img"' +
            ' aria-label="' + escAttr(label) + '"' +
            ' title="' + escAttr(label) + '"></span>'
        );
    }

    window.SessionThemeTint = {
        attrs: attrs,
        swatchHtml: swatchHtml,
        colorsFor: colorsFor,
    };
})();
