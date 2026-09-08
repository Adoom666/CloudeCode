/**
 * THE PROJECT INFO MODAL - everything a project card has no room for.
 *
 * WHY A MODAL AND NOT AN INLINE DISCLOSURE. The owner asked for it in
 * those words: "i think that the info should probably open in a modal
 * and not inline." An inline expansion in a 272px rail pushes 76 cards
 * down the list and takes the card you were reading off screen with it,
 * which is the same unscannability the label truncation exists to
 * prevent.
 *
 * WHY IT GOES THROUGH client/js/modal-stack.js. That module is the ONE
 * place that knows which modal is on top, and it owns the capture-phase
 * Escape router, the background scroll lock, the covered-overlay inert
 * marking and the focus restore. A hand-rolled overlay would register a
 * second document-level Escape listener, which is the exact defect
 * ModalStack was written to fix: one keypress reaching two handlers and
 * collapsing the whole stack. This modal pushes on open and pops on
 * close, so Escape closes IT and whatever is underneath stays open.
 *
 * ------------------------------------------------------------------
 * WHAT IS IN HERE AND WHY EACH THING IS IN HERE RATHER THAN ON THE CARD
 * ------------------------------------------------------------------
 *   - THE MACHINES. They were pills on the card face and are now here,
 *     each one a LINK that narrows the rail to that machine. Measured
 *     2026-09-02 against the live instance: 77 project nodes, of which
 *     exactly 3 appear on both machines. So the single-machine case is
 *     74 of 77 and is the ORDINARY one - it is written as a complete
 *     sentence ("Collected from one machine.") rather than as a list of
 *     one that reads like a list with items missing.
 *   - THE FULL PATH. The card face shows only the folder name, because
 *     the slug middle-truncates to something that removes precisely the
 *     segment identifying it. The path has to live somewhere, and a
 *     `title` attribute is not somewhere you can select text from.
 *   - THE COUNTS, restated with their definitions, because the card has
 *     room for the numbers and not for what they mean.
 *
 * A MACHINE LIST THAT WAS NEVER REPORTED IS NOT AN EMPTY MACHINE LIST.
 * A merged node carries `hosts` and `members`; the per-corpus rows the
 * by-machine tree renders do not. When they are absent this modal
 * renders an outcome block through archive-outcome-view.js reading
 * COULD NOT EVALUATE - it does not draw an empty section, which would
 * claim the project belongs to no machine, and it does not guess.
 *
 * ------------------------------------------------------------------
 * THE OVERLAY SEAM (rename / group / soft delete)
 * ------------------------------------------------------------------
 * THE CONTRACT LANDED 2026-09-02 AND IS WIRED. It is not a client-side
 * store: `GET /api/v1/archive/overlay/projects`
 * (src/api/archive_overlay_routes.py) returns the SAME merged nodes with
 * the override already substituted into `display_name`, the archive's
 * own derived name preserved as `archive_display_name`, and per node:
 *
 *   overlay: {status: 'none'|'applied'|'cannot_determine',
 *             group: string|null, hidden: boolean,
 *             applied: Array<'display_name'|'group'|'hidden'>,
 *             identity_key: string|null, identity_kind: string|null}
 *
 * ArchiveNavCard.presentationFor reads that block; this modal renders
 * it. `renamed` comes off `applied`, NEVER off comparing two strings,
 * because the server's own contract note warns that renaming a project
 * to its own folder name is a thing a person may do.
 *
 * THAT WIRING STEP IS NOW TAKEN (2026-09-02).
 * `api.listArchiveMergedProjects` points at `/archive/overlay/projects`,
 * so nodes arrive carrying their own `overlay` block and a rename shows.
 * Nothing in this file changed for it, which was the design: a node with
 * no block is still reported as `absent` rather than as 'none', and that
 * path stays reachable for a build served by an older server.
 *
 * The injected `overlay` option survives as a fallback for tests and for
 * a build with no overlay endpoint, and is consulted ONLY when the row
 * carries no block of its own.
 *
 * Exports window.ArchiveNavInfo.
 */

