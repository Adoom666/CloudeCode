/**
 * AgentWrappersPanel - state and behavior for the wrappers settings
 * screen, mounted by client/js/settings-panel.js into the "wrappers" tab.
 *
 * feat/universal-wrappers: this screen used to administer claude wrappers
 * only, alongside four static per-CLI command fields elsewhere in
 * settings. It now administers wrappers for EVERY command family
 * (claude, codex, hermes, openclaw, shell), grouped by family, with each
 * family's static command shown inside its own group as the collapsed
 * fallback it actually is.
 *
 * Markup lives in agent-wrappers-view.js (pure render functions); this
 * file owns fetching, editing state, and event wiring. Split for the
 * repo's 500-line file budget.
 *
 * Unlike settings-panel.js's generic fields (which batch into one Save
 * button), every wrapper action here writes immediately via its own API
 * call and re-renders - the same "applies immediately" contract the
 * appearance section uses, because add/edit/delete/set-default are each a
 * complete server-side mutation (see Settings.add_wrapper et al) with no
 * partial state a batched Save could usefully defer.
 *
 * Must load AFTER api.js (window.API) and agent-wrappers-view.js, and
 * BEFORE settings-panel.js calls window.AgentWrappersPanel.mount().
 */
(function () {
    'use strict';

    var View = window.AgentWrappersView;

    var wrappers = [];
    var families = [];
    var examples = null;   // lazy-loaded, null = not fetched yet
    var rootEl = null;
    var editingId = null;  // wrapper id open in the editor, or '__new__'
    var editingFamily = 'claude'; // family the '__new__' editor is scoped to
    var editorSeed = null; // pre-filled fields for an imported example

    /**
     * Build the wrapper object the editor should render.
     * Inputs: none (reads module state).
     * Output: object - AgentWrapper-shaped dict, blank for a new wrapper.
     */
    function editorSubject() {
        if (editingId !== '__new__') {
            return wrappers.filter(function (x) { return x.id === editingId; })[0] || {};
        }
        if (editorSeed) return editorSeed;
        var familyHasNone = wrappers.filter(function (w) {
            return View.familyOf(w) === editingFamily;
        }).length === 0;
        return {
            id: '', family: editingFamily, label: '', script: '', entry: '',
            description: '',
            // The first wrapper in a family becomes that family's default,
            // otherwise adding one would leave the family still resolving
            // to its legacy command with no visible reason.
            default: familyHasNone,
            accepts_model: false,
        };
    }

    /**
     * Render the section markup for the current state.
     * Output: string - HTML.
     */
    function sectionHtml() {
        var editorHtml = editingId
            ? View.renderEditor(editorSubject(), editingId === '__new__', families)
            : '';
        return View.renderSection(wrappers, families, editorHtml);
    }

    /**
     * Re-render the section in place and re-wire its event handlers.
     * Output: void.
     */
    function rerender() {
        if (!rootEl) return;
        rootEl.outerHTML = sectionHtml();
        rootEl = document.querySelector('[data-settings-section="wrappers"]');
        wire();
    }

    /**
     * Apply an API response that carries both lists.
     * Inputs: result (object) - {wrappers, families}.
     * Output: void.
     */
    function applyResult(result) {
        wrappers = (result && result.wrappers) || [];
        if (result && result.families && result.families.length) families = result.families;
    }

    /**
     * Read the editor form into an AgentWrapper-shaped request body.
     * Output: object.
     */
    function readEditorForm() {
        var q = function (id) { return document.getElementById(id); };
        var entry = q('wrapper-field-entry').value.trim();
        var description = q('wrapper-field-description').value.trim();
        var familyEl = q('wrapper-field-family');
        return {
            id: q('wrapper-field-id').value.trim(),
            // The select is disabled when editing, so read the stored
            // family in that case: a disabled select still has a value,
            // but relying on it would silently depend on render order.
            family: (editingId === '__new__')
                ? familyEl.value
                : View.familyOf(editorSubject()),
            label: q('wrapper-field-label').value.trim(),
            script: q('wrapper-field-script').value,
            entry: entry || null,
            description: description || null,
            default: q('wrapper-field-default').checked,
            // Was previously omitted from this form entirely, which reset
            // accepts_model to false on every edit of a model-taking
            // wrapper. It is a real field now.
            accepts_model: q('wrapper-field-accepts-model').checked,
        };
    }

    /**
     * Save the open editor (add or update), reload, and close it.
     * Output: Promise<void>.
     */
    async function saveEditor() {
        var body = readEditorForm();
        var statusEl = document.getElementById('wrapper-editor-status');
        var saveBtn = document.getElementById('wrapper-editor-save');
        if (!body.id || !body.label || !body.script.trim()) {
            if (statusEl) statusEl.textContent = 'id, label, and script are required';
            return;
        }
        if (saveBtn) saveBtn.disabled = true;
        if (statusEl) statusEl.textContent = 'saving...';
        try {
            var result = (editingId === '__new__')
                ? await window.API.addWrapper(body)
                : await window.API.updateWrapper(editingId, body);
            applyResult(result);
            editingId = null;
            editorSeed = null;
            await refreshFamilies();
            rerender();
        } catch (err) {
            console.error('AgentWrappersPanel: save failed', err);
            if (statusEl) statusEl.textContent = 'save failed: ' + (err && err.message ? err.message : 'unknown error');
            if (saveBtn) saveBtn.disabled = false;
        }
    }

    /**
     * Re-fetch the list so family counts / in-use flags reflect the write.
     * Description: mutation responses already carry `families`, but a
     *   server that predates that field would leave the groups stale;
     *   this is a cheap, best-effort reconcile.
     * Output: Promise<void>.
     */
    async function refreshFamilies() {
        try {
            applyResult(await window.API.listWrappers());
        } catch (err) {
            console.error('AgentWrappersPanel: refresh failed', err);
        }
    }

    /**
     * Delete a wrapper after a confirm, then reload the list.
     * Inputs: id (string) - wrapper id.
     * Output: Promise<void>.
     */
    async function deleteWrapper(id) {
        if (!window.confirm('delete wrapper "' + id + '"? this cannot be undone.')) return;
        try {
            applyResult(await window.API.deleteWrapper(id));
            await refreshFamilies();
            rerender();
        } catch (err) {
            console.error('AgentWrappersPanel: delete failed', err);
            window.alert('delete failed: ' + (err && err.message ? err.message : 'unknown error'));
        }
    }

    /**
     * Mark a wrapper as its family's default, then reload the list.
     * Inputs: id (string) - wrapper id.
     * Output: Promise<void>.
     */
    async function makeDefault(id) {
        try {
            applyResult(await window.API.setDefaultWrapper(id));
            await refreshFamilies();
            rerender();
        } catch (err) {
            console.error('AgentWrappersPanel: set-default failed', err);
            window.alert('set-default failed: ' + (err && err.message ? err.message : 'unknown error'));
        }
    }

    /**
     * Fetch + load an example into the editor as a new, UNSAVED wrapper
     * the user reviews before saving. Never writes on its own.
     * Output: Promise<void>.
     */
    async function importExample(familyName) {
        if (!examples) {
            try {
                var resp = await window.API.listWrapperExamples();
                examples = resp.wrappers || [];
            } catch (err) {
                console.error('AgentWrappersPanel: failed to load examples', err);
                window.alert('failed to load examples');
                return;
            }
        }
        // Scoped to the family whose button was pressed. The endpoint
        // returns every family's examples in one list; offering a codex
        // user the claude keychain wrapper would be an invitation to
        // import the wrong thing into the wrong group.
        var offered = examples.filter(function (e) {
            return View.familyOf(e) === familyName;
        });
        if (offered.length === 0) {
            window.alert('no example wrappers for ' + familyName + '.');
            return;
        }
        var labels = offered.map(function (e, i) { return (i + 1) + '. ' + e.label; }).join('\n');
        var choice = window.prompt('import which ' + familyName + ' example?\n' + labels + '\n\nenter a number:', '1');
        var idx = parseInt(choice, 10) - 1;
        if (isNaN(idx) || idx < 0 || idx >= offered.length) return;
        var chosen = offered[idx];
        editingId = '__new__';
        editingFamily = View.familyOf(chosen);
        // Seed through the render path rather than poking the DOM after
        // the fact, so the family select and both checkboxes come up
        // already correct instead of being patched field by field.
        editorSeed = {
            id: chosen.id,
            family: editingFamily,
            label: chosen.label,
            script: chosen.script,
            entry: chosen.entry || '',
            description: chosen.description || '',
            default: false,
            accepts_model: !!chosen.accepts_model,
        };
        rerender();
    }

    /**
     * Persist one family's legacy static command via PATCH /config/settings.
     *
     * Description: the fold-in of the removed "agents" settings tab. Those
     *   three text fields used to ride settings-panel.js's batched Save;
     *   here each row writes on its own, matching every other action on
     *   this screen. A re-fetch follows so the group's "in use now" copy
     *   and its "what runs now" preview reflect the write, which is the
     *   whole reason the value is worth showing at all.
     *
     *   An EMPTY value is sent through deliberately for claude_command
     *   (the server documents empty as "clear back to the cld/cldor
     *   fallback"), and refused client-side for the other three, whose
     *   route handler rejects a blank. Refusing it here turns a 422 into
     *   a sentence.
     * Inputs:
     *   familyName (string) - the family whose row was saved.
     *   field (string) - its config key, e.g. "codex_command".
     * Output: Promise<void>.
     */
    async function saveLegacyCommand(familyName, field) {
        var input = rootEl.querySelector('[data-legacy-input="' + familyName + '"]');
        var statusEl = rootEl.querySelector('[data-legacy-status="' + familyName + '"]');
        if (!input) return;
        var value = input.value;
        if (!value.trim() && field !== 'claude_command') {
            if (statusEl) statusEl.textContent = 'cannot be blank: nothing would launch.';
            return;
        }
        var patch = { agents: {} };
        patch.agents[field] = value;
        if (statusEl) statusEl.textContent = 'saving...';
        try {
            await window.API.updateSettings(patch);
            await refreshFamilies();
            rerender();
            var after = rootEl.querySelector('[data-legacy-status="' + familyName + '"]');
            if (after) after.textContent = 'saved';
            // Keep the row the user was editing open across the rerender.
            var details = rootEl.querySelector('[data-legacy-family="' + familyName + '"]');
            if (details) details.open = true;
        } catch (err) {
            console.error('AgentWrappersPanel: legacy command save failed', err);
            if (statusEl) {
                statusEl.textContent = 'save failed: ' + (err && err.message ? err.message : 'unknown error');
            }
        }
    }

    /**
     * Close the editor and return to the list.
     * Output: void.
     */
    function closeEditor() {
        editingId = null;
        editorSeed = null;
        rerender();
    }

    /**
     * Wire click handlers for the currently-rendered section. Called once
     * per render (mount + every rerender).
     * Output: void.
     */
    function wire() {
        if (!rootEl) return;

        rootEl.querySelectorAll('[data-wrapper-add-family]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                editingId = '__new__';
                editingFamily = btn.getAttribute('data-wrapper-add-family');
                editorSeed = null;
                rerender();
            });
        });

        rootEl.querySelectorAll('[data-wrapper-import-family]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                importExample(btn.getAttribute('data-wrapper-import-family'));
            });
        });

        rootEl.querySelectorAll('[data-legacy-save]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                saveLegacyCommand(
                    btn.getAttribute('data-legacy-save'),
                    btn.getAttribute('data-legacy-field')
                );
            });
        });

        var cancelBtn = rootEl.querySelector('#wrapper-editor-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', closeEditor);

        var saveBtn = rootEl.querySelector('#wrapper-editor-save');
        if (saveBtn) saveBtn.addEventListener('click', saveEditor);

        rootEl.querySelectorAll('[data-wrapper-action]').forEach(function (btn) {
            var action = btn.getAttribute('data-wrapper-action');
            var id = btn.getAttribute('data-wrapper-id');
            if (action === 'edit') {
                btn.addEventListener('click', function () {
                    editingId = id;
                    editorSeed = null;
                    rerender();
                });
            } else if (action === 'delete') {
                btn.addEventListener('click', function () { deleteWrapper(id); });
            } else if (action === 'default') {
                btn.addEventListener('click', function () { makeDefault(id); });
            }
        });
    }

    /**
     * Mount the wrappers section into a parent element.
     * Inputs: parentEl (Element) - container to append into.
     * Output: Promise<void>.
     */
    async function mount(parentEl) {
        editingId = null;
        editorSeed = null;
        try {
            applyResult(await window.API.listWrappers());
        } catch (err) {
            console.error('AgentWrappersPanel: failed to load wrappers', err);
            wrappers = [];
            families = [];
        }
        var holder = document.createElement('div');
        holder.innerHTML = sectionHtml();
        rootEl = holder.firstElementChild;
        parentEl.appendChild(rootEl);
        wire();
    }

    window.AgentWrappersPanel = { mount: mount };
})();
