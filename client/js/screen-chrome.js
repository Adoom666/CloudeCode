/**
 * Screen-state marker for app chrome.
 *
 * WHY THIS EXISTS, AND WHY IT IS NOT "THREE MORE LINES IN showAuth()".
 * App.showAuth() used to hide the authenticated-only header controls by
 * NAMING them, one classList.add('hidden') per button, and
 * showLaunchpad() / showTerminal() un-hid the same names again. That is a
 * hand-maintained list in four places, and the failure mode is silent:
 * a control added to the header without being remembered in every one of
 * those places renders on the LOGIN screen, where the user is not
 * authenticated and none of it does anything. Two controls leaked exactly
 * that way (the slash-commands button and the header kebab) and were
 * found by the user, not by a test, because nothing about the code looked
 * wrong - the list was simply short.
 *
 * THE DEFAULT IS INVERTED HERE. This module stamps a single marker on
 * <body> saying whether the app is on an AUTHENTICATED screen, and
 * client/css/screen-chrome.css hides the authenticated-only chrome
 * whenever that marker is ABSENT. Absence is the default, so a new
 * control is hidden on the login screen with no code written, and has to
 * opt IN (data-show-on-auth) to appear there. Forgetting is now the safe
 * direction.
 *
 * WHY A BODY MARKER RATHER THAN A CSS `:has(#auth-screen.active)` RULE:
 * both work today, but `:has()` on the screen div encodes "which screens
 * are the unauthenticated ones" as a list inside a selector - the same
 * enumeration this module exists to delete, one layer down. A boolean
 * that is false until something asserts it cannot be short.
 *
 * `data-screen` is written alongside it purely so the current screen is
 * readable in devtools and in a test measurement; no rule keys off it.
 */

console.log('[ScreenChrome Module] Loading...');

/**
 * Screens on which the user is authenticated and the full app chrome is
 * meaningful. Anything not listed here is treated as unauthenticated,
 * which is the fail-closed direction: an unknown screen name hides the
 * chrome rather than showing it.
 * @type {string[]}
 */
const AUTHENTICATED_SCREENS = ['launchpad', 'terminal', 'archive'];

const ScreenChrome = {
    /**
     * Description: record the current screen on <body> so CSS can gate
     *   authenticated-only chrome on it. Idempotent; safe to call with
     *   the same screen repeatedly.
     * Inputs: screen (string) - screen name, e.g. 'auth', 'launchpad',
     *   'terminal'. Any value not in AUTHENTICATED_SCREENS is treated as
     *   unauthenticated.
     * Output: void.
     * Example: ScreenChrome.apply('auth');  // login chrome only
     */
    apply(screen) {
        const body = document.body;
        if (!body) return;
        const name = typeof screen === 'string' ? screen : '';
        body.dataset.screen = name;
        body.classList.toggle(
            'is-authenticated',
            AUTHENTICATED_SCREENS.indexOf(name) !== -1
        );
    },

    /**
     * Description: whether a screen name counts as authenticated. Exposed
     *   so a test can assert the policy without re-listing it.
     * Inputs: screen (string).
     * Output: boolean.
     */
    isAuthenticated(screen) {
        return AUTHENTICATED_SCREENS.indexOf(screen) !== -1;
    },

    /** @type {string[]} the authenticated screen names, for tests. */
    AUTHENTICATED_SCREENS
};

window.ScreenChrome = ScreenChrome;
console.log('[ScreenChrome Module] Loaded');
