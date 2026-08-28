// SNES theme - consumer CRT shadow-mask triad with an interlace shimmer.
//
// green_crt already owns the terminal-monitor look: dark horizontal scanlines
// with a bright band rolling top to bottom. A living-room television reads
// differently, so this effect is built on a different axis entirely. A
// shadow-mask (or aperture-grille) tube resolves colour from three adjacent
// phosphor dots per triad, which shows up as a faint vertical column texture
// rather than horizontal lines; and consumer sets of the era visibly
// interlaced, which reads not as something moving across the screen but as a
// slow global brightness pulse between the two scan fields. Nothing here
// travels top to bottom, so there is no roll band to confuse with green_crt's.
//
// Colours come from the theme's own cssVars: the phosphor triad borrows the
// theme's existing red/green/blue-ish semantic colours (danger, success,
// info) rather than inventing new literals, since a real triad is exactly
// three primaries next to each other.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Triad pitch in CSS pixels: one full red-green-blue column group per this. */
const TRIAD_PITCH = 3;

/** Opacity of a single phosphor-dot column. Low enough to read as texture. */
const TRIAD_ALPHA = 0.05;

/** Milliseconds for one full interlace shimmer pulse (dim to bright to dim). */
const SHIMMER_PERIOD_MS = 24000;

/** Peak opacity added by the interlace shimmer at its brightest point. */
const SHIMMER_ALPHA = 0.045;

/**
 * Build the repeating triad-column pattern once: three 1px-wide columns
 * (red, green, blue) per pitch, the way an aperture-grille tube resolves
 * colour from adjacent phosphor strips.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} r rgba() string for the red phosphor column
 * @param {string} g rgba() string for the green phosphor column
 * @param {string} b rgba() string for the blue phosphor column
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildTriadPattern(ctx, r, g, b) {
    const tile = document.createElement('canvas');
    tile.width = TRIAD_PITCH;
    tile.height = 1;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = r;
    tctx.fillRect(0, 0, 1, 1);
    tctx.fillStyle = g;
    tctx.fillRect(1, 0, 1, 1);
    tctx.fillStyle = b;
    tctx.fillRect(2, 0, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Build the repeating interlace-field pattern: alternating 1px rows, so the
 * shimmer overlay only ever touches every other scanline.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} fieldColour rgba() string for the interlaced field
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildFieldPattern(ctx, fieldColour) {
    const tile = document.createElement('canvas');
    tile.width = 1;
    tile.height = 2;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = fieldColour;
    tctx.fillRect(0, 0, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild both patterns. Runs on init and on every
 * resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; both patterns are size-free)
 * @returns {{triad: CanvasPattern|null, field: CanvasPattern|null,
 *   bg: string, glow: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#2A2A30');
    const red = rgbTriple(readVar('--color-danger', '#f18686'), '241, 134, 134');
    const green = rgbTriple(readVar('--color-success', '#5CD065'), '92, 208, 101');
    const blue = rgbTriple(readVar('--color-info', '#5BB5E8'), '91, 181, 232');
    const glow = rgbTriple(readVar('--color-accent', '#a68adc'), '166, 138, 220');
    return {
        triad: buildTriadPattern(
            ctx,
            `rgba(${red}, ${TRIAD_ALPHA})`,
            `rgba(${green}, ${TRIAD_ALPHA})`,
            `rgba(${blue}, ${TRIAD_ALPHA})`,
        ),
        field: buildFieldPattern(ctx, `rgba(${glow}, 1)`),
        bg,
        glow,
    };
}

/**
 * Paint the base panel colour and the triad texture. Shared by the animated
 * frame and the reduced-motion static frame, which differ only by the
 * interlace shimmer.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{triad: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function paintBase(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    if (state.triad) {
        ctx.fillStyle = state.triad;
        ctx.fillRect(0, 0, env.width, env.height);
    }
}

/**
 * Draw one frame: triad base plus the interlace field pulsing brighter and
 * dimmer. The pulse is a global alpha oscillation, not a travelling band, so
 * nothing here can read as green_crt's roll.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{triad: CanvasPattern|null, field: CanvasPattern|null, bg: string,
 *   glow: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);
    if (!state.field) return;

    const phase = (t % SHIMMER_PERIOD_MS) / SHIMMER_PERIOD_MS;
    // Smooth 0..1..0 pulse, not a linear sawtooth, so the shimmer breathes
    // rather than snapping.
    const pulse = (1 - Math.cos(phase * Math.PI * 2)) / 2;
    const alpha = pulse * SHIMMER_ALPHA;
    if (alpha <= 0.0005) return;

    const prevAlpha = ctx.globalAlpha;
    ctx.globalAlpha = alpha;
    ctx.fillStyle = state.field;
    ctx.fillRect(0, 0, env.width, env.height);
    ctx.globalAlpha = prevAlpha;
}

/**
 * Reduced-motion frame: the triad texture with no interlace shimmer at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{triad: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'snes',
    // 18fps: the shimmer breathes over 24 seconds, well above the perceptual
    // floor for that speed.
    fps: 18,
    fpsMobile: 10,
    background: '#2A2A30',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
