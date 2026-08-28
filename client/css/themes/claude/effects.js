// Claude theme - a barely-there warm ambient glow that breathes behind the
// terminal.
//
// The simplest effect in this set: a single soft radial gradient held near
// the centre of the viewport, its opacity rising and falling on one slow
// sine wave. Nothing moves across the screen; the only motion is the glow
// itself getting very slightly warmer and cooler, the way a room does with
// the light changing outside. A second, much slower sine nudges the glow's
// centre by a few percent of the viewport so it never looks perfectly
// static, without ever reading as movement. Full breathe cycle: 42 seconds.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded
// beyond the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Full breathe cycle, low point to low point. */
const BREATHE_PERIOD_MS = 42000;

/** Full drift cycle for the glow's centre. Much slower than the breathe. */
const DRIFT_PERIOD_MS = 97000;

/** Opacity floor and ceiling of the glow at its dimmest and brightest. */
const ALPHA_MIN = 0.015;
const ALPHA_MAX = 0.05;

/** Glow radius as a fraction of the viewport's larger edge. */
const RADIUS_FRACTION = 0.55;

/** How far the glow's centre drifts, as a fraction of the viewport. */
const DRIFT_FRACTION = 0.06;

/**
 * Resolve the palette. Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; geometry is fractional)
 * @returns {{bg: string, tint: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg-page', '#0a0a0a');
    const tint = rgbTriple(readVar('--color-accent', '#d77757'), '215, 119, 87');
    return { bg, tint };
}

/**
 * Draw one frame: flat backdrop, then the single breathing glow.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    const breathe = (Math.sin((t / BREATHE_PERIOD_MS) * Math.PI * 2) + 1) / 2;
    const alpha = ALPHA_MIN + (ALPHA_MAX - ALPHA_MIN) * breathe;

    const driftX = Math.sin((t / DRIFT_PERIOD_MS) * Math.PI * 2) * DRIFT_FRACTION;
    const driftY = Math.cos((t / (DRIFT_PERIOD_MS * 1.3)) * Math.PI * 2) * DRIFT_FRACTION;
    const cx = env.width * (0.5 + driftX);
    const cy = env.height * (0.46 + driftY);
    const r = Math.max(env.width, env.height) * RADIUS_FRACTION;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    grad.addColorStop(0, `rgba(${state.tint}, ${alpha})`);
    grad.addColorStop(0.6, `rgba(${state.tint}, ${alpha * 0.4})`);
    grad.addColorStop(1, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the glow frozen at its midpoint brightness.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{bg: string, tint: string}} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, BREATHE_PERIOD_MS / 4);
}

const effect = createEffect({
    id: 'claude',
    // A single gradient repaint. Cheap; the cap exists only to keep this off
    // the terminal's compositing budget, not because the draw is expensive.
    fps: 15,
    fpsMobile: 10,
    background: '#0a0a0a',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
