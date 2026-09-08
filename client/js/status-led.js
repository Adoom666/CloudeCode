/**
 * Status LED - the two-ring session indicator.
 *
 * WHY TWO RINGS AND NOT ONE DOT. The old `.status-dot` collapsed two
 * genuinely independent questions into one colour: WHAT is the chat doing
 * (is it working, is it blocked on me, is it dead) and WHETHER it wants my
 * attention (is there something unread here). One dot cannot answer both -
 * `finished_unread` had to exist as a whole extra state just to say "done,
 * and also unread", and there was no way at all to say "working, and also
 * unread". Two rings say both at once and need no combined state.
 *
 * So this module models the LED as TWO INDEPENDENT DIMENSIONS:
 *
 *   inner dot  - the chat's own status. One of INNER_STATES.
 *   outer halo - activity and attention. One of OUTER_STATES.
 *
 * They are set separately (`data-inner` / `data-outer`) and every
 * combination renders. That is deliberate: it is what lets a gallery
 * enumerate the whole matrix, and it is why neither vocabulary contains a
 * value that means something about the other one.
 *
 * PURE BY CONTRACT. `ledHtml()` touches no DOM, reads no globals, and
 * returns a string. It must stay that way - a standalone gallery page
 * embeds `client/css/status-led.css` and calls it directly, with none of
 * this app around it. No `document`, no `window`, no fetch.
 *
 * Colour and motion live entirely in `client/css/status-led.css`, keyed
 * off the two data attributes. There are no colour values in this file.
 *
 * Loads BEFORE client/js/session-status-ui.js, which maps this app's
 * `activity_status` onto these two dimensions and renders through here.
 */

console.log('[StatusLed Module] Loading...');