console.log('[ArchiveNavInfo Module] Loading...');

(function () {
    'use strict';

    var ROW = window.ArchiveNavRow;
    if (!ROW) {
        console.error('[ArchiveNavInfo] MISSING DEPENDENCY: window.ArchiveNavRow. ' +
            'Load client/js/archive-nav-row.js BEFORE this file.');
    }

    /** Root class for the modal's own elements. @type {string} */
    var ROOT_CLASS = 'archive-nav-info';

    /**
     * Description: the sentence that heads the machines section, written
     *   for the count it actually has. Pure and exported, because the
     *   one-machine case is 74 of 77 projects and getting it to read as
     *   a statement rather than as a truncated list is the point.
     * Inputs: hosts (Array|null|undefined).
     * Output: {text: string, known: boolean} - `known` false means the
     *   caller must render a could-not-evaluate block instead of a list.
     * Example: machinesHeading(['a'])
     *   // -> {text: 'Collected from one machine.', known: true}
     */
    function machinesHeading(hosts) {
        if (!Array.isArray(hosts)) {
            return { text: 'Machines could not be determined.', known: false };
        }
        if (hosts.length === 0) {
            return { text: 'Machines could not be determined.', known: false };
        }
        if (hosts.length === 1) {
            return { text: 'Collected from one machine.', known: true };
        }
        return {
            text: 'Collected from ' + ROW.renderCount(hosts.length) +
                  ' machines. This project exists on each of them.',
            known: true
        };
    }

    /**
     * Description: pair every host name with the member row that carries
     *   its id and its own transcript count, so a link can address the
     *   machine and not just name it. Pure.
     *
     *   A HOST NAMED WITH NO MEMBER ROW IS STILL LISTED. It is a real
     *   fact the server reported; what it loses is the link, and the row
     *   says so rather than being dropped, because a dropped machine is
     *   the merge hiding exactly what it is only allowed to demote.
     * Inputs: row (object) - a project node.
     * Output: Array<{name, host_id, transcript_count, linkable}>.
     */
    function machineRows(row) {
        var r = row || {};
        var hosts = Array.isArray(r.hosts) ? r.hosts : [];
        var members = Array.isArray(r.members) ? r.members : [];
        return hosts.map(function (name) {
            var hit = null;
            for (var i = 0; i < members.length; i++) {
                if (String(members[i].host_display_name) === String(name)) {
                    hit = members[i];
                    break;
                }
            }
            return {
                name: String(name),
                host_id: hit ? hit.host_id : null,
                transcript_count: hit && typeof hit.transcript_count === 'number'
                    ? hit.transcript_count : null,
                linkable: !!(hit && hit.host_id !== null && hit.host_id !== undefined)
            };
        });
    }

    /**
     * Description: build one labelled field - a term and its value, with
     *   the value selectable and allowed to WRAP. The rail's no-wrap rule
     *   is about a 272px column with 77 siblings; a path in a dialog has
     *   nothing underneath it to push down, and a path you cannot read in
     *   full is the reason this dialog exists.
     * Inputs: doc (Document), term (string), value (string|null),
     *         absent (string) - what to say when there is no value.
     * Output: Element.
     */
    function field(doc, term, value, absent) {
        var el = ROW.el;
        var wrap = el(doc, 'div', ROOT_CLASS + '__field', null);
        wrap.appendChild(el(doc, 'span', ROOT_CLASS + '__term', term));
        var has = typeof value === 'string' && value.length > 0;
        var v = el(doc, 'span', ROOT_CLASS + '__value' +
            (has ? '' : ' ' + ROOT_CLASS + '__value--unknown'),
            has ? value : absent);
        v.setAttribute('data-known', has ? 'true' : 'false');
        wrap.appendChild(v);
        return wrap;
    }

    /**
     * Description: render the machines section, or the reason there is
     *   not one.
     * Inputs: doc (Document), row (object), onFilterHost (function|null).
     * Output: Element.
     */
    function renderMachines(doc, row, onFilterHost) {
        var el = ROW.el;
        var section = el(doc, 'section', ROOT_CLASS + '__section ' +
            ROOT_CLASS + '__section--machines', null);
        section.setAttribute('data-section', 'machines');
        section.appendChild(el(doc, 'h3', ROOT_CLASS + '__section-title', 'Machines'));

        var heading = machinesHeading(row && row.hosts);
        if (!heading.known) {
            // NOT an empty list. This node came from the per-corpus
            // listing, which does not carry `hosts` at all - so "no
            // machines" is a thing nobody measured, and it goes through
            // the client's one status interpreter like every other
            // could-not-evaluate in this UI.
            section.setAttribute('data-machines', 'cannot-determine');
            section.appendChild(window.ArchiveOutcomeView.renderOutcomeBlock({
                result_status: 'cannot_determine',
                scope_status: 'resolved',
                unevaluated: [{
                    subject: 'project.hosts',
                    reason: 'this project row carries no host list, so the ' +
                            'machines it was collected from were never reported'
                }],
                meta: {}
            }, { document: doc }));
            return section;
        }

        var rows = machineRows(row);
        section.setAttribute('data-machines', String(rows.length));
        section.appendChild(el(doc, 'p', ROOT_CLASS + '__lede', heading.text));

        var list = el(doc, 'ul', ROOT_CLASS + '__machines', null);
        for (var i = 0; i < rows.length; i++) {
            list.appendChild(renderMachineRow(doc, rows[i], onFilterHost));
        }
        section.appendChild(list);
        return section;
    }

    /**
     * Description: one machine, as a link back to that machine's view of
     *   the rail.
     * Inputs: doc (Document), m (object) - a machineRows entry.
     *         onFilterHost (function|null).
     * Output: Element - an <li>.
     */
    function renderMachineRow(doc, m, onFilterHost) {
        var el = ROW.el;
        var li = el(doc, 'li', ROOT_CLASS + '__machine', null);
        li.setAttribute('data-host-name', m.name);

        var tag = m.linkable ? 'button' : 'span';
        var body = el(doc, tag, ROOT_CLASS + '__machine-link', null);
        if (m.linkable) {
            body.setAttribute('type', 'button');
            body.setAttribute('data-action', 'filter-host');
            body.setAttribute('data-host-id', String(m.host_id));
            body.setAttribute('title', 'Show only the projects collected from ' + m.name);
            if (typeof onFilterHost === 'function') {
                body.addEventListener('click', function () { onFilterHost(m.host_id, m.name); });
            }
        } else {
            // Named, unlinked, and SAYING it is unlinked. Silently
            // rendering it as plain text would read as a styling choice.
            body.setAttribute('data-action', 'none');
            body.setAttribute('title',
                'This machine has no id on this row, so the rail cannot be ' +
                'narrowed to it from here.');
        }
        body.appendChild(el(doc, 'span', ROOT_CLASS + '__machine-name', m.name));
        body.appendChild(el(doc, 'span', ROOT_CLASS + '__machine-count',
            ROW.renderCount(m.transcript_count) + ' transcripts here'));
        li.appendChild(body);
        return li;
    }

    /**
     * Description: render the presentation-overlay section, or nothing.
     *   Drawn ONLY when an overlay is actually present; a disabled
     *   rename box that cannot save is furniture, and furniture in a
     *   dialog reads as a broken feature.
     * Inputs: doc (Document), pres (object) - ArchiveNavCard.presentationFor
     *         output. Output: Element|null.
     */
    function renderOverlaySection(doc, pres) {
        if (!pres) return null;
        var interesting = pres.renamed || pres.group || pres.hidden ||
            pres.overlayStatus === 'cannot_determine';
        if (!interesting) return null;
        var el = ROW.el;
        var section = el(doc, 'section', ROOT_CLASS + '__section', null);
        section.setAttribute('data-section', 'overlay');
        section.setAttribute('data-overlay-status', String(pres.overlayStatus || 'absent'));
        section.appendChild(el(doc, 'h3', ROOT_CLASS + '__section-title', 'Your labels'));
        if (pres.overlayStatus === 'cannot_determine') {
            // The server says this project has neither an observed_cwd
            // nor an id, so no overlay row CAN attach to it. That is not
            // "nothing set" and must not render as a blank panel.
            section.appendChild(el(doc, 'p', ROOT_CLASS + '__note',
                ROW.NOT_KNOWN + ' - this project cannot be addressed by the ' +
                'overlay, so it can never carry a name, a group or a hidden ' +
                'flag. Nothing was set and nothing can be.'));
            return section;
        }
        if (pres.renamed) {
            // The server's own name stays on screen beside the override.
            // A rename that HIDES what a thing really is turns the modal
            // into the second place you cannot find out.
            section.appendChild(field(doc, 'Shown as', pres.name, ''));
            section.appendChild(field(doc, 'Actual name', pres.serverName, ''));
        }
        if (pres.group) section.appendChild(field(doc, 'Group', pres.group, ''));
        if (pres.hidden) {
            section.appendChild(field(doc, 'Hidden', 'yes - hidden from the default list', ''));
        }
        return section;
    }

    /**
     * Description: build and open the modal. Registers with ModalStack,
     *   moves focus into the dialog, and returns a handle whose close()
     *   is the ONLY way it comes down, so the pop and the DOM removal can
     *   never diverge.
     * Inputs: options (object) -
     *   document (Document), row (object) - the project node,
     *   presentation (object|null) - ArchiveNavCard.presentationFor output,
     *   onFilterHost (function(hostId, hostName)) - called when a machine
     *     link is chosen; the caller closes nothing, this does,
     *   modalStack (object|null) - injected for tests; defaults to
     *     window.ModalStack.
     * Output: {element, close, isOpen}.
     */
    function open(options) {
        var opts = options || {};
        var doc = opts.document || (typeof window !== 'undefined' ? window.document : null);
        if (!doc) throw new Error('ArchiveNavInfo.open needs a document');
        var el = ROW.el;
        var row = opts.row || {};
        var stack = opts.modalStack ||
            (typeof window !== 'undefined' ? window.ModalStack : null);
        var pres = opts.presentation ||
            (window.ArchiveNavCard
                ? window.ArchiveNavCard.presentationFor(row, opts.overlay)
                : { name: ROW.labelFor(ROW.NODE_KINDS.PROJECT, row), serverName: '',
                    group: null, hidden: false, renamed: false });

        // `modal-overlay` is the app's own overlay class, and ModalStack
        // enumerates `:scope > .modal-overlay` on the body to decide
        // whether a foreign dialog has been layered over the stack. Using
        // any other class here would make this modal invisible to that
        // check and let an Escape meant for a confirm dialog close this
        // one underneath it.
        var overlay = el(doc, 'div', 'modal-overlay ' + ROOT_CLASS + '__overlay', null);
        overlay.setAttribute('data-modal', 'archive-nav-info');

        var dialog = el(doc, 'div', ROOT_CLASS, null);
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-label', 'Details for ' + pres.name);

        var head = el(doc, 'div', ROOT_CLASS + '__head', null);
        var title = el(doc, 'h2', ROOT_CLASS + '__title', pres.name);
        title.setAttribute('title', pres.name);
        head.appendChild(title);
        var closeBtn = el(doc, 'button', ROOT_CLASS + '__close', 'Close');
        closeBtn.setAttribute('type', 'button');
        closeBtn.setAttribute('data-action', 'close');
        closeBtn.setAttribute('aria-label', 'Close details');
        head.appendChild(closeBtn);
        dialog.appendChild(head);

        var body = el(doc, 'div', ROOT_CLASS + '__body', null);

        // ---- where it is -------------------------------------------
        var where = el(doc, 'section', ROOT_CLASS + '__section', null);
        where.setAttribute('data-section', 'path');
        where.appendChild(el(doc, 'h3', ROOT_CLASS + '__section-title', 'Where it is'));
        where.appendChild(field(doc, 'Full path', row.observed_cwd,
            'NOT KNOWN - the server observed no working directory for this project'));
        where.appendChild(field(doc, 'Slug', row.full_path || row.slug,
            'NOT KNOWN - no slug was reported'));
        body.appendChild(where);

        // ---- what is in it -----------------------------------------
        var counts = el(doc, 'section', ROOT_CLASS + '__section', null);
        counts.setAttribute('data-section', 'counts');
        counts.appendChild(el(doc, 'h3', ROOT_CLASS + '__section-title', 'What is in it'));
        var session = window.ArchiveNavCard
            ? window.ArchiveNavCard.sessionCountFor(row)
            : { state: 'not-reported', value: null };
        counts.appendChild(field(doc, 'Your sessions',
            session.state === 'known' ? ROW.renderCount(session.value) : null,
            ROW.NOT_KNOWN + ' - ' + (window.ArchiveNavCard
                ? (window.ArchiveNavCard.SESSION_REASONS[session.state] || '')
                : '')));
        counts.appendChild(field(doc, 'All transcripts',
            ROW.renderCount(ROW.countFor(ROW.NODE_KINDS.PROJECT, row)), ''));
        counts.appendChild(el(doc, 'p', ROOT_CLASS + '__note',
            'Sessions are top-level conversations (session_ref_scheme = uuid). ' +
            'The transcript total also counts agent sidechain files, which are ' +
            'about 93 percent of this archive.'));
        body.appendChild(counts);

        // ---- machines ----------------------------------------------
        var handle = { element: overlay, close: null, isOpen: function () { return open_; } };
        var open_ = true;

        /**
         * Description: take the modal down exactly once, popping the
         *   stack first so focus is restored before the node leaves the
         *   DOM. Idempotent. Inputs: none. Output: void.
         */
        function close() {
            if (!open_) return;
            open_ = false;
            if (stack && typeof stack.pop === 'function') stack.pop(overlay);
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        }
        handle.close = close;

        body.appendChild(renderMachines(doc, row, function (hostId, hostName) {
            close();
            if (typeof opts.onFilterHost === 'function') opts.onFilterHost(hostId, hostName);
        }));

        var overlaySection = renderOverlaySection(doc, pres);
        if (overlaySection) body.appendChild(overlaySection);

        dialog.appendChild(body);
        overlay.appendChild(dialog);

        closeBtn.addEventListener('click', function () { close(); });
        // A click on the backdrop closes; a click INSIDE the dialog must
        // not. The dialog stops the event before it reaches the overlay
        // rather than the overlay testing the target, because target
        // testing breaks the moment a child is replaced.
        dialog.addEventListener('click', function (e) {
            if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
        });
        overlay.addEventListener('click', function () { close(); });

        (opts.container || doc.body).appendChild(overlay);
        if (stack && typeof stack.push === 'function') {
            stack.push(overlay, { onEscape: close });
        }
        if (typeof closeBtn.focus === 'function') closeBtn.focus();
        return handle;
    }

    /**
     * Description: a SINGLE-INSTANCE opener. Owns the "at most one info
     *   modal, and reopening replaces the one already up" rule, which is
     *   modal lifecycle and not rail state - the rail was carrying it and
     *   went over this repo's 500-line cap doing so.
     * Inputs: options (object) -
     *   document (Document), overlay (object|null) - the presentation
     *   overlay, onFilterHost (function(hostId, hostName)).
     * Output: {open(row, presentation) -> handle|null, current() -> handle|null}
     */
    function wire(options) {
        var opts = options || {};
        var current = null;
        return {
            open: function (row, presentation) {
                if (current) current.close();
                current = open({
                    document: opts.document,
                    row: row,
                    presentation: presentation,
                    overlay: opts.overlay,
                    modalStack: opts.modalStack,
                    container: opts.container,
                    onFilterHost: opts.onFilterHost
                });
                return current;
            },
            current: function () { return current; }
        };
    }

    window.ArchiveNavInfo = {
        open: open,
        wire: wire,
        machinesHeading: machinesHeading,
        machineRows: machineRows,
        ROOT_CLASS: ROOT_CLASS
    };
    console.log('[ArchiveNavInfo Module] Exported as window.ArchiveNavInfo');
})();
