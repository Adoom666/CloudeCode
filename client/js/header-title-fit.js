/**
 * Header title fitting - MIDDLE elision for the in-session identity.
 *
 * WHY THIS EXISTS AT ALL, AND WHY IT IS NOT JUST CSS.
 *
 * `#header-title-text` already carries `text-overflow: ellipsis` in
 * styles.css, and that alone is enough to stop the title shoving the
 * top-right controls off a 390px screen. So the layout bug is fixed
 * without a single line of JavaScript, and if this module never loads the
 * header still behaves.
 *
 * What CSS cannot do is choose WHICH end to throw away. `text-overflow`
 * only elides at the end. Session names in this app are project-prefixed
 * and the prefix is shared across every session in that project:
 *
 *     cloude_claude-config-sync-1
 *     cloude_claude-config-sync-2
 *
 * An end-ellipsis renders both of those as `cloude_claude-config...`,
 * which is the one rendering that destroys the only information the user
 * needs from the header - which session am I looking at. Middle elision
 * keeps both the recognisable prefix and the distinguishing tail:
 * `cloude_cla...sync-2`.
 *
 * So: CSS is the floor (correct layout, degraded text), this module is
 * the finished behaviour (correct layout, useful text).
 *
 * The full, untruncated name is always preserved in `dataset.fullTitle`.
 * Nothing reads the rendered text as data - the rename flow takes its
 * value from the session state in terminal.js, not from the DOM - but the
 * dataset attribute makes that guarantee explicit rather than incidental.
 */

console.log('[HeaderTitleFit Module] Loading...');

/** Character used to mark the removed middle. A real ellipsis, not "...". */
const ELLIPSIS = '…';

/**
 * Characters of tail to protect before the head is allowed any budget.
 * Session names distinguish themselves at the END (a trailing counter, a
 * branch name, a worktree suffix), so the tail is the half worth keeping.
 */
const MIN_TAIL_CHARS = 6;

/** Below this many characters, eliding costs more than it saves. */
const MIN_ELIDE_LENGTH = 8;

/**
 * Build the middle-elided candidate that retains `keep` characters of the
 * original text, biased toward the tail.
 *
 * @param {string} text  Full text.
 * @param {number} keep  Total characters to retain, excluding the ellipsis.
 * @returns {string} The candidate string.
 */
function buildCandidate(text, keep) {
    if (keep <= 0) return ELLIPSIS;
    if (keep >= text.length) return text;
    // Tail is favoured: it holds the distinguishing suffix. Give it at
    // least MIN_TAIL_CHARS whenever the budget can afford it, otherwise
    // split as evenly as possible with the odd character going tailward.
    let tail = Math.max(Math.ceil(keep / 2), Math.min(MIN_TAIL_CHARS, keep));
    if (tail > keep) tail = keep;
    const head = keep - tail;
    return text.slice(0, head) + ELLIPSIS + text.slice(text.length - tail);
}

/**
 * Middle-elide `text` so its rendered width fits inside `maxWidth`.
 *
 * Pure: all measurement is injected, so this is testable without a DOM
 * and without a layout engine.
 *
 * @param {string} text      The full text to fit.
 * @param {number} maxWidth  Available width in CSS pixels.
 * @param {function(string): number} measure  Rendered width of a string.
 * @returns {string} `text` unchanged if it already fits, otherwise the
 *   longest middle-elided form that fits.
 */
function elideToWidth(text, maxWidth, measure) {
    const full = String(text == null ? '' : text);
    if (!full) return '';
    if (!(maxWidth > 0)) return full;
    if (measure(full) <= maxWidth) return full;
    if (full.length < MIN_ELIDE_LENGTH) return full;

    // Binary search the largest retained-character budget that still fits.
    // Width is monotonic in `keep`, so a bisection is exact here.
    let low = 0;
    let high = full.length - 1;
    let best = ELLIPSIS;
    while (low <= high) {
        const mid = Math.floor((low + high) / 2);
        const candidate = buildCandidate(full, mid);
        if (measure(candidate) <= maxWidth) {
            best = candidate;
            low = mid + 1;
        } else {
            high = mid - 1;
        }
    }
    return best;
}

