/**
 * File editor - which roots the picker browses, and WHY one is missing.
 *
 * Extracted from config-editor-panel.js so the decision "does this root
 * get listed, and if not what does the user get told instead" is a pure
 * function with no DOM, testable directly
 * (tests/test_config_editor_roots.node.mjs).
 *
 * THE BUG THIS MODULE EXISTS TO KILL. The panel used to skip the two
 * project roots with a bare `continue` whenever the working directory
 * could not be resolved. The result was a tree that rendered ~/.claude
 * alone, looked complete, and said nothing - a short tree is
 * indistinguishable from a correct one, so the user reads it as "my
 * project files are gone". Absence must be NARRATED: three outcomes
 * (listed / genuinely absent / could not evaluate), never two.
 *
 * TWO OUTCOMES LOOK ALIKE BUT ARE NOT. "genuinely absent" splits again in
 * practice, and only one half deserves a sentence. A project with no
 * .claude/ subfolder, or a workdir the server lists zero entries for, is a
 * MEASURED absence - the server looked and there is nothing there, which
 * needs no announcement any more than a directory picker narrates every
 * folder it doesn't show a badge on (see _buildRootEl in
 * config-editor-panel.js: these render no row and no message at all). A
 * workdir the server reports as flatly UNAVAILABLE (its own directory
 * vanished out from under an attached session) is different in kind - the
 * server could not even look - and that one IS a could-not-evaluate case
 * and does get a named notice (workdirUnavailableNotice below).
 *
 * SCOPING. Everything below lives inside an IIFE and reaches the app only
 * as `window.ConfigEditorRoots`. These are classic <script> tags sharing
 * ONE global scope, so a top-level `const CONFIG_EDITOR_ROOTS` here
 * collides with the identically named binding in config-editor-panel.js:
 * the second declaration is an uncaught SyntaxError that kills the whole
 * panel file at parse time, leaving window.ConfigEditorPanel undefined and
 * the editor button dead. Do not lift anything out of this closure.
 */

