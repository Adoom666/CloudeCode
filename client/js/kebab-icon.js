/**
 * The kebab (vertical three-dot) glyph, in ONE place.
 * ----------------------------------------------------------------------
 * This app draws a kebab in ONE place: the app header's overflow menu at
 * the top right (client/js/header-menu.js). It briefly drew a second one
 * on every conversation row, which is why the mark was extracted here
 * rather than typed out twice; that row menu was removed on 2026-09-08
 * and the extraction is kept, because a glyph inlined at its only call
 * site is exactly how this app ended up with two drifting copies of it
 * the first time.
 *
 * GLYPH WEIGHT IS LOAD BEARING, and this is the header's docblock moved
 * here with the markup it describes. The original kebab drew three
 * r=1.5 dots into a 16px box: 3 CSS pixels each, about 7 percent of the
 * button's interior in ink, against 16 percent for the file-editor icon
 * beside it and 37 percent for the conversations toggle. It was the
 * faintest control in the header by a factor of two to five, and the
 * user reported it as "an empty button" - which it very nearly is at
 * that weight. Rendering the same 16-unit viewBox into a 20px box at
 * r=2 roughly doubles the ink and puts the kebab in the same visual band
 * as its siblings, with the dots still separated.
 * scripts/verify_login_chrome.py measures that ink against a floor so it
 * cannot silently regress back to a bordered blank square.
 *
 * THAT MEASUREMENT IS WHY THE DEFAULT SIZE IS 20 AND WHY THE STRING IS
 * BYTE-IDENTICAL to the literal header-menu.js used to hold.
 * tests/test_kebab_icon_shared.node.mjs asserts that identity, so this
 * extraction cannot have moved a pixel in the header, and a future edit
 * that shrinks the dots fails there rather than in a screenshot nobody
 * takes.
 *
 * The viewBox is fixed at 16 units whatever the pixel size, so a smaller
 * rendering scales the same three dots down rather than drawing a
 * different, thinner mark.
 *
 * No dependencies. Must load BEFORE header-menu.js.
 */

console.log('[KebabIcon Module] Loading...');

(function () {
    'use strict';

    /**
     * The header's rendered size, in CSS pixels. The default, and the
     * size scripts/verify_login_chrome.py has an ink floor for.
     * @type {number}
     */
    var DEFAULT_SIZE = 20;

    /**
     * Description: the vertical three-dot mark as a self-contained
     *   `<svg>` string, ready to interpolate into a button's innerHTML.
     *   `fill="currentColor"` on the dots, so the caller's CSS `color`
     *   drives the glyph exactly as it does for every other icon here.
     * Inputs:
     *   size (number|undefined) - rendered width and height in CSS
     *     pixels. Defaults to DEFAULT_SIZE (20).
     * Output:
     *   string - one `<svg>` element, `aria-hidden` because the button
     *     around it carries the accessible name.
     * Example:
     *   KebabIcon.svg()   // 20x20, byte-identical to the header's glyph
     *   KebabIcon.svg(16) // a smaller rendering, same three dots
     */
    function svg(size) {
        var px = (typeof size === 'number' && size > 0) ? size : DEFAULT_SIZE;
        return (
            '<svg width="' + px + '" height="' + px + '" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
            '<circle cx="8" cy="3" r="2" fill="currentColor"/>' +
            '<circle cx="8" cy="8" r="2" fill="currentColor"/>' +
            '<circle cx="8" cy="13" r="2" fill="currentColor"/>' +
            '</svg>'
        );
    }

    window.KebabIcon = { svg: svg, DEFAULT_SIZE: DEFAULT_SIZE };
})();

console.log('[KebabIcon Module] Exported as window.KebabIcon');
