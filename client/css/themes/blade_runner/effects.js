// Blade Runner theme - cyan-cobalt rain streaks falling across a rain-slick night.
// Mounts a fixed full-viewport <canvas> at z-index: -1.
// Throttled to ~30fps desktop / ~15fps mobile.
// Pauses on document.visibilitychange (hidden -> suspend RAF).
// Refuses to mount entirely under prefers-reduced-motion: reduce.
// Public API: init({ themeContext }), destroy().
//
// Lifecycle (rAF, visibility, resize, teardown, failure reporting) is owned by
// the shared harness. This file is only the streak field and its draw call; the
// drawing math, colours, densities and frame caps are unchanged from the
// pre-harness version, so the rendered output is identical.

import { createEffect } from '../_shared/effects-base.js';

/**
 * Build one rain streak: a vertical line 1-2px wide, 40-100px long,
 * falling 6-14px per frame.
 * @param {number} width Viewport width in CSS pixels
 * @param {number} height Viewport height in CSS pixels
 * @param {boolean} seed When true, scatter across the whole viewport so the
 *   first frame is full; otherwise spawn just above the viewport and fall in
 * @returns {{x: number, y: number, len: number, w: number, speed: number, alpha: number}}
 */
function makeStreak(width, height, seed) {
    // Streak: vertical rain line. 1-2px wide, 40-100px long, falls 6-14 px/frame.
    const len = 40 + Math.random() * 60;
    return {
        x: Math.random() * width,
        // If seed: scatter across the whole viewport so first frame is full.
        // Otherwise spawn just above viewport and fall in.
        y: seed ? Math.random() * height : -len - Math.random() * height * 0.5,
        len,
        w: Math.random() < 0.7 ? 1 : 2,
        speed: 6 + Math.random() * 8,
        // Cyan-cobalt with varying alpha - Deakins blue shadow on Cronenweth wet street.
        alpha: 0.18 + Math.random() * 0.42,
    };
}

/**
 * Seed the streak field at the density the viewport size calls for.
 * Runs on init and on every resize, reseeding exactly as before.
 * @param {CanvasRenderingContext2D} _ctx Canvas 2D context (unused here)
 * @param {{width: number, height: number, isMobile: boolean}} env Viewport environment
 * @returns {{streaks: object[]}} Draw state
 */
function setup(_ctx, env) {
    const density = env.isMobile ? 40 : 80;
    const streaks = new Array(density);
    for (let i = 0; i < density; i++) {
        streaks[i] = makeStreak(env.width, env.height, true);
    }
    return { streaks };
}

/**
 * Draw one frame of falling rain streaks.
 * @param {CanvasRenderingContext2D} ctx Canvas 2D context
 * @param {{width: number, height: number}} env Viewport environment
 * @param {{streaks: object[]}} state Draw state
 * @returns {void}
 */
function draw(ctx, env, state) {
    const { streaks } = state;
    const w = env.width;
    const h = env.height;

    // Translucent night-cobalt overlay creates the smear/trail.
    ctx.fillStyle = 'rgba(10, 14, 26, 0.35)';
    ctx.fillRect(0, 0, w, h);

    // Draw streaks as cyan-cobalt vertical gradients (head bright, tail fade).
    for (let i = 0; i < streaks.length; i++) {
        const s = streaks[i];
        const grad = ctx.createLinearGradient(s.x, s.y, s.x, s.y + s.len);
        grad.addColorStop(0, 'rgba(0, 80, 140, 0)');
        grad.addColorStop(0.6, `rgba(0, 140, 200, ${s.alpha * 0.55})`);
        grad.addColorStop(1, `rgba(0, 212, 255, ${s.alpha})`);
        ctx.fillStyle = grad;
        ctx.fillRect(s.x, s.y, s.w, s.len);

        s.y += s.speed;
        if (s.y > h + 8) {
            streaks[i] = makeStreak(w, h, false);
        }
    }
}

const effect = createEffect({
    id: 'blade_runner',
    canvasId: 'blade-runner-rain',
    fps: 30,
    fpsMobile: 15,
    background: '#0a0e1a',
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
