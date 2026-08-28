// Pokemon theme - fine warm dot-matrix with a slow colour-temperature drift.
//
// Deliberately restrained rather than literal: no sprites, no Poke Balls, no
// on-brand iconography, just the texture of a small warm-toned display and
// the kind of colour-temperature wander a CCFL backlight shows as it ages and
// warms up over a session. The dot pitch is half of gameboy's lattice (finer,
// since this is not meant to read as a DMG panel), and the motion is a
// crossfade between two tints rather than any travelling band or pulse, which
// keeps it distinct from every other effect in this batch.
//
// Colours come from the theme's own cssVars; nothing here is hardcoded beyond
// the fallbacks that keep the draw loop total if a var is missing.
//
// Public API: init({ themeContext }), destroy(), getStatus().

import { createEffect, readVar, rgbTriple } from '../_shared/effects-base.js';

/** Dot-matrix pitch in CSS pixels. Half of gameboy's cell pitch: a finer grain. */
const DOT_PITCH = 2;

/** Opacity of a single lattice dot. Texture, not a visible grid. */
const DOT_ALPHA = 0.045;

/** Milliseconds for one full warm-to-cool-to-warm colour temperature cycle. */
const DRIFT_PERIOD_MS = 52000;

/** Peak opacity either tint layer reaches at the extreme of its half-cycle. */
const DRIFT_ALPHA = 0.05;

/**
 * Build the repeating dot-matrix pattern once: a single faint dot at each
 * cell's top-left corner, at half the pitch of the gameboy lattice so the
 * two effects do not read as the same texture at different sizes.
 * @param {CanvasRenderingContext2D} ctx Destination context, used only as the
 *   pattern factory
 * @param {string} dotColour rgba() string for the lattice dot
 * @returns {CanvasPattern|null} Repeating pattern, or null if unavailable
 */
function buildDotPattern(ctx, dotColour) {
    const tile = document.createElement('canvas');
    tile.width = DOT_PITCH;
    tile.height = DOT_PITCH;
    const tctx = tile.getContext('2d');
    if (!tctx) return null;
    tctx.fillStyle = dotColour;
    tctx.fillRect(0, 0, 1, 1);
    return ctx.createPattern(tile, 'repeat');
}

/**
 * Resolve the palette and rebuild the dot pattern. Runs on init and on every
 * resize.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {object} _env Viewport environment (unused; the pattern is size-free)
 * @returns {{pattern: CanvasPattern|null, bg: string, warm: string, cool: string}} Draw state
 */
function setup(ctx, _env) {
    const bg = readVar('--color-bg-page', '#081730');
    const fg = readVar('--color-fg', '#FFFFFF');
    const warm = rgbTriple(readVar('--color-accent', '#FFCB05'), '255, 203, 5');
    const cool = rgbTriple(readVar('--color-info', '#3B8FE8'), '59, 143, 232');
    return {
        pattern: buildDotPattern(ctx, `rgba(${rgbTriple(fg, '255, 255, 255')}, ${DOT_ALPHA})`),
        bg,
        warm,
        cool,
    };
}

/**
 * Paint the base panel colour and the dot-matrix texture. Shared by the
 * animated frame and the reduced-motion static frame, which differ only by
 * the colour-temperature tint.
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
 * Draw one frame: dot-matrix base plus a warm/cool tint that crossfades
 * smoothly, standing in for backlight colour-temperature wander. The two
 * layers are complementary (one falls as the other rises) so the whole
 * canvas drifts between the two tints rather than pulsing in and out.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string, warm: string, cool: string}} state Draw state
 * @param {number} t Milliseconds elapsed since mount
 * @returns {void}
 */
function draw(ctx, env, state, t) {
    paintBase(ctx, env, state);

    const phase = (t % DRIFT_PERIOD_MS) / DRIFT_PERIOD_MS;
    const warmth = (1 + Math.cos(phase * Math.PI * 2)) / 2; // 1 warm .. 0 cool
    const warmAlpha = warmth * DRIFT_ALPHA;
    const coolAlpha = (1 - warmth) * DRIFT_ALPHA;

    if (warmAlpha > 0.0005) {
        ctx.fillStyle = `rgba(${state.warm}, ${warmAlpha})`;
        ctx.fillRect(0, 0, env.width, env.height);
    }
    if (coolAlpha > 0.0005) {
        ctx.fillStyle = `rgba(${state.cool}, ${coolAlpha})`;
        ctx.fillRect(0, 0, env.width, env.height);
    }
}

/**
 * Reduced-motion frame: the dot-matrix texture with no colour drift at all.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{pattern: CanvasPattern|null, bg: string}} state Draw state
 * @returns {void}
 */
function staticFrame(ctx, env, state) {
    paintBase(ctx, env, state);
}

const effect = createEffect({
    id: 'pokemon',
    // 15fps: the drift takes 52 seconds to complete a cycle, far above the
    // perceptual floor for that speed.
    fps: 15,
    fpsMobile: 10,
    background: '#081730',
    setup,
    draw,
    staticFrame,
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
