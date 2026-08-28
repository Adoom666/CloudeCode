/**
 * SlashFavorites - the star affordance that decides which slash commands
 * get a chip.
 *
 * WHAT CHANGED. `common_slash_commands` in config.json used to be a
 * HAND-PICKED list: whoever edited the file chose the chip row, and a
 * user who wanted a different set had to edit JSON. The same key is now
 * written by a star the user toggles on any command in the palette. Same
 * key, same two entry forms, same migration chain (see
 * src/core/slash_favorites.py) - only the author changed.
 *
 * THREE STATES, NOT TWO. A user who has never starred anything gets the
 * built-in defaults, and the row SAYS they are defaults rather than
 * implying they were chosen. A user who starred and then unstarred
 * everything gets an empty row with an empty state, not the defaults
 * re-seeded on top of a choice they just made. The server tells the two
 * apart by key presence in config.json and reports it as `defaulted`.
 *
 * WHY THIS IS ITS OWN FILE. slash-commands.js is already past the repo's
 * 500-line budget; every line of star markup, star wiring and chip
 * rendering lives here so that file does not grow further.
 *
 * THE STAR IS NOT THE `more` CONTROL. Those two sit on the same row and
 * had to stay unmistakable:
 *   - `more` is a WORD, in normal flow, at the row's bottom-left;
 *   - the star is an ICON, absolutely positioned at the row's top-right.
 * Different shape, different place, different reading order. Both stop
 * propagation, because the row itself selects the command on click, so
 * asking to read a description or to star something would otherwise run
 * it and close the modal.
 *
 * TOUCH. The star declares its own 44px box and explicitly re-declares
 * width/height/border-radius/padding/display, because the bare
 * `button { width: 36px; height: 36px; border-radius: 50% }` element rule
 * in styles.css exists for the round header icons and reaches every
 * button that does not opt out. Nine user-visible bugs have come from
 * that rule; the CSS carries the same warning.
 *
 * Must load AFTER api.js and BEFORE slash-commands.js.
 */

console.log('[SlashFavorites Module] Loading...');

