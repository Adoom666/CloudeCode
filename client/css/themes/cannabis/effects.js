// Cannabis theme - deep forest color blooms with a slow brushed-brass sheen.
//
// PRE-EXISTING MOTION, READ FIRST: theme.css for this theme is empty, so
// there is no CSS animation to avoid stacking on.
//
// The manifest calls this "botanical luxury... anti-rasta", so the brief here
// is deliberately not a leaf, a smoke wisp or a red/gold/green flag - it is
// an expensive, quiet interior: deep forest color and brushed metal. Two
// unrelated layers do the work. A continuous field of soft forest-green
// blooms drifts the way still air over a greenhouse does - slow growth
// implied by motion, not shape. Over it, one long diagonal brass gradient
// band sweeps the full width on a very slow cycle, the way light catches
// brushed metal as you move past it; brief and gone before it ever reads as
// a "shine" effect. The two layers use unrelated periods so they never sync.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Offscreen buffer scale for the bloom field. */
const BUFFER_SCALE = 0.5;

/** Forest blooms on desktop and on mobile. */
const BLOOMS_DESKTOP = 4;
const BLOOMS_MOBILE = 3;

/** Brass sheen sweep period in ms. Long and unhurried, luxury pacing. */
const SHEEN_PERIOD_MS = 52000;

/** Brass sheen band width as a fraction of the viewport diagonal. */
const SHEEN_BAND_FRACTION = 0.22;

/** Peak opacity of the brass sheen at its brightest point. */
const SHEEN_ALPHA = 0.05;

/**
 * Build one forest bloom with its own drift period, so no two blooms ever
 * share a rhythm and the field never visibly loops.
 * @param {number} i Bloom index, used to spread the seeds deterministically
 * @param {string[]} tints Palette triples, e.g. ["123, 170, 94"]
 * @returns {{bx: number, by: number, ax: number, ay: number, px: number,
 *   py: number, phx: number, phy: number, r: number, alpha: number, tint: string}}
 */
function makeBloom(i, tints) {
    return {
        bx: 0.1 + Math.random() * 0.8,
        by: 0.1 + Math.random() * 0.8,
        ax: 0.05 + Math.random() * 0.09,
        ay: 0.04 + Math.random() * 0.07,
        px: 48000 + Math.random() * 34000,
        py: 55000 + Math.random() * 30000,
        phx: Math.random() * Math.PI * 2,
        phy: Math.random() * Math.PI * 2,
        r: 0.24 + Math.random() * 0.2,
        alpha: 0.03 + Math.random() * 0.018,
        tint: tints[i % tints.length],
    };
}

/**
 * Resolve the palette, size the offscreen buffer and seed the bloom field.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{blooms: object[], buffer: HTMLCanvasElement|null,
 *   bctx: CanvasRenderingContext2D|null, bw: number, bh: number, bg: string,
 *   brassTriple: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg-page', '#0e150d');
    const tints = [
        rgbTriple(readVar('--color-success', '#7BAA5E'), '123, 170, 94'),
        rgbTriple(readVar('--color-info', '#88AA88'), '136, 170, 136'),
        rgbTriple(readVar('--color-accent-strong', '#ba8f49'), '186, 143, 73'),
    ];
    const brassTriple = rgbTriple(readVar('--color-accent', '#C49C50'), '196, 156, 80');

    const count = env.isMobile ? BLOOMS_MOBILE : BLOOMS_DESKTOP;
    const blooms = new Array(count);
    for (let i = 0; i < count; i++) blooms[i] = makeBloom(i, tints);

    const bw = Math.max(1, Math.floor(env.width * BUFFER_SCALE));
    const bh = Math.max(1, Math.floor(env.height * BUFFER_SCALE));
    const buffer = document.createElement('canvas');
    buffer.width = bw;
    buffer.height = bh;
    const bctx = buffer.getContext('2d');

    return { blooms, buffer, bctx, bw, bh, bg, brassTriple };
}

/**
 * Render the bloom field into the offscreen buffer at a given instant.
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function renderBlooms(state, t) {
    const { bctx, bw, bh, blooms, bg } = state;
    bctx.fillStyle = bg;
    bctx.fillRect(0, 0, bw, bh);

    const maxEdge = Math.max(bw, bh);
    for (let i = 0; i < blooms.length; i++) {
        const b = blooms[i];
        const cx = (b.bx + b.ax * Math.sin((t / b.px) * Math.PI * 2 + b.phx)) * bw;
        const cy = (b.by + b.ay * Math.sin((t / b.py) * Math.PI * 2 + b.phy)) * bh;
        const r = b.r * maxEdge;

        const grad = bctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${b.tint}, ${b.alpha})`);
        grad.addColorStop(0.55, `rgba(${b.tint}, ${b.alpha * 0.42})`);
        grad.addColorStop(1, `rgba(${b.tint}, 0)`);
        bctx.fillStyle = grad;
        bctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }
}

/**
 * Draw the brass sheen band directly onto the destination canvas (full
 * resolution, since a hard-edged diagonal gradient benefits from it and it is
 * a single gradient fill, not a per-pixel cost).
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawSheen(ctx, env, state, t) {
    // The gradient's own start/end points carry the diagonal, so no canvas
    // transform (save/rotate) is needed - a linear gradient is free to run
    // at any angle across a straight fillRect.
    const w = env.width;
    const h = env.height;
    const diag = Math.sqrt(w * w + h * h);
    // Travels from off the top-left corner to off the bottom-right corner
    // and loops; phase runs 0..1 over one period.
    const phase = (t % SHEEN_PERIOD_MS) / SHEEN_PERIOD_MS;
    const bandWidth = diag * SHEEN_BAND_FRACTION;
    const travel = diag * 1.6;
    const offset = -diag * 0.3 + phase * travel;

    // Unit vector along the top-left to bottom-right diagonal.
    const ux = w / diag;
    const uy = h / diag;
    const x0 = ux * (offset - bandWidth);
    const y0 = uy * (offset - bandWidth);
    const x1 = ux * (offset + bandWidth);
    const y1 = uy * (offset + bandWidth);

    const grad = ctx.createLinearGradient(x0, y0, x1, y1);
    grad.addColorStop(0, `rgba(${state.brassTriple}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.brassTriple}, ${SHEEN_ALPHA})`);
    grad.addColorStop(1, `rgba(${state.brassTriple}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);
}

/**
 * Draw one frame: bloom field offscreen and scaled up, then the brass sheen
 * band composited on top at full resolution.
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
    renderBlooms(state, t);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
    drawSheen(ctx, env, state, t);
}

/**
 * Reduced-motion frame: the bloom field frozen, sheen band parked at its
 * quietest point (fully off to one side) rather than mid-sweep.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    if (!state.bctx) {
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, env.width, env.height);
        return;
    }
    renderBlooms(state, 0);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
}

const effect = createEffect({
    id: 'cannabis',
    // Blooms drift a few percent of the viewport per minute; the sheen band
    // is the fastest-moving element and still takes 52s edge to edge.
    fps: 14,
    fpsMobile: 9,
    background: '#0e150d',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
