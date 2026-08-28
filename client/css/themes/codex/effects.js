// Codex theme - a faint indigo wash drifting behind soft white surfaces.
//
// Two large, very soft radial washes drift past each other on independent
// slow sinusoids - the same continuous-field idea as the lovecraft fog, but
// tuned for a LIGHT surface: peak opacity is kept low enough that the page
// never reads as darkened, only very faintly tinted, and the tint is the
// theme's own indigo accent rather than a hardcoded colour. Full drift
// cycles run 55 to 95 seconds.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded
// beyond the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Peak opacity ceiling. Kept low because the surface underneath is white. */
const ALPHA_MAX = 0.032;

/** Peak opacity floor, so the wash never fully vanishes between beats. */
const ALPHA_MIN = 0.018;

/**
 * Build one wash: a drifting soft radial gradient.
 * @param {string} tint Palette triple, e.g. "124, 92, 255"
 * @returns {object} Wash parameters consumed by draw()
 */
function makeWash(tint) {
    return {
        bx: 0.2 + Math.random() * 0.6,
        by: 0.2 + Math.random() * 0.6,
        ax: 0.10 + Math.random() * 0.16,
        ay: 0.08 + Math.random() * 0.12,
        px: 55000 + Math.random() * 40000,
        py: 60000 + Math.random() * 35000,
        phx: Math.random() * Math.PI * 2,
        phy: Math.random() * Math.PI * 2,
        r: 0.38 + Math.random() * 0.2,
        alpha: ALPHA_MIN + Math.random() * (ALPHA_MAX - ALPHA_MIN),
        tint,
    };
}

/**
 * Resolve the palette and seed the two washes. Runs on init and on every
 * resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {object} _env Viewport environment (unused; geometry is fractional)
 * @returns {{washes: object[], bg: string}} Draw state
 */
function setup(_ctx, _env) {
    const bg = readVar('--color-bg-page', '#eeeef3');
    const tint = rgbTriple(readVar('--color-accent', '#7c5cff'), '124, 92, 255');
    return { washes: [makeWash(tint), makeWash(tint)], bg };
}

/**
 * Draw one frame: opaque page-colour backdrop, then each wash at its current
 * drift position. Painted with plain source-over so the surface only ever
 * tints toward indigo, never toward black.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{washes: object[], bg: string}} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);

    const maxEdge = Math.max(env.width, env.height);
    for (let i = 0; i < state.washes.length; i++) {
        const w = state.washes[i];
        const cx = (w.bx + w.ax * Math.sin((t / w.px) * Math.PI * 2 + w.phx)) * env.width;
        const cy = (w.by + w.ay * Math.sin((t / w.py) * Math.PI * 2 + w.phy)) * env.height;
        const r = w.r * maxEdge;

        const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${w.tint}, ${w.alpha})`);
        grad.addColorStop(0.6, `rgba(${w.tint}, ${w.alpha * 0.4})`);
        grad.addColorStop(1, `rgba(${w.tint}, 0)`);
        ctx.fillStyle = grad;
        ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }
}

/**
 * Reduced-motion frame: both washes frozen at their seed instant.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{washes: object[], bg: string}} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'codex',
    // Slowest drift in the set (55s+ per cycle); nothing here needs more
    // than a slideshow's worth of frames per second.
    fps: 12,
    fpsMobile: 8,
    background: '#eeeef3',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
