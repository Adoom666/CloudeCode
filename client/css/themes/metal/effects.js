// Metal theme - brushed and blackened steel plate. Thrash discipline.
//
// A texture effect, not a particle system: a static fine vertical-grain
// pattern baked once (the microgrooves brushed metal actually has), with a
// single soft diagonal sheen band drifting very slowly across it, the way
// light catches an anisotropic surface as the viewing angle changes. The
// grain never redraws frame to frame; only the sheen moves.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Grain tile width in CSS pixels. Height is a single repeating column band. */
const GRAIN_TILE_W = 96;

/** Number of vertical grain lines baked into one tile. */
const GRAIN_LINES = 48;

/** Opacity range for individual grain lines, kept low so it reads as texture. */
const GRAIN_ALPHA_MIN = 0.015;
const GRAIN_ALPHA_MAX = 0.04;

/** Milliseconds for the sheen band to cross the viewport once. */
const SHEEN_PERIOD_MS = 42000;

/** Sheen band width as a fraction of the viewport diagonal. */
const SHEEN_BAND_FRACTION = 0.20;

/** Peak opacity of the sheen band. */
const SHEEN_ALPHA = 0.06;

/**
 * Build a repeating vertical brushed-grain pattern tile. Line placement and
 * opacity are randomised once per setup so the texture does not look like an
 * obvious repeat, while the tile itself still repeats seamlessly.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} tint "r, g, b" triple for the grain colour
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildGrainPattern(ctx, tint) {
    const tile = document.createElement('canvas');
    tile.width = GRAIN_TILE_W;
    tile.height = 4;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    for (let i = 0; i < GRAIN_LINES; i++) {
        const x = Math.random() * GRAIN_TILE_W;
        const alpha = GRAIN_ALPHA_MIN + Math.random() * (GRAIN_ALPHA_MAX - GRAIN_ALPHA_MIN);
        tctx.fillStyle = `rgba(${tint}, ${alpha})`;
        tctx.fillRect(x, 0, 1, 4);
    }
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the grain pattern.
 * Runs on init and on every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, sheenTint: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#050303');
    const fg = readVar('--color-fg-muted', '#9c948a');
    return {
        pattern: buildGrainPattern(ctx, rgbTriple(fg, '156, 148, 138')),
        bg,
        sheenTint: rgbTriple(readVar('--color-fg', '#E8E2D6'), '232, 226, 214'),
    };
}

/**
 * Paint the background and the static brushed-grain texture.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function paintBase(ctx, env, state) {
    ctx.fillStyle = state.bg;
    ctx.fillRect(0, 0, env.width, env.height);
    if (state.pattern) {
        ctx.fillStyle = state.pattern;
        ctx.fillRect(0, 0, env.width, env.height);
    }
}

/**
 * Draw the diagonal sheen band at a given instant.
 *
 * Built entirely from `createLinearGradient(x0, y0, x1, y1)` anchored at a
 * shallow-angle axis (not the true corner-to-corner diagonal, so the raking
 * light reads at a different angle than black_market's corner sweep), with
 * the moving band expressed as shifting colour-stop offsets rather than a
 * rotated draw surface (the harness canvas only guarantees fillRect +
 * gradients + drawImage, so every effect in this set is built on those
 * primitives alone).
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{sheenTint: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function drawSheen(ctx, env, state, t) {
    // Shallow-angle axis: mostly horizontal travel with a modest vertical
    // component, so the highlight reads as raking light rather than a
    // corner-to-corner spotlight sweep.
    const ax = env.width;
    const ay = env.height * 0.35;
    const axisLen = Math.hypot(ax, ay);
    const bandW = axisLen * SHEEN_BAND_FRACTION;
    const travel = axisLen + bandW * 2;
    const center = ((t % SHEEN_PERIOD_MS) / SHEEN_PERIOD_MS) * travel - bandW;

    const clamp01 = (v) => Math.min(1, Math.max(0, v));
    const stopStart = clamp01((center - bandW) / axisLen);
    const stopMid = clamp01(center / axisLen);
    const stopEnd = clamp01((center + bandW) / axisLen);

    const grad = ctx.createLinearGradient(0, env.height * 0.65, ax, ay);
    grad.addColorStop(stopStart, `rgba(${state.sheenTint}, 0)`);
    grad.addColorStop(stopMid, `rgba(${state.sheenTint}, ${SHEEN_ALPHA})`);
    grad.addColorStop(stopEnd, `rgba(${state.sheenTint}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Draw one frame: brushed grain plus the drifting sheen.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);
    drawSheen(ctx, env, state, t);
}

/**
 * Reduced-motion frame: brushed grain with no sheen at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {object} state Draw state from setup()
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'metal',
    // The sheen takes 42s to cross; 14fps keeps the raking-light motion
    // smooth without spending frames the grain texture cannot use.
    fps: 14,
    fpsMobile: 8,
    background: '#050303',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