(function () {
    'use strict';

    /**
     * The two star glyphs. Filled = starred, outline = not. Colour comes
     * from `currentColor` so a theme never has to know about this file.
     * Inputs: on (boolean) - whether the command is currently starred.
     * Output: string - inline SVG markup.
     */
    function starIcon(on) {
        var d = 'M12 3.6l2.5 5.06 5.58.81-4.04 3.94.95 5.56L12 16.35l-4.99 2.62.95-5.56L3.92 9.47l5.58-.81z';
        return (
            '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">' +
            '<path d="' + d + '" fill="' + (on ? 'currentColor' : 'none') + '"' +
            ' stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
            '</svg>'
        );
    }

    /**
     * Escape a string for safe interpolation into innerHTML-built markup.
     * Description: command names and descriptions come from scraped docs
     *   and user-authored files, both untrusted for innerHTML purposes.
     * Inputs: str (any) - value to escape; stringified first.
     * Output: string - HTML-escaped text.
     */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    /**
     * Build the star toggle for one command.
     *
     * Description: a real `<button>` with `aria-pressed`, so a screen
     *   reader announces the state rather than just the icon, and the
     *   label names the ACTION the press performs.
     * Inputs:
     *   command (string) - e.g. "/clear".
     *   on (boolean) - whether it is currently starred.
     * Output: string - HTML for one star button.
     * Example: starButton('/clear', true)
     */
    function starButton(command, on) {
        var safe = escapeHtml(command);
        return (
            '<button type="button" class="command-star' + (on ? ' command-star--on' : '') + '"' +
            ' data-fav-toggle="' + safe + '"' +
            ' data-fav-state="' + (on ? '1' : '0') + '"' +
            ' aria-pressed="' + (on ? 'true' : 'false') + '"' +
            ' title="' + (on ? 'remove from favorites' : 'add to favorites') + '"' +
            ' aria-label="' + (on ? 'remove ' : 'add ') + safe + (on ? ' from favorites' : ' to favorites') + '">' +
            starIcon(on) +
            '</button>'
        );
    }

    /**
     * Render the chip row from the user's favorites.
     *
     * Description: each chip is a command button plus its own star, so the
     *   row is self-editing - unstarring is done where the chip is, not by
     *   hunting the command down in the list below. The empty and
     *   defaulted states are DISTINCT and both say what they are; an
     *   empty row with no explanation reads as a failed fetch.
     * Inputs:
     *   details (Array<{command, description}>) - the favorites, in order.
     *   defaulted (boolean) - true when the user has never starred
     *     anything and these are the built-in defaults.
     *   longDescriptionFor (function(string): string) - resolves a
     *     command to its full description for the hover tooltip.
     * Output: string - HTML for the chip grid.
     */
    function renderChips(details, defaulted, longDescriptionFor) {
        var list = details || [];
        if (list.length === 0) {
            return (
                '<div class="common-commands-empty">' +
                'no favorites. tap a star below to add one.' +
                '</div>'
            );
        }
        var note = defaulted
            ? '<div class="common-commands-note">defaults. star any command below to make this row yours.</div>'
            : '';
        var chips = list.map(function (entry) {
            var cmd = entry.command;
            var short = entry.description || '';
            var long = (longDescriptionFor ? longDescriptionFor(cmd) : '') || short;
            var shortHtml = short
                ? '<span class="command-button-desc">' + escapeHtml(short) + '</span>'
                : '';
            return (
                '<div class="command-chip">' +
                '<button class="command-button" data-command="' + escapeHtml(cmd) + '"' +
                ' title="' + escapeHtml(long) + '">' +
                '<span class="command-button-name">' + escapeHtml(cmd) + '</span>' +
                shortHtml +
                '</button>' +
                starButton(cmd, true) +
                '</div>'
            );
        }).join('');
        return note + '<div class="common-commands-chips">' + chips + '</div>';
    }

    /**
     * Wire every star under `root` to the toggle endpoint.
     *
     * Description: reads the desired state off the button rather than
     *   flipping whatever the server has, so a stale render cannot invert
     *   the user's intent (the request carries `favorite`, not "toggle").
     *   On success the caller repaints from the AUTHORITATIVE post-write
     *   payload the server returns; on failure the star is left exactly
     *   as it was and the error is surfaced, rather than the UI showing a
     *   star that did not persist.
     * Inputs:
     *   root (Element) - container holding `[data-fav-toggle]` buttons.
     *   onChange (function(object): void) - called with the post-write
     *     `{commands, command_details, defaulted}` payload.
     * Output: void.
     */
    function wire(root, onChange) {
        if (!root) return;
        root.querySelectorAll('[data-fav-toggle]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                // The chip and the list row both select the command on
                // click; without this, starring would run it.
                e.stopPropagation();
                e.preventDefault();
                if (btn.disabled) return;
                var command = btn.getAttribute('data-fav-toggle');
                var wanted = btn.getAttribute('data-fav-state') !== '1';
                btn.disabled = true;
                window.API.toggleFavoriteCommand(command, wanted).then(function (payload) {
                    if (onChange) onChange(payload);
                }).catch(function (err) {
                    console.error('[SlashFavorites] toggle failed', err);
                    btn.disabled = false;
                    window.alert(
                        'could not update favorites: ' +
                        (err && err.message ? err.message : 'unknown error')
                    );
                });
            });
        });
    }

    /**
     * Build a fast lookup of which commands are currently starred.
     * Inputs: details (Array<{command}>) - the favorites.
     * Output: Set<string> - starred command strings.
     */
    function favoriteSet(details) {
        var set = new Set();
        (details || []).forEach(function (d) {
            if (d && d.command) set.add(d.command);
        });
        return set;
    }

    /**
     * Repaint the two star-bearing regions of the open modal after a
     * favorites write.
     *
     * Description: called with the AUTHORITATIVE post-write payload
     *   already folded into the caller's state, so the UI can never end
     *   up claiming a star that is not on disk. Only the chip grid is
     *   rebuilt; the list rows SURVIVE and just have their star swapped,
     *   because the live filter (slash-command-filter.js) holds direct
     *   references to those `.command-item` elements and replacing them
     *   would silently break search until the modal was reopened.
     * Inputs:
     *   modal (Element|null) - the slash-commands modal, or null when it
     *     has not been created yet (a no-op).
     *   opts (object):
     *     chipHtml (function(): string) - renders the new chip grid.
     *     details (Array<{command}>) - the new favorites.
     *     onSelect (function(string): void) - chip click handler.
     *     onChange (function(object): void) - next payload handler.
     * Output: void.
     */
    function repaint(modal, opts) {
        if (!modal) return;

        var grid = modal.querySelector('#common-commands-grid');
        if (grid) {
            grid.innerHTML = opts.chipHtml();
            grid.querySelectorAll('.command-button').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    opts.onSelect(btn.dataset.command);
                });
            });
            wire(grid, opts.onChange);
        }

        var starred = favoriteSet(opts.details);
        modal.querySelectorAll('.command-item').forEach(function (item) {
            var old = item.querySelector('.command-star');
            if (!old) return;
            var command = item.dataset.command;
            var holder = document.createElement('div');
            holder.innerHTML = starButton(command, starred.has(command));
            old.replaceWith(holder.firstElementChild);
            wire(item, opts.onChange);
        });
    }

    window.SlashFavorites = {
        starButton: starButton,
        repaint: repaint,
        renderChips: renderChips,
        wire: wire,
        favoriteSet: favoriteSet,
    };
})();

console.log('[SlashFavorites Module] Exported as window.SlashFavorites');
