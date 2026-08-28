// Legacy Apple theme - 1-bit desktop stipple with a drifting ambient highlight.
//
// Early black-and-white Macintosh screens had no greyscale: every mid-tone,
// including the classic desktop pattern, was a 1-bit dither of pure black and
// white dots. This paints that stipple as a fixed 2x2 checkerboard texture,
// then adds one very soft highlight that wanders the screen on a slow
// diagonal path, the way ambient light falling across a CRT bezel makes a
// static dither pattern read slightly differently moment to moment. The
// highlight is a radial blob on a Lissajous-style path, not a linear band, so
// it cannot be mistaken for green_crt's roll or gameboy's corner-to-corner
// smear.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Stipple tile size in CSS pixels. A 2x2 checkerboard is the classic 50% dither. */
const STIPPLE_PITCH = 2;

/** Opacity of a single stipple dot. Reads as paper grain, not a visible grid. */
const STIPPLE_ALPHA = 0.05;

/** Milliseconds for the highlight to complete one full diagonal wander. */
const DRIFT_PERIOD_MS = 45000;

/** Peak opacity of the highlight at the centre of its blob. */
const DRIFT_ALPHA = 0.05;

/** Highlight blob radius as a fraction of the viewport diagonal. */
const DRIFT_RADIUS_FRACTION = 0.55;

/**
 * Build the repeating 2x2 stipple pattern once: two diagonal dots filled,
 * two left transparent, the canonical 50% Bayer dither used for mid-tones on
 * 1-bit displays.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} dotColour rgba() string for the filled stipple dots
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildStipplePattern(ctx, dotColour) {
    const tile = document.createElement('canvas');
    tile.width = STIPPLE_PITCH;
    tile.height = STIPPLE_PITCH;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = dotColour;
    tctx.fillRect(0, 0, 1, 1);
    tctx.fillRect(1, 1, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the stipple pattern. Runs on init and on
 * every resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, ink: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#cfc4b4');
    const ink = readVar('--color-fg', '#000000');
    return {
        pattern: buildStipplePattern(ctx, `rgba(${rgbTriple(ink, '0, 0, 0')}, ${STIPPLE_ALPHA})`),
        bg,
        ink: rgbTriple(ink, '0, 0, 0'),
    };
}

/**
 * Paint the base desktop colour and the stipple texture. Shared by the
 * animated frame and the reduced-motion static frame, which differ only by
 * the wandering highlight.
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
 * Draw one frame: stipple base plus a soft highlight blob wandering a slow
 * diagonal Lissajous path across the desktop.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, ink: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const phase = (t % DRIFT_PERIOD_MS) / DRIFT_PERIOD_MS;
    const angle = phase * Math.PI * 2;
    // A diagonal Lissajous loop (1:1 frequency ratio, quarter-turn phase
    // offset) rather than a straight ping-pong, so the highlight visits every
    // corner of the desktop over one cycle instead of retracing one line.
    const cx = env.width * (0.5 + 0.4 * Math.cos(angle));
    const cy = env.height * (0.5 + 0.4 * Math.sin(angle + Math.PI / 2));
    const radius = Math.hypot(env.width, env.height) * DRIFT_RADIUS_FRACTION;

    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    grad.addColorStop(0, `rgba(${state.ink}, ${DRIFT_ALPHA})`);
    grad.addColorStop(1, `rgba(${state.ink}, 0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, env.width, env.height);
}

/**
 * Reduced-motion frame: the stipple texture with no wandering highlight at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'legacy_apple',
    // 12fps: the wander takes 45 seconds to loop, far above the perceptual
    // floor for that speed, and this is the quietest motion of the batch.
    fps: 12,
    fpsMobile: 10,
    background: '#cfc4b4',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
