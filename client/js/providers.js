/**
 * Provider Selector Modal (client/js/providers.js)
 *
 * Shown before every real launch (new project, open-folder, clone-github,
 * existing-project row click). Lets the user pick a launch wrapper and,
 * for wrappers that take one, an OpenRouter model, managing the model
 * list inline - no separate settings screen.
 *
 * TWO STEPS, because there are two independent axes and flattening them
 * into one list produced two real bugs:
 *
 *   (a) A hardcoded "claude" row sat above the wrapper rows and launched
 *       whatever wrapper was marked default, so a machine whose default
 *       is `cld` showed two rows with identical behaviour. There is now
 *       NO bare "claude" row: every configured wrapper appears exactly
 *       once, the default one labelled "<label> (default)". That single
 *       "claude" row returns only when NO wrappers are configured, where
 *       it means the legacy fallback (Settings.get_agent_command).
 *   (b) Picking a model next to a wrapper that ignores models routed the
 *       model to the DEFAULT wrapper, which forwards "$@" to claude - so
 *       the model id arrived as a PROMPT argument and Claude answered
 *       the literal string. Models are now offered ONLY after choosing a
 *       wrapper whose `accepts_model` is true (src/core/agent_wrappers.py),
 *       and the resulting launch carries that wrapper's id. The server
 *       enforces the same rule independently.
 *
 * Family grouping (feat/universal-wrappers) lives in provider-groups.js.
 *
 * Split out of launchpad.js to keep that file under its line budget.
 * Attaches showProviderModal() onto the existing window.Launchpad instance
 * so the two gated call sites (`this.showProviderModal()` in
 * _createNewSessionInner / selectProject) work unchanged. Must load AFTER
 * launchpad.js (see client/index.html script order) so window.Launchpad
 * and its _escapeHtml / showConfirmModal helpers already exist.
 *
 * Reuses the folder-picker's list markup/CSS (.folder-picker-list,
 * .folder-picker-item, .folder-picker-item-active, .folder-picker-status)
 * and its keyboard-nav + capture-phase Escape pattern
 * (client/js/launchpad.js showFolderPickerModal).
 */
