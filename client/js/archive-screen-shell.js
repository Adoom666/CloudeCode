/**
 * The archive screen's SHELL - the DOM the composition root mounts into.
 *
 * WHY IT IS SEPARATE. `archive-screen.js` is the composition root and it
 * is the file this repo's 500-line cap bites hardest, because every new
 * capability on this screen adds a wire to it. Everything here is pure
 * DOM CONSTRUCTION: it makes elements, it decides nothing, it reads no
 * outcome and it holds no state beyond the memo that makes it
 * idempotent. That is the seam, and it is the same one
 * archive-screen-tools.js sits on.
 *
 * THE SHELL IS BUILT AT SCRIPT LOAD, NOT INSIDE `show()`. `App.showArchive()`
 * calls `_placeStatusLight('archive')` and `GlobalAudioToggle.place('archive')`
 * BEFORE `show()`, and both re-parent into `#archive-bar-status`, part of
 * this shell. Building it lazily would mean that on the FIRST navigation
 * the target does not exist, both calls silently no-op (they tolerate a
 * missing target, correctly), and the status light and audio button are
 * absent with no error anywhere - a silent half-wired screen. The caller
 * invokes this at its own script load; the ordering requirement lives
 * there, in index.html, where the script tags are.
 *
 * Exports window.ArchiveScreenShell.
 */

console.log('[ArchiveScreenShell Module] Loading...');

(function () {
    'use strict';

    /** Memoised shell, so build() is idempotent. @type {?object} */
    var shell = null;

    /** Create an element. Inputs: tag, cls|null, text|null. Output: Element. */
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== null && text !== undefined) n.textContent = text;
        return n;
    }

    /** Apply attributes to a node and return it, so a control and what
     *  makes it one read as one expression. Inputs: node, map. Output:
     *  node. Example: attrs(el('button', 'x', 'Go'), {type: 'button'}) */
    function attrs(node, map) {
        var names = Object.keys(map || {});
        for (var i = 0; i < names.length; i++) {
            node.setAttribute(names[i], map[names[i]]);
        }
        return node;
    }

    /** Build the screen shell exactly once, idempotent. Output:
     *  object|null - null when #archive-screen is absent, reported not
     *  swallowed: it means index.html and this module disagree. */
    /** @type {string} */
    var SCREEN_ID = 'archive-screen';
    /** @type {string} */
    var ROOT_CLASS = 'archive-screen';

    function buildShell() {
        if (shell) return shell;
        var root = document.getElementById(SCREEN_ID);
        if (!root) {
            console.error('ArchiveScreen: #' + SCREEN_ID + ' is missing from ' +
                'index.html. The screen cannot mount.');
            return null;
        }
        root.textContent = '';

        var crumb = attrs(el('nav', ROOT_CLASS + '__crumb', null),
                          { 'aria-label': 'Archive location' });
        var back = attrs(el('button', ROOT_CLASS + '__back', 'Back'),
                         { type: 'button', 'data-action': 'back-pane' });

        var grid = el('div', ROOT_CLASS + '__grid', null);
        var navPane = el('div', ROOT_CLASS + '__pane ' + ROOT_CLASS + '__pane--nav', null);
        var listPane = el('div', ROOT_CLASS + '__pane ' + ROOT_CLASS + '__pane--list', null);
        var readPane = el('div', ROOT_CLASS + '__pane ' + ROOT_CLASS + '__pane--reader', null);
        grid.appendChild(navPane);
        grid.appendChild(listPane);
        grid.appendChild(readPane);

        // `#archive-bar-status` is the re-parent target for
        // App._placeStatusLight('archive') and GlobalAudioToggle.place();
        // the *-text span mirrors the home and terminal bars so
        // _syncStatusLabel() has a target.
        var bar = el('div', ROOT_CLASS + '__bar', null);
        var status = el('span', ROOT_CLASS + '__status', null);
        status.id = 'archive-bar-status';
        var statusText = el('span', ROOT_CLASS + '__status-text', null);
        statusText.id = 'archive-bar-status-text';
        status.appendChild(statusText);
        bar.appendChild(status);

        root.appendChild(back);
        root.appendChild(crumb);
        root.appendChild(grid);
        root.appendChild(bar);

        shell = { root: root, crumb: crumb, back: back, grid: grid,
                  navPane: navPane, listPane: listPane, readPane: readPane,
                  // The two draggable dividers. Built with the shell, not
                  // in show(), for the same reason the shell is: they are
                  // part of the grid's structure, and a lazily-built
                  // handle would be absent on the first navigation with
                  // nothing anywhere reporting it.
                  panes: window.ArchivePaneResize.create({
                      document: document, grid: grid, rootClass: ROOT_CLASS
                  }) };
        return shell;
    }

    /** Description: the built shell, or null before build(). Output:
     *  object|null. */
    function current() { return shell; }

    window.ArchiveScreenShell = { build: buildShell, shell: current };
    console.log('[ArchiveScreenShell Module] Exported as window.ArchiveScreenShell');
})();
