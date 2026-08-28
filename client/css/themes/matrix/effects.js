// Matrix theme - falling katakana background.
// Mounts a fixed full-viewport <canvas> at z-index: -1.
// Throttled to ~30fps desktop / ~15fps mobile.
// Pauses on document.visibilitychange (hidden -> suspend RAF).
// Refuses to mount entirely under prefers-reduced-motion: reduce.
// Public API: init({ themeContext }), destroy().
//
// Lifecycle (rAF, visibility, resize, teardown, failure reporting) is owned by
// the shared harness. This file is only the glyph field and its draw call; the
// drawing math, colours, densities and frame caps are unchanged from the
// pre-harness version, so the rendered output is identical.

import { createEffect } from '../_shared/effects-base.js';

const GLYPHS = (() => {
    const out = [];
    // Katakana block (U+30A0 - U+30FF) - pull the visually dense ones.
    for (let cp = 0x30a0; cp <= 0x30ff; cp++) out.push(String.fromCharCode(cp));
    // Digits + a few latin letters for variety (the original film mixes them in).
    for (let i = 0; i < 10; i++) out.push(String(i));
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').forEach((c) => out.push(c));
    return out;
})();

/**
 * Pick one random glyph from the katakana/digit/latin pool.
 * @returns {string} A single-character string
 */
function pickGlyph() {
    return GLYPHS[(Math.random() * GLYPHS.length) | 0];
}

/**
 * Seed the column head positions and pin the glyph font.
 * Runs on init and on every resize, reseeding the field exactly as before.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context, already DPR-scaled
 * @param {{width: number, height: number}} env Viewport environment
 * @returns {{fontSize: number, colCount: number, columns: number[]}} Draw state
 */
function setup(ctx, env) {
    const fontSize = env.width < 768 ? 13 : 16;
    const colCount = Math.max(1, Math.floor(env.width / fontSize));
    const columns = new Array(colCount);
    for (let i = 0; i < colCount; i++) {
        columns[i] = Math.random() * (env.height / fontSize);
    }
    ctx.font = `${fontSize}px "SF Mono", "Menlo", monospace`;
    ctx.textBaseline = 'top';
    return { fontSize, colCount, columns };
}

/**
 * Draw one frame of falling glyphs.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{fontSize: number, colCount: number, columns: number[]}} state Draw state
 * @returns {void}
 */
function draw(ctx, env, state) {
    const { fontSize, colCount, columns } = state;

    // Translucent black overlay creates the trailing fade.
    ctx.fillStyle = 'rgba(0, 0, 0, 0.06)';
    ctx.fillRect(0, 0, env.width, env.height);

    ctx.fillStyle = '#00ff41';
    for (let i = 0; i < colCount; i++) {
        const x = i * fontSize;
        const y = columns[i] * fontSize;
        const glyph = pickGlyph();
        // Occasional bright head-glyph for sparkle.
        if (Math.random() < 0.015) {
            ctx.fillStyle = '#ccffcc';
            ctx.fillText(glyph, x, y);
            ctx.fillStyle = '#00ff41';
        } else {
            ctx.fillText(glyph, x, y);
        }
        columns[i]++;
        if (y > env.height && Math.random() > 0.975) {
            columns[i] = 0;
        }
    }
}

const effect = createEffect({
    id: 'matrix',
    canvasId: 'matrix-rain',
    fps: 30,
    fpsMobile: 15,
    background: '#000000',
    setup,
    draw,
    // No staticFrame on purpose: under reduced motion this theme mounts
    // nothing at all and the CSS fallback handles the static look, which is
    // the pre-harness behaviour.
});

export const init = effect.init;
export const destroy = effect.destroy;
export const getStatus = effect.getStatus;
export default effect;
