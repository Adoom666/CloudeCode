// Dracula theme - the canonical dev theme. This is the one theme in the set
// built for people who want calm, so it is the most restrained effect here.
//
// Two fixed radial blooms, one anchored top-right and one bottom-left, that
// slowly cross-fade through the palette's own accent hues (purple, pink,
// cyan, green) and breathe in radius. Nothing drifts and nothing sweeps;
// the only motion is colour and a gentle scale, both on cycles well over a
// minute, so it reads as "the light in the room changed slightly" rather
// than as an animation.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Milliseconds for one full pass through the hue cycle. Deliberately glacial. */
const HUE_CYCLE_MS = 90000;

/** Milliseconds per breathing (radius/alpha) cycle. */
const BREATHE_PERIOD_MS = 34000;

/** Peak opacity of each bloom at the top of its breath. */
const PEAK_ALPHA = 0.05;

/**
 * Linearly interpolate between two "r, g, b" triples.
 * @param {string} a First triple, e.g. "189, 147, 249"
 * @param {string} b Second triple
 * @param {number} f Blend factor, 0 returns a, 1 returns b
 * @returns {string} Interpolated "r, g, b" triple
 */
function lerpTriple(a, b, f) {
    const pa = a.split(',').map(Number);
    const pb = b.split(',').map(Number);
    const out = pa.map((v, i) => Math.round(v + (pb[i] - v) * f));
    return out.join(', ');
}

/**
 * Resolve the palette. Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; nothing here is size-baked)
 * @returns {{bg: string, hues: string[]}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg-page', '#1e1f29');
    const hues = [
        rgbTriple(readVar('--color-accent', '#bd93f9'), '189, 147, 249'),
        rgbTriple(readVar('--color-accent-strong', '#ff95d2'), '255, 149, 210'),
        rgbTriple(readVar('--color-info', '#8be9fd'), '139, 233, 253'),
        rgbTriple(readVar('--color-success', '#50fa7b'), '80, 250, 123'),
    ];
    return { bg, hues };
}

/**
 * Blend across the hue cycle at a given instant. The cycle visits each hue in
 * order and cross-fades smoothly between neighbours, looping.
 * @param {string[]} hues Palette triples
 * @param {number} t Milliseconds elapsed since mount
 * @param {number} offset Fractional phase offset (0 to 1) so the two blooms
 *   never share the same hue at the same instant
 * @returns {string} Current blended "r, g, b" triple
 */
function currentHue(hues, t, offset) {
    const frac = (((t / HUE_CYCLE_MS) + offset) % 1 + 1) % 1;
    const scaled = frac * hues.length;
    const i = Math.floor(scaled) % hues.length;
    const j = (i + 1) % hues.length;
    return lerpTriple(hues[i], hues[j], scaled - Math.floor(scaled));
}

/**
 * Draw the two blooms at a given instant.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{hues: string[]}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawBlooms(ctx, env, state, t) {
    const maxEdge = Math.max(env.width, env.height);
    const spots = [
        { cx: env.width * 0.88, cy: env.height * 0.10, base: 0.30, hueOffset: 0, phase: 0 },
        { cx: env.width * 0.10, cy: env.height * 0.92, base: 0.26, hueOffset: 0.5, phase: Math.PI },
    ];
    for (const s of spots) {
        const tint = currentHue(state.hues, t, s.hueOffset);
        const breathe = (t / BREATHE_PERIOD_MS) * Math.PI * 2 + s.phase;
        const level = 0.55 + 0.45 * Math.sin(breathe);
        const alpha = PEAK_ALPHA * level;
        const r = maxEdge * s.base * (0.9 + 0.1 * level);
        const grad = ctx.createRadialGradient(s.cx, s.cy, 0, s.cx, s.cy, r);
        grad.addColorStop(0, `rgba(${tint}, ${alpha})`);
        grad.addColorStop(1, `rgba(${tint}, 0)`);
        ctx.fillStyle = grad;
        ctx.fillRect(s.cx - r, s.cy - r, r * 2, r * 2);
    }
}

/**
 * Draw one frame: background plus the two breathing, colour-cycling blooms.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    drawBlooms(ctx, env, state, t);
}

/**
 * Reduced-motion frame: blooms frozen at their seed hue and breath level.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    drawBlooms(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'dracula',
    // Nothing here moves faster than a 34s breath; 12fps is more than enough
    // and keeps this the cheapest effect in the set, fitting for calm.
    fps: 12,
    fpsMobile: 8,
    background: '#1e1f29',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
