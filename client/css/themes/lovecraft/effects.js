// HP Lovecraft theme - abyssal fog banks drifting behind the terminal.
//
// A continuous-field effect rather than a particle or raster one: a handful of
// very large, very soft radial gradients sliding past each other on unrelated
// slow sinusoids, so the surface never repeats and nothing ever resolves into
// a shape the eye wants to track. Full drift cycles run 40 to 110 seconds.
//
// Fill-rate, not geometry, is the cost of soft gradients this large, so the
// field is rendered to a half-resolution offscreen buffer and scaled up. Fog
// has no high-frequency detail to lose, and it quarters the pixels touched.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Offscreen buffer scale. Fog carries no detail that survives at 1:1 anyway. */
const BUFFER_SCALE = 0.5;

/** Number of fog banks on desktop and on mobile. */
const BANKS_DESKTOP = 6;
const BANKS_MOBILE = 4;

/**
 * Build one fog bank with its own drift period, so no two banks ever share a
 * rhythm and the field never visibly loops.
 * @param {number} i Bank index, used to spread the seeds deterministically
 * @param {string[]} tints Palette triples, e.g. ["74, 132, 114"]
 * @returns {{bx: number, by: number, ax: number, ay: number, px: number,
 *   py: number, phx: number, phy: number, r: number, alpha: number, tint: string}}
 */
function makeBank(i, tints) {
    return {
        // Base position as a fraction of the viewport.
        bx: 0.12 + Math.random() * 0.76,
        by: 0.12 + Math.random() * 0.76,
        // Drift amplitude as a fraction of the viewport.
        ax: 0.06 + Math.random() * 0.14,
        ay: 0.04 + Math.random() * 0.1,
        // Drift periods in ms, deliberately coprime-ish across banks.
        px: 40000 + Math.random() * 70000,
        py: 47000 + Math.random() * 63000,
        phx: Math.random() * Math.PI * 2,
        phy: Math.random() * Math.PI * 2,
        // Radius as a fraction of the viewport's larger edge.
        r: 0.3 + Math.random() * 0.28,
        alpha: 0.05 + Math.random() * 0.035,
        tint: tints[i % tints.length],
    };
}

/**
 * Resolve the palette, size the offscreen buffer and seed the fog banks.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{banks: object[], buffer: HTMLCanvasElement|null,
 *   bctx: CanvasRenderingContext2D|null, bw: number, bh: number, bg: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg-page', '#04080a');
    const tints = [
        rgbTriple(readVar('--color-accent', '#4a8472'), '74, 132, 114'),
        rgbTriple(readVar('--color-info', '#498397'), '73, 131, 151'),
        rgbTriple(readVar('--color-success', '#6e9a78'), '110, 154, 120'),
    ];

    const count = env.isMobile ? BANKS_MOBILE : BANKS_DESKTOP;
    const banks = new Array(count);
    for (let i = 0; i < count; i++) banks[i] = makeBank(i, tints);

    const bw = Math.max(1, Math.floor(env.width * BUFFER_SCALE));
    const bh = Math.max(1, Math.floor(env.height * BUFFER_SCALE));
    const buffer = document.createElement('canvas');
    buffer.width = bw;
    buffer.height = bh;
    const bctx = buffer.getContext('2d');

    return { banks, buffer, bctx, bw, bh, bg };
}

/**
 * Render the fog field into the offscreen buffer at a given instant.
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function renderField(state, t) {
    const { bctx, bw, bh, banks, bg } = state;
    bctx.fillStyle = bg;
    bctx.fillRect(0, 0, bw, bh);

    const maxEdge = Math.max(bw, bh);
    for (let i = 0; i < banks.length; i++) {
        const b = banks[i];
        const cx = (b.bx + b.ax * Math.sin((t / b.px) * Math.PI * 2 + b.phx)) * bw;
        const cy = (b.by + b.ay * Math.sin((t / b.py) * Math.PI * 2 + b.phy)) * bh;
        const r = b.r * maxEdge;

        const grad = bctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${b.tint}, ${b.alpha})`);
        grad.addColorStop(0.55, `rgba(${b.tint}, ${b.alpha * 0.45})`);
        grad.addColorStop(1, `rgba(${b.tint}, 0)`);
        bctx.fillStyle = grad;
        bctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }
}

/**
 * Draw one frame: render the field offscreen, then scale it onto the canvas.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    if (!state.bctx) {
        // No offscreen buffer: paint the flat backdrop rather than leaving a
        // transparent canvas over whatever was there before.
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, env.width, env.height);
        return;
    }
    renderField(state, t);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the fog field frozen at its seed instant.
 * The atmosphere survives, the drift does not.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'lovecraft',
    // The fastest bank crosses about 6 percent of the viewport per 10 seconds.
    // 12fps is far more than that motion can consume.
    fps: 12,
    fpsMobile: 8,
    background: '#04080a',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
