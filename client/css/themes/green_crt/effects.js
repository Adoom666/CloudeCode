// Green CRT theme - phosphor scanlines with a slow vertical refresh roll.
//
// A raster effect rather than a particle effect: nothing moves across the
// screen, the whole surface is banded and one very faint bright band drifts
// down it on a 26 second cycle, the way a real CRT rolls when its vertical
// hold is a hair off. Deliberately the quietest kind of motion there is, since
// this sits behind text somebody reads for hours.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Scanline pitch in CSS pixels: one dark line every this many rows. */
const SCANLINE_PITCH = 3;

/** Opacity of a scanline. Low enough to read as texture, not as stripes. */
const SCANLINE_ALPHA = 0.26;

/** Seconds for the refresh band to travel the full viewport height. */
const ROLL_PERIOD_MS = 26000;

/** Peak opacity of the refresh band. This is the whole animation, so it has to
 * clear the perception floor even though the instinct is to keep it faint: at
 * the original 0.035 it composited a maximum RGB delta of 6/255 over the
 * #020a04 page. Faint is the goal; invisible is the bug. */
const ROLL_ALPHA = 0.075;

/** Refresh band height as a fraction of viewport height. */
const ROLL_BAND_FRACTION = 0.3;

/**
 * Build the repeating scanline pattern once, so each frame is two fills rather
 * than several hundred one-pixel rects.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildScanlinePattern(ctx) {
    const tile = document.createElement('canvas');
    tile.width = 1;
    tile.height = SCANLINE_PITCH;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = `rgba(0, 0, 0, ${SCANLINE_ALPHA})`;
    tctx.fillRect(0, 0, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the scanline pattern.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, glow: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#010604');
    const accent = readVar('--color-accent', '#33FF33');
    return {
        pattern: buildScanlinePattern(ctx),
        bg,
        glow: rgbTriple(accent, '51, 255, 51'),
    };
}

/**
 * Paint the background and the scanline texture. Shared by the animated frame
 * and the reduced-motion static frame, which differ only by the roll band.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function paintBase(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    if (state.pattern) {
        ctx.fillStyle = state.pattern;
        ctx.fillRect(0, 0, env.width, env.height);
    }
}

/**
 * Draw one frame: scanline base plus the drifting refresh band.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, glow: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const bandH = env.height * ROLL_BAND_FRACTION;
    const travel = env.height + bandH;
    const y = ((t % ROLL_PERIOD_MS) / ROLL_PERIOD_MS) * travel - bandH;

    const grad = ctx.createLinearGradient(0, y, 0, y + bandH);
    grad.addColorStop(0, `rgba(${state.glow}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.glow}, ${ROLL_ALPHA})`);
    grad.addColorStop(1, `rgba(${state.glow}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, y, env.width, bandH);
}

/**
 * Reduced-motion frame: the scanline texture with no roll band at all.
 * The phosphor look survives; the only moving part is the part that goes.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'green_crt',
    // 20fps is well above the perceptual floor for a band that takes 26
    // seconds to cross the screen, and leaves the terminal more headroom.
    fps: 20,
    fpsMobile: 10,
    background: '#010604',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
