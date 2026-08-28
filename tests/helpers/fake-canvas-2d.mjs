// Fake CanvasRenderingContext2D for the theme-effects test harness.
//
// WHY THIS EXISTS AS A REAL IMPLEMENTATION AND NOT A HANDFUL OF NO-OPS
//
// The previous double implemented only fillRect, fillText, drawImage and the
// two gradient factories. Every other 2D call was absent, so an effect that
// used save/restore, a path, or a transform died with a TypeError inside the
// effect loop, which the harness reports as "draw threw". Three separate
// authors read that as "my drawing code is wrong" and rewrote working code to
// avoid the missing methods. The suite stayed green throughout, so the double
// was silently dictating the design of every effect written against it.
//
// The fix is not "add empty methods". An empty save()/restore() pair is worse
// than no save() at all: the call succeeds, the state is never restored, and
// the effect produces a wrong result with no error anywhere. So state-bearing
// calls here carry real semantics, and calls this double genuinely cannot
// answer THROW rather than inventing a plausible return value.
//
// Three outcomes, never two:
//   - implemented and honest  -> full semantics (state stack, transforms)
//   - implemented but approximate -> answers, and says so (measureText)
//   - cannot be answered here -> throws a named error (getImageData, hit tests)

/**
 * Canvas 2D drawing state. Every key is snapshotted by save() and rolled back
 * by restore(); `transform` and `lineDash` are copied by value, not shared.
 * @type {Readonly<object>}
 */
export const CTX_STATE_DEFAULTS = Object.freeze({
    globalAlpha: 1,
    globalCompositeOperation: 'source-over',
    fillStyle: '#000000',
    strokeStyle: '#000000',
    lineWidth: 1,
    lineCap: 'butt',
    lineJoin: 'miter',
    miterLimit: 10,
    lineDashOffset: 0,
    shadowBlur: 0,
    shadowColor: 'rgba(0, 0, 0, 0)',
    shadowOffsetX: 0,
    shadowOffsetY: 0,
    font: '10px sans-serif',
    textAlign: 'start',
    textBaseline: 'alphabetic',
    direction: 'inherit',
    filter: 'none',
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'low',
});

/** Identity affine transform in canvas [a, b, c, d, e, f] order. */
const IDENTITY = [1, 0, 0, 1, 0, 0];

/**
 * Raised for a call this double cannot answer truthfully. Loud on purpose: the
 * alternative is returning a made-up value the caller cannot distinguish from
 * a measurement.
 */
export class FakeCanvasUnsupported extends Error {
    /**
     * @param {string} method Name of the 2D method that cannot be answered
     * @param {string} why Plain-English reason it cannot be answered
     */
    constructor(method, why) {
        super(`fake 2D context cannot answer ${method}(): ${why}`);
        this.name = 'FakeCanvasUnsupported';
        this.method = method;
    }
}

/**
 * Multiply two affine matrices in canvas [a, b, c, d, e, f] order.
 * @param {number[]} m Existing transform (applied second)
 * @param {number[]} n Transform being applied (applied first)
 * @returns {number[]} New 6-element product matrix
 * @example matMul([1, 0, 0, 1, 0, 0], [2, 0, 0, 2, 0, 0]) // [2, 0, 0, 2, 0, 0]
 */
export function matMul(m, n) {
    return [
        m[0] * n[0] + m[2] * n[1],
        m[1] * n[0] + m[3] * n[1],
        m[0] * n[2] + m[2] * n[3],
        m[1] * n[2] + m[3] * n[3],
        m[0] * n[4] + m[2] * n[5] + m[4],
        m[1] * n[4] + m[3] * n[5] + m[5],
    ];
}

/** Methods that put marks on the canvas, and so bump `drawCalls`. */
const DRAW_OPS = new Set([
    'fillRect', 'strokeRect', 'fill', 'stroke',
    'fillText', 'strokeText', 'drawImage', 'putImageData',
]);

