// Acid Trip theme - slow complementary color blobs drifting behind the text.
//
// PRE-EXISTING MOTION, READ FIRST: theme.css already runs a 60s linear
// hue-rotate filter on <body> (acidHueShift) plus an 8s accent glow pulse on
// buttons (acidAccentBreath). The body filter applies to the whole subtree,
// including a fixed-position canvas appended to document.body, so this
// canvas's own paint gets the same hue sweep for free - a second hue-rotate
// here would double-apply on top of that and the two 60s/8s clocks would beat
// against each other. So this effect does NOT touch hue at all. It supplies
// the one thing the CSS animation cannot: spatial movement. A handful of
// soft, oversized color-wheel blobs (the yellow/pink/cyan/green already in
// the theme's own vars) drift past each other on unrelated slow sinusoids,
// giving the "chromatic warp" the brief asks for as motion in space while the
// CSS supplies motion in color. Same continuous-field technique as lovecraft,
// half-resolution offscreen buffer for the soft gradients.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Offscreen buffer scale. Soft blobs carry no detail worth full resolution. */
const BUFFER_SCALE = 0.5;

/** Number of color blobs on desktop and on mobile. */
const BLOBS_DESKTOP = 5;
const BLOBS_MOBILE = 3;

/**
 * Build one drifting color blob with its own period, so no two blobs ever
 * share a rhythm and the field never visibly loops.
 * @param {number} i Blob index, used to spread the seeds deterministically
 * @param {string[]} tints Palette triples, e.g. ["0, 255, 224"]
 * @returns {{bx: number, by: number, ax: number, ay: number, px: number,
 *   py: number, phx: number, phy: number, r: number, alpha: number, tint: string}}
 */
function makeBlob(i, tints) {
    return {
        bx: 0.1 + Math.random() * 0.8,
        by: 0.1 + Math.random() * 0.8,
        ax: 0.08 + Math.random() * 0.16,
        ay: 0.06 + Math.random() * 0.14,
        // 26-45s periods: slower than the 60s hue sweep, faster than nothing,
        // deliberately not a clean divisor of it so the two never re-sync.
        px: 26000 + Math.random() * 19000,
        py: 31000 + Math.random() * 21000,
        phx: Math.random() * Math.PI * 2,
        phy: Math.random() * Math.PI * 2,
        r: 0.26 + Math.random() * 0.22,
        alpha: 0.035 + Math.random() * 0.02,
        tint: tints[i % tints.length],
    };
}

/**
 * Resolve the palette, size the offscreen buffer and seed the color blobs.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} _ctx Destination context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{blobs: object[], buffer: HTMLCanvasElement|null,
 *   bctx: CanvasRenderingContext2D|null, bw: number, bh: number, bg: string}} Draw state
 */
function setup(_ctx, env) {
    const bg = readVar('--color-bg-page', '#0e0420');
    const tints = [
        rgbTriple(readVar('--color-fg', '#FFEE00'), '255, 238, 0'),
        rgbTriple(readVar('--color-border', '#FF00C8'), '255, 0, 200'),
        rgbTriple(readVar('--color-accent', '#00FFE0'), '0, 255, 224'),
        rgbTriple(readVar('--color-success', '#00FF40'), '0, 255, 64'),
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
 * Render the blob field into the offscreen buffer at a given instant.
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function renderField(state, t) {
    const { bctx, bw, bh, blobs, bg } = state;
    bctx.fillStyle = bg;
    bctx.fillRect(0, 0, bw, bh);

    const maxEdge = Math.max(bw, bh);
    for (let i = 0; i < blobs.length; i++) {
        const b = blobs[i];
        const cx = (b.bx + b.ax * Math.sin((t / b.px) * Math.PI * 2 + b.phx)) * bw;
        const cy = (b.by + b.ay * Math.sin((t / b.py) * Math.PI * 2 + b.phy)) * bh;
        const r = b.r * maxEdge;

        const grad = bctx.createRadialGradient(cx, cy, 0, cx, cy, r);
        grad.addColorStop(0, `rgba(${b.tint}, ${b.alpha})`);
        grad.addColorStop(0.5, `rgba(${b.tint}, ${b.alpha * 0.4})`);
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
        ctx.fillStyle = state.bg;
        ctx.fillRect(0, 0, env.width, env.height);
        return;
    }
    renderField(state, t);
    ctx.drawImage(state.buffer, 0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the blob field frozen at its seed instant. The body
 * hue-rotate is already disabled under reduced motion by theme.css, so a
 * frozen frame here does not clash with anything still moving.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    draw(ctx, env, state, 0);
}

const effect = createEffect({
    id: 'acid_trip',
    // Slowest blob crosses roughly 8 percent of the viewport per 13 seconds;
    // 14fps is comfortably more than that motion needs.
    fps: 14,
    fpsMobile: 10,
    background: '#0e0420',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
