// Terminal theme - a single faint phosphor breath, nothing else.
//
// PRE-EXISTING MOTION, READ FIRST: theme.css has no keyframes to avoid, but
// two sibling themes already own the two obvious CRT tropes - matrix owns the
// falling-glyph rain and green_crt owns the scanline-plus-refresh-roll raster.
// Terminal is documented as "the plainest theme in the app... zero
// decoration", so doing either of those here would both duplicate a sibling
// and contradict this theme's own manifest. What is left, and what the brief
// asks for, is close to nothing: one soft radial glow, dead center, breathing
// in and out of visibility the way a CRT's phosphor coating holds a faint
// afterglow between refreshes. No scanlines, no bands, no particles, no
// hue motion. If this were any quieter it would not be worth mounting a
// canvas for at all.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Full breath cycle: fade up, fade down. Slow enough to never be "noticed". */
const BREATH_PERIOD_MS = 34000;

/** Peak opacity at the brightest point of the breath.
 *
 * The breath multiplies this by (0.25 + 0.75 x phase), so the effective peak
 * is what matters, not this constant. At the original 0.025 that came to
 * 0.0177 and composited a maximum RGB delta of 5/255 over pure black -
 * subliminal rather than subtle. */
const PEAK_ALPHA = 0.065;

/** Glow radius as a fraction of the viewport's larger edge. */
const RADIUS_FRACTION = 0.55;

/**
 * Resolve the palette. No offscreen buffer needed: this is one gradient
 * fill per frame, cheap enough at full resolution.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; palette is size-free)
 * @returns {{bg: string, tint: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg', '#000000');
    const tint = rgbTriple(readVar('--color-accent', '#00CD00'), '0, 205, 0');
    return { bg, tint };
}

/**
 * Draw one frame: flat black fill, then a single centered radial glow whose
 * opacity rides a slow sine wave.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    // 0..1..0 across the period, never fully off so the breath reads as
    // continuous rather than as a pulse with a dead gap.
    const phase = (Math.sin((t / BREATH_PERIOD_MS) * Math.PI * 2) + 1) / 2;
    const alpha = PEAK_ALPHA * (0.25 + 0.75 * phase);

    const cx = env.width / 2;
    const cy = env.height / 2;
    const r = Math.max(env.width, env.height) * RADIUS_FRACTION;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    grad.addColorStop(0, `rgba(${state.tint}, ${alpha})`);
    grad.addColorStop(1, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the glow held at its resting (quietest) point rather
 * than mid-breath, so a static mount still reads as "almost nothing".
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    const cx = env.width / 2;
    const cy = env.height / 2;
    const r = Math.max(env.width, env.height) * RADIUS_FRACTION;
    const alpha = PEAK_ALPHA * 0.25;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    grad.addColorStop(0, `rgba(${state.tint}, ${alpha})`);
    grad.addColorStop(1, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

const effect = createEffect({
    id: 'terminal',
    // The entire animation is an opacity ramp; a low cap costs nothing.
    fps: 12,
    fpsMobile: 8,
    background: '#000000',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
