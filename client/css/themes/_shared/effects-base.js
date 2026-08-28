// Shared harness for theme background effects.
//
// WHY THIS LIVES HERE
// The bundled-theme static mount serves `client/css/themes/` at
// `/static/css/themes/`, so a sibling `_shared/` directory is reachable from
// any theme's effects.js via the relative module specifier
// `../_shared/effects-base.js` with no server change, no new mount and no
// change to registry.js. The theme scanner in `src/api/routes.py`
// (`_scan_themes_root`) skips any directory without a `theme.json`, so
// `_shared/` is invisible to theme discovery. The manifest's `effects` value
// is still a bare filename, which is all `effectsUrlFor()` permits; the
// harness import is the module's own business, not the manifest's.
//
// WHAT IT GUARANTEES, for every effect built on it
//   1. Honours `prefers-reduced-motion: reduce` (static frame, or nothing).
//   2. Zero CPU in a hidden tab (Page Visibility API suspends the rAF loop).
//   3. A hard frame-rate cap so an effect cannot compete with the terminal.
//   4. Complete teardown: rAF cancelled, every listener removed, canvas gone.
//   5. Never intercepts input (`pointer-events: none`, no key/pointer listeners).
//   6. A canvas or 2D-context failure degrades to no effect without throwing.
//
// THREE-OUTCOME RULE
// An effect that cannot initialise is NOT reported as success and NOT allowed
// to take the theme down with it. `getStatus()` and the
// `documentElement.dataset.themeEffects` marker both carry a distinct
// `unavailable` state with a reason, which is observable from the DOM and from
// tests. Skipping silently, or reporting a state nobody measured, is the
// defect this rule exists to prevent.
//
// Public API of this module: createEffect(spec) -> { init, destroy, getStatus }

/**
 * Effect lifecycle states. Exported so tests and callers can compare against
 * names rather than string literals.
 * @type {Readonly<Record<string, string>>}
 */
export const STATUS = Object.freeze({
    INACTIVE: 'inactive',           // never inited, or fully destroyed
    RUNNING: 'running',             // rAF loop live, drawing
    PAUSED: 'paused',               // mounted but suspended (tab hidden)
    STATIC: 'static',               // reduced motion, one frame painted
    SKIPPED: 'skipped',             // reduced motion, no static frame offered
    UNAVAILABLE: 'unavailable',     // could not initialise (the third outcome)
});

/** DOM attribute the harness publishes its status on, for tests and CSS. */
const STATUS_ATTR = 'themeEffects';

/** Upper bound on devicePixelRatio. Above 2x the cost is not worth the pixels. */
const MAX_DPR = 2;

/** Viewport width below which an effect uses its mobile budget. */
const MOBILE_BREAKPOINT = 768;

/**
 * Largest inter-frame gap credited to the effect clock. Anything longer is a
 * suspension, not elapsed animation time.
 */
const MAX_FRAME_GAP_MS = 250;

/**
 * Read a CSS custom property off :root, with a fallback.
 * The theme's own cssVars are the colour source for effects; an effect should
 * not carry literal colours that can drift from the manifest.
 * @param {string} name CSS custom property name, e.g. "--color-accent"
 * @param {string} fallback Value returned when the property is unset or empty
 * @returns {string} The trimmed property value, or the fallback
 */
export function readVar(name, fallback) {
    try {
        const v = getComputedStyle(document.documentElement)
            .getPropertyValue(name);
        const trimmed = v ? v.trim() : '';
        return trimmed || fallback;
    } catch (_) {
        return fallback;
    }
}

/**
 * Parse a hex colour into an "r, g, b" string usable inside rgba().
 * Accepts #rgb and #rrggbb. Returns the fallback triple on anything else, so a
 * malformed manifest value degrades to a visible-but-wrong colour rather than
 * an exception inside a draw loop.
 * @param {string} hex Colour string, e.g. "#33FF33"
 * @param {string} [fallback] Triple used when hex cannot be parsed
 * @returns {string} e.g. "51, 255, 51"
 */