(function () {
'use strict';

/**
 * Single source of truth for the three roots the panel browses: API root
 * id, display label, and default expand/collapse state. "user" and
 * "project" default OPEN (small, config-focused, the reason this feature
 * exists); "workdir" defaults CLOSED (a real project directory can be
 * arbitrarily large - same reasoning as plugins/ being force-collapsed,
 * just a per-user-choice default rather than a forced one, since a small
 * project's workdir is perfectly reasonable to browse open).
 * @type {Array<{id: string, label: string, defaultExpanded: boolean}>}
 */
const CONFIG_EDITOR_ROOTS = [
    { id: 'user', label: '~/.claude', defaultExpanded: true },
    { id: 'project', label: 'project .claude', defaultExpanded: true },
    { id: 'workdir', label: 'project files', defaultExpanded: false },
];

/**
 * Resolve the active project's working directory, and - when it cannot be
 * resolved - WHY. Reads TerminalController's current session rather than
 * tracking a copy: single source of truth for "what session is attached
 * right now".
 *
 * THE LAUNCHER BUG THIS GUARDS AGAINST. `_currentSession` is bookkeeping,
 * not a live-attachment flag: detachSession() (terminal.js) and every
 * other path back to the launchpad set `sessionActive = false` but never
 * null out `_currentSession` - it is stashed for other modules to
 * introspect (launchpad self-adopt filter, debug) and is deliberately
 * left in place so those readers can see "what was I last attached to".
 * Reading `_currentSession` alone therefore reports the LAST session, not
 * the CURRENT one: back on the launcher with no project open, this
 * function would still resolve a stale working_dir and the sidebar would
 * show a real path the user is nowhere near - which is exactly the report
 * that opened this fix ("project .claude: not present in
 * /Users/.../scrolltest" shown while on the launcher, not in a project).
 * `sessionActive` is the authoritative "is a session attached right now"
 * flag; both must be true for a project context to exist.
 *
 * The session object is a WRAPPER on some paths (SessionInfo, from
 * `GET /sessions/{id}` via the launchpad rejoin and the deep-link router)
 * and a bare Session on others (the create and adopt responses). The real
 * fields live at `wrapper.session.*` in the first case, which is the same
 * unwrap TerminalController._unwrapSession performs.
 *
 * @param {object|null|undefined} terminalController  window.TerminalController.
 * @returns {{path: string|null, reason: 'ok'|'no-session'|'no-working-dir'}}
 *   `path` is the absolute working directory when reason is 'ok', else null.
 */
function resolveProjectContext(terminalController) {
    const tc = terminalController;
    if (!tc || !tc.sessionActive || !tc._currentSession) return { path: null, reason: 'no-session' };
    const s = tc._currentSession;
    const unwrapped = (s.session && typeof s.session === 'object') ? s.session : s;
    const workingDir = unwrapped && unwrapped.working_dir;
    if (!workingDir) return { path: null, reason: 'no-working-dir' };
    return { path: workingDir, reason: 'ok' };
}

/**
 * The user-facing sentence for a project context that did not resolve -
 * for the ONE reason that is actually anomalous rather than expected.
 *
 * 'no-session' is the ordinary state of the launcher: the user has no
 * project open, on the screen whose entire point is not being in one.
 * That needs no explanation any more than a missing .claude/ subfolder
 * does (see _buildRootEl in config-editor-panel.js) - it is a MEASURED
 * absence, not a failure. Narrating it was itself the original complaint
 * this fix answers ("on the launcher screen ... but I'm not even in a
 * project"): even a CORRECTLY-scoped "no session attached, open a
 * session to browse project files" sentence is project talk on a screen
 * that has no project, which is exactly the noise the user asked to be
 * rid of. So 'no-session' returns null now - silent, like ~/.claude
 * simply being the only thing there is to show.
 *
 * 'no-working-dir' is different in kind: a session IS attached (the user
 * really is in a project) and it reports no working directory anyway,
 * which should never happen in ordinary use - that is could-not-evaluate,
 * per THE THREE-OUTCOME RULE, and stays narrated.
 * @param {'ok'|'no-session'|'no-working-dir'} reason  From resolveProjectContext.
 * @returns {string|null}  Null when reason is 'ok' or 'no-session' -
 *   nothing to explain in either case.
 */
function projectRootsNotice(reason) {
    if (reason === 'no-working-dir') {
        return 'the attached session reports no working directory, so '
            + 'project .claude and project files cannot be listed.';
    }
    return null;
}

/**
 * The user-facing sentence for the "workdir" root when the server says it
 * cannot be reached (HTTP 400 - resolve_roots() only registers "workdir"
 * when project_path resolves to a real, existing directory). This is NOT
 * the ordinary "no .claude/ subfolder" case: a session's working
 * directory exists by construction when the session starts, so its
 * absence now means the directory was deleted, unmounted, or renamed out
 * from under a still-attached session. THE THREE-OUTCOME RULE: that is
 * "could not evaluate", not a measured absence, so it must be named
 * rather than silently dropped the way an absent "project .claude" is
 * (see planRoots/_buildRootEl - a missing .claude/ needs no announcement
 * at all, this does).
 * @param {string} label  The root's display label ("project files").
 * @param {string} projectPath  Absolute working directory the session
 *   reported.
 * @returns {string}  One lowercase sentence.
 */
function workdirUnavailableNotice(label, projectPath) {
    const where = projectPath || 'the working directory';
    return `${label}: could not reach ${where} - it may have been moved, `
        + 'deleted, or unmounted.';
}

/**
 * The user-facing sentence for ONE directory node whose own contents
 * could not be enumerated (server set TreeNode.list_error - see
 * config_files.py's THREE-OUTCOME RULE handling of OSError from
 * iterdir()). Distinct from a whole-root notice: this is a node partway
 * INSIDE an otherwise-successful tree, not a whole root. Kept as a pure
 * function (like every other notice-builder in this module) so the exact
 * wording is asserted in tests/test_config_editor_roots.node.mjs rather
 * than only visible by opening the picker. See config-editor-panel.js's
 * _buildRootEl for the whole-root equivalent: a project with no .claude/
 * subfolder, or a root the server reports zero entries for, renders NO
 * notice at all - that is a measured absence, not a failure to evaluate.
 * @param {string} listError  The server-supplied reason (e.g. a
 *   Permission denied strerror), never null/undefined when called - the
 *   caller only invokes this when node.list_error is truthy.
 * @returns {string}  One lowercase sentence, never conflated with "this
 *   directory is empty".
 */
function listErrorNotice(listError) {
    return `could not list contents: ${listError}`;
}

/**
 * Turn a project context into the ordered list of things the tree must
 * render. Every root is either fetched or explained - nothing is dropped
 * silently, which is the whole point of this function existing.
 * @param {{path: string|null, reason: string}} context  From resolveProjectContext.
 * @returns {Array<{kind: 'root', def: object} | {kind: 'notice', message: string}>}
 */
function planRoots(context) {
    const plan = [];
    const resolved = !!(context && context.path);
    for (const def of CONFIG_EDITOR_ROOTS) {
        if (def.id === 'user' || resolved) {
            plan.push({ kind: 'root', def });
        }
    }
    if (!resolved) {
        const message = projectRootsNotice(context && context.reason);
        if (message) plan.push({ kind: 'notice', message });
    }
    return plan;
}

window.ConfigEditorRoots = {
    ROOTS: CONFIG_EDITOR_ROOTS,
    resolveProjectContext,
    projectRootsNotice,
    listErrorNotice,
    workdirUnavailableNotice,
    planRoots,
};
console.log('[ConfigEditorRoots Module] Exported as window.ConfigEditorRoots');

}());
