// Alien theme - Nostromo MOTHER 6000 industrial ship computer console.
//
// Deliberately NOT a phosphor-fall or scanline effect: this repo already has
// two of those (matrix's falling katakana, green_crt's scanlines + vertical
// roll band) and alien's own manifest describes an industrial console, not a
// creature or a CRT. The signature here is a static diagonal hazard-stripe
// raster (the yellow/black tape you see on ship bulkheads) plus a single slow
// horizontal sensor sweep, the way a status console scans left to right
// rather than top to bottom. Nothing falls, nothing rolls vertically.
//
// The stripe raster is baked once into a repeating pattern tile and never
// redrawn per frame; only the sweep bar and the panel pulse move.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Hazard-stripe tile edge length in CSS pixels. */
const TILE_SIZE = 28;

/** Spacing between diagonal stripe strokes inside the tile. */
const STRIPE_PITCH = 8;

/** Opacity of the baked stripe raster. Texture, not signage. */
const STRIPE_ALPHA = 0.032;

/** Milliseconds for the sensor sweep to cross the full viewport width. */
const SWEEP_PERIOD_MS = 34000;

/** Sweep band width as a fraction of viewport width. */
const SWEEP_BAND_FRACTION = 0.22;

/** Peak opacity of the sweep band. */
const SWEEP_ALPHA = 0.055;

/** Milliseconds per status-panel pulse cycle. */
const PULSE_PERIOD_MS = 22000;

/** Peak opacity of the status-panel glow at the top of its pulse. */
const PULSE_ALPHA = 0.05;

/**
 * Build a repeating diagonal hazard-stripe pattern tile. Each stripe is a
 * 45-degree staircase built from single-row fillRect calls offset by one
 * column per row, so the whole tile is drawn with fillRect alone (the
 * harness canvas only guarantees fillRect + gradients + drawImage; no
 * stroke/path API is assumed available).
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} tint "r, g, b" triple for the stripe colour
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildHazardPattern(ctx, tint) {
    const tile = document.createElement('canvas');
    tile.width = TILE_SIZE;
    tile.height = TILE_SIZE;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = `rgba(${tint}, ${STRIPE_ALPHA})`;
    const stripeW = 3;
    for (let y = 0; y < TILE_SIZE; y++) {
        for (let x = -TILE_SIZE; x < TILE_SIZE * 2; x += STRIPE_PITCH) {
            tctx.fillRect(x + y, y, stripeW, 1);
        }
    }
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the stripe pattern.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, sweepTint: string,
 *   panelTint: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#02080a');
    const accent = readVar('--color-accent', '#33ff66');
    const warning = readVar('--color-warning', '#ffd700');
    const accentTint = rgbTriple(accent, '51, 255, 102');
    return {
        pattern: buildHazardPattern(ctx, accentTint),
        bg,
        sweepTint: accentTint,
        panelTint: rgbTriple(warning, '255, 215, 0'),
    };
}

/**
 * Paint the background and the static hazard raster. Shared by the animated
 * frame and the reduced-motion static frame, which differ only by the moving
 * sweep and pulse.
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
 * Draw the status-panel pulse: a soft glow parked in the upper-right region,
 * breathing on a slow sine so it reads as an idle console indicator rather
 * than a light show.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{panelTint: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawPanelPulse(ctx, env, state, t) {
    const phase = (t % PULSE_PERIOD_MS) / PULSE_PERIOD_MS;
    const alpha = PULSE_ALPHA * (0.4 + 0.6 * (0.5 + 0.5 * Math.sin(phase * Math.PI * 2)));
    const cx = env.width * 0.86;
    const cy = env.height * 0.14;
    const r = Math.max(env.width, env.height) * 0.22;
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    grad.addColorStop(0, `rgba(${state.panelTint}, ${alpha})`);
    grad.addColorStop(1, `rgba(${state.panelTint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
}

/**
 * Draw one frame: hazard raster, sensor sweep, panel pulse.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, sweepTint: string,
 *   panelTint: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const bandW = env.width * SWEEP_BAND_FRACTION;
    const travel = env.width + bandW;
    const x = ((t % SWEEP_PERIOD_MS) / SWEEP_PERIOD_MS) * travel - bandW;

    const grad = ctx.createLinearGradient(x, 0, x + bandW, 0);
    grad.addColorStop(0, `rgba(${state.sweepTint}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.sweepTint}, ${SWEEP_ALPHA})`);
    grad.addColorStop(1, `rgba(${state.sweepTint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(x, 0, bandW, env.height);

    drawPanelPulse(ctx, env, state, t);
}

/**
 * Reduced-motion frame: hazard raster and panel glow at its mid-pulse level,
 * no sweep at all. The console texture survives; nothing crosses the screen.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
    drawPanelPulse(ctx, env, state, PULSE_PERIOD_MS / 4);
}

const effect = createEffect({
    id: 'alien',
    // The sweep crosses the viewport in 34s and the pulse breathes over 22s;
    // 14fps is comfortably above what either motion needs.
    fps: 14,
    fpsMobile: 8,
    background: '#02080a',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
