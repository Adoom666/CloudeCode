/**
 * Session detail - where a session says HOW it became yours, and which
 * project it belongs to.
 * ----------------------------------------------------------------------
 * WHY THIS SURFACE EXISTS. The session ROW badges ownership with two
 * values, not three: `created` and `adopted` both render as OURS, and
 * `observed` is the only external one (design 4.6, decided 2026-08-17).
 * That is the right call for a list - a third badge on every row buys
 * nothing - but the distinction is not thrown away, it is moved here.
 * A user who wants to know whether he started a session or picked it up
 * can ask, and get a straight answer.
 *
 * THREE-OUTCOME RENDERING, AND IT IS THE WHOLE POINT OF THE FILE.
 * Every field below has a state that means "we could not tell", and
 * none of them is allowed to render as a plausible-looking value:
 *
 *   ORIGIN      an unrecognised or missing `origin` renders
 *               "unknown - how this session started was not recorded".
 *               NEVER "started here", which is what a `||` default
 *               would have produced for exactly the rows we know least
 *               about.
 *   PROJECT     `none` and `unknown` are DIFFERENT LINES OF TEXT.
 *               `none` says the session belongs to no project - a
 *               complete answer. `unknown` says the working directory
 *               could not be determined - not an answer, and the row
 *               belongs in NEEDS ATTENTION. Rendering them with the
 *               same words would put a measurement and a failure behind
 *               one string, which is the defect this whole build step
 *               exists to remove.
 *   CLAIMED AT  absent means never adopted, which is a fact about a
 *               `created` session and says nothing about an `observed`
 *               one.
 *
 * This module renders an HTML string and returns it. It owns no state,
 * polls nothing and binds no events, so it can be asserted against
 * RENDERED TEXT in a test rather than against a state object. This repo
 * shipped a feature with 282 green state assertions that painted zero
 * pixels; the tests for this file read the string it produces.
 */

console.log('[SessionDetail Module] Loading...');

