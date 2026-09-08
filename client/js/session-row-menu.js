/**
 * Session sidebar ROW OVERFLOW MENU - the kebab on a conversation row,
 * and the one menu behind it.
 * ----------------------------------------------------------------------
 * "on the left menu, the menu items. lets fold the icons a thin 3 dots
 * up and down sub menu, like in main sites top right. and right click on
 * item should open the same submenu"
 *
 * "main sites top right" is this app's own header overflow
 * (client/js/header-menu.js). The MARK is literally the same object -
 * both call client/js/kebab-icon.js - and the pattern is the same:
 * a real <button> carrying aria-haspopup/aria-expanded/aria-controls,
 * a role="menu" panel, Escape to close, focus returned to the trigger.
 * It is NOT the same INSTANCE, and it cannot be: the header folds two
 * singleton nodes addressed by id, while a sidebar row is destroyed and
 * rebuilt on every repaint, so re-parenting live nodes into a floating
 * panel would leave the menu holding detached elements.
 *
 * WHAT MOVED INTO THE MENU, and nothing was dropped or renamed. All
 * three of the row's old inline controls are now menu items built by THE
 * SAME BUILDERS that drew them: pin/unpin (SessionSidebarRows
 * .pinButtonHtml), mark unread (SessionStatusUI.markUnreadHtml) and
 * close/remove plus RESTART on a dead row (SessionRowActions.html, which
 * already emitted restart alongside remove). Each item therefore carries
 * the identical data attribute, aria state and glyph, and its LABEL is
 * that control's own `title` - no string is written twice.
 *
 * THE GROUP CHIP FOLDED IN TOO, A ROUND LATER. "no i dont need to see the
 * group name in the item. its in the group i can see the group on the
 * sidebar" - so the chip's DISPLAY half is simply gone, and its ACTION
 * half (opening the group picker) rides in here as one more menu item,
 * built by SessionSidebarGroupActions.rowMenuItemHtml, the module that
 * already owns the group-picking domain.
 *
 * WHAT STAYED ON THE ROW: the drag grip (a handle, not an action), the
 * status dot, the name, the theme swatch and the tmux/external badge -
 * none of them are actions, and none of them name a group any more.
 *
 * WHY THE PANEL IS MOUNTED ON document.body AND NOT IN THE ROW. This is
 * a measured constraint, not a preference: `.session-sidebar-panel`
 * carries `transform: translateX(-100%)` (client/css/session-sidebar.css)
 * for its open/close slide, and a transformed ancestor becomes the
 * containing block for `position: fixed` descendants. A fixed panel
 * rendered inside the row would therefore be positioned against the
 * sliding sidebar instead of the viewport, and would be clipped by the
 * list's own overflow. Mounting on the body is what makes the viewport
 * clamp in client/js/anchor-popover.js mean what it says.
 *
 * That choice costs two things, both handled: the list-scoped click
 * router in session-sidebar-clicks.js never sees these clicks, so
 * ``dispatch`` below routes them into the same exported handlers by
 * hand; and ``onRowActionClick`` walks up to `.session-sidebar-row` for
 * `data-active` and `data-session-id`, which from the body finds
 * nothing, so it gained a by-name lookup fallback.
 *
 * SCROLL CLOSES IT. A fixed panel over a scrolling list would otherwise
 * hang in space while the row it belongs to slid away.
 *
 * Load AFTER kebab-icon.js, session-status-ui.js, session-row-actions.js,
 * anchor-popover.js and session-sidebar-rows.js; BEFORE
 * session-row-menu-gestures.js.
 */

console.log('[SessionRowMenu Module] Loading...');