export function rgbTriple(hex, fallback = '128, 128, 128') {
    if (typeof hex !== 'string') return fallback;
    let h = hex.trim().replace(/^#/, '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (h.length !== 6 || /[^0-9a-fA-F]/.test(h)) return fallback;
    const n = parseInt(h, 16);
    return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

/**
 * True when the user has asked the OS to reduce motion.
 * @returns {boolean} False when matchMedia is unavailable (assume motion is ok)
 */
function prefersReducedMotion() {
    try {
        return !!(window.matchMedia
            && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (_) {
        return false;
    }
}

/**
 * Publish the harness status to the document element so it is observable
 * without a JS handle. Cleared when no effect is mounted.
 * @param {string|null} status One of STATUS, or null to clear
 * @returns {void}
 */
function publishStatus(status) {
    try {
        if (status == null || status === STATUS.INACTIVE) {
            delete document.documentElement.dataset[STATUS_ATTR];
        } else {
            document.documentElement.dataset[STATUS_ATTR] = status;
        }
    } catch (_) { /* detached document; nothing to publish to */ }
}

/**
 * Build a theme background effect with the shared lifecycle guarantees.
 *
 * @param {object} spec Effect definition.
 * @param {string} spec.id Theme id, used for logging and the default canvas id.
 * @param {string} [spec.canvasId] Explicit canvas element id. Defaults to
 *   `<id>-effects`. Pinned by themes whose canvas id predates the harness.
 * @param {number} [spec.fps] Desktop frame cap in frames per second. Default 30.
 * @param {number} [spec.fpsMobile] Mobile frame cap. Default 15.
 * @param {string|null} [spec.background] Opaque CSS colour painted behind the
 *   effect. When set, the 2D context is requested with `alpha: false`, which is
 *   measurably cheaper. Pass null for a transparent canvas.
 * @param {function(CanvasRenderingContext2D, object): (object|void)} spec.setup
 *   Called on init and again on every resize. Receives the context and the
 *   environment; returns the effect's state object (or mutates a closure).
 * @param {function(CanvasRenderingContext2D, object, object, number): void} spec.draw
 *   Draws exactly one frame. Receives context, environment, state, and
 *   milliseconds elapsed since mount. Prefer driving motion from that elapsed
 *   time rather than from a per-frame increment, so the frame cap changes the
 *   smoothness of an effect and not its speed.
 * @param {function(CanvasRenderingContext2D, object, object, number): void} [spec.staticFrame]
 *   Optional single frame rendered under reduced motion. When omitted, reduced
 *   motion means nothing is mounted at all.
 * @returns {{init: function(object=): void, destroy: function(): void,
 *   getStatus: function(): {status: string, reason: string|null}}}
 *   The module contract registry.js consumes: init() and destroy(), plus
 *   getStatus() for the three-outcome report.
 */
export function createEffect(spec) {
    let canvas = null;
    let ctx = null;
    let state = null;
    let env = null;
    let rafId = null;
    let lastFrameAt = 0;
    let prevFrameAt = 0;
    let elapsedMs = 0;
    let resizeHandler = null;
    let visHandler = null;
    let motionQuery = null;
    let motionHandler = null;
    let status = STATUS.INACTIVE;
    let reason = null;

    const fps = typeof spec.fps === 'number' ? spec.fps : 30;
    const fpsMobile = typeof spec.fpsMobile === 'number' ? spec.fpsMobile : 15;

    /**
     * Record the current lifecycle state and mirror it onto the DOM.
     * @param {string} next One of STATUS
     * @param {string|null} [why] Human-readable detail, required for UNAVAILABLE
     * @returns {void}
     */
    function setStatus(next, why) {
        status = next;
        reason = why || null;
        publishStatus(next);
    }

    /**
     * Recompute the environment and hand the effect a chance to reseed.
     * Called on init and on every window resize, matching the pre-harness
     * behaviour where a resize reseeded the whole particle field.
     * @returns {void}
     */
    function resize() {
        if (!canvas || !ctx) return;
        const width = window.innerWidth;
        const height = window.innerHeight;
        const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(height * dpr);
        canvas.style.width = width + 'px';
        canvas.style.height = height + 'px';
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        env = {
            width,
            height,
            dpr,
            isMobile: width < MOBILE_BREAKPOINT,
            readVar,
            rgbTriple,
        };
        const produced = spec.setup(ctx, env);
        if (produced !== undefined) state = produced;
    }

    /**
     * rAF callback. Reschedules first so the loop stays vsync-aligned, then
     * drops the frame when it arrives inside the cap window. An empty callback
     * costs microseconds; a setTimeout-driven loop costs jank, so the cap is
     * enforced by skipping work rather than by skipping the callback.
     * @param {number} now High-resolution timestamp supplied by rAF
     * @returns {void}
     */
    function tick(now) {
        rafId = requestAnimationFrame(tick);
        const cap = env && env.isMobile ? fpsMobile : fps;
        if (now - lastFrameAt < 1000 / cap) return;
        // Advance the effect clock by the real gap, clamped so that a long
        // suspension (hidden tab, sleeping laptop) resumes where it left off
        // instead of teleporting the animation forward by minutes.
        if (prevFrameAt !== 0) {
            elapsedMs += Math.min(now - prevFrameAt, MAX_FRAME_GAP_MS);
        }
        prevFrameAt = now;
        lastFrameAt = now;
        try {
            spec.draw(ctx, env, state, elapsedMs);
        } catch (e) {
            // A throwing draw would otherwise repeat 30 times a second
            // forever. Stop cleanly and report, rather than flooding.
            console.warn('ThemeEffects: draw threw for', spec.id, e);
            teardownRuntime();
            setStatus(STATUS.UNAVAILABLE, 'draw threw: ' + (e && e.message));
        }
    }

    /**
     * Start the rAF loop if it is not already running.
     * @returns {void}
     */
    function startLoop() {
        if (rafId != null) return;
        lastFrameAt = 0;
        prevFrameAt = 0;
        rafId = requestAnimationFrame(tick);
        setStatus(STATUS.RUNNING);
    }

    /**
     * Cancel the rAF loop. Safe to call when no loop is running.
     * @returns {void}
     */
    function stopLoop() {
        if (rafId != null) {
            cancelAnimationFrame(rafId);
            rafId = null;
        }
    }

    /**
     * Remove every listener and drop the canvas. Shared by destroy() and by
     * the draw-threw bailout, which must not leave a half-mounted effect.
     * @returns {void}
     */
    function teardownRuntime() {
        stopLoop();
        if (resizeHandler) {
            window.removeEventListener('resize', resizeHandler);
            resizeHandler = null;
        }
        if (visHandler) {
            document.removeEventListener('visibilitychange', visHandler);
            visHandler = null;
        }
        if (motionQuery && motionHandler) {
            if (typeof motionQuery.removeEventListener === 'function') {
                motionQuery.removeEventListener('change', motionHandler);
            } else if (typeof motionQuery.removeListener === 'function') {
                motionQuery.removeListener(motionHandler);
            }
        }
        motionQuery = null;
        motionHandler = null;
        if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
        canvas = null;
        ctx = null;
        state = null;
        env = null;
        prevFrameAt = 0;
        elapsedMs = 0;
    }

    /**
     * Create the canvas element and acquire a 2D context.
     * @returns {string|null} Null on success, or a reason string on failure
     */
    function mountCanvas() {
        let el;
        try {
            el = document.createElement('canvas');
        } catch (e) {
            return 'canvas element creation failed';
        }
        el.id = spec.canvasId || (spec.id + '-effects');
        el.setAttribute('aria-hidden', 'true');
        Object.assign(el.style, {
            position: 'fixed',
            inset: '0',
            width: '100vw',
            // Replaced with an innerHeight pixel value by resize() on the very
            // next statement, so this applies for a single frame. Dynamic unit
            // regardless: raw 100vh is the 739px LARGE viewport on a 699px iOS
            // screen.
            height: '100dvh',
            zIndex: '-1',
            pointerEvents: 'none',
            background: spec.background || 'transparent',
        });
        let c = null;
        try {
            document.body.appendChild(el);
            c = el.getContext('2d', { alpha: !spec.background });
        } catch (e) {
            try { if (el.parentNode) el.parentNode.removeChild(el); } catch (_) {}
            return '2d context threw: ' + (e && e.message);
        }
        if (!c) {
            try { if (el.parentNode) el.parentNode.removeChild(el); } catch (_) {}
            return '2d context unavailable';
        }
        canvas = el;
        ctx = c;
        return null;
    }

    /**
     * Mount the effect. Idempotent: a second call while mounted is a no-op.
     * Never throws; every failure path lands on the UNAVAILABLE status so the
     * CSS theme still applies cleanly around a dead effect.
     * @param {{themeContext?: {id: string, manifest: object}}} [_opts]
     *   Supplied by registry.js. Unused by the harness; effects that need the
     *   manifest can read it in their own init wrapper.
     * @returns {void}
     */
    function init(_opts) {
        if (canvas) return;
        try {
            if (prefersReducedMotion()) {
                if (typeof spec.staticFrame !== 'function') {
                    setStatus(STATUS.SKIPPED, 'prefers-reduced-motion: reduce');
                    return;
                }
                const failure = mountCanvas();
                if (failure) {
                    setStatus(STATUS.UNAVAILABLE, failure);
                    console.warn('ThemeEffects:', spec.id, 'unavailable:', failure);
                    return;
                }
                resize();
                spec.staticFrame(ctx, env, state, 0);
                // A static frame still needs to survive a resize, but it must
                // never animate, so only the resize listener is attached.
                resizeHandler = () => {
                    resize();
                    try {
                        spec.staticFrame(ctx, env, state, 0);
                    } catch (e) {
                        console.warn('ThemeEffects: staticFrame threw for', spec.id, e);
                    }
                };
                window.addEventListener('resize', resizeHandler, { passive: true });
                setStatus(STATUS.STATIC, 'prefers-reduced-motion: reduce');
                return;
            }

            const failure = mountCanvas();
            if (failure) {
                setStatus(STATUS.UNAVAILABLE, failure);
                console.warn('ThemeEffects:', spec.id, 'unavailable:', failure);
                return;
            }
            resize();

            resizeHandler = () => {
                // A throwing reseed must not leave a live loop drawing against
                // stale state, and must not escape into the resize dispatch.
                try {
                    resize();
                } catch (e) {
                    console.warn('ThemeEffects: resize threw for', spec.id, e);
                    teardownRuntime();
                    setStatus(STATUS.UNAVAILABLE, 'resize threw: ' + (e && e.message));
                }
            };
            window.addEventListener('resize', resizeHandler, { passive: true });

            visHandler = () => {
                if (document.hidden) {
                    stopLoop();
                    setStatus(STATUS.PAUSED);
                } else {
                    startLoop();
                }
            };
            document.addEventListener('visibilitychange', visHandler);

            // Respect a mid-session change to the OS motion preference rather
            // than making the user reload to be taken seriously.
            try {
                motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
                motionHandler = (e) => {
                    if (e.matches) {
                        stopLoop();
                        setStatus(STATUS.SKIPPED, 'prefers-reduced-motion turned on');
                    } else if (!document.hidden) {
                        startLoop();
                    }
                };
                if (typeof motionQuery.addEventListener === 'function') {
                    motionQuery.addEventListener('change', motionHandler);
                } else if (typeof motionQuery.addListener === 'function') {
                    motionQuery.addListener(motionHandler);
                } else {
                    motionQuery = null;
                    motionHandler = null;
                }
            } catch (_) {
                motionQuery = null;
                motionHandler = null;
            }

            if (document.hidden) {
                setStatus(STATUS.PAUSED);
            } else {
                startLoop();
            }
        } catch (e) {
            // Belt and braces: whatever went wrong, the theme still applies.
            teardownRuntime();
            setStatus(STATUS.UNAVAILABLE, 'init threw: ' + (e && e.message));
            console.warn('ThemeEffects: init threw for', spec.id, e);
        }
    }

    /**
     * Unmount the effect completely. Safe to call when nothing is mounted, and
     * safe to call twice. A surviving rAF loop after this returns is a defect.
     * @returns {void}
     */
    function destroy() {
        teardownRuntime();
        setStatus(STATUS.INACTIVE);
    }

    /**
     * Report what the effect is actually doing, including the third outcome.
     * @returns {{status: string, reason: string|null}} Current status and, for
     *   the non-running states, why.
     */
    function getStatus() {
        return { status, reason };
    }

    return { init, destroy, getStatus };
}