/**
 * Build a recording fake CanvasRenderingContext2D.
 *
 * Recording surface available to tests:
 *   - `ops`      : ordered [{ method, args }] of every call made
 *   - `drawCalls`: count of mark-making calls only (see DRAW_OPS)
 *   - `countOf(m)`: how many times method `m` was called
 *   - `stackDepth`: current save() depth, 1 when balanced
 *   - `unbalancedRestores`: restore() calls made against an empty stack
 *
 * Known approximations, stated rather than hidden:
 *   - measureText() estimates width from character count and the font size in
 *     the current `font` string. It is NOT a font measurement. The returned
 *     object carries `approximate: true`; do not assert layout against it.
 *
 * Deliberately unsupported (these throw FakeCanvasUnsupported):
 *   - getImageData(): nothing is rasterised, so read-back cannot report what
 *     was drawn. Returning zeroed pixels would make a read-back-driven effect
 *     silently draw nothing while the suite stayed green.
 *   - isPointInPath() / isPointInStroke(): no geometry is evaluated, so any
 *     boolean returned here would be fiction.
 *
 * @param {object} [canvasEl] Element to expose as `ctx.canvas`, if any
 * @returns {object} Fake CanvasRenderingContext2D
 */
export function createFakeCtx(canvasEl = null) {
    const stack = [{
        ...CTX_STATE_DEFAULTS,
        transform: IDENTITY.slice(),
        lineDash: [],
    }];
    const top = () => stack[stack.length - 1];

    const ctx = {
        canvas: canvasEl,
        drawCalls: 0,
        ops: [],
        unbalancedRestores: 0,
        /** @returns {number} Current save() depth; 1 means balanced. */
        get stackDepth() { return stack.length; },
        /**
         * Count recorded calls to one method.
         * @param {string} method Method name
         * @returns {number} Number of calls recorded
         */
        countOf(method) {
            let n = 0;
            for (const op of this.ops) if (op.method === method) n++;
            return n;
        },
        /**
         * Snapshot the live drawing state, for assertions around save/restore.
         * @returns {object} Plain copy of the current state
         */
        stateSnapshot() {
            return { ...top(), transform: top().transform.slice(), lineDash: top().lineDash.slice() };
        },
    };

    // Style properties read and write the TOP of the stack, which is what makes
    // save()/restore() actually mean something.
    for (const key of Object.keys(CTX_STATE_DEFAULTS)) {
        Object.defineProperty(ctx, key, {
            enumerable: true,
            configurable: true,
            get() { return top()[key]; },
            set(v) { top()[key] = v; },
        });
    }

    /**
     * Record a call, bumping drawCalls when it is a mark-making one.
     * @param {string} method Method name
     * @param {IArguments|Array} args Call arguments
     * @returns {void}
     */
    const rec = (method, args) => {
        ctx.ops.push({ method, args: Array.prototype.slice.call(args) });
        if (DRAW_OPS.has(method)) ctx.drawCalls++;
    };

    /**
     * Build a recording gradient handle.
     * @param {string} kind Factory that produced it
     * @param {IArguments} args Factory arguments
     * @returns {object} Gradient with a recorded stop list
     */
    const makeGradient = (kind, args) => {
        const stops = [];
        rec(kind, args);
        return {
            kind,
            stops,
            addColorStop(offset, color) { stops.push({ offset, color }); },
        };
    };

    Object.assign(ctx, {
        // ---- state stack -------------------------------------------------
        save() {
            rec('save', arguments);
            stack.push({
                ...top(),
                transform: top().transform.slice(),
                lineDash: top().lineDash.slice(),
            });
        },
        restore() {
            rec('restore', arguments);
            // Matches the real spec: an extra restore() is a no-op. Counted so
            // an imbalance is still observable instead of vanishing.
            if (stack.length === 1) { ctx.unbalancedRestores++; return; }
            stack.pop();
        },
        reset() {
            rec('reset', arguments);
            stack.length = 0;
            stack.push({ ...CTX_STATE_DEFAULTS, transform: IDENTITY.slice(), lineDash: [] });
        },

        // ---- transforms --------------------------------------------------
        translate(x, y) {
            rec('translate', arguments);
            top().transform = matMul(top().transform, [1, 0, 0, 1, x, y]);
        },
        scale(x, y) {
            rec('scale', arguments);
            top().transform = matMul(top().transform, [x, 0, 0, y, 0, 0]);
        },
        rotate(rad) {
            rec('rotate', arguments);
            const c = Math.cos(rad);
            const s = Math.sin(rad);
            top().transform = matMul(top().transform, [c, s, -s, c, 0, 0]);
        },
        transform(a, b, c, d, e, f) {
            rec('transform', arguments);
            top().transform = matMul(top().transform, [a, b, c, d, e, f]);
        },
        setTransform(a, b, c, d, e, f) {
            rec('setTransform', arguments);
            if (a && typeof a === 'object') {
                top().transform = [a.a, a.b, a.c, a.d, a.e, a.f].map((n) => (typeof n === 'number' ? n : 0));
                return;
            }
            top().transform = [a, b, c, d, e, f].map((n) => (typeof n === 'number' ? n : 0));
        },
        resetTransform() {
            rec('resetTransform', arguments);
            top().transform = IDENTITY.slice();
        },
        getTransform() {
            const t = top().transform;
            return { a: t[0], b: t[1], c: t[2], d: t[3], e: t[4], f: t[5] };
        },

        // ---- path construction -------------------------------------------
        beginPath() { rec('beginPath', arguments); },
        closePath() { rec('closePath', arguments); },
        moveTo() { rec('moveTo', arguments); },
        lineTo() { rec('lineTo', arguments); },
        bezierCurveTo() { rec('bezierCurveTo', arguments); },
        quadraticCurveTo() { rec('quadraticCurveTo', arguments); },
        arc() { rec('arc', arguments); },
        arcTo() { rec('arcTo', arguments); },
        ellipse() { rec('ellipse', arguments); },
        rect() { rec('rect', arguments); },
        roundRect() { rec('roundRect', arguments); },

        // ---- painting ------------------------------------------------------
        fill() { rec('fill', arguments); },
        stroke() { rec('stroke', arguments); },
        clip() { rec('clip', arguments); },
        fillRect() { rec('fillRect', arguments); },
        strokeRect() { rec('strokeRect', arguments); },
        clearRect() { rec('clearRect', arguments); },
        fillText() { rec('fillText', arguments); },
        strokeText() { rec('strokeText', arguments); },
        drawImage() { rec('drawImage', arguments); },
        putImageData() { rec('putImageData', arguments); },

        // ---- line dash -----------------------------------------------------
        setLineDash(segments) {
            rec('setLineDash', arguments);
            top().lineDash = Array.isArray(segments) ? segments.slice() : [];
        },
        getLineDash() { return top().lineDash.slice(); },

        // ---- factories -------------------------------------------------------
        createLinearGradient() { return makeGradient('createLinearGradient', arguments); },
        createRadialGradient() { return makeGradient('createRadialGradient', arguments); },
        createConicGradient() { return makeGradient('createConicGradient', arguments); },
        createPattern() {
            rec('createPattern', arguments);
            return { setTransform() {} };
        },
        createImageData(w, h) {
            rec('createImageData', arguments);
            // Spec-correct: a fresh ImageData is transparent black.
            const width = Math.max(0, Math.round(Number(w) || 0));
            const height = Math.max(0, Math.round(Number(h) || 0));
            return { width, height, data: new Uint8ClampedArray(width * height * 4) };
        },

        // ---- measurement ------------------------------------------------------
        measureText(text) {
            rec('measureText', arguments);
            const s = String(text ?? '');
            const m = /(\d+(?:\.\d+)?)px/.exec(String(top().font));
            const size = m ? parseFloat(m[1]) : 10;
            const width = s.length * size * 0.6;
            return {
                width,
                actualBoundingBoxLeft: 0,
                actualBoundingBoxRight: width,
                actualBoundingBoxAscent: size * 0.8,
                actualBoundingBoxDescent: size * 0.2,
                // Flag so nobody mistakes this for a font measurement.
                approximate: true,
            };
        },

        // ---- cannot be answered here ------------------------------------------
        getImageData() {
            throw new FakeCanvasUnsupported(
                'getImageData',
                'nothing is rasterised, so read-back cannot report what was drawn',
            );
        },
        isPointInPath() {
            throw new FakeCanvasUnsupported(
                'isPointInPath',
                'no geometry is evaluated, so any boolean would be fiction',
            );
        },
        isPointInStroke() {
            throw new FakeCanvasUnsupported(
                'isPointInStroke',
                'no geometry is evaluated, so any boolean would be fiction',
            );
        },
    });

    return ctx;
}
