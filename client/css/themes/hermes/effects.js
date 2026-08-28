// Hermes theme - a faint amber phosphor glow drifting side to side.
//
// A single soft vertical band, full viewport height, drifts left and right
// on one slow sine wave rather than sweeping in one direction and cutting
// back - the same restrained "nothing crosses the screen and starts over"
// idea as the rest of this set, oriented as a CRT column instead of a roll
// or a centred blob. A second, faster sine breathes the band's own opacity.
// Full drift cycle: 68 seconds. Full breathe cycle: 31 seconds.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded
// beyond the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Full left-right drift cycle. */
const DRIFT_PERIOD_MS = 68000;

/** Slower secondary breathe on the band's own opacity. */
const BREATHE_PERIOD_MS = 31000;

/** Band centre travels this far either side of viewport centre, as a fraction of width. */
const DRIFT_FRACTION = 0.34;

/** Band half-width as a fraction of viewport width. */
const BAND_WIDTH_FRACTION = 0.28;

/** Opacity floor and ceiling of the band. */
const ALPHA_MIN = 0.012;
const ALPHA_MAX = 0.04;

/**
 * Resolve the palette. Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; geometry is fractional)
 * @returns {{bg: string, tint: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg-page', '#000000');
    const tint = rgbTriple(readVar('--color-accent', '#ffcc00'), '255, 204, 0');
    return { bg, tint };
}

/**
 * Draw one frame: flat backdrop, then the drifting amber column.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    const drift = Math.sin((t / DRIFT_PERIOD_MS) * Math.PI * 2);
    const cx = env.width * (0.5 + drift * DRIFT_FRACTION);
    const bandW = env.width * BAND_WIDTH_FRACTION;

    const breathe = (Math.sin((t / BREATHE_PERIOD_MS) * Math.PI * 2) + 1) / 2;
    const alpha = ALPHA_MIN + (ALPHA_MAX - ALPHA_MIN) * breathe;

    const grad = ctx.createLinearGradient(cx - bandW, 0, cx + bandW, 0);
    grad.addColorStop(0, `rgba(${state.tint}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.tint}, ${alpha})`);
    grad.addColorStop(1, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(cx - bandW, 0, bandW * 2, env.height);
}

/**
 * Reduced-motion frame: the column frozen at centre, mid breathe.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'hermes',
    // A single soft gradient repaint; the cap is a courtesy to the terminal's
    // compositing budget, not a cost the draw itself needs.
    fps: 14,
    fpsMobile: 10,
    background: '#000000',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
