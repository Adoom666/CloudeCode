// Legacy Windows theme - light-CRT raster with a drifting convergence fringe.
//
// This is the one theme in the batch built on a LIGHT background (industrial
// grey), so an effect tuned for a dark panel would either vanish or turn to
// mud here. The base texture is a faint dark dot-raster (dark dots read fine
// against light grey; a light overlay would not) borrowed conceptually from a
// consumer monitor's shadow mask. On top of it, two soft colour fringes
// drift horizontally past each other in opposite directions, standing in for
// a misconverged CRT gun where the red and blue channels do not quite line
// up with green at the edges of the picture. The motion is horizontal and
// bidirectional, unlike green_crt's vertical roll, gameboy's diagonal smear,
// pokemon's global crossfade, or legacy_apple's Lissajous wander.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Raster dot pitch in CSS pixels. */
const RASTER_PITCH = 3;

/** Opacity of a single raster dot. Kept low against the light grey surface. */
const RASTER_ALPHA = 0.035;

/** Milliseconds for one fringe to cross the viewport width. */
const FRINGE_PERIOD_MS = 34000;

/** Peak opacity of each convergence fringe band. */
const FRINGE_ALPHA = 0.05;

/** Fringe band width as a fraction of viewport width. */
const FRINGE_BAND_FRACTION = 0.4;

/**
 * Build the repeating raster-dot pattern once: a single faint dark dot per
 * cell, read as texture rather than a visible grid.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} dotColour rgba() string for the raster dot
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildRasterPattern(ctx, dotColour) {
    const tile = document.createElement('canvas');
    tile.width = RASTER_PITCH;
    tile.height = RASTER_PITCH;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = dotColour;
    tctx.fillRect(0, 0, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the raster pattern. Runs on init and on
 * every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, red: string, blue: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg', '#C0C0C0');
    const ink = readVar('--color-fg', '#000000');
    const red = rgbTriple(readVar('--color-danger', '#800000'), '128, 0, 0');
    const blue = rgbTriple(readVar('--color-accent', '#000080'), '0, 0, 128');
    return {
        pattern: buildRasterPattern(ctx, `rgba(${rgbTriple(ink, '0, 0, 0')}, ${RASTER_ALPHA})`),
        bg,
        red,
        blue,
    };
}

/**
 * Paint the base surface colour and the raster texture. Shared by the
 * animated frame and the reduced-motion static frame, which differ only by
 * the convergence fringes.
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
 * Draw one frame: raster base plus two soft vertical colour fringes sliding
 * horizontally in opposite directions, crossing and separating over the
 * cycle the way a misconverged CRT gun drifts at the edges of the picture.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, red: string, blue: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const bandW = env.width * FRINGE_BAND_FRACTION;
    const travel = env.width + bandW;
    const pRed = (t % FRINGE_PERIOD_MS) / FRINGE_PERIOD_MS;
    const pBlue = 1 - pRed;
    const xRed = pRed * travel - bandW;
    const xBlue = pBlue * travel - bandW;

    const redGrad = ctx.createLinearGradient(xRed, 0, xRed + bandW, 0);
    redGrad.addColorStop(0, `rgba(${state.red}, 0)`);
    redGrad.addColorStop(0.5, `rgba(${state.red}, ${FRINGE_ALPHA})`);
    redGrad.addColorStop(1, `rgba(${state.red}, 0)`);
    ctx.fillStyle = redGrad;
    ctx.fillRect(0, 0, env.width, env.height);

    const blueGrad = ctx.createLinearGradient(xBlue, 0, xBlue + bandW, 0);
    blueGrad.addColorStop(0, `rgba(${state.blue}, 0)`);
    blueGrad.addColorStop(0.5, `rgba(${state.blue}, ${FRINGE_ALPHA})`);
    blueGrad.addColorStop(1, `rgba(${state.blue}, 0)`);
    ctx.fillStyle = blueGrad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the raster texture with no convergence fringes at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'legacy_windows',
    // 15fps: each fringe takes 34 seconds to cross, well above the
    // perceptual floor for that speed.
    fps: 15,
    fpsMobile: 10,
    background: '#C0C0C0',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
