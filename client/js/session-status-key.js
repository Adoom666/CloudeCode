/**
 * The status-light KEY - a foldable legend naming every dot in the list.
 *
 * WHY IT EXISTS. Since the five-colour pass the LED is the ONLY thing on a
 * sidebar row that says what a session is doing: the envelope icon is gone
 * and there is no text badge beside it. That is a good indicator and an
 * unlabelled one - the meaning lives in a `title` nobody hovers on a phone
 * and an `aria-label` a sighted user never hears. This is where the
 * vocabulary is written down in words, in the one place a reader is
 * already looking at the lights.
 *
 * IT DRAWS REAL LEDS, NOT PICTURES OF THEM. Every swatch is
 * `StatusLed.ledHtml` with the same (inner, outer) pair the rows resolve
 * to, so the key cannot show a colour, a size or a shape the app does not
 * actually paint. A hand-drawn legend is a second implementation of the
 * component, and a second implementation is a drift waiting to happen -
 * which is the exact failure this project has already paid for with two
 * stylesheets painting one dot.
 *
 * IT SITS WHERE THE ORDER NOTE USED TO. The sidebar footer previously
 * carried "N remembered positions are held for sessions not currently
 * listed", removed on 2026-09-09 because it named an internal bookkeeping
 * detail no reader could act on. The remembered positions themselves are
 * untouched - see client/js/session-sidebar-arrangement.js, which still
 * keeps those slots, and the `data-order-missing` count still stamped on
 * the list element for diagnosis.
 *
 * COLLAPSED BY DEFAULT, and the fold is a preference, not data: it rides
 * the app's existing `cloude.*` localStorage convention (the same one
 * client/js/session-sidebar-pin.js and session-sidebar-density.js use),
 * and an unreadable or absent value means collapsed rather than an error.
 *
 * Depends on client/js/status-led.js. Uses window.SessionSidebarGroups for
 * the chevron glyph when it is loaded, and renders without one when it is
 * not - a missing glyph must not cost the reader the legend.
 *
 * Must load AFTER status-led.js and BEFORE session-sidebar-rows.js.
 */

console.log('[SessionStatusKey Module] Loading...');

