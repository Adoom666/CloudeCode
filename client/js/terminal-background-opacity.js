/**
 * Terminal Background Opacity - derives the xterm theme actually rendered
 * from the raw manifest/session palette, so the terminal surface lets a
 * hint of the animated theme background (client/css/themes/_shared/
 * effects-base.js) show through instead of blocking it outright.
 *
 * Split out of terminal.js (which carries a hard line-count ratchet, see
 * tests/test_terminal_layout.node.mjs) rather than grown inline, matching
 * this codebase's existing pattern for terminal.js concerns
 * (terminal-input-kind.js, terminal-scroll.js, terminal-layout.js).
 *
 * THREE-OUTCOME RULE: isThemeEffectVisible() is the single gate every
 * caller (terminal.js's construction, its xtermThemeChange listener, its
 * MutationObserver on data-theme-effects, and client/css/
 * terminal-opacity.css's #terminal override) must agree with. When the
 * animated background cannot be confirmed on screen, the terminal renders
 * at full opacity - never translucent over nothing.
 *
 * Public API: window.TerminalBackgroundOpacity = {
 *   TERMINAL_BG_OPACITY, parseCssColorChannels, isThemeEffectVisible,
 *   withTerminalBackgroundOpacity
 * }
 */
(function () {
    'use strict';

    /**
     * Fraction of the xterm background colour that stays opaque when an
     * animated theme background is confirmed on screen. 0.90 means the
     * terminal surface lets 10% of whatever is behind it bleed through -
     * "hints of the animated background", not a wash. Measured
     * empirically against the 9-255/255 range of composited deltas the
     * effect harness produces across the 23 themes (see
     * scripts/verify/measure-theme-effect-visibility.py): a flat 0.90
     * keeps every theme's residual bleed-through proportional to that
     * theme's own effect strength (roughly page-delta * 0.10), so a
     * faint effect (corporate_v2, page delta 9) stays faint at the
     * terminal too - measured there at composited delta 0/255, because
     * an effect that faint was already below the point any terminal
     * opacity could surface it - and a strong one (matrix, page delta
     * 255) reads as a real but subtle hint (measured composited delta
     * 19/255 through the terminal). Contrast measured at 15.4:1-18.9:1
     * across matrix/corporate_v2/codex, all far above the WCAG AA
     * 4.5:1 floor. See scripts/verify/measure-terminal-opacity.py for
     * the instrument and scripts/ci/mutate-terminal-opacity.sh for the
     * mutation-tested gate on this file's logic.
     * @type {number}
     */
    var TERMINAL_BG_OPACITY = 0.90;

    /**
     * Description: Parse a CSS colour string xterm.js themes use (#rgb,
     * #rrggbb, or rgb()/rgba()) into 0-255 channel values.
     * Inputs: color (string) - the colour to parse.
     * Outputs: {r:number, g:number, b:number} on success, or null when
     * the string is not one of the recognised forms - callers must treat
     * null as "leave the colour alone", never as black.
     * @param {string} color
     * @returns {{r: number, g: number, b: number} | null}
     */
    function parseCssColorChannels(color) {
        if (typeof color !== 'string') return null;
        var hex3 = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(color);
        if (hex3) {
            return {
                r: parseInt(hex3[1] + hex3[1], 16),
                g: parseInt(hex3[2] + hex3[2], 16),
                b: parseInt(hex3[3] + hex3[3], 16)
            };
        }
        var hex6 = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color);
        if (hex6) {
            return {
                r: parseInt(hex6[1], 16),
                g: parseInt(hex6[2], 16),
                b: parseInt(hex6[3], 16)
            };
        }
        var rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(color);
        if (rgb) {
            return { r: Number(rgb[1]), g: Number(rgb[2]), b: Number(rgb[3]) };
        }
        return null;
    }

    /**
     * Description: Read whether the page's animated theme background is
     * actually a painted, non-empty canvas right now. Backed by the
     * status client/css/themes/_shared/effects-base.js publishes to
     * document.documentElement.dataset.themeEffects (STATUS_ATTR there).
     * RUNNING/PAUSED/STATIC all mean a frame was drawn (paused just
     * means the rAF loop is suspended while the tab is hidden, the last
     * frame is still on the canvas) so translucency reveals something
     * real. UNAVAILABLE/SKIPPED/INACTIVE, or the attribute never having
     * been set at all (no effect script has run yet, or the theme
     * declares none), mean the canvas behind the terminal is blank -
     * translucency there would only wash the terminal out over nothing,
     * which is the case the three-outcome rule requires this module to
     * refuse.
     * Inputs: none (reads global document state).
     * Outputs: boolean - true when a translucent terminal has something
     * to show through, false when the terminal must stay fully opaque.
     * @returns {boolean}
     */
    function isThemeEffectVisible() {
        try {
            var status = document.documentElement.dataset.themeEffects;
            return status === 'running' || status === 'paused' || status === 'static';
        } catch (_) {
            return false;
        }
    }

    /**
     * Description: Derive the xterm theme actually handed to the
     * Terminal instance from the raw manifest/session palette - the one
     * place that decides how opaque the terminal surface is. Only the
     * background channel changes; foreground and every ANSI colour stay
     * exactly as authored so text contrast is never affected by this
     * transform.
     * Inputs: rawTheme (object|null|undefined) - an xterm theme object
     * as produced by a theme manifest's `xterm` block or
     * terminal.js's DEFAULT_XTERM_THEME.
     * Outputs: a new object with the same keys as rawTheme; `background`
     * becomes an rgba() string at TERMINAL_BG_OPACITY when the animated
     * background is confirmed visible (isThemeEffectVisible()), or is
     * left exactly as authored (fully opaque) otherwise - including when
     * rawTheme.background cannot be parsed, which is the safe default
     * per the three-outcome rule (never guess, never render translucent
     * over an unparsed colour).
     * @param {object} rawTheme
     * @returns {object}
     */
    function withTerminalBackgroundOpacity(rawTheme) {
        if (!rawTheme || typeof rawTheme !== 'object') return rawTheme;
        if (!isThemeEffectVisible()) return rawTheme;
        var channels = parseCssColorChannels(rawTheme.background);
        if (!channels) return rawTheme;
        return Object.assign({}, rawTheme, {
            background: 'rgba(' + channels.r + ', ' + channels.g + ', ' + channels.b + ', ' + TERMINAL_BG_OPACITY + ')'
        });
    }

    /**
     * Description: Wire one xterm.js Terminal instance up to this module -
     * every subsequent apply(rawTheme) call renders through
     * withTerminalBackgroundOpacity(), AND a MutationObserver on
     * document.documentElement's data-theme-effects attribute re-applies
     * the last raw theme whenever that status flips (most commonly
     * UNAVAILABLE/INACTIVE -> RUNNING a beat after page load while the
     * effect's canvas is still mounting, or RUNNING <-> PAUSED as the tab
     * hides/shows). This is what keeps terminal.js itself down to calling
     * apply() at its three theme-assignment sites instead of duplicating
     * the raw-theme bookkeeping and observer setup in three places.
     * Inputs: term (object) - an xterm.js Terminal instance (must expose
     * `.options.theme`, settable).
     * Outputs: { apply(rawTheme), dispose() }. apply() is idempotent and
     * safe to call with the same rawTheme repeatedly; dispose()
     * disconnects the observer (not currently called anywhere - the
     * Terminal instance is a page-lifetime singleton, same as its other
     * unclosed subscriptions, e.g. _unsubscribeXtermTheme - but provided
     * so a future per-session Terminal is not stuck without one).
     * @param {object} term
     * @returns {{apply: function(object): void, dispose: function(): void}}
     */
    function attach(term) {
        var lastRaw = null;
        function apply(rawTheme) {
            lastRaw = rawTheme;
            if (!term) return;
            try {
                term.options.theme = withTerminalBackgroundOpacity(rawTheme);
            } catch (e) {
                if (window.console && console.warn) {
                    console.warn('TerminalBackgroundOpacity: failed to apply theme', e);
                }
            }
        }
        var observer = null;
        try {
            observer = new MutationObserver(function () { apply(lastRaw); });
            observer.observe(document.documentElement, {
                attributes: true,
                attributeFilter: ['data-theme-effects']
            });
        } catch (e) {
            if (window.console && console.warn) {
                console.warn('TerminalBackgroundOpacity: effect-status observer unavailable', e);
            }
        }
        return {
            apply: apply,
            dispose: function () { if (observer) observer.disconnect(); }
        };
    }

    window.TerminalBackgroundOpacity = {
        TERMINAL_BG_OPACITY: TERMINAL_BG_OPACITY,
        parseCssColorChannels: parseCssColorChannels,
        isThemeEffectVisible: isThemeEffectVisible,
        withTerminalBackgroundOpacity: withTerminalBackgroundOpacity,
        attach: attach
    };
})();
