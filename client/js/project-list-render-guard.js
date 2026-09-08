/**
 * WHEN A REPAINT IS WORTH DOING, AND WHEN IT IS PURE DESTRUCTION.
 * ----------------------------------------------------------------------
 * The launchpad's 5s running-sessions poller ends in two unconditional
 * `innerHTML =` writes: `renderRunningSessions()` and
 * `renderProjectList()`. The running-sessions one has carried a
 * signature diff since the pulse-glow flicker was traced to it. The
 * project list never had one, so it tore down and rebuilt its entire
 * subtree every five seconds forever, whether anything had changed or
 * not, and whether or not the launchpad was even the screen on display.
 *
 * Measured on a 9-project / 6-session fixture before this module
 * existed: 10 poller ticks against UNCHANGED data produced 10 rebuilds
 * of `#project-list`, 360 `addEventListener` registrations and 40
 * fetches - and the identical 10 / 360 / 40 with the launchpad hidden
 * behind the terminal screen. After: 0 / 0 / 0 in both cases. The live
 * screen is bigger than the fixture (about 794 nodes and 45 listeners
 * per rebuild), so the saving there is proportionally larger.
 *
 * THE SIGNATURE IS THE MARKUP ITSELF, and that is a deliberate
 * departure from `_lastRunningSig`, which fingerprints a hand-listed
 * set of row fields. A hand-listed fingerprint has one failure mode
 * that is invisible until a user reports it: a field the template
 * renders but the signature does not name stays on screen stale
 * FOREVER, because the guard keeps answering "nothing changed" about a
 * thing that changed. This project has already paid for that twice -
 * see the `wrapper:` line in `renderRunningSessions`' signature and
 * `tests/test_session_theme_tint.node.mjs`. Comparing the built HTML
 * against the last painted HTML cannot drift out of sync with the
 * template, because it IS the template's output. Building the string is
 * microseconds; parsing it into ~800 nodes and re-binding ~45 listeners
 * is what actually costs.
 *
 * SKIPPING A PAINT IS NOT REMEMBERING IT. A skip on any ground other
 * than "identical" leaves the stored signature alone, so the very next
 * tick paints. That is what makes deferral safe: there is no queue, no
 * timer and no flag to leak - the work simply happens on the next pass
 * once the reason to defer has gone.
 *
 * THREE OUTCOMES ON VISIBILITY, and the third one paints. `visible` and
 * `hidden` are read off `#launchpad-screen`'s `active` class, which is
 * what `App.showLaunchpad()` / `App.hideAllScreens()` actually toggle.
 * When neither the element nor `App.currentScreen` can be read the
 * answer is `cannot_determine`, and that POLLS AND PAINTS. Not having
 * been able to look is not evidence the screen is hidden, and a guard
 * that treated it as such would silently freeze the launchpad on any
 * page whose markup it did not recognise.
 *
 * BUSY IS MEASURED IN THE CONTAINER WE ARE ABOUT TO WIPE. An open row
 * overflow menu (`SessionRowMenu.isOpen()` - its own predicate, reused
 * rather than re-derived) and a live inline rename input both mean a
 * user is mid-interaction with something a repaint would delete under
 * their hands. The focus test is scoped with `container.contains()` on
 * purpose: a focused input anywhere else on the page is none of this
 * guard's business, and a page-wide focus test would let one stray
 * focused field freeze the list indefinitely.
 *
 * Named for its first consumer, but `isBusy` is shared: launchpad.js
 * consults it from `renderRunningSessions()` too, because the inline
 * rename input lives in THAT list and it is that list which would
 * clobber it. Load BEFORE launchpad.js.
 */

console.log('[ProjectListRenderGuard Module] Loading...');