(function () {
    /**
     * localStorage key for the fold. '1' open, anything else closed.
     * @type {string}
     */
    const STORAGE_KEY = 'cloude.statusKey.open';

    /** @type {string} The id `aria-controls` points at. */
    const BODY_ID = 'session-status-key-body';

    /**
     * Every entry in the key, top to bottom.
     *
     * `inner` and `outer` are fed straight to `StatusLed.ledHtml`, so a
     * swatch is the shipped component and not a copy of it. `text` is the
     * plain-English line beside it, and it says only what the state
     * actually claims - see docs/session-status.md, which is the authority
     * for every one of these sentences.
     *
     * ORDER IS BY URGENCY, matching the group-header fold in
     * client/js/session-status-summary.js: the states that want the user
     * come first, rest and not-measured last. A reader scanning the top of
     * this list is reading the states worth acting on.
     *
     * ONE ROW PER LIGHT, NOT ONE ROW PER STATE. This list carried nine
     * rows until 2026-09-09 and the owner asked for seven: "there should
     * only be one entry per colour". The two yellow rows collapsed into
     * one and the two red rows collapsed into one, because a reader
     * looking up a dot is asking what the COLOUR on their screen means,
     * and being shown the same yellow twice with two different sentences
     * makes them hunt for a difference the light cannot show them.
     *
     * THE STATE MACHINE DID NOT CHANGE. There are still eight inner
     * states; `waiting-permission` and `waiting-input` still paint one
     * yellow, `dead` and `disconnected` still paint one red, and the
     * component's own `title` and `aria-label` still say WHICH of the
     * pair any given dot is - see INNER_LABELS in client/js/status-led.js
     * and the test that pins it. What collapsed is this legend's rows.
     *
     * Green appears twice and grey appears twice, and that is not a
     * violation of the rule: those are four visually distinct lights, not
     * two. A solid green dot and a green ring are different shapes, and
     * so are a solid grey dot and a grey outline - which is exactly the
     * distinction the last row exists to explain.
     *
     * @type {Array<{inner: string, outer: string, text: string}>}
     */
    const ENTRIES = [
        // YELLOW. Both stopped-on-a-human states land here. Drawn as
        // `waiting-permission` because that is the commoner of the two;
        // the other paints the identical hue, and its own dot on a real
        // row still says "startup prompt" in words.
        {
            inner: 'waiting-permission',
            outer: 'active',
            text: 'stopped, waiting on you',
        },
        // LIGHT BLUE. The one state that is working AND wants a human.
        {
            inner: 'notice',
            outer: 'active',
            text: 'still working, but needs your attention',
        },
        // GREEN, SOLID.
        { inner: 'working', outer: 'active', text: 'working' },
        // GREEN, AS A RING. The finished-turn treatment: a green outline
        // around a cleared centre, which is why the row after this one
        // has to say what a SOLID grey dot means instead.
        { inner: 'done', outer: 'unread', text: 'done, unread' },
        // GREY, SOLID.
        { inner: 'done', outer: 'steady', text: 'idle' },
        // RED. A dead pane and a dead socket paint the same red; the dot
        // on a real row says which in its tooltip and its aria-label.
        { inner: 'dead', outer: 'off', text: 'dead / disconnected session' },
        // GREY, AS AN OUTLINE. The row the owner had to ask about, so the
        // sentence has to land without a second reading: the difference
        // from the solid grey above is not severity, it is whether anyone
        // looked. Solid grey is a MEASURED "nothing pending"; this is no
        // measurement at all.
        {
            inner: 'unknown',
            outer: 'dim',
            text: 'not measured - nothing reported in, so this is not idle',
        },
    ];

    /**
     * The one line explaining the OTHER place a dot appears.
     *
     * A group header carries the same component rolled up over everything
     * in the section, so a reader who has just learned the vocabulary
     * needs to be told it is reused rather than left to guess that a
     * header dot means something else.
     * @type {string}
     */
    const HEADER_NOTE =
        'a group header shows one dot for everything inside it: '
        + 'the state in that group that most wants you.';

    /**
     * Whether the key is currently unfolded.
     *
     * Description: reads the stored preference. A browser that refuses
     *   localStorage (private mode, a blocked origin) answers `false`
     *   rather than throwing - the key still works, it simply opens
     *   closed every time.
     * Inputs: none.
     * Output: boolean.
     * Example: isOpen() -> false
     */
    function isOpen() {
        try {
            return localStorage.getItem(STORAGE_KEY) === '1';
        } catch (_) {
            return false;
        }
    }

    /**
     * Remember the fold.
     *
     * Description: a write failure is swallowed on purpose, with the same
     *   reasoning as `isOpen` - losing a preference is not worth breaking
     *   a render over.
     * Inputs: open (boolean).
     * Output: void.
     * Example: setOpen(true)
     */
    function setOpen(open) {
        try {
            localStorage.setItem(STORAGE_KEY, open ? '1' : '0');
        } catch (_) {
            /* a preference that cannot be stored is still a working key */
        }
    }

    /**
     * The chevron, borrowed from the group headers so there is one glyph.
     *
     * Inputs: none. Output: string - HTML, or '' when groups is absent.
     */
    function chevronHtml() {
        const G = globalThis.SessionSidebarGroups;
        return G && G.chevronHtml ? G.chevronHtml() : '';
    }

    /**
     * One row of the key: a real LED and the sentence beside it.
     *
     * Description: the LED carries its own `title` and `aria-label` from
     *   the component, and the sentence is visible text, so the meaning is
     *   available three ways and never by colour alone.
     * Inputs: entry (Object) - one member of ENTRIES.
     * Output: string - HTML for one `<li>`.
     * Example: itemHtml(ENTRIES[3])
     */
    function itemHtml(entry) {
        const led = globalThis.StatusLed.ledHtml({
            inner: entry.inner,
            outer: entry.outer,
        });
        return (
            '<li class="session-status-key__item" '
            + `data-key-state="${entry.inner}/${entry.outer}">`
            + `<span class="session-status-key__swatch">${led}</span>`
            + `<span class="session-status-key__text">${entry.text}</span>`
            + '</li>'
        );
    }

    /**
     * The whole key, folded or unfolded per the stored preference.
     *
     * Description: a real `<button>` carrying `aria-expanded` and
     *   `aria-controls`, which is the same disclosure shape the group
     *   headers use - so it is reachable by Tab, operated by Enter and
     *   Space for free, and announced correctly. The body is emitted
     *   either way and hidden with the `hidden` attribute rather than
     *   dropped, so `aria-controls` always resolves to a real element.
     *
     *   The `open` state is read at RENDER time, which is what makes it
     *   survive the list repainting itself: the sidebar rebuilds this
     *   markup from a signature that knows nothing about the fold.
     * Inputs: none (reads localStorage, and StatusLed off the global).
     * Output: string - HTML, or '' when the LED component is absent (a
     *   key with no swatches would be a legend for nothing).
     * Example: keyHtml()
     */
    function keyHtml() {
        if (!globalThis.StatusLed) return '';
        const open = isOpen();
        const verb = open ? 'Hide' : 'Show';
        const title = `${verb} the key to the status lights`;
        return (
            '<div class="session-status-key" data-status-key>'
            + '<button type="button" class="session-status-key__toggle" '
            + 'data-status-key-toggle '
            + `aria-expanded="${open ? 'true' : 'false'}" `
            + `aria-controls="${BODY_ID}" `
            + `title="${title}" aria-label="${title}">`
            + chevronHtml()
            + '<span class="session-status-key__label">what the lights mean</span>'
            + '</button>'
            + `<div class="session-status-key__body" id="${BODY_ID}"`
            + `${open ? '' : ' hidden'}>`
            + '<ul class="session-status-key__list">'
            + ENTRIES.map(itemHtml).join('')
            + '</ul>'
            + `<p class="session-status-key__note">${HEADER_NOTE}</p>`
            + '</div>'
            + '</div>'
        );
    }

    /**
     * Fold or unfold the key IN PLACE, and remember which.
     *
     * Description: toggles the live DOM rather than asking the sidebar to
     *   repaint. A repaint would rebuild every row to move one attribute,
     *   and would drop the focus the user is holding on the button they
     *   just pressed.
     * Inputs: btnEl (Element) - the clicked `[data-status-key-toggle]`.
     * Output: boolean - the new open state, or false when the markup the
     *   button belongs to could not be found.
     * Example: onToggleClick(btn) -> true
     */
    function onToggleClick(btnEl) {
        if (!btnEl) return false;
        const root = btnEl.closest('[data-status-key]');
        const body = root ? root.querySelector('.session-status-key__body') : null;
        if (!body) return false;
        const open = btnEl.getAttribute('aria-expanded') !== 'true';
        btnEl.setAttribute('aria-expanded', open ? 'true' : 'false');
        const title = `${open ? 'Hide' : 'Show'} the key to the status lights`;
        btnEl.setAttribute('title', title);
        btnEl.setAttribute('aria-label', title);
        body.hidden = !open;
        setOpen(open);
        return open;
    }

    globalThis.SessionStatusKey = {
        STORAGE_KEY, BODY_ID, ENTRIES, HEADER_NOTE,
        isOpen, setOpen, keyHtml, itemHtml, onToggleClick,
    };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = globalThis.SessionStatusKey;
    }

    console.log('[SessionStatusKey Module] Exported as window.SessionStatusKey');
})();
