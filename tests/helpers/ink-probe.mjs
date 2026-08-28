// Measures the INK an effect deposits: the peak effective alpha of any
// mark-making call in a frame.
//
// WHY THIS EXISTS. The theme-effects suite could prove an effect was running
// and could not prove anyone would ever see it. Every one of the 23 themes
// passed that suite while compositing a maximum RGB delta of ZERO against the
// page. "Running" and "visible" are different claims and need different
// measurements.
//
// WHAT IT APPROXIMATES. Peak effective alpha is `globalAlpha` multiplied by
// the alpha of the paint source, sampled at each mark-making call. It is an
// upper bound on the per-pixel contribution of one frame, NOT a prediction of
// the composited delta: coverage, blend mode, overdraw and the backdrop all
// move the real number. It is used as a FLOOR - an effect whose peak alpha is
// near zero cannot be visible under any backdrop, which is the defect being
// guarded against. A generous peak with tiny coverage can still be faint, so
// this is a necessary and not a sufficient condition, and the browser-side
// delta measurement remains the ground truth.

/** Canvas methods that actually put pixels on the surface. */
const MARK_OPS = [
    'fillRect', 'strokeRect', 'fill', 'stroke', 'fillText', 'strokeText',
    'drawImage', 'putImageData',
];

/** Methods whose paint source is the stroke style rather than the fill. */
const STROKE_OPS = new Set(['strokeRect', 'stroke', 'strokeText']);

/**
 * Alpha of a CSS colour string, as used by the effect modules.
 * Recognises rgba()/hsla() with an explicit alpha, 8-digit and 4-digit hex,
 * and treats every other opaque form as alpha 1.
 * @param {*} style Value of ctx.fillStyle or ctx.strokeStyle
 * @returns {number} Alpha in 0..1; 1 for a gradient or unrecognised value
 */
export function styleAlpha(style) {
    if (style == null) return 0;
    if (typeof style === 'object') {
        // A gradient handle from the fake context carries its colour stops.
        // The alpha of these effects lives in the STOPS, not in fillStyle, so
        // reading the handle as opaque reports 1.0 for almost every theme and
        // measures nothing. Take the strongest stop: that is the brightest
        // pixel the gradient can produce.
        if (Array.isArray(style.stops)) {
            if (style.stops.length === 0) return 0;
            return Math.max(...style.stops.map((s) => styleAlpha(s && s.color)));
        }
        return 1;   // pattern, or a handle we cannot introspect
    }
    const s = String(style).trim();
    const fn = s.match(/^(?:rgba|hsla)\(([^)]*)\)$/i);
    if (fn) {
        const parts = fn[1].split(/[,/]/).map((x) => x.trim()).filter(Boolean);
        if (parts.length >= 4) {
            const a = parseFloat(parts[3]);
            return Number.isFinite(a) ? Math.max(0, Math.min(1, a)) : 1;
        }
        return 1;
    }
    const hex = s.match(/^#([0-9a-f]{3,8})$/i);
    if (hex) {
        const h = hex[1];
        if (h.length === 8) return parseInt(h.slice(6, 8), 16) / 255;
        if (h.length === 4) return parseInt(h[3] + h[3], 16) / 255;
        return 1;
    }
    if (s === 'transparent') return 0;
    return 1;
}

/**
 * Share of the canvas a fillRect/strokeRect covers, when derivable.
 * @param {string} name Method name
 * @param {Array} args Call arguments
 * @param {object} ctx The context, for canvas dimensions
 * @returns {number} Coverage in 0..1, or NaN when it cannot be derived
 */
function coverage(name, args, ctx) {
    if (name !== 'fillRect' && name !== 'strokeRect') return NaN;
    const el = ctx.canvas;
    if (!el || !el.width || !el.height) return NaN;
    const [, , w, h] = args;
    if (typeof w !== 'number' || typeof h !== 'number') return NaN;
    // The rect is in USER space; the canvas is in DEVICE pixels and the
    // harness has already applied a devicePixelRatio scale. Comparing the two
    // directly reports a full-canvas clear as 25% coverage at DPR 2 and the
    // clear then counts as decoration, which is what made the first version of
    // this metric read 1.0 for every theme.
    const m = typeof ctx.getTransform === 'function' ? ctx.getTransform() : null;
    const sx = m ? Math.hypot(m.a, m.b) : 1;
    const sy = m ? Math.hypot(m.c, m.d) : 1;
    return Math.abs(w * sx * h * sy) / (el.width * el.height);
}

/**
 * Instrument a fake 2D context so every mark-making call records the
 * effective alpha in force at the moment it was issued.
 *
 * Marks are split into two populations, because they answer different
 * questions. A near-full-canvas fillRect is the frame CLEAR or the motion
 * trail: its alpha says how fast history fades, not how bright the effect is,
 * and it is opaque in most themes, which is why an undifferentiated peak
 * reads 1.0 for all 23 and discriminates nothing. Everything else is
 * DECORATIVE - the glyphs, particles and streaks that are the effect - and
 * its peak alpha is the number that tracks whether a human sees anything.
 *
 * @param {object} ctx A context from createFakeCtx()
 * @param {number} [clearCoverage] Coverage at or above which a rect counts as
 *   a clear rather than decoration. Default 0.9.
 * @returns {{peak: function(): number, peakDecorative: function(): number,
 *   decorativeCount: function(): number, samples: function(): object[]}}
 */
export function trackInk(ctx, clearCoverage = 0.9) {
    const samples = [];
    for (const name of MARK_OPS) {
        const original = ctx[name];
        if (typeof original !== 'function') continue;
        ctx[name] = function instrumented(...args) {
            const source = STROKE_OPS.has(name) ? ctx.strokeStyle : ctx.fillStyle;
            const ga = typeof ctx.globalAlpha === 'number' ? ctx.globalAlpha : 1;
            const cov = coverage(name, args, ctx);
            samples.push({
                name,
                alpha: ga * styleAlpha(source),
                // A mark is the GROUND only when it covers the canvas AND is
                // opaque - that is a wipe, and a wipe is what the effect is
                // drawn ON, never the effect itself. A full-canvas mark that
                // is TRANSLUCENT is a wash or a motion trail, and in several
                // themes (corporate_v2, terminal, codex) the full-canvas wash
                // IS the entire effect, so classifying it as ground reported
                // "no decoration at all" for ten themes.
                // NaN coverage means "not a rect we can size" - a path, glyph
                // or image - which is decorative by construction.
                isClear: Number.isFinite(cov)
                    && cov >= clearCoverage
                    && (ga * styleAlpha(source)) >= 0.99,
            });
            return original.apply(this, args);
        };
    }
    const decorative = () => samples.filter((s) => !s.isClear);
    return {
        peak: () => (samples.length ? Math.max(...samples.map((s) => s.alpha)) : 0),
        peakDecorative: () => {
            const d = decorative();
            return d.length ? Math.max(...d.map((s) => s.alpha)) : 0;
        },
        decorativeCount: () => decorative().length,
        samples: () => samples.slice(),
    };
}
