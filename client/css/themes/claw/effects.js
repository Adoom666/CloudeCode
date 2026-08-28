// Claw theme - a sparse, static field of slowly twinkling stars.
//
// Every star is placed once, in setup(), and never moves again: only its
// opacity drifts on its own private sine wave, each with a different period
// and phase so the field never pulses in unison. This is deliberately NOT a
// particle-rain effect (see matrix); the whole point is a field that reads
// as depth, not as traffic - a static starfield wash, not falling anything.
// Individual twinkle cycles run 22 to 48 seconds.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded
// beyond the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Roughly one star per this many square CSS pixels, desktop. */
const DENSITY_DESKTOP = 9000;

/** Sparser on mobile: smaller viewport, same felt density, fewer draws. */
const DENSITY_MOBILE = 13000;

/** Hard cap so an ultrawide monitor cannot balloon the star count. */
const MAX_STARS = 220;

/** Floor so a tiny viewport still reads as a field, not three dots. */
const MIN_STARS = 24;

/**
 * Build one star: a fixed point, a size class, a private twinkle rhythm.
 * @param {number} width Viewport width in CSS pixels
 * @param {number} height Viewport height in CSS pixels
 * @param {string[]} tints Palette triples to draw from
 * @returns {{x: number, y: number, r: number, period: number, phase: number,
 *   alphaMin: number, alphaMax: number, tint: string}} One star's parameters
 */
function makeStar(width, height, tints) {
    const dim = Math.random() < 0.8;
    return {
        x: Math.random() * width,
        y: Math.random() * height,
        r: dim ? 0.6 + Math.random() * 0.5 : 1.0 + Math.random() * 0.8,
        period: 22000 + Math.random() * 26000,
        phase: Math.random() * Math.PI * 2,
        alphaMin: dim ? 0.01 : 0.02,
        alphaMax: dim ? 0.035 : 0.07,
        tint: tints[(Math.random() * tints.length) | 0],
    };
}

/**
 * Resolve the palette and seed the star field. Runs on init and on every
 * resize, so a resize reseeds the field rather than stretching it.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{stars: object[], bg: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg-page', '#050308');
    const tints = [
        rgbTriple(readVar('--color-fg', '#e6e6ea'), '230, 230, 234'),
        rgbTriple(readVar('--color-accent', '#ff5b5b'), '255, 91, 91'),
        rgbTriple(readVar('--color-info', '#7cc6ff'), '124, 198, 255'),
    ];
    const density = env.isMobile ? DENSITY_MOBILE : DENSITY_DESKTOP;
    const count = Math.min(
        MAX_STARS,
        Math.max(MIN_STARS, Math.floor((env.width * env.height) / density)),
    );
    const stars = new Array(count);
    for (let i = 0; i < count; i++) stars[i] = makeStar(env.width, env.height, tints);
    return { stars, bg };
}

/**
 * Draw one frame: flat backdrop, then every star at its own twinkle phase.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{stars: object[], bg: string}} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    for (let i = 0; i < state.stars.length; i++) {
        const s = state.stars[i];
        const wave = (Math.sin((t / s.period) * Math.PI * 2 + s.phase) + 1) / 2;
        const alpha = s.alphaMin + (s.alphaMax - s.alphaMin) * wave;
        ctx.fillStyle = `rgba(${s.tint}, ${alpha})`;
        // A filled square rather than an arc: at sub-2px radii the two are
        // visually indistinguishable, and a square needs only fillRect,
        // which keeps this effect on the same minimal canvas API surface
        // every other theme in the set uses.
        ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
    }
}

/**
 * Reduced-motion frame: every star frozen at its own twinkle midpoint.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{stars: object[], bg: string}} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    for (let i = 0; i < state.stars.length; i++) {
        const s = state.stars[i];
        const alpha = (s.alphaMin + s.alphaMax) / 2;
        ctx.fillStyle = `rgba(${s.tint}, ${alpha})`;
        ctx.fillRect(s.x - s.r, s.y - s.r, s.r * 2, s.r * 2);
    }
}

const effect = createEffect({
    id: 'claw',
    // Twinkle is per-star opacity only; the fastest star cycle is 22s, far
    // slower than this cap can resolve.
    fps: 14,
    fpsMobile: 10,
    background: '#050308',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