(function () {
    /**
     * Inner-dot vocabulary: what the chat itself is doing.
     *
     * `waiting-permission` and `waiting-input` are separate because they
     * ask different things of the user - one is "approve this tool call",
     * the other is "answer a question / press a key to get past a startup
     * prompt". BOTH ARE REACHABLE FROM LIVE DATA as of 2026-09-08: the
     * server split its single `question` state into `question` (a
     * PermissionRequest - the agent is stopped) and `notice` (a
     * Notification - it wants attention and is not stopped), and the
     * startup gate feeds `waiting-input` too. See docs/session-status.md.
     *
     * @type {string[]}
     */
    const INNER_STATES = [
        'working',
        'waiting-permission',
        'waiting-input',
        'done',
        'dead',
        'unknown',
    ];

    /**
     * Outer-halo vocabulary: activity and attention.
     *
     * `active` and `unread` both animate, and they are not the same
     * claim: `active` means the session is MOVING, `unread` means it is
     * WAITING FOR YOU. A session can be both (working, with an earlier
     * unread Stop) - the halo shows `unread`, because the thing the user
     * needs to act on outranks the thing that is proceeding without them.
     *
     * @type {string[]}
     */
    const OUTER_STATES = ['active', 'steady', 'unread', 'off', 'dim'];

    /** @type {string} Fallback inner state for any unrecognised input. */
    const INNER_FALLBACK = 'unknown';

    /** @type {string} Fallback outer state for any unrecognised input. */
    const OUTER_FALLBACK = 'dim';

    /**
     * Human-readable label per inner state. The LED never conveys meaning
     * by colour alone - this text becomes the title and aria-label.
     * @type {Object<string, string>}
     */
    const INNER_LABELS = {
        working: 'working',
        'waiting-permission': 'waiting on you - permission',
        'waiting-input': 'waiting on you',
        done: 'done',
        dead: 'dead - process exited',
        unknown: 'status not measured',
    };

    /**
     * Human-readable suffix per outer state, appended to the inner label
     * only when it adds something the inner label does not already say.
     * @type {Object<string, string>}
     */
    const OUTER_LABELS = {
        active: '',
        steady: '',
        unread: 'unread',
        off: '',
        dim: '',
    };

    /**
     * Escape a value for interpolation into a double-quoted HTML attribute.
     *
     * Description: `&` is replaced first or later replacements would be
     *   re-escaped. Deliberately string-based, not the
     *   `textContent`/`innerHTML` trick used elsewhere in the client:
     *   that idiom does not escape quote characters and so is wrong for
     *   an attribute value. Also, this module may not touch the DOM.
     * Inputs:
     *   value (any) - stringified first; null/undefined become ''.
     * Output:
     *   string - safe between the quotes of an attribute.
     * Example:
     *   escapeAttr('a"b') -> 'a&quot;b'
     */
    function escapeAttr(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /**
     * Clamp any value onto a known inner state.
     *
     * Inputs: inner (string|null|undefined).
     * Output: string - a member of INNER_STATES.
     * Example: normalizeInner('nope') -> 'unknown'
     */
    function normalizeInner(inner) {
        return INNER_STATES.indexOf(inner) >= 0 ? inner : INNER_FALLBACK;
    }

    /**
     * Clamp any value onto a known outer state.
     *
     * Inputs: outer (string|null|undefined).
     * Output: string - a member of OUTER_STATES.
     * Example: normalizeOuter(undefined) -> 'dim'
     */
    function normalizeOuter(outer) {
        return OUTER_STATES.indexOf(outer) >= 0 ? outer : OUTER_FALLBACK;
    }

    /**
     * The accessible label for one (inner, outer) pair.
     *
     * Description: colour and motion never carry the meaning alone, so
     *   every LED ships this string as both `title` and `aria-label`. The
     *   outer half is appended only when it says something new - "done,
     *   unread" is worth two words, "working" plus `active` is not.
     * Inputs: inner (string), outer (string) - already normalized.
     * Output: string.
     * Example: ledLabel('done', 'unread') -> 'done, unread'
     */
    function ledLabel(inner, outer) {
        const head = INNER_LABELS[inner];
        const tail = OUTER_LABELS[outer];
        return tail ? head + ', ' + tail : head;
    }

    /**
     * Build the markup for one status LED.
     *
     * Description: PURE - no DOM, no globals, no side effects. Returns a
     *   single `<span>` carrying `data-inner` and `data-outer`; every
     *   colour and every animation is selected off those two attributes
     *   by client/css/status-led.css. The halo is a pseudo-element on the
     *   same span, so the LED occupies one inline box and drops into any
     *   row that used to hold a `.status-dot` with no layout change.
     *
     *   `role="img"` marks it a meaningful glyph rather than decoration.
     *   Unrecognised inputs are clamped rather than rejected, so a stale
     *   cached API response can never render an unstyled or unlabelled
     *   LED.
     * Inputs:
     *   opts (Object|null) - `{inner, outer, size, title}`. `inner` and
     *     `outer` are clamped onto the two vocabularies. `size` is an
     *     optional CSS length for the whole LED (default comes from the
     *     stylesheet); it is emitted as a custom property, never as a
     *     raw style rule, so the stylesheet keeps control of the ratio
     *     between the dot and its halo. `title` overrides the derived
     *     label when a caller has a more specific sentence.
     * Output:
     *   string - HTML for one inline `<span>`.
     * Example:
     *   ledHtml({inner: 'working', outer: 'active'})
     *   // '<span class="status-led" data-inner="working"
     *   //    data-outer="active" role="img" title="working"
     *   //    aria-label="working"></span>'
     */
    function ledHtml(opts) {
        const o = opts || {};
        const inner = normalizeInner(o.inner);
        const outer = normalizeOuter(o.outer);
        const label = o.title == null ? ledLabel(inner, outer) : String(o.title);
        // Only a plain CSS length is accepted. Anything else is dropped
        // rather than sanitised: this value lands in a style attribute,
        // and an allowlist is the only version of that which is safe to
        // reason about.
        const size =
            typeof o.size === 'string' && /^[0-9]+(\.[0-9]+)?(px|rem|em)$/.test(o.size)
                ? o.size
                : '';
        const styleAttr = size ? ` style="--led-size: ${escapeAttr(size)}"` : '';
        // Extra classes for a caller that needs this element findable by
        // an existing selector (session-status-ui.js keeps the legacy
        // `status-dot` vocabulary on it). Restricted to a plain class-name
        // shape so the attribute cannot be broken out of.
        const extra =
            typeof o.extraClass === 'string' && /^[A-Za-z0-9 _-]+$/.test(o.extraClass)
                ? o.extraClass.trim() + ' '
                : '';
        return (
            `<span class="${escapeAttr(extra)}status-led" data-inner="${escapeAttr(inner)}" ` +
            `data-outer="${escapeAttr(outer)}" role="img" ` +
            `title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}"` +
            `${styleAttr}></span>`
        );
    }

    /**
     * Map this app's server-side signals onto the two LED dimensions.
     *
     * Description: THE ONE PLACE the unified `activity_status` vocabulary
     *   (src/core/session_status.py) is translated into (inner, outer).
     *   Every surface - sidebar row, launchpad card, terminal header,
     *   group header - renders through here, so a light cannot mean two
     *   things in two places.
     *
     *   Order matters and encodes the priority the user asked for:
     *   dead outranks everything (a corpse is not "busy"), then anything
     *   blocking on the user, then activity, then attention, then rest.
     *   `unread` is applied to the HALO independently of all of it, which
     *   is the whole reason there are two rings - see the module header.
     * Inputs:
     *   signals (Object|null) - `{activity_status, unread, startup_gate}`
     *     exactly as they arrive on a `/sessions/list` row. All three are
     *     optional; missing ones degrade to not-measured rather than to a
     *     confident answer.
     * Output:
     *   Object - `{inner, outer}`, both members of the vocabularies.
     * Example:
     *   ledStateFor({activity_status: 'idle', unread: true})
     *   // {inner: 'done', outer: 'unread'}
     */
    function ledStateFor(signals) {
        const s = signals || {};
        const status = s.activity_status;
        const unread = !!s.unread;
        const gate = s.startup_gate;

        // A dead pane is dead whatever else is true of it, and an unread
        // flag must not paint a corpse as something to go and read.
        if (status === 'dead' || status === 'stopped') {
            return { inner: 'dead', outer: 'off' };
        }

        // Blocked on a keypress at startup. Measured by the startup gate,
        // which is a separate probe from the hook stream, so it is
        // checked on its own rather than folded into `activity_status`.
        if (gate === 'awaiting_startup_prompt') {
            return { inner: 'waiting-input', outer: 'active' };
        }

        // THE AGENT IS STOPPED. `question` is a PermissionRequest and
        // nothing else since the 2026-09-08 split - it gets the louder
        // inner hue, because a yes/no nobody has answered is the one
        // state on this screen that will not resolve itself.
        if (status === 'question') {
            return { inner: 'waiting-permission', outer: 'active' };
        }

        // Claude asked to be looked at and is NOT blocked. Same halo as a
        // permission prompt - both want the user - and the calmer of the
        // two inner hues, shared with the startup gate above because both
        // are "come and look", not "approve this".
        if (status === 'notice') {
            return { inner: 'waiting-input', outer: 'active' };
        }

        // `running` is the pre-hook-era spelling and still arrives from a
        // stale cached response; mapping it here rather than letting it
        // fall through keeps a half-upgraded deployment meaningful.
        if (
            status === 'working' ||
            status === 'working_subagent' ||
            status === 'running'
        ) {
            return { inner: 'working', outer: unread ? 'unread' : 'active' };
        }

        if (status === 'finished_unread') {
            return { inner: 'done', outer: 'unread' };
        }

        if (status === 'idle') {
            return { inner: 'done', outer: unread ? 'unread' : 'steady' };
        }

        // Everything else - `unknown`, an absent field, a state this
        // client does not know yet. NOT `done`: not having measured is
        // not the same as having measured rest, and collapsing the two is
        // the false green this project keeps paying for.
        return { inner: 'unknown', outer: unread ? 'unread' : 'dim' };
    }

    const api = {
        INNER_STATES: INNER_STATES.slice(),
        OUTER_STATES: OUTER_STATES.slice(),
        INNER_LABELS: INNER_LABELS,
        ledHtml: ledHtml,
        ledStateFor: ledStateFor,
        ledLabel: ledLabel,
        normalizeInner: normalizeInner,
        normalizeOuter: normalizeOuter,
    };

    // Published on the global object rather than on `window` by name, so
    // the same file loads unchanged in a browser, in a standalone gallery
    // page, and under `node --test` (which has a globalThis and no
    // window). This module has no other environment dependency; keep it
    // that way.
    globalThis.StatusLed = api;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }

    console.log('[StatusLed Module] Loaded');
})();