(function () {
    'use strict';

    /** The open panel element, or null. Only ever one, app-wide. */
    var panelEl = null;

    /** The kebab the open panel belongs to, so focus can go home. */
    var triggerEl = null;

    /** Document-level dismiss handlers, bound only while open. */
    var onDocPointer = null;
    var onDocKey = null;

    /** The list element the open panel is scrolling with, or null. */
    var scrollEl = null;

    /** Bound scroll/resize closer, so it can be removed again. */
    var onReflow = null;

    /** DOM contract. Read by the gestures module and by the tests. */
    var KEBAB_ATTR = 'data-row-menu';
    var KEBAB_CLASS = 'session-sidebar-row-kebab';
    var PANEL_ID = 'session-row-menu-panel';
    var PANEL_CLASS = 'session-row-menu';
    var ITEM_CLASS = 'session-row-menu__item';

    /**
     * Description: HTML-escape for an attribute, routed through
     *   SessionSidebarRows so this module owns no second escaper.
     * Inputs: value (any). Output: string.
     */
    function esc(value) {
        if (window.SessionSidebarRows) return window.SessionSidebarRows.esc(value);
        var div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML.replace(/"/g, '&quot;');
    }

    /**
     * Description: the row's kebab trigger. It carries the state the menu
     *   is built from, so the menu is always painted from the same
     *   payload the row was, rather than from whatever the DOM has drifted
     *   to since.
     *
     *   THIN GLYPH, WIDE TARGET. The mark is 16px; padding and an
     *   overlay give it a 44px tap area on a coarse pointer
     *   (client/css/session-row-menu.css), and it carries no chip, ring
     *   or circle - "just dont want any circles around icons on left
     *   menu ... no border is probably better".
     * Inputs:
     *   r (object) - one merged session row, as session-sidebar-rows.js
     *     builds from. Reads `name`, `status`, `is_pinned`, `unread`.
     * Output:
     *   string - HTML for one `<button>`.
     * Example:
     *   kebabHtml({name: 'cloude_api', status: 'working'})
     */
    function kebabHtml(r) {
        var name = (r && r.name) || '';
        var label = 'more actions for ' + name;
        return (
            '<button type="button" class="' + KEBAB_CLASS + '" '
            + KEBAB_ATTR + '="' + esc(name) + '" '
            + 'data-row-status="' + esc((r && r.status) || 'unknown') + '" '
            + 'data-row-pinned="' + ((r && r.is_pinned) ? '1' : '0') + '" '
            + 'data-row-unread="' + ((r && r.unread) ? '1' : '0') + '" '
            + 'tabindex="-1" aria-haspopup="menu" aria-expanded="false" '
            + 'aria-controls="' + PANEL_ID + '" '
            + 'title="more" aria-label="' + esc(label) + '">'
            + (window.KebabIcon ? window.KebabIcon.svg(16) : '')
            + '</button>'
        );
    }

    /**
     * Description: the raw HTML of every control this row's menu offers,
     *   in order, FROM THE MODULES THAT ALREADY OWN THOSE CONTROLS. This
     *   is the single definition the kebab tap, the right click and the
     *   long press all share: three entry points, one list.
     *
     *   A builder may emit more than one button (restart AND remove for
     *   a dead row), so this returns strings to be expanded.
     * Inputs:
     *   kebab (Element) - the trigger, carrying the row's state.
     * Output:
     *   Array<string> - HTML fragments, one or more controls each.
     */
    function controlHtmlFor(kebab) {
        var name = kebab.getAttribute(KEBAB_ATTR) || '';
        var status = kebab.getAttribute('data-row-status') || 'unknown';
        var pinned = kebab.getAttribute('data-row-pinned') === '1';
        var unread = kebab.getAttribute('data-row-unread') === '1';
        var out = [];
        if (window.SessionSidebarRows) {
            out.push(window.SessionSidebarRows.pinButtonHtml(name, pinned));
        }
        if (window.SessionStatusUI) {
            out.push(window.SessionStatusUI.markUnreadHtml(name, unread));
        }
        if (window.SessionSidebarGroupActions) {
            out.push(window.SessionSidebarGroupActions.rowMenuItemHtml(name));
        }
        if (window.SessionRowActions) {
            out.push(window.SessionRowActions.html(
                status, name, 'session-sidebar-row-delete'));
        }
        return out;
    }

    /**
     * Description: turn one control's own markup into a labelled menu
     *   item, in place. It keeps its element, classes, data attributes
     *   and aria state, and GAINS `role="menuitem"`, a tab stop and a
     *   visible label taken from its own `title` - reading the label
     *   rather than restating it is why this menu cannot drift from the
     *   row.
     * Inputs:
     *   el (Element) - a control built by one of the row builders.
     * Output:
     *   Element - the same element, prepared for the menu.
     */
    function decorateItem(el) {
        el.classList.add(ITEM_CLASS);
        el.setAttribute('role', 'menuitem');
        // The inline pin is `tabindex="-1"` because the ROW owns the tab
        // stop in the list. A menu item must be reachable on its own.
        el.setAttribute('tabindex', '0');
        var label = el.getAttribute('title') || el.getAttribute('aria-label') || '';
        // A control that already renders its own text is not given a
        // second label. Nothing folded in today has that shape - every
        // one of them is icon-only - but the guard costs nothing to keep
        // and protects whatever the next folded control turns out to be.
        if (label && !el.textContent.trim()) {
            var span = document.createElement('span');
            span.className = 'session-row-menu__label';
            span.textContent = label;
            el.appendChild(span);
        }
        return el;
    }

    /**
     * Description: build the panel for one row, unmounted.
     * Inputs: kebab (Element) - the trigger.
     * Output: Element|null - null when no control could be built.
     */
    function buildPanel(kebab) {
        var name = kebab.getAttribute(KEBAB_ATTR) || '';
        var holder = document.createElement('div');
        holder.innerHTML = controlHtmlFor(kebab).join('');
        var items = Array.prototype.slice.call(holder.children);
        if (!items.length) return null;

        var panel = document.createElement('div');
        panel.id = PANEL_ID;
        panel.className = PANEL_CLASS;
        panel.setAttribute('role', 'menu');
        panel.setAttribute('aria-label', 'actions for ' + name);
        panel.setAttribute('data-row-menu-for', name);
        items.forEach(function (el) { panel.appendChild(decorateItem(el)); });
        return panel;
    }

    /**
     * Description: every focusable item in the open panel, in order.
     * Inputs: none. Output: Array<Element>.
     */
    function items() {
        if (!panelEl) return [];
        return Array.prototype.slice.call(
            panelEl.querySelectorAll('[role="menuitem"]'));
    }

    /**
     * Description: move focus within the open menu.
     * Inputs: delta (number) - +1 next, -1 prev, 0 first, Infinity last.
     * Output: void.
     */
    function moveFocus(delta) {
        var list = items();
        if (!list.length) return;
        var at = list.indexOf(document.activeElement);
        var next;
        if (delta === 0) next = 0;
        else if (delta === Infinity) next = list.length - 1;
        else if (at === -1) next = delta > 0 ? 0 : list.length - 1;
        else next = (at + delta + list.length) % list.length;
        list[next].focus();
    }

    /**
     * Description: is a menu open right now?
     * Inputs: none. Output: boolean.
     */
    function isOpen() {
        return !!panelEl;
    }

    /**
     * Description: the tmux name the open menu belongs to, or null, so a
     *   second gesture on the SAME row does not tear the menu down and
     *   rebuild it in a new place.
     * Inputs: none. Output: string|null.
     */
    function openFor() {
        return panelEl ? panelEl.getAttribute('data-row-menu-for') : null;
    }

    /**
     * Description: close the open menu, if any, and put focus back where
     *   the user left it. Focus is only MOVED when it is currently inside
     *   the menu - closing because the user clicked elsewhere must not
     *   yank focus off whatever they just clicked.
     * Inputs: none. Output: void.
     */
    function close() {
        if (onDocPointer) {
            document.removeEventListener('pointerdown', onDocPointer, true);
            onDocPointer = null;
        }
        if (onDocKey) {
            document.removeEventListener('keydown', onDocKey, true);
            onDocKey = null;
        }
        if (onReflow) {
            if (scrollEl) scrollEl.removeEventListener('scroll', onReflow);
            window.removeEventListener('resize', onReflow);
            onReflow = null;
            scrollEl = null;
        }
        var hadFocus = !!(panelEl && panelEl.contains(document.activeElement));
        var forName = panelEl ? panelEl.getAttribute('data-row-menu-for') : null;
        if (panelEl && panelEl.parentNode) panelEl.parentNode.removeChild(panelEl);
        panelEl = null;
        var trigger = triggerEl;
        triggerEl = null;
        if (!trigger) return;
        // A POLL REPAINT REWRITES THE WHOLE LIST, so the kebab we opened
        // from can be a detached node by now. Re-resolve it by name: that
        // way `aria-expanded` is cleared on the button the user can
        // actually see rather than on a corpse, and focus goes back to a
        // real control instead of to <body>, off screen. Commit 7bf95e5
        // fixed the same class of bug one level up.
        var live = document.contains(trigger)
            ? trigger
            : (forName && document.querySelector(
                '[' + KEBAB_ATTR + '="' + CSS.escape(forName) + '"]'));
        (live || trigger).setAttribute('aria-expanded', 'false');
        if (!hadFocus) return;
        if (live) { live.focus(); return; }
        var list = document.getElementById('session-sidebar-list');
        if (list && typeof list.focus === 'function') list.focus();
    }

    /**
     * Description: run the action a menu item stands for, by handing it
     *   to the module that already owns that action. The menu adds no
     *   behaviour of its own - it is a second place to reach the same
     *   handlers the inline controls reached.
     *
     *   The menu is closed FIRST. Every one of these ends in a repaint
     *   that rewrites the list, and a panel still holding focus while its
     *   row is destroyed is the focus-stranding bug all over again.
     * Inputs:
     *   e (Event) - the click inside the panel.
     * Output: void.
     */
    function dispatch(e) {
        var target = e.target;
        if (!target || typeof target.closest !== 'function') return;
        var ctrl = window.SessionSidebar;
        var clicks = window.SessionSidebarClicks;

        var actionEl = window.SessionRowActions
            ? target.closest('[' + window.SessionRowActions.ATTR_ACTION + ']')
            : null;
        if (actionEl && clicks) {
            e.preventDefault();
            close();
            clicks.onRowActionClick(ctrl, actionEl);
            return;
        }
        var unreadEl = target.closest('[data-mark-unread]');
        if (unreadEl && clicks) {
            e.preventDefault();
            close();
            clicks.onMarkUnreadClick(ctrl, unreadEl);
            return;
        }
        var pinEl = target.closest('[data-pin-session]');
        if (pinEl && window.SessionSidebarReorder) {
            // onPinClick reads the control back off the event and does its
            // own preventDefault/stopPropagation, so it is given the real
            // event rather than a name.
            window.SessionSidebarReorder.onPinClick(e);
            close();
            return;
        }
        var groupEl = target.closest('[data-group-pick]');
        if (groupEl && window.SessionSidebarGroupActions) {
            e.preventDefault();
            // The anchor for the picker has to be captured BEFORE close(),
            // which nulls triggerEl and detaches groupEl along with the
            // rest of this panel - an anchor with no box would open the
            // picker at the viewport's top-left corner instead of near
            // the row it belongs to.
            var anchor = triggerEl || groupEl;
            var pickName = groupEl.getAttribute('data-group-pick');
            close();
            window.SessionSidebarGroupActions.openPickerFor(anchor, pickName);
            return;
        }
        // A click on the panel's own padding is not an action. Swallow it
        // so it cannot reach anything behind the menu.
        e.preventDefault();
    }

    /**
     * Description: mount and show the panel for a kebab, placed by the
     *   given placer. THE ONE OPEN PATH - the kebab tap, the right click
     *   and the long press all arrive here, so there is one menu, one
     *   set of items and one set of dismiss rules however it was opened.
     *
     *   Opening closes whatever was open first, including the group
     *   picker, so there is never a second menu on screen.
     * Inputs:
     *   kebab (Element) - the row's trigger.
     *   place (function(Element): void) - positions the mounted panel.
     * Output: boolean - whether a menu is now open.
     */
    function openWith(kebab, place) {
        close();
        if (window.SessionSidebarGroupActions
            && typeof window.SessionSidebarGroupActions.closeMenu === 'function') {
            window.SessionSidebarGroupActions.closeMenu();
        }
        if (!kebab) return false;
        var panel = buildPanel(kebab);
        if (!panel) return false;

        document.body.appendChild(panel);
        panelEl = panel;
        triggerEl = kebab;
        kebab.setAttribute('aria-expanded', 'true');
        place(panel);

        panel.addEventListener('click', dispatch);

        onDocPointer = function (ev) {
            if (!panelEl) return;
            if (panelEl.contains(ev.target)) return;
            if (triggerEl && triggerEl.contains(ev.target)) return;
            close();
        };
        onDocKey = function (ev) {
            if (!panelEl) return;
            if (ev.key === 'Escape') {
                ev.preventDefault();
                ev.stopPropagation();
                close();
                return;
            }
            if (!panelEl.contains(document.activeElement)) return;
            if (ev.key === 'ArrowDown') { ev.preventDefault(); moveFocus(1); }
            else if (ev.key === 'ArrowUp') { ev.preventDefault(); moveFocus(-1); }
            else if (ev.key === 'Home') { ev.preventDefault(); moveFocus(0); }
            else if (ev.key === 'End') { ev.preventDefault(); moveFocus(Infinity); }
        };
        // ESCAPE IS BOUND SYNCHRONOUSLY. It used to be deferred with the
        // pointer handler below, and there was a real window - measured
        // in Chromium - in which the menu was on screen and Escape did
        // nothing. A person is unlikely to be that fast; a keyboard macro
        // and an automated test are not, and "the control works unless
        // you are quick" is not a state worth shipping. There is nothing
        // for the defer to protect against here anyway: the keydown that
        // opens a menu (Enter or Space on the kebab) is delivered before
        // the click it synthesises, so it can never be seen by a listener
        // this call has not bound yet.
        document.addEventListener('keydown', onDocKey, true);
        // The POINTER handler stays deferred a tick, so the pointerdown
        // that opened this does not immediately dismiss it. Same guard
        // session-theme-menu.js uses.
        setTimeout(function () {
            if (!panelEl) return;
            document.addEventListener('pointerdown', onDocPointer, true);
        }, 0);

        // A FIXED PANEL OVER A SCROLLING LIST MUST NOT HANG IN SPACE.
        // Closing is the honest answer: the row it belongs to has moved,
        // and re-placing it every frame would make a phone scroll stutter.
        onReflow = function () { close(); };
        scrollEl = document.getElementById('session-sidebar-list');
        if (scrollEl) scrollEl.addEventListener('scroll', onReflow, { passive: true });
        window.addEventListener('resize', onReflow, { passive: true });

        moveFocus(0);
        return true;
    }

    /**
     * Description: open the menu against the row's kebab - above it with
     *   right edges flush, dropping below when there is no room, clamped
     *   into the visible viewport. The placement rule is the app's one
     *   rule (client/js/anchor-popover.js), not a copy.
     * Inputs: kebab (Element).
     * Output: boolean.
     */
    function openForKebab(kebab) {
        return openWith(kebab, function (panel) {
            if (window.AnchorPopover) window.AnchorPopover.place(panel, kebab);
        });
    }

    /**
     * Description: open the menu at a pointer position - what a right
     *   click means. Same panel, same items, same dismiss rules.
     * Inputs: kebab (Element) - still the trigger, so focus goes home to
     *   a visible control rather than to the pointer's last position.
     *   x (number), y (number) - client coordinates.
     * Output: boolean.
     */
    function openAtPoint(kebab, x, y) {
        return openWith(kebab, function (panel) {
            if (window.AnchorPopover) window.AnchorPopover.placeAt(panel, x, y);
        });
    }

    /**
     * Description: the kebab belonging to a row element, or null.
     * Inputs: rowEl (Element|null). Output: Element|null.
     */
    function kebabIn(rowEl) {
        if (!rowEl || typeof rowEl.querySelector !== 'function') return null;
        return rowEl.querySelector('[' + KEBAB_ATTR + ']');
    }

    window.SessionRowMenu = {
        KEBAB_ATTR: KEBAB_ATTR,
        KEBAB_CLASS: KEBAB_CLASS,
        PANEL_ID: PANEL_ID,
        PANEL_CLASS: PANEL_CLASS,
        ITEM_CLASS: ITEM_CLASS,
        kebabHtml: kebabHtml,
        kebabIn: kebabIn,
        controlHtmlFor: controlHtmlFor,
        buildPanel: buildPanel,
        openForKebab: openForKebab,
        openAtPoint: openAtPoint,
        close: close,
        isOpen: isOpen,
        openFor: openFor,
        items: items,
    };
})();

console.log('[SessionRowMenu Module] Exported as window.SessionRowMenu');
