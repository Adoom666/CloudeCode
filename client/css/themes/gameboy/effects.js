// Game Boy theme - DMG-01 dot matrix LCD texture with response-time ghosting.
//
// A real DMG panel is a passive-matrix LCD, not a phosphor tube: the image is
// a fixed grid of square liquid-crystal cells with visible cell boundaries,
// and the crystals are slow enough to respond that a moving edge leaves a
// faint trailing smear behind it (the "ghosting" every Tetris player
// remembers). This effect paints the pixel-cell lattice once as a tiled
// pattern, then drifts one very soft smear band across the panel on a slow
// cycle to stand in for that response lag. Nothing here resembles green_crt's
// horizontal-roll scanlines: there is no scanline raster at all, the lattice
// is a two-axis grid, and the smear is diagonal rather than a horizontal band.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** LCD cell pitch in CSS pixels. DMG cells read as roughly square, ~4px. */
const CELL_PITCH = 4;

/** Opacity of a single cell boundary line. Texture, not a visible grid. */
const CELL_ALPHA = 0.05;

/** Milliseconds for the ghosting smear to cross the panel once, diagonally. */
const SMEAR_PERIOD_MS = 31000;

/** Peak opacity of the ghosting smear itself. */
const SMEAR_ALPHA = 0.055;

/** Smear band width as a fraction of the panel diagonal. */
const SMEAR_BAND_FRACTION = 0.35;

/**
 * Build the repeating cell-lattice pattern once: a faint dot at each cell
 * corner, the way adjacent LCD cells read as a grid rather than a smooth
 * fill. Two fills per frame beats thousands of tiny rects.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} lineColour rgba() string for the lattice dots
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildLatticePattern(ctx, lineColour) {
    const tile = document.createElement('canvas');
    tile.width = CELL_PITCH;
    tile.height = CELL_PITCH;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = lineColour;
    tctx.fillRect(0, 0, 1, CELL_PITCH);
    tctx.fillRect(0, 0, CELL_PITCH, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the lattice pattern. Runs on init and on
 * every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, ink: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#9bbc0f');
    const ink = readVar('--color-fg', '#0f380f');
    const inkTriple = rgbTriple(ink, '15, 56, 15');
    return {
        pattern: buildLatticePattern(ctx, `rgba(${inkTriple}, ${CELL_ALPHA})`),
        bg,
        ink: inkTriple,
    };
}

/**
 * Paint the base panel colour and the cell lattice. Shared by the animated
 * frame and the reduced-motion static frame, which differ only by the smear.
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
 * Draw one frame: the lattice base plus a diagonal ghosting smear that
 * crawls corner to corner, standing in for LCD response lag.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, ink: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const diag = Math.hypot(env.width, env.height);
    const bandW = diag * SMEAR_BAND_FRACTION;
    const travel = diag + bandW;
    const p = (t % SMEAR_PERIOD_MS) / SMEAR_PERIOD_MS;
    const offset = p * travel - bandW;

    // A band perpendicular to the panel diagonal, translated along it.
    const ux = env.width / diag;
    const uy = env.height / diag;
    const x0 = ux * offset;
    const y0 = uy * offset;
    const x1 = ux * (offset + bandW);
    const y1 = uy * (offset + bandW);

    const grad = ctx.createLinearGradient(x0, y0, x1, y1);
    grad.addColorStop(0, `rgba(${state.ink}, 0)`);
    grad.addColorStop(0.5, `rgba(${state.ink}, ${SMEAR_ALPHA})`);
    grad.addColorStop(1, `rgba(${state.ink}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the lattice texture with no ghosting smear at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'gameboy',
    // 15fps: the smear crawls the panel over 31 seconds, well above the
    // perceptual floor for that speed, and keeps the mobile budget cheap.
    fps: 15,
    fpsMobile: 10,
    background: '#9bbc0f',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