(function () {
    'use strict';

    /** Visibility verdicts. The third is not a flavour of the second. */
    var VISIBLE = 'visible';
    var HIDDEN = 'hidden';
    var CANNOT_DETERMINE = 'cannot_determine';

    /** Paint verdicts, one per reason a decision was reached. */
    var PAINT = 'paint';
    var SKIP_UNCHANGED = 'skip_unchanged';
    var SKIP_HIDDEN = 'skip_hidden';
    var SKIP_BUSY = 'skip_busy';

    /** The screen element whose `active` class means "on display". */
    var SCREEN_ID = 'launchpad-screen';
    var ACTIVE_CLASS = 'active';

    /**
     * Elements that mean a user is editing inside the list right now.
     * Exact class names taken from the code that creates them
     * (launchpad.js `_handleRenameRunningSession`), never guessed.
     */
    var EDITOR_SELECTORS = ['.running-session-rename-input'];

    /** Tag names whose focus means "typing", for the focus test. */
    var EDITOR_TAGS = { INPUT: true, TEXTAREA: true, SELECT: true };

    /**
     * Is the launchpad the screen currently on display?
     *
     * Description: reads the DOM first, because the DOM is what the user
     *   is actually looking at; falls back to `App.currentScreen`, which
     *   is the same fact one level removed; answers `cannot_determine`
     *   when neither can be read rather than guessing either way.
     * Inputs: doc (Document) - the document to read.
     * Output: string - VISIBLE, HIDDEN or CANNOT_DETERMINE.
     * Example: ProjectListRenderGuard.launchpadVisibility(document)
     */
    function launchpadVisibility(doc) {
        var el = doc && typeof doc.getElementById === 'function'
            ? doc.getElementById(SCREEN_ID) : null;
        if (el && el.classList && typeof el.classList.contains === 'function') {
            return el.classList.contains(ACTIVE_CLASS) ? VISIBLE : HIDDEN;
        }
        var app = typeof window !== 'undefined' ? window.App : null;
        if (app && typeof app.currentScreen === 'string' && app.currentScreen) {
            return app.currentScreen === 'launchpad' ? VISIBLE : HIDDEN;
        }
        return CANNOT_DETERMINE;
    }

    /**
     * Should the 5s poller run its fetches this tick?
     *
     * Description: false ONLY for a measured `hidden`. An unknown screen
     *   state keeps polling - see the header on why the third outcome
     *   sides with doing the work.
     * Inputs: doc (Document).
     * Output: boolean.
     * Example: if (!ProjectListRenderGuard.shouldPoll(document)) return;
     */
    function shouldPoll(doc) {
        return launchpadVisibility(doc) !== HIDDEN;
    }

    /**
     * Is the user mid-interaction with something inside this container?
     *
     * Description: an open row overflow menu, a live inline rename
     *   input, or focus sitting in an editable field INSIDE the
     *   container. Any of the three makes a repaint destructive rather
     *   than merely wasteful.
     * Inputs: opts (object) - {container: Element, doc: Document}.
     * Output: boolean.
     * Example: if (guard.isBusy({container: el, doc: document})) return;
     */
    function isBusy(opts) {
        var container = opts && opts.container;
        var doc = (opts && opts.doc) || (typeof document !== 'undefined' ? document : null);
        if (!container) return false;

        var menu = typeof window !== 'undefined' ? window.SessionRowMenu : null;
        if (menu && typeof menu.isOpen === 'function' && menu.isOpen()) return true;

        if (typeof container.querySelector === 'function') {
            for (var i = 0; i < EDITOR_SELECTORS.length; i++) {
                if (container.querySelector(EDITOR_SELECTORS[i])) return true;
            }
        }

        var active = doc ? doc.activeElement : null;
        if (active
                && typeof container.contains === 'function'
                && container.contains(active)) {
            if (EDITOR_TAGS[active.tagName]) return true;
            if (active.isContentEditable) return true;
        }
        return false;
    }

    /**
     * Decide whether to write `html` into `container`, and why.
     *
     * Description: the single decision point. Returns the signature the
     *   caller should store, which is the NEW markup only when the paint
     *   is actually happening - every skip hands back the previous
     *   signature so the next tick reconsiders from the same place.
     * Inputs: opts (object) -
     *   html (string) - the markup the renderer just built.
     *   lastSignature (string|null) - what was last painted, or null.
     *   container (Element) - the element the markup would be written to.
     *   doc (Document) - optional, defaults to the global document.
     * Output: object - {paint: boolean, reason: string, signature: string|null}.
     * Example:
     *   var v = guard.decide({html: h, lastSignature: s, container: el});
     *   if (v.paint) { el.innerHTML = h; s = v.signature; }
     */
    function decide(opts) {
        var html = (opts && typeof opts.html === 'string') ? opts.html : '';
        var last = (opts && opts.lastSignature !== undefined) ? opts.lastSignature : null;
        var container = opts && opts.container;
        var doc = (opts && opts.doc) || (typeof document !== 'undefined' ? document : null);

        if (isBusy({ container: container, doc: doc })) {
            return { paint: false, reason: SKIP_BUSY, signature: last };
        }
        if (launchpadVisibility(doc) === HIDDEN) {
            return { paint: false, reason: SKIP_HIDDEN, signature: last };
        }
        if (last !== null && html === last) {
            return { paint: false, reason: SKIP_UNCHANGED, signature: last };
        }
        return { paint: true, reason: PAINT, signature: html };
    }

    window.ProjectListRenderGuard = {
        VISIBLE: VISIBLE,
        HIDDEN: HIDDEN,
        CANNOT_DETERMINE: CANNOT_DETERMINE,
        PAINT: PAINT,
        SKIP_UNCHANGED: SKIP_UNCHANGED,
        SKIP_HIDDEN: SKIP_HIDDEN,
        SKIP_BUSY: SKIP_BUSY,
        launchpadVisibility: launchpadVisibility,
        shouldPoll: shouldPoll,
        isBusy: isBusy,
        decide: decide,
    };
})();

console.log('[ProjectListRenderGuard Module] Exported as window.ProjectListRenderGuard');
