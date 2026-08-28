// Jagermeister theme - forest-bottle green and heritage orange, 1878 ritual
// gravitas.
//
// A continuous-field effect like lovecraft's, but the motion is circular
// rather than free sinusoidal drift: each blob orbits a shared center on its
// own radius and period, which reads as a slow liquid swirl rather than fog
// banks wandering independently. Green and orange blobs alternate so the
// swirl carries both halves of the palette.
//
// Fill-rate is the cost of soft gradients this large, so the field renders
// to a half-resolution offscreen buffer and scales up, same as lovecraft.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Offscreen buffer scale. The swirl carries no detail that survives at 1:1 anyway. */
const BUFFER_SCALE = 0.5;

/** Number of orbiting blobs on desktop and on mobile. */
const BLOBS_DESKTOP = 5;
const BLOBS_MOBILE = 3;

/**
 * Build one orbiting blob. Orbit radius and period are randomised per blob so
 * the swirl never visibly loops, and blobs alternate tint so green and
 * orange both read through the motion.
 * @param {number} i Blob index, used to alternate tint and spread phase
 * @param {string[]} tints Palette triples, e.g. ["30, 86, 50"]
 * @returns {{orbitR: number, period: number, phase: number, r: number,
 *   alpha: number, tint: string}}
 */
function makeBlob(i, tints) {
    return {
        // Orbit radius as a fraction of the viewport's larger edge.
        orbitR: 0.14 + Math.random() * 0.22,
        // Orbit period in ms. Long and deliberately non-shared across blobs.
        period: 55000 + Math.random() * 50000,
        phase: Math.random() * Math.PI * 2,
        // Blob radius as a fraction of the viewport's larger edge.
        r: 0.26 + Math.random() * 0.20,
        alpha: 0.045 + Math.random() * 0.03,
        tint: tints[i % tints.length],
    };
}

/**
 * Resolve the palette, size the offscreen buffer and seed the orbiting blobs.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{blobs: object[], buffer: HTMLCanvasElement|null,
 *   bctx: CanvasRenderingContext2D|null, bw: number, bh: number, bg: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg-page', '#081f12');
    const tints = [
        rgbTriple(readVar('--color-success', '#7EBC5E'), '126, 188, 94'),
        rgbTriple(readVar('--color-accent', '#F18A00'), '241, 138, 0'),
    ];

    const count = env.isMobile ? BLOBS_MOBILE : BLOBS_DESKTOP;
    const blobs = new Array(count);
    for (let i = 0; i < count; i++) blobs[i] = makeBlob(i, tints);

    const bw = Math.max(1, Math.floor(env.width * BUFFER_SCALE));
    const bh = Math.max(1, Math.floor(env.height * BUFFER_SCALE));
    const buffer = document.createElement('canvas');
    buffer.width = bw;
    buffer.height = bh;
    const bctx = buffer.getContext('2d');

    return { blobs, buffer, bctx, bw, bh, bg };
}

/**
 * Render the swirl field into the offscreen buffer at a given instant. Every
 * blob orbits the same center point, so the whole field reads as one slow
 * rotating liquid rather than independent wandering.
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function renderField(state, t) {
    const { bctx, bw, bh, blobs, bg } = state;
    bctx.fillStyle = bg;
    bctx.fillRect(0, 0, bw, bh);

    const maxEdge = Math.max(bw, bh);
    const ccx = bw / 2;
    const ccy = bh / 2;
    for (let i = 0; i < blobs.length; i++) {
        const b = blobs[i];
        const angle = (t / b.period) * Math.PI * 2 + b.phase;
        const cx = ccx + Math.cos(angle) * b.orbitR * maxEdge;
        const cy = ccy + Math.sin(angle) * b.orbitR * maxEdge;
        const r = b.r * maxEdge;

        const grad = bctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${b.tint}, ${b.alpha})`);
        grad.addColorStop(0.6, `rgba(${b.tint}, ${b.alpha * 0.4})`);
        grad.addColorStop(1, `rgba(${b.tint}, 0)`);
        bctx.fillStyle = grad;
        bctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }
}

/**
 * Draw one frame: render the swirl offscreen, then scale it onto the canvas.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    if (!state.bctx) {
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, env.width, env.height);
        return;
    }
    renderField(state, t);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the swirl frozen at its seed instant.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'jagermeister',
    // The fastest orbit crosses well under a tenth of the viewport per
    // second; 12fps is more than that motion can consume.
    fps: 12,
    fpsMobile: 8,
    background: '#081f12',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
