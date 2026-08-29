/**
 * THE ONE PLACE THAT DECIDES WHAT A SESSION IS CALLED ON SCREEN.
 *
 * A session's NAME IS A LABEL - free-form text a human typed, stored as
 * ``sessions.title`` and delivered as ``label``. The tmux session name is
 * an internal handle derived from that label once, at creation, and never
 * moved again. Keeping them separate is what stops a rename from moving
 * the field session identity is keyed on.
 *
 * WHY THIS IS A MODULE AND NOT A METHOD. Five surfaces render a session's
 * name: the launchpad rows, the browser tab title, the in-page header, a
 * toast card and the attribution prompt. Every one of them has to answer
 * the same question - what do I show when this session has no label? -
 * and a session with no label is not an edge case: it is EVERY session
 * that existed before labels did, plus every external session this app
 * never created. Three surfaces each answering that question separately
 * will drift, and the drift is invisible, because each one looks correct
 * on its own. So the rule lives here, once, and the surfaces call it.
 *
 * THE RULE HAS THREE OUTCOMES, NOT TWO:
 *
 *   1. a label      -> the label, verbatim. Whatever a human typed, and
 *                      that is the whole point of the feature: spaces,
 *                      ``:``, ``.``, quotes and ``$`` are all legal in a
 *                      label because a label is never handed to tmux.
 *   2. no label     -> the ``cloude_``-stripped tmux name. This is
 *                      EXACTLY what every one of these surfaces rendered
 *                      before labels existed, so a session with no label
 *                      looks precisely as it always did.
 *   3. neither      -> null, meaning THIS SESSION CANNOT BE NAMED. Never
 *                      an empty string, never the literal word "null",
 *                      never a silent blank - rendering nothing where a
 *                      name goes is worse than rendering the handle, and
 *                      far worse than saying you do not know.
 *
 * WHAT IS DELIBERATELY *NOT* HERE. Outcome 3 is returned, not rendered.
 * The surfaces word it differently on purpose - the tab title falls back
 * to the bare brand, a toast card says so in a sentence - and that is a
 * difference in WORDING, not a second fallback rule. A caller that
 * renders null as a blank has a bug; a caller that renders it as its own
 * sentence is correct.
 *
 * NOT AN IDENTITY, EITHER. Two sessions may carry the same label, and a
 * label matches nothing on the server. Anything that LOOKS UP a session -
 * the deep-link slug matcher, group membership, the adopt POST body, the
 * pinned-theme PATCH URL - must keep using the tmux name. This module is
 * for what a human reads and for nothing else.
 */
