// Calming theme - two soft mist patches drifting at near-imperceptible speed.
//
// PRE-EXISTING MOTION, READ FIRST: theme.css already runs calmingBreath, a
// 30s ease-in-out background-color animation on <body> between the theme's
// two bg tones - that IS this theme's "breathing" and owns the word. Stacking
// a second full-canvas breathing gradient underneath it would be the same
// motion twice, so this effect does not pulse opacity or color at all. What
// it adds instead is pure spatial drift: two large, very faint mist patches
// sliding across the page on 90-140 second cycles, an order of magnitude
// slower than the CSS breath so the two never read as one animation. The
// brief's own instruction was "if in doubt, halve it" - this halves the
// lovecraft-class effect twice over: half as many banks, half the alpha
// ceiling, and roughly four times the period.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Offscreen buffer scale. */
const BUFFER_SCALE = 0.5;

/** Mist patches on desktop and on mobile - deliberately sparse. */
const PATCHES_DESKTOP = 2;
const PATCHES_MOBILE = 2;

/**
 * Build one mist patch with a very long, unshared drift period.
 * @param {number} i Patch index, used to spread the seeds deterministically
 * @param {string[]} tints Palette triples, e.g. ["123, 163, 116"]
 * @returns {{bx: number, by: number, ax: number, ay: number, px: number,
 *   py: number, phx: number, phy: number, r: number, alpha: number, tint: string}}
 */
function makePatch(i, tints) {
    return {
        bx: 0.15 + Math.random() * 0.7,
        by: 0.15 + Math.random() * 0.7,
        ax: 0.05 + Math.random() * 0.08,
        ay: 0.04 + Math.random() * 0.06,
        // 90-140s periods - roughly 4x the 30s CSS breath, never in phase.
        px: 90000 + Math.random() * 50000,
        py: 100000 + Math.random() * 40000,
        phx: Math.random() * Math.PI * 2,
        phy: Math.random() * Math.PI * 2,
        r: 0.34 + Math.random() * 0.16,
        // This theme asks to be the faintest thing in the app, but it is a
        // LIGHT theme (#F5F1E8): a dark tint has far less headroom against
        // cream than a bright tint has against black, so the same alpha buys
        // less delta here. At 0.02-0.035 the banks composited a maximum RGB
        // delta of 7/255. Still the faintest in the app, now measurably so.
        alpha: 0.05 + Math.random() * 0.03,
        tint: tints[i % tints.length],
    };
}

/**
 * Resolve the palette, size the offscreen buffer and seed the mist patches.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{patches: object[], buffer: HTMLCanvasElement|null,
 *   bctx: CanvasRenderingContext2D|null, bw: number, bh: number, bg: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg', '#F5F1E8');
    const tints = [
        rgbTriple(readVar('--color-accent', '#6b9563'), '107, 149, 99'),
        rgbTriple(readVar('--color-info', '#4d728a'), '77, 114, 138'),
    ];

    const count = env.isMobile ? PATCHES_MOBILE : PATCHES_DESKTOP;
    const patches = new Array(count);
    for (let i = 0; i < count; i++) patches[i] = makePatch(i, tints);

    const bw = Math.max(1, Math.floor(env.width * BUFFER_SCALE));
    const bh = Math.max(1, Math.floor(env.height * BUFFER_SCALE));
    const buffer = document.createElement('canvas');
    buffer.width = bw;
    buffer.height = bh;
    const bctx = buffer.getContext('2d');

    return { patches, buffer, bctx, bw, bh, bg };
}

/**
 * Render the mist field into the offscreen buffer at a given instant.
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function renderField(state, t) {
    const { bctx, bw, bh, patches, bg } = state;
    bctx.fillStyle = bg;
    bctx.fillRect(0, 0, bw, bh);

    const maxEdge = Math.max(bw, bh);
    for (let i = 0; i < patches.length; i++) {
        const p = patches[i];
        const cx = (p.bx + p.ax * Math.sin((t / p.px) * Math.PI * 2 + p.phx)) * bw;
        const cy = (p.by + p.ay * Math.sin((t / p.py) * Math.PI * 2 + p.phy)) * bh;
        const r = p.r * maxEdge;

        const grad = bctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${p.tint}, ${p.alpha})`);
        grad.addColorStop(0.6, `rgba(${p.tint}, ${p.alpha * 0.4})`);
        grad.addColorStop(1, `rgba(${p.tint}, 0)`);
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
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, env.width, env.height);
        return;
    }
    renderField(state, t);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the mist field frozen at its seed instant.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'calming',
    // The whole field moves a few percent of the viewport per minute-plus;
    // a low cap is more than this motion can ever need.
    fps: 12,
    fpsMobile: 8,
    background: '#F5F1E8',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