(function () {
    // Single localStorage key: '' (or absent) = claude, else the model id.
    const LAST_MODEL_KEY = 'cloude_provider_last_model';

    // Same guard as the server (src/models.py MODEL_ID_PATTERN /
    // is_valid_model_id) - enforced here too so bad input never leaves
    // the browser, but the server remains authoritative (shell-injection
    // guard). The (?!-) lookahead blocks a leading '-', which would
    // otherwise slip past cldor's own "$1" != -* guard and get forwarded
    // as an injected flag to claude --dangerously-skip-permissions.
    const MODEL_ID_RE = /^(?!-)[A-Za-z0-9._~/-]{1,120}$/;

    function readLastChoice() {
        try {
            return localStorage.getItem(LAST_MODEL_KEY) || '';
        } catch (_) {
            return '';
        }
    }

    function rememberChoice(model) {
        try {
            localStorage.setItem(LAST_MODEL_KEY, model || '');
        } catch (_) {
            // localStorage unavailable (private mode, quota) - non-fatal,
            // just means next launch won't pre-select this choice.
        }
    }

    /**
     * Show the provider selector modal.
     * @returns {Promise<{model: string|null, wrapperId?: string}|null>}
     *   null = cancelled (abort the launch). {model: null} = the legacy
     *   claude fallback, only reachable when no wrappers are configured.
     *   {model: null, wrapperId: "..."} = launch through that wrapper
     *   with no model. {model: "...", wrapperId: "..."} = that wrapper
     *   with an OpenRouter model, only ever produced for a wrapper whose
     *   accepts_model is true. wrapperId is forwarded as agent_type by
     *   the launchpad caller.
     */
    function showProviderModal() {
        return new Promise((resolve) => {
            const escapeHtml = (s) => window.Launchpad._escapeHtml(s);

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header" id="provider-header">» select provider</div>
                    <div class="modal-body">
                        <div class="folder-picker-list" id="provider-list" tabindex="-1">
                            <div class="folder-picker-empty">loading...</div>
                        </div>
                        <div class="folder-picker-status" id="provider-status" role="status" aria-live="polite"></div>
                        <div class="modal-description" id="provider-hint">
                            ↑/↓ to move · Enter to launch · type to jump · Esc to cancel
                        </div>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const listEl = overlay.querySelector('#provider-list');
            const statusEl = overlay.querySelector('#provider-status');
            const headerEl = overlay.querySelector('#provider-header');
            const hintEl = overlay.querySelector('#provider-hint');

            let models = [];       // OpenRouter model ids
            let wrappers = [];     // EVERY configured wrapper, unfiltered - each appears exactly once
            let families = [];     // family registry from the server, for group headings and their order
            let items = [];        // nav list for the CURRENT step
            let activeIndex = -1;
            let currentModel = readLastChoice(); // '' = none, else a model id
            let typeBuffer = '';
            let typeTimer = null;
            let confirmPending = false; // suspends our keys while showConfirmModal owns them
            let addInputOpen = false;
            // Two-step nav: pick a wrapper, then (only for a wrapper that
            // accepts one) pick a model. See the module docstring.
            let step = 'wrapper';
            let selectedWrapper = null;
            // LOCAL (LM Studio) model step. `localProbe` is null until the
            // first fetch resolves, and 'loading' while it is in flight. It
            // is fetched ON DEMAND rather than alongside getProviders(), so
            // a box that is off costs nothing unless the user actually asks
            // for local models.
            let selectedFamily = null;
            let localProbe = null;

            const close = (value) => {
                document.removeEventListener('keydown', onKeyDown, true);
                if (typeTimer) clearTimeout(typeTimer);
                document.body.removeChild(overlay);
                resolve(value);
            };

            const clearStatus = () => {
                statusEl.textContent = '';
                statusEl.className = 'folder-picker-status';
            };
            const showStatus = (msg, kind) => {
                statusEl.textContent = msg;
                statusEl.className = `folder-picker-status folder-picker-status--${kind || 'info'}`;
            };

            /**
             * Build the nav list for the current step.
             * Step 'wrapper': one row per configured wrapper, never a
             *   duplicate and never a synthetic "claude" peer. Only when
             *   NO wrappers exist does a single legacy claude row appear.
             * Step 'model': a "no model" row, the model catalog, and the
             *   add row - reached only from a wrapper that accepts models.
             */
            const buildItems = () => {
                if (step === 'local-model') {
                    // A status row is NOT a selectable item: it is a
                    // sentence about why there is nothing to pick, and
                    // making it Enter-able would offer a launch that cannot
                    // happen. So the nav list is genuinely empty and the
                    // status is rendered separately.
                    if (!localProbe || localProbe === 'loading'
                        || localProbe.state !== 'reachable'
                        || !(localProbe.models || []).length) {
                        items = [];
                        return;
                    }
                    items = localProbe.models.map((m) => ({ type: 'local-model', model: m }));
                    return;
                }
                if (step === 'model') {
                    items = [{ type: 'no-model' }]
                        .concat(models.map((m) => ({ type: 'model', model: m })))
                        .concat([{ type: 'add' }]);
                    return;
                }
                if (!wrappers.length) {
                    items = [{ type: 'claude' }];
                    return;
                }
                // Grouped by family. The rules (one row per wrapper, no
                // duplicates, heading carried on a group's first row) live
                // in provider-groups.js, unit tested without a DOM.
                items = window.ProviderGroups.buildWrapperItems(wrappers, families);
            };

            const findIndexForModel = (model) => {
                if (step !== 'model') return 0;
                if (!model) return 0; // the "no model" row is always index 0
                const idx = items.findIndex((it) => it.type === 'model' && it.model === model);
                return idx >= 0 ? idx : 0; // fall back if it's gone (e.g. just removed)
            };

            /** Label used for type-ahead matching, per item type. */
            const itemLabel = (it) => {
                if (it.type === 'claude') return 'claude';
                if (it.type === 'family') return it.label;
                if (it.type === 'local-model') return it.model;
                if (it.type === 'wrapper') return it.label;
                if (it.type === 'model') return it.model;
                if (it.type === 'no-model') return 'no model';
                return '';
            };

            const setActive = (idx, { scroll = true } = {}) => {
                const els = listEl.querySelectorAll('.folder-picker-item');
                if (!els.length) { activeIndex = -1; return; }
                idx = Math.max(0, Math.min(idx, els.length - 1));
                els.forEach((el, i) => el.classList.toggle('folder-picker-item-active', i === idx));
                activeIndex = idx;
                const item = items[idx];
                if (item && (item.type === 'no-model' || item.type === 'model')) {
                    currentModel = item.type === 'no-model' ? '' : item.model;
                }
                if (scroll) els[idx].scrollIntoView({ block: 'nearest' });
            };

            /** Advance from the wrapper step into the model step. */
            const enterModelStep = (wrapper) => {
                selectedWrapper = wrapper;
                step = 'model';
                currentModel = readLastChoice();
                clearStatus();
                render();
                listEl.focus();
            };

            /**
             * Enter the LOCAL model step for a needs_model family.
             *
             * Renders a loading state immediately and fetches after, so the
             * modal never appears to hang on a box that is off. Every
             * outcome the probe returns - reachable, unreachable,
             * not-configured, or reachable-but-serving-nothing - gets its
             * own sentence rather than being collapsed into one empty list.
             */
            const enterLocalModelStep = (item) => {
                // `item` is whatever will be launched: a pinned family row
                // (agentType = the family name) or a WRAPPER in a
                // needs_model family (agentType = the wrapper id, so the
                // launch still goes through the user's own script). Both
                // shapes carry `agentType` and `label`; nothing below cares
                // which one it got.
                selectedFamily = item;
                step = 'local-model';
                localProbe = 'loading';
                clearStatus();
                render();
                listEl.focus();
                window.API.getLocalModels().then((res) => {
                    if (step !== 'local-model') return;
                    localProbe = res || { state: 'unreachable', models: [] };
                    render();
                }).catch((err) => {
                    if (step !== 'local-model') return;
                    // Failing to ASK is not the same as a box that said no,
                    // so it carries its own reason rather than being
                    // rendered as a bare unreachable with nothing to act on.
                    localProbe = {
                        state: 'unreachable',
                        models: [],
                        detail: (err && err.message) || 'the request failed',
                    };
                    render();
                });
            };

            /** Go back from the model step to the wrapper step. */
            const backToWrapperStep = () => {
                step = 'wrapper';
                selectedWrapper = null;
                selectedFamily = null;
                localProbe = null;
                addInputOpen = false;
                clearStatus();
                render();
                listEl.focus();
            };

            const activateItem = (idx) => {
                const item = items[idx];
                if (!item) return;
                if (item.type === 'add') {
                    openAddInput();
                    return;
                }
                if (item.type === 'wrapper') {
                    const wrapper = wrappers.find((w) => w.id === item.wrapperId);
                    if (wrapper && item.needsModel) {
                        // Ask the LOCAL box, not OpenRouter. Checked BEFORE
                        // accepts_model because both are true for `cldl` and
                        // only this one names the catalog - testing them the
                        // other way round is the bug this replaces.
                        enterLocalModelStep({
                            agentType: wrapper.id,
                            label: item.familyLabel || wrapper.label,
                        });
                        return;
                    }
                    if (wrapper && wrapper.accepts_model) {
                        enterModelStep(wrapper);
                        return;
                    }
                    // A wrapper that ignores models is launched straight
                    // away, with no model - never one carried over from a
                    // previous launch. Wrapper choice itself is per-launch,
                    // not remembered (localStorage here is model-only).
                    close({ model: null, wrapperId: item.wrapperId });
                    return;
                }
                if (item.type === 'local-model') {
                    close({
                        model: item.model,
                        agentType: selectedFamily ? selectedFamily.agentType : 'local',
                    });
                    return;
                }
                if (item.type === 'family') {
                    if (item.needsModel) {
                        // This family CANNOT launch bare - the server
                        // refuses it rather than downgrading - so Enter
                        // goes to the model step instead of offering a
                        // launch that is going to fail.
                        enterLocalModelStep(item);
                        return;
                    }
                    // A pinned family row: launched by FAMILY NAME as the
                    // agent_type, which the server resolves to that
                    // family's own command. Never a model - a reserved
                    // family runs one fixed command and does not take an
                    // OpenRouter model id, so one would arrive at the CLI
                    // as a prompt argument.
                    close({ model: null, agentType: item.agentType });
                    return;
                }
                if (item.type === 'claude') {
                    // No wrappers configured: the legacy fallback path.
                    close({ model: null });
                    return;
                }
                // Model step. Both branches carry the wrapper that was
                // chosen at step 1, so the model can only ever reach a
                // wrapper that declared it accepts one.
                const model = item.type === 'no-model' ? null : item.model;
                rememberChoice(model || '');
                close({ model, wrapperId: selectedWrapper ? selectedWrapper.id : undefined });
            };

            const render = () => {
                buildItems();
                if (headerEl) {
                    if (step === 'local-model') {
                        headerEl.textContent =
                            `» ${selectedFamily ? selectedFamily.label : 'local'}: select model`;
                    } else {
                        headerEl.textContent = step === 'model'
                            ? `» ${selectedWrapper ? selectedWrapper.label : 'wrapper'}: select model`
                            : '» select provider';
                    }
                }
                if (hintEl) {
                    hintEl.textContent = (step === 'model' || step === 'local-model')
                        ? '↑/↓ to move · Enter to launch · Esc to go back'
                        : '↑/↓ to move · Enter to launch · type to jump · Esc to cancel';
                }
                // The LOCAL step's non-reachable outcomes. Each renders its
                // OWN sentence: "not configured" and "unreachable" are
                // different facts and sending someone to check a machine
                // when they have simply never set the address is a waste of
                // their time. A reachable box serving nothing is a third
                // thing again, and is not an error at all.
                if (step === 'local-model' && !items.length) {
                    let msg;
                    if (!localProbe || localProbe === 'loading') {
                        msg = 'asking the LM Studio server for its models…';
                    } else if (localProbe.state === 'not-configured') {
                        msg = 'no LM Studio address is set. add '
                            + '"local_host": "host:port" under "providers" in '
                            + 'config.json, then restart.';
                    } else if (localProbe.state === 'unreachable') {
                        msg = `could not reach LM Studio at ${escapeHtml(localProbe.host || 'the configured address')}`
                            + (localProbe.detail ? ` (${escapeHtml(String(localProbe.detail))})` : '');
                    } else {
                        msg = `LM Studio at ${escapeHtml(localProbe.host || '')} is running but has no chat models loaded`;
                    }
                    listEl.innerHTML =
                        `<div class="provider-local-status">${msg}</div>`;
                    activeIndex = -1;
                    return;
                }
                listEl.innerHTML = items.map((item, i) => {
                    if (item.type === 'claude') {
                        return `<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">◆</span>
                            <span class="folder-picker-name provider-item-name">claude</span>
                        </div>`;
                    }
                    if (item.type === 'wrapper') {
                        const safeLabel = escapeHtml(item.label);
                        // Only a model-accepting wrapper advertises a
                        // second step; every other row launches on Enter.
                        const more = item.acceptsModel
                            ? '<span class="provider-item-more">models ›</span>'
                            : '';
                        // The family heading rides ahead of the group's
                        // first row and is NOT a .folder-picker-item, so it
                        // never enters the nav index.
                        const heading = item.groupLabel
                            ? `<div class="provider-family-heading">${escapeHtml(item.groupLabel)}</div>`
                            : '';
                        return `${heading}<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">»</span>
                            <span class="folder-picker-name provider-item-name">${safeLabel}</span>
                            ${more}
                        </div>`;
                    }
                    if (item.type === 'family') {
                        const safeLabel = escapeHtml(item.label);
                        const heading = item.groupLabel
                            ? `<div class="provider-family-heading">${escapeHtml(item.groupLabel)}</div>`
                            : '';
                        return `${heading}<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">◆</span>
                            <span class="folder-picker-name provider-item-name">${safeLabel}</span>
                        </div>`;
                    }
                    if (item.type === 'local-model') {
                        const safe = escapeHtml(item.model);
                        return `<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">◇</span>
                            <span class="folder-picker-name provider-item-name">${safe}</span>
                        </div>`;
                    }
                    if (item.type === 'no-model') {
                        return `<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">◆</span>
                            <span class="folder-picker-name provider-item-name">no model (wrapper default)</span>
                        </div>`;
                    }
                    if (item.type === 'model') {
                        const safe = escapeHtml(item.model);
                        return `<div class="folder-picker-item" data-index="${i}">
                            <span class="folder-picker-icon">◇</span>
                            <span class="folder-picker-name provider-item-name">${safe}</span>
                            <button type="button" class="provider-item-remove" data-remove="${safe}" title="remove ${safe}" aria-label="remove ${safe}">×</button>
                        </div>`;
                    }
                    return `<div class="folder-picker-item provider-add-row" data-index="${i}">
                        <span class="folder-picker-icon">+</span>
                        <span class="folder-picker-name provider-item-name">add model</span>
                    </div>`;
                }).join('');

                listEl.querySelectorAll('.folder-picker-item').forEach((el) => {
                    const idx = parseInt(el.dataset.index, 10);
                    el.addEventListener('mousemove', () => {
                        if (idx !== activeIndex) setActive(idx, { scroll: false });
                    });
                    el.addEventListener('click', (e) => {
                        if (e.target.closest('.provider-item-remove')) return;
                        setActive(idx);
                        activateItem(idx);
                    });
                });
                listEl.querySelectorAll('.provider-item-remove').forEach((btn) => {
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        removeModel(btn.dataset.remove);
                    });
                });

                setActive(findIndexForModel(currentModel), { scroll: false });
            };

            const removeModel = async (model) => {
                confirmPending = true;
                const ok = await window.Launchpad.showConfirmModal(
                    'remove model',
                    `remove "${model}" from the provider list?`,
                    null,
                    'remove',
                    'cancel'
                );
                confirmPending = false;
                if (!ok) return;
                try {
                    const data = await window.API.removeProviderModel(model);
                    models = Array.isArray(data.models) ? data.models : models.filter((m) => m !== model);
                    clearStatus();
                    render();
                    listEl.focus();
                } catch (error) {
                    showStatus(error.message || String(error), 'error');
                }
            };

            const openAddInput = () => {
                if (addInputOpen) return;
                addInputOpen = true;
                clearStatus();
                const addRowEl = listEl.querySelector('.provider-add-row');
                if (!addRowEl) { addInputOpen = false; return; }
                addRowEl.innerHTML = `
                    <input type="text" class="provider-add-input" id="provider-add-input"
                           spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off"
                           aria-label="new model id" placeholder="provider/model-id">
                `;
                const input = addRowEl.querySelector('#provider-add-input');
                input.addEventListener('click', (e) => e.stopPropagation());
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        submitAdd(input.value.trim());
                    }
                    // Escape is handled by the shared capture-phase listener
                    // below (closes just this input, not the whole modal).
                });
                setTimeout(() => input.focus(), 0);
            };

            const closeAddInput = () => {
                if (!addInputOpen) return;
                addInputOpen = false;
                clearStatus();
                render();
                listEl.focus();
            };

            const submitAdd = async (value) => {
                if (!value) return;
                if (!MODEL_ID_RE.test(value)) {
                    showStatus('invalid model id - use letters, numbers, . _ ~ / -', 'error');
                    return;
                }
                try {
                    const data = await window.API.addProviderModel(value);
                    models = Array.isArray(data.models) ? data.models : models.concat([value]);
                    addInputOpen = false;
                    clearStatus();
                    render();
                    listEl.focus();
                } catch (error) {
                    if (error && error.status === 409) {
                        showStatus(`"${value}" is already in the list`, 'error');
                    } else if (error && error.status === 400) {
                        showStatus(error.message || 'invalid model id', 'error');
                    } else {
                        showStatus(error.message || String(error), 'error');
                    }
                }
            };

            // Modal-wide key handling (capture phase so it works no matter
            // which child holds focus, and reliably fires on Escape even
            // when nothing inside the overlay has DOM focus - the bug in
            // showConfirmModal's overlay-scoped listener). Removed on close.
            const onKeyDown = (e) => {
                // A confirm sub-modal (remove) is open on top of us - let
                // IT own every key until it resolves, so its own Escape/
                // Enter don't also get intercepted here.
                if (confirmPending) return;

                if (e.key === 'Escape') {
                    e.preventDefault();
                    if (addInputOpen) { closeAddInput(); return; }
                    // In the model step, Escape backs out one level rather
                    // than abandoning the launch - the wrapper choice was
                    // a deliberate step, not a modal the user fell into.
                    if (step === 'model' || step === 'local-model') {
                        backToWrapperStep();
                        return;
                    }
                    close(null);
                    return;
                }

                // While typing a new model id, let the input own its keystrokes.
                if (document.activeElement && document.activeElement.id === 'provider-add-input') return;

                const ae = document.activeElement;
                if (ae && ae.tagName === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) return;

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    setActive(activeIndex < 0 ? 0 : activeIndex + 1);
                    return;
                }
                if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    setActive(activeIndex < 0 ? items.length - 1 : activeIndex - 1);
                    return;
                }
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (activeIndex >= 0) activateItem(activeIndex);
                    return;
                }

                // Type-ahead: printable single chars only, no modifier combos.
                if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
                    e.preventDefault();
                    typeBuffer += e.key.toLowerCase();
                    if (typeTimer) clearTimeout(typeTimer);
                    typeTimer = setTimeout(() => { typeBuffer = ''; }, 800);
                    const matchIdx = items.findIndex((it) => {
                        const label = itemLabel(it);
                        return label && label.toLowerCase().startsWith(typeBuffer);
                    });
                    if (matchIdx >= 0) setActive(matchIdx);
                }
            };
            document.addEventListener('keydown', onKeyDown, true);

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) close(null);
            });

            // Load the model list, then render. Default focus goes to the
            // LIST (not any input) so Enter re-launches the pre-selected
            // choice with zero clicks.
            (async () => {
                try {
                    const data = await window.API.getProviders();
                    models = Array.isArray(data.models) ? data.models : [];
                } catch (error) {
                    console.error('Launchpad: Failed to load providers:', error);
                    models = [];
                    showStatus('could not load saved models - showing claude only', 'error');
                }
                // Best-effort: a failure here leaves `wrappers` empty,
                // which renders the single legacy claude row rather than
                // blocking the launch gate. No id is filtered out - a
                // wrapper the user configured must appear exactly once,
                // including one they chose to call "claude".
                try {
                    const wrapperData = await window.API.listWrappers();
                    wrappers = Array.isArray(wrapperData.wrappers) ? wrapperData.wrappers : [];
                    families = Array.isArray(wrapperData.families) ? wrapperData.families : [];
                } catch (error) {
                    console.error('Launchpad: Failed to load wrappers:', error);
                    wrappers = [];
                    families = [];
                }
                render();
                setTimeout(() => listEl.focus(), 100);
            })();
        });
    }

    window.Launchpad.showProviderModal = showProviderModal;
})();