(function () {
    'use strict';

    /**
     * The prefix this app puts on the tmux names it creates. Stripped for
     * display because it is an artefact of the launcher, not anything a
     * user typed. Mirrors APP_TMUX_PREFIX in src/core/session_label.py.
     */
    var APP_TMUX_PREFIX = 'cloude_';

    /**
     * Longest label the SERVER will store. Mirrors LABEL_MAX_CHARS in
     * src/core/session_label.py.
     *
     * WHY A MIRROR AND NOT A FETCH. Nothing serves this constant, and an
     * editor has to know its own limit before it can draw a `maxlength`,
     * which is before any request it could learn the answer from. So it
     * is copied - but copied ONCE, here, rather than into each of the
     * three rename controls, and the copy is enforced:
     * tests/test_label_constants_parity.py parses both files and fails if
     * the two numbers drift. A mirror nobody checks is just a second
     * declaration waiting to disagree.
     *
     * The direction of a drift decides how it hurts, which is why the
     * parity test asserts EQUALITY rather than `client <= server`. A
     * client that permits MORE than the server accepts sends a label that
     * comes back rejected after the user typed it; a client that permits
     * LESS silently truncates at the `maxlength` and stores something the
     * user did not write, with no error anywhere. The second is worse and
     * is exactly what the hardcoded 64 was doing.
     * @type {number}
     */
    var LABEL_MAX_CHARS = 200;

    /**
     * Control characters, which no label may contain. Mirrors
     * _CONTROL_CHARS in src/core/session_label.py, newline and tab
     * included: a label is rendered on one line on every surface.
     * @type {RegExp}
     */
    var CONTROL_CHARS = /[\x00-\x1f\x7f]/;

    /**
     * What a surface shows for outcome 3. A sentence, because a blank
     * cell and an unknowable one look identical to a reader and mean
     * opposite things.
     */
    var UNKNOWN = 'unknown session';

    /**
     * Trim a value only if it is genuinely a string.
     *
     * Description: a JSON ``null`` arrives as null and a mis-shaped
     * payload could carry a number or an object. None of those is a
     * name, and ``String(null)`` would put the literal word "null" in a
     * browser tab, which is the exact failure this returns null for.
     * Inputs: value (*) - anything.
     * Output: string|null - the trimmed string, or null.
     */
    function cleanString(value) {
        if (typeof value !== 'string') return null;
        var trimmed = value.trim();
        return trimmed.length ? trimmed : null;
    }

    /**
     * Strip the app's tmux prefix and derive a readable display form.
     *
     * Description: THE ONE PLACE THIS APP TURNS A TMUX NAME INTO
     *   SOMETHING A HUMAN READS, mirroring ``label_from_tmux_name`` in
     *   ``src/core/session_label.py`` byte for byte on the transform
     *   itself - strip the ``cloude_`` prefix (only when something
     *   survives it), then replace every underscore with a space. The
     *   two used to disagree: this function stripped the prefix and
     *   stopped, so a session named ``Media_Compression`` rendered here
     *   with its underscore intact while the SAME name, run through the
     *   server's v9 label backfill, rendered as ``Media Compression`` -
     *   one conversation, two spellings, on two surfaces. Pinned equal
     *   by tests/test_label_derivation_parity.node.mjs (JS side) and
     *   tests/test_label_derivation_parity.py (Python side), both
     *   walking the SAME table of cases in tests/label_derivation_cases.json
     *   so the two can never drift apart again without a failing test.
     *
     *   ONE DELIBERATE NON-MIRROR. Python falls back to the ORIGINAL
     *   tmux name when the derived form is empty (``or tmux_name``), so
     *   ``label_from_tmux_name('cloude_')`` returns ``'cloude'`` rather
     *   than nothing - because that function feeds a stored ``title``
     *   column that is never allowed to go blank. This function instead
     *   returns null for that case, because null here means outcome 3
     *   of the module header's three-outcome rule - "this session
     *   cannot be named" - and a caller renders that as its own
     *   sentence (``UNKNOWN`` or similar) rather than a fabricated
     *   handle. Different callers, different empty-case contracts;
     *   the TRANSFORM applied to a non-degenerate name is identical.
     * Inputs: tmuxName (string|null).
     * Output: string|null - the display form, or null when nothing is
     *   left (a name that is ONLY the prefix strips to the empty string,
     *   which must not be rendered as a nameless row).
     * Example: SessionLabel.stripAppPrefix('cloude_Media')  -> 'Media'
     * Example: SessionLabel.stripAppPrefix('Media_Compression')
     *   -> 'Media Compression'
     */
    function stripAppPrefix(tmuxName) {
        var name = cleanString(tmuxName);
        if (name === null) return null;
        if (name.indexOf(APP_TMUX_PREFIX) === 0) {
            name = name.slice(APP_TMUX_PREFIX.length);
        }
        if (!name.length) return null;
        var derived = name.split('_').join(' ').trim();
        return derived.length ? derived : null;
    }

    /**
     * The string a HUMAN should see for one session, or null.
     *
     * Description: the whole fallback rule, and the only copy of it.
     *
     *   THE ONE PRESENTATION SWITCH, AND WHY IT IS NOT A SECOND RULE.
     *   ``stripPrefix: false`` keeps the ``cloude_`` prefix on the
     *   fallback. The FALLBACK CHAIN is identical either way - label,
     *   else the tmux name, else null - and only how the tmux name is
     *   spelled changes. The attribution prompt is the one surface that
     *   passes it, because there the prefix is EVIDENCE: that card is
     *   asking "did you start this session?", one of its own hints is
     *   that the name matches the auto-generated form, and the user may
     *   need to match the exact string against their own ``tmux ls``.
     *   Every other surface strips it, because there it is an artefact
     *   of the launcher and not part of the name.
     * Inputs: row (object|null) - reads ``label`` and ``name``. Either
     *   may be absent, empty or the wrong type. options (object|null) -
     *   ``{stripPrefix: false}`` to keep the ``cloude_`` prefix on the
     *   fallback; stripping is the default.
     * Output: string|null - see outcomes 1-3 in the module header. Never
     *   an empty string.
     * Example: SessionLabel.resolve({name: 'cloude_Media',
     *            label: 'Media Compression'})  -> 'Media Compression'
     */
    function resolve(row, options) {
        if (!row || typeof row !== 'object') return null;
        var label = cleanString(row.label);
        if (label !== null) return label;
        var strip = !(options && options.stripPrefix === false);
        return strip ? stripAppPrefix(row.name) : cleanString(row.name);
    }

    /**
     * The same rule, for a toast's field names.
     *
     * Description: a toast carries ``session_label`` / ``session_name``
     * rather than ``label`` / ``name``, because a toast already has a
     * ``title`` of its own and the two would collide. That is a SHAPE
     * difference, not a policy difference, so it is normalised into
     * :func:`resolve` rather than given a second copy of the chain.
     * Inputs: toast (object|null) - a server-shape toast.
     * Output: string|null - same three outcomes as :func:`resolve`.
     * Example: SessionLabel.resolveToast({session_name: 'cloude_a'}) -> 'a'
     */
    function resolveToast(toast) {
        if (!toast || typeof toast !== 'object') return null;
        return resolve({ label: toast.session_label, name: toast.session_name });
    }

    /**
     * What a rename editor should OPEN ON, read off the element that is
     * rendering the name.
     *
     * Description: all three rename controls seed themselves from the
     * RENDERED display value rather than re-deriving one. That is the
     * whole point: a seed computed a second time can disagree with what
     * the user is looking at, and when it did, a plain Enter overwrote a
     * user's label with a handle-derived string. Reading the element
     * means display and seed cannot disagree by construction - if a
     * surface's display half ever regresses, its seed regresses with it
     * and one test catches both.
     *
     * WHY `dataset.fullTitle` COMES FIRST. The in-page header
     * (`#header-title-text`) is MIDDLE-ELIDED by
     * client/js/header-title-fit.js: its `textContent` is a truncated
     * string with an ellipsis in it, and the fitter keeps the full value
     * in `dataset.fullTitle`, re-eliding from it on every resize. So on
     * that one surface the element holds its display value in two
     * places, and `textContent` is the WRONG one - seeding from it would
     * put "client: acme...$rate" in the box and store that truncation
     * over the real label on a plain Enter. That is the same silent data
     * loss this rule exists to remove, so the rule has to know about it.
     *
     * An element that does not elide carries no `fullTitle` and falls
     * through to its text, so this is one rule and not a special case.
     * Inputs: el (Element|null) - the element rendering the name.
     * Output: string - the seed, trimmed. Empty string when there is
     *   nothing to seed from, which a caller must treat as "do not open
     *   an editor" rather than as an empty starting value.
     * Example: SessionLabel.seedFromElement(titleEl)  -> 'Media Compression'
     */
    function seedFromElement(el) {
        if (!el) return '';
        var full = el.dataset ? el.dataset.fullTitle : null;
        var value = (typeof full === 'string' && full.length)
            ? full
            : (el.textContent || '');
        return String(value).trim();
    }

    /**
     * The server's label rule, mirrored so an obviously bad label is
     * refused without a round trip. THE SERVER REMAINS AUTHORITATIVE -
     * this is an early out, never the decision.
     *
     * WHAT IT DELIBERATELY DOES NOT REFUSE. The rename controls used to
     * enforce ``^[A-Za-z0-9_-]{1,64}$``, the old TMUX NAME rule, against
     * a field that is no longer a tmux name. That regex refused "Media
     * Compression" - the feature's own worked example - before it could
     * reach a server that would have accepted it happily. Spaces, ``:``,
     * ``.``, quotes, ``$`` and non-ASCII are all legal in a label,
     * because a label is never handed to tmux. Only the server's own two
     * refusals are mirrored: empty, and control characters.
     *
     * Inputs: raw (string|null) - the value from an edit control.
     * Output: object - ``{ok: true, value: string}`` with surrounding
     *   whitespace stripped, or ``{ok: false, reason: string}`` carrying
     *   a sentence fit to show the user.
     * Example: SessionLabel.validate(' Media Compression ')
     *   // {ok: true, value: 'Media Compression'}
     */
    function validate(raw) {
        var cleaned = cleanString(raw);
        if (cleaned === null) {
            return { ok: false, reason: 'a session label cannot be empty' };
        }
        if (cleaned.length > LABEL_MAX_CHARS) {
            return {
                ok: false,
                reason: 'a session label may be at most '
                    + LABEL_MAX_CHARS + ' characters',
            };
        }
        if (CONTROL_CHARS.test(cleaned)) {
            return {
                ok: false,
                reason: 'a session label cannot contain control characters'
                    + ' such as a newline or a tab',
            };
        }
        return { ok: true, value: cleaned };
    }

    window.SessionLabel = {
        APP_TMUX_PREFIX: APP_TMUX_PREFIX,
        LABEL_MAX_CHARS: LABEL_MAX_CHARS,
        UNKNOWN: UNKNOWN,
        stripAppPrefix: stripAppPrefix,
        resolve: resolve,
        resolveToast: resolveToast,
        seedFromElement: seedFromElement,
        validate: validate,
    };
})();
