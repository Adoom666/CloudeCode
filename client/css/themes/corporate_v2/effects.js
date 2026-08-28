// Corporate Modern v2.0 theme - one faint indigo gradient sweeping the surface.
//
// The whole effect: a single soft diagonal band translating across the
// viewport at a near-imperceptible pace, then wrapping to start again. No
// second layer, no twinkle, no breathe - this palette is defined by
// restraint (single indigo accent, cool near-black), so the effect does less
// than every other theme in this set. One full sweep takes 75 seconds.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded
// beyond the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Milliseconds for the band to cross the diagonal once and wrap. */
const SWEEP_PERIOD_MS = 75000;

/** Band half-width along the sweep axis, as a fraction of the diagonal. */
const BAND_FRACTION = 0.22;

/** Peak opacity at the band's centre. The ceiling for this theme.
 *
 * TUNED AGAINST MEASURED PIXELS, not against taste. At the original 0.025
 * this band composited a maximum RGB delta of 2/255 over the #0A0A0B page,
 * across 0.24% of the viewport - present in the DOM, absent to the eye. The
 * tint is a desaturated slate, so the delta is roughly ALPHA_PEAK x 80 rather
 * than x 245; reaching a perceptible floor needs a number that looks large
 * next to the other themes and is not. See tests/test_theme_effects_
 * visibility.node.mjs for the floor this has to clear. */
const ALPHA_PEAK = 0.12;

/**
 * Resolve the palette. Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; geometry is fractional)
 * @returns {{bg: string, tint: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg', '#0A0A0B');
    const tint = rgbTriple(readVar('--color-accent', '#6366F1'), '99, 102, 241');
    return { bg, tint };
}

/**
 * Draw one frame: flat backdrop, then one soft band along the diagonal at
 * its current sweep position.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    const diag = Math.hypot(env.width, env.height);
    const phase = (t % SWEEP_PERIOD_MS) / SWEEP_PERIOD_MS;
    // Sweep travels from one corner past the opposite one so the band is
    // fully off-screen at both ends of the cycle, never popping in or out.
    const travel = diag * (1 + BAND_FRACTION * 2);
    const centre = -diag * BAND_FRACTION + phase * travel;
    const bandHalf = diag * BAND_FRACTION;

    // Gradient axis runs corner to corner (top-left to bottom-right).
    const ux = env.width / diag;
    const uy = env.height / diag;
    const x0 = ux * (centre - bandHalf);
    const y0 = uy * (centre - bandHalf);
    const x1 = ux * (centre + bandHalf);
    const y1 = uy * (centre + bandHalf);

    const grad = ctx.createLinearGradient(x0, y0, x1, y1);
    grad.addColorStop(0, `rgba(${state.tint}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.tint}, ${ALPHA_PEAK})`);
    grad.addColorStop(1, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the band frozen mid-sweep.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, SWEEP_PERIOD_MS * 0.4);
}

const effect = createEffect({
    id: 'corporate_v2',
    // One gradient repaint, no per-element loop at all; the cap exists only
    // so this never competes with the terminal's own compositing.
    fps: 12,
    fpsMobile: 8,
    background: '#0A0A0B',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
