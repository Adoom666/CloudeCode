// Black Market theme - VIP basement door, watched from the shadows.
//
// Two parts, deliberately unlike lovecraft's free-drifting fog banks: two
// fixed-position violet vignettes that breathe in place rather than travel
// (the room does not move, the light in it does), plus a single diagonal
// surveillance-sweep beam that crosses the screen on a slow corner-to-corner
// diagonal, the way a camera or a flashlight beam would scan a room. Nothing
// here drifts freely; everything either breathes or sweeps on a fixed path.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Milliseconds per vignette breathing cycle. Slow enough to feel like a room, not a light show. */
const BREATHE_PERIOD_MS = 52000;

/** Peak opacity of each vignette at the top of its breath. */
const BREATHE_ALPHA = 0.045;

/** Milliseconds for the sweep beam to cross the diagonal once. */
const SWEEP_PERIOD_MS = 38000;

/** Sweep beam width as a fraction of the viewport diagonal. */
const SWEEP_BAND_FRACTION = 0.16;

/** Peak opacity of the sweep beam. */
const SWEEP_ALPHA = 0.05;

/**
 * Resolve the palette. Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; nothing here is size-baked)
 * @returns {{bg: string, tint: string, tint2: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg-page', '#000000');
    const accent = readVar('--color-accent', '#9D4EDD');
    const accentStrong = readVar('--color-accent-strong', '#B57BFF');
    return {
        bg,
        tint: rgbTriple(accent, '157, 78, 221'),
        tint2: rgbTriple(accentStrong, '181, 123, 255'),
    };
}

/**
 * Draw the two fixed-position vignettes, each breathing on its own phase so
 * they never peak together.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{tint: string, tint2: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawVignettes(ctx, env, state, t) {
    const maxEdge = Math.max(env.width, env.height);
    const spots = [
        { cx: env.width * 0.18, cy: env.height * 0.22, r: maxEdge * 0.34, tint: state.tint, phase: 0 },
        { cx: env.width * 0.82, cy: env.height * 0.78, r: maxEdge * 0.30, tint: state.tint2, phase: Math.PI },
    ];
    for (const s of spots) {
        const phase = (t / BREATHE_PERIOD_MS) * Math.PI * 2 + s.phase;
        const alpha = BREATHE_ALPHA * (0.3 + 0.7 * (0.5 + 0.5 * Math.sin(phase)));
        const grad = ctx.createRadialGradient(s.cx, s.cy, 0, s.cx, s.cy, s.r);
        grad.addColorStop(0, `rgba(${s.tint}, ${alpha})`);
        grad.addColorStop(1, `rgba(${s.tint}, 0)`);
        ctx.fillStyle = grad;
        ctx.fillRect(s.cx - s.r, s.cy - s.r, s.r * 2, s.r * 2);
    }
}

/**
 * Draw the diagonal sweep beam at a given instant. Travels from top-left to
 * bottom-right along the viewport diagonal and loops.
 *
 * Built entirely from `createLinearGradient(x0, y0, x1, y1)` anchored at the
 * two corners, with the moving band expressed as shifting colour-stop
 * offsets rather than a rotated/translated draw surface (the harness canvas
 * only guarantees fillRect + gradients + drawImage, so every effect in this
 * set is built on those primitives alone).
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{tint: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawSweep(ctx, env, state, t) {
    const diag = Math.hypot(env.width, env.height);
    const bandW = diag * SWEEP_BAND_FRACTION;
    const travel = diag + bandW * 2;
    const center = ((t % SWEEP_PERIOD_MS) / SWEEP_PERIOD_MS) * travel - bandW;

    const clamp01 = (v) => Math.min(1, Math.max(0, v));
    const stopStart = clamp01((center - bandW) / diag);
    const stopMid = clamp01(center / diag);
    const stopEnd = clamp01((center + bandW) / diag);

    const grad = ctx.createLinearGradient(0, 0, env.width, env.height);
    grad.addColorStop(stopStart, `rgba(${state.tint}, 0)`);
    grad.addColorStop(stopMid, `rgba(${state.tint}, ${SWEEP_ALPHA})`);
    grad.addColorStop(stopEnd, `rgba(${state.tint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Draw one frame: background, breathing vignettes, sweep beam.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    drawVignettes(ctx, env, state, t);
    drawSweep(ctx, env, state, t);
}

/**
 * Reduced-motion frame: vignettes frozen at their seed phase, no sweep.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    drawVignettes(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'black_market',
    // Both motions run tens of seconds per cycle; 14fps is ample.
    fps: 14,
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
