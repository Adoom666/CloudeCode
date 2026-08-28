/**
 * File-tree collapsed-state persistence for the file browser drawer.
 *
 * Split out of client/js/config-editor-panel.js, which had grown past this
 * project's 500-line ceiling. Cohesive on its own terms: it answers one
 * question - "should this directory node start collapsed?" - and owns the
 * one localStorage key that backs the answer. No DOM, no fetches.
 *
 * WHY ANY OF THIS EXISTS. The tree renders LAZILY: a directory's children
 * are not built into the DOM until it is first expanded. Against this
 * user's own ~/.claude that took the eager render from ~965 nodes to a
 * handful. A persisted "expanded" flag on a 1000+ file directory would
 * reintroduce the wall of nodes on the next open, which is why
 * FORCE_COLLAPSED_DIRS is not merely a default - it OVERRIDES whatever
 * was persisted.
 *
 * Storage follows the app's `cloude.*` convention (cloude.theme,
 * cloude.session.sidebar, cloude.configEditor.pinned) and its
 * never-throw treatment: a browser with storage denied loses persistence,
 * not the tree.
 *
 * Must load BEFORE config-editor-panel.js.
 */

console.log('[ConfigEditorTreeState Module] Loading...');

(function () {
    /**
     * localStorage key holding the whole "root:relPath" -> collapsed map.
     * @type {string}
     */
    const STORAGE_KEY = 'cloude.configEditor.collapsed';

    /**
     * Directories that are read-only in the tree (see config_files.py's
     * READONLY_COLLAPSED_DIRS) and always start collapsed, ignoring any
     * persisted choice. See the module docstring for why this beats the
     * persisted value rather than merely seeding it.
     * @type {Set<string>}
     */
    const FORCE_COLLAPSED_DIRS = new Set(['plugins']);

    /** @type {?Object<string, boolean>} Lazily-read state map. */
    let cache = null;

    /**
     * Read the persisted collapsed-state map, lazily.
     *
     * @returns {Object<string, boolean>} "root:relPath" -> collapsed.
     */
    function load() {
        if (cache) return cache;
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            cache = raw ? JSON.parse(raw) : {};
        } catch (err) {
            console.warn('ConfigEditorTreeState: failed to read collapsed-state:', err);
            cache = {};
        }
        return cache;
    }

    /**
     * Persist one node's collapsed flag.
     *
     * @param {string} key - "root:relPath", or "root:__root__" for a root.
     * @param {boolean} collapsed - The new state.
     * @returns {void}
     */
    function setNodeCollapsed(key, collapsed) {
        const state = load();
        state[key] = collapsed;
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (err) {
            console.warn('ConfigEditorTreeState: failed to persist collapsed-state:', err);
        }
    }

    /**
     * Resolve whether one directory (or root) node should start collapsed.
     *
     * Precedence: a forced-collapsed directory always wins; then a
     * persisted user choice; then `fallbackExpanded`, a root's own declared
     * default; then collapsed. The server's per-node `collapsed` hint is
     * deliberately NOT consulted - today it only flags read-only roots, and
     * "collapsed by default" is a client decision.
     *
     * @param {string} rootId - "user" | "project" | "workdir".
     * @param {{name: string, rel_path: string}} node - Server TreeNode, or a
     *   synthetic root node ({name, rel_path: '', is_dir: true}).
     * @param {boolean} [fallbackExpanded] - Default when nothing is
     *   persisted. Roots pass their own; directory nodes omit it and fall
     *   through to collapsed.
     * @returns {boolean} True when the node should render collapsed.
     */
    function startsCollapsed(rootId, node, fallbackExpanded) {
        if (FORCE_COLLAPSED_DIRS.has(node.name)) return true;
        const key = `${rootId}:${node.rel_path || '__root__'}`;
        const state = load();
        if (Object.prototype.hasOwnProperty.call(state, key)) return state[key];
        return fallbackExpanded === undefined ? true : !fallbackExpanded;
    }

    /**
     * True when a directory's collapsed state must never be persisted.
     *
     * @param {string} name - The directory's own name, not its path.
     * @returns {boolean} True for a forced-collapsed directory.
     */
    function isForcedCollapsed(name) {
        return FORCE_COLLAPSED_DIRS.has(name);
    }

    window.ConfigEditorTreeState = {
        load, setNodeCollapsed, startsCollapsed, isForcedCollapsed,
        STORAGE_KEY, FORCE_COLLAPSED_DIRS,
    };
    console.log('[ConfigEditorTreeState Module] Exported as window.ConfigEditorTreeState');
})();