(function () {
    'use strict';

    /**
     * How a session came to be, in the user's words rather than the
     * column's. Keys are `sessions.origin` values; anything not here is
     * rendered as the explicit unknown, never as a default.
     */
    var ORIGIN_LABELS = {
        created: 'Started by Cloude Code',
        adopted: 'Adopted from tmux',
        observed: 'External - started outside Cloude Code'
    };

    /**
     * The second line under ORIGIN. Says what the value MEANS for
     * ownership, because "adopted" on its own does not tell a user
     * whether the session is his.
     */
    var ORIGIN_DETAILS = {
        created: 'This app created the tmux session.',
        adopted: 'Started outside this app, then claimed. It is yours for good.',
        observed: 'Seen on this socket and never claimed.'
    };

    /**
     * Project attribution, rendered so that "belongs to nothing" and
     * "could not tell" can never read as the same sentence.
     */
    var ATTRIBUTION_LABELS = {
        explicit: 'Set explicitly',
        derived_deepest: 'Matched by working directory',
        none: 'No project',
        unknown: 'Could not determine'
    };

    var ATTRIBUTION_DETAILS = {
        explicit: 'This session was assigned to its project directly.',
        derived_deepest:
            'The working directory sits inside this project root.',
        none:
            'The working directory was read and it is not inside any known project.',
        unknown:
            'The working directory could not be read, so no project could be matched.'
    };

    /**
     * Escape text for interpolation into an HTML string.
     * @param {*} value  Any value; coerced to string.
     * @returns {string} HTML-safe text.
     */
    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Whether an origin value badges the session as OURS.
     *
     * Mirrors src/core/session_store.is_owned_origin, which is the
     * server-side single spelling of the same test. Both `created` and
     * `adopted` are ours; `observed` is the only external value. An
     * unrecognised value is NOT ours - a badge we cannot justify is a
     * claim we have no evidence for.
     *
     * @param {string} origin  A `sessions.origin` value.
     * @returns {boolean} True when the session is ours.
     */
    function isOwnedOrigin(origin) {
        return origin === 'created' || origin === 'adopted';
    }

    /**
     * Render the ORIGIN block: how this session became what it is.
     *
     * @param {object} record  A SessionRecord from GET /sessions/records.
     * @returns {string} HTML for one detail field.
     */
    function renderOrigin(record) {
        var origin = record && record.origin;
        var known = Object.prototype.hasOwnProperty.call(
            ORIGIN_LABELS, origin);
        // An unrecognised or absent origin is stated as unknown. It is
        // never defaulted to 'created', which is the value a `||` would
        // have produced and the one a user would most readily believe.
        var label = known
            ? ORIGIN_LABELS[origin]
            : 'Unknown';
        var detail = known
            ? ORIGIN_DETAILS[origin]
            : 'How this session started was not recorded.';
        var owned = known && isOwnedOrigin(origin);
        var ownership = known
            ? (owned ? 'Yours' : 'Not yours')
            : 'Ownership cannot be determined';
        return '' +
            '<div class="session-detail-field" data-field="origin"' +
            ' data-origin="' + escapeHtml(known ? origin : 'unrecognised') + '">' +
            '<span class="session-detail-label">Origin</span>' +
            '<span class="session-detail-value">' + escapeHtml(label) + '</span>' +
            '<span class="session-detail-note">' + escapeHtml(detail) + '</span>' +
            '<span class="session-detail-ownership">' +
            escapeHtml(ownership) + '</span>' +
            '</div>';
    }

    /**
     * Render the CLAIMED AT block, or say plainly that it was never
     * claimed.
     *
     * `adopted_at` is first-write-wins on the server, so this is the
     * moment of the FIRST claim and does not move when the UI re-opens
     * a session through the adopt path - which it does routinely.
     *
     * @param {object} record  A SessionRecord.
     * @returns {string} HTML for one detail field.
     */
    function renderAdoptedAt(record) {
        var at = record && record.adopted_at;
        var value = at ? String(at) : 'Never adopted';
        var note = at
            ? 'The moment it was first claimed. This never moves.'
            : 'This session was not claimed through adoption.';
        return '' +
            '<div class="session-detail-field" data-field="adopted-at">' +
            '<span class="session-detail-label">Claimed</span>' +
            '<span class="session-detail-value">' + escapeHtml(value) + '</span>' +
            '<span class="session-detail-note">' + escapeHtml(note) + '</span>' +
            '</div>';
    }

    /**
     * Render the PROJECT block, keeping "no project" and "could not
     * determine" as visibly different answers.
     *
     * @param {object} record  A SessionRecord.
     * @param {string|null} projectName  Resolved display name, or null.
     * @returns {string} HTML for one detail field.
     */
    function renderProject(record, projectName) {
        var attribution = (record && record.project_attribution) || 'unknown';
        var known = Object.prototype.hasOwnProperty.call(
            ATTRIBUTION_LABELS, attribution);
        var label = known ? ATTRIBUTION_LABELS[attribution] : 'Could not determine';
        var note = known
            ? ATTRIBUTION_DETAILS[attribution]
            : ATTRIBUTION_DETAILS.unknown;
        // The project NAME is only shown when a project was actually
        // matched. Showing a name beside "No project" or beside "Could
        // not determine" would attach the session to a project nobody
        // matched it to.
        var name = (attribution === 'explicit' || attribution === 'derived_deepest')
            ? (projectName || 'Unnamed project')
            : label;
        return '' +
            '<div class="session-detail-field" data-field="project"' +
            ' data-attribution="' + escapeHtml(known ? attribution : 'unknown') + '">' +
            '<span class="session-detail-label">Project</span>' +
            '<span class="session-detail-value">' + escapeHtml(name) + '</span>' +
            '<span class="session-detail-note">' + escapeHtml(note) + '</span>' +
            '</div>';
    }

    /**
     * Render the WORKING DIRECTORY block.
     *
     * The path is shown EXACTLY as it was probed. It is never expanded,
     * shortened or resolved for display: a symlinked path is the path
     * the user chose, and rewriting it on screen would make the row
     * disagree with what he typed.
     *
     * @param {object} record  A SessionRecord.
     * @returns {string} HTML for one detail field.
     */
    function renderWorkingDir(record) {
        var dir = record && record.working_dir;
        return '' +
            '<div class="session-detail-field" data-field="working-dir">' +
            '<span class="session-detail-label">Working directory</span>' +
            '<span class="session-detail-value">' +
            escapeHtml(dir ? dir : 'Could not determine') + '</span>' +
            '</div>';
    }

    /**
     * Render the whole session detail panel.
     *
     * @param {object} record  A SessionRecord from GET /sessions/records.
     * @param {object} [options]  `projectName` (string|null) - the
     *     resolved display name of `record.project_id`, when the caller
     *     has one.
     * @returns {string} HTML for the panel. Returns an explicit
     *     "no record" panel rather than an empty string when `record` is
     *     absent, because a blank panel and a missing session look
     *     identical on screen.
     */
    function render(record, options) {
        var opts = options || {};
        if (!record) {
            return '<div class="session-detail" data-state="no-record">' +
                '<span class="session-detail-note">' +
                'No stored record for this session.</span></div>';
        }
        return '<div class="session-detail" data-state="ok">' +
            renderOrigin(record) +
            renderAdoptedAt(record) +
            renderProject(record, opts.projectName || null) +
            renderWorkingDir(record) +
            '</div>';
    }

    window.SessionDetail = {
        render: render,
        renderOrigin: renderOrigin,
        renderProject: renderProject,
        isOwnedOrigin: isOwnedOrigin,
        ORIGIN_LABELS: ORIGIN_LABELS,
        ATTRIBUTION_LABELS: ATTRIBUTION_LABELS
    };
})();