/**
 * Width in CSS pixels of `text` rendered with `el`'s computed font.
 *
 * Uses a detached absolutely positioned ruler rather than a canvas so
 * letter-spacing, font-feature settings and the real resolved family all
 * apply. The ruler is created per call and removed immediately; fitting
 * runs on resize, not per frame, so the cost is irrelevant.
 *
 * @param {Element} el  Element whose font metrics apply.
 * @returns {function(string): number} Measurement function.
 */
function makeMeasurer(el) {
    const cs = window.getComputedStyle(el);
    const ruler = document.createElement('span');
    ruler.style.cssText =
        'position:absolute;visibility:hidden;white-space:pre;top:0;left:-9999px;' +
        'font:' + cs.font + ';letter-spacing:' + cs.letterSpacing;
    // getComputedStyle().font is empty in some engines when the shorthand
    // cannot be reconstructed; fall back to the longhands that matter.
    if (!cs.font) {
        ruler.style.fontFamily = cs.fontFamily;
        ruler.style.fontSize = cs.fontSize;
        ruler.style.fontWeight = cs.fontWeight;
        ruler.style.fontStyle = cs.fontStyle;
    }
    document.body.appendChild(ruler);
    const measure = (text) => {
        ruler.textContent = text;
        return ruler.getBoundingClientRect().width;
    };
    measure.dispose = () => { if (ruler.parentNode) ruler.remove(); };
    return measure;
}

/**
 * Outer width of `el` including its horizontal margins.
 *
 * @param {Element} el  Element to measure.
 * @param {CSSStyleDeclaration} cs  Its computed style.
 * @returns {number} Width in CSS pixels.
 */
function outerWidth(el, cs) {
    return el.getBoundingClientRect().width +
        (parseFloat(cs.marginLeft) || 0) +
        (parseFloat(cs.marginRight) || 0);
}

/**
 * Width in CSS pixels the flex parent of `el` leaves available to `el`.
 *
 * Derived from the PARENT box and `el`'s siblings, never from `el`'s own
 * rendered width. That distinction is the whole reason this function
 * exists: `h1` is `flex: 0 1 auto`, so once the title is shortened the
 * h1 hugs the shorter text and its own width no longer reports the space
 * that was on offer. Reading it back would give a smaller budget on every
 * pass - a measure-shrink-remeasure ratchet that converges on a single
 * ellipsis. The siblings here (`.session-sidebar-toggle`, `.controls`)
 * are all fixed-size and `flex-shrink: 0`, so this budget is stable.
 *
 * @param {Element} el  Element whose slot is wanted.
 * @returns {number} Available width, 0 when it cannot be determined.
 */
function slotWidth(el) {
    const parent = el.parentElement;
    if (!parent) return 0;
    const pcs = window.getComputedStyle(parent);
    let avail = parent.clientWidth -
        (parseFloat(pcs.paddingLeft) || 0) -
        (parseFloat(pcs.paddingRight) || 0);

    let visible = 0;
    for (const child of parent.children) {
        const cs = window.getComputedStyle(child);
        if (cs.display === 'none') continue;
        visible++;
        if (child === el) continue;
        avail -= outerWidth(child, cs);
    }

    const gap = parseFloat(pcs.columnGap) || parseFloat(pcs.gap) || 0;
    if (visible > 1) avail -= gap * (visible - 1);

    const ecs = window.getComputedStyle(el);
    avail -= (parseFloat(ecs.marginLeft) || 0) + (parseFloat(ecs.marginRight) || 0);
    return avail;
}

/**
 * Space in CSS pixels the title span may occupy.
 *
 * Two steps, for the reason given on `slotWidth`: take the slot the
 * header grants the `h1`, then subtract everything else inside the `h1`
 * (identity icon, rename pencil) which are all `flex-shrink: 0` and
 * therefore fixed.
 *
 * The version chip used to be one of them. It moved to the home screen's
 * bottom bar, and because this loop enumerates the h1's LIVE children
 * rather than a hard-coded list, its width came back to the title budget
 * with no change here.
 *
 * @param {Element} titleEl  The `#header-title-text` span.
 * @returns {number} Available width, or 0 when it cannot be determined.
 */
function availableWidth(titleEl) {
    const h1 = titleEl.parentElement;
    if (!h1 || !h1.parentElement) return 0;

    const hcs = window.getComputedStyle(h1);
    let avail = slotWidth(h1) -
        (parseFloat(hcs.paddingLeft) || 0) -
        (parseFloat(hcs.paddingRight) || 0);

    let visible = 0;
    for (const child of h1.children) {
        const cs = window.getComputedStyle(child);
        if (cs.display === 'none') continue;
        visible++;
        if (child === titleEl) continue;
        avail -= outerWidth(child, cs);
    }

    const gap = parseFloat(hcs.columnGap) || parseFloat(hcs.gap) || 0;
    if (visible > 1) avail -= gap * (visible - 1);
    return avail;
}

const HeaderTitleFit = {
    /** @type {Element|null} */
    _el: null,

    /**
     * Start tracking the header title element and refit it whenever the
     * available width can have changed. Idempotent.
     *
     * @returns {void}
     */
    init() {
        if (this._el) return;
        const el = document.getElementById('header-title-text');
        if (!el) {
            console.warn('[HeaderTitleFit] no #header-title-text, skipping');
            return;
        }
        this._el = el;
        if (!el.dataset.fullTitle) el.dataset.fullTitle = el.textContent || '';

        const refit = () => this.refresh();
        window.addEventListener('resize', refit);
        window.addEventListener('orientationchange', refit);
        // The header reflows when the control cluster folds at 768px, which
        // is a width change of the h1 with no window resize in the PWA case.
        if (typeof window.ResizeObserver === 'function' && el.parentElement) {
            new window.ResizeObserver(refit).observe(el.parentElement);
        }
        this.refresh();
        console.log('[HeaderTitleFit] initialized');
    },

    /**
     * Paint `text` as the header title, middle-elided to fit.
     *
     * Callers pass the FULL name every time; the module owns truncation.
     *
     * @param {string} text  Full session name or brand string.
     * @returns {void}
     */
    setTitle(text) {
        const el = this._el || document.getElementById('header-title-text');
        if (!el) return;
        this._el = el;
        el.dataset.fullTitle = String(text == null ? '' : text);
        this.refresh();
    },

    /**
     * Re-elide the current full title against the current available width.
     *
     * @returns {void}
     */
    refresh() {
        const el = this._el;
        if (!el) return;
        const full = el.dataset.fullTitle || '';
        const width = availableWidth(el);
        if (!(width > 0)) {
            // Header not laid out yet (hidden screen, fonts still loading).
            // Leave the full text in place; CSS ellipsis covers this frame.
            if (el.textContent !== full) el.textContent = full;
            return;
        }
        const measure = makeMeasurer(el);
        try {
            const fitted = elideToWidth(full, width, measure);
            if (el.textContent !== fitted) el.textContent = fitted;
        } finally {
            measure.dispose();
        }
    },

    /** Exposed for tests. @see elideToWidth */
    _elideToWidth: elideToWidth,
    /** Exposed for tests. @see buildCandidate */
    _buildCandidate: buildCandidate
};

window.HeaderTitleFit = HeaderTitleFit;

// Same self-init contract as header-menu.js: safe whether this file is
// parsed before or after DOMContentLoaded.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.HeaderTitleFit.init());
} else {
    window.HeaderTitleFit.init();
}

console.log('[HeaderTitleFit Module] Exported as window.HeaderTitleFit');
