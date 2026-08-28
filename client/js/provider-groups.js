/**
 * ProviderGroups - pure grouping helpers for the launch picker's wrapper
 * step (feat/universal-wrappers).
 *
 * Split out of client/js/providers.js to keep that file inside the repo's
 * 500-line budget, and because these are the rules most worth testing
 * without a DOM: exactly one row per wrapper, no duplicates, the default
 * badged, and a family heading only where one is actually useful.
 *
 * Loaded as a plain script (window.ProviderGroups) and also importable by
 * the node test suite, which reads the file and evaluates it against a
 * stub window - the same pattern the other node suites in tests/ use.
 */
(function (root) {
    'use strict';

    /**
     * The family a wrapper belongs to, tolerating a wrapper stored before
     * the field existed (an unmigrated or hand-edited config.json).
     * @param {object} w wrapper object from the API
     * @returns {string} family name, defaulting to 'claude'
     */
    function wrapperFamily(w) {
        return (w && w.family) || 'claude';
    }

    /**
     * Family names in registry order, with any family present in the
     * wrapper data but absent from the registry appended rather than
     * dropped - a wrapper must never become unreachable because the
     * server did not name its family.
     * @param {Array} wrappers configured wrappers
     * @param {Array} families family summaries from the API
     * @returns {string[]} ordered family names
     */
    function familyOrder(wrappers, families) {
        var names = (families || []).map(function (f) { return f.name; });
        (wrappers || []).forEach(function (w) {
            var name = wrapperFamily(w);
            if (names.indexOf(name) === -1) names.push(name);
        });
        return names;
    }

    /**
     * Display label for a family name.
     * @param {string} name family name
     * @param {Array} families family summaries from the API
     * @returns {string} the registry's label, or the name itself
     */
    function familyLabel(name, families) {
        var match = (families || []).filter(function (f) { return f.name === name; })[0];
        return (match && match.label) || name;
    }

    /**
     * Whether a family may be offered as a PINNED, modelless row when it
     * has no wrappers of its own.
     *
     * A missing `pickable` reads as FALSE, deliberately. An older server
     * does not ship the field, and a client talking to one must behave
     * exactly as it did before rather than start offering rows whose
     * launch behaviour it cannot predict.
     * @param {string} name family name
     * @param {Array} families family summaries from the API
     * @returns {boolean}
     */
    function familyPickable(name, families) {
        var match = (families || []).filter(function (f) { return f.name === name; })[0];
        return !!(match && match.pickable);
    }

    /**
     * Whether a family CANNOT be launched without a model id.
     *
     * True for `local` (LM Studio): `cldl` addresses one specific model and
     * has no meaningful default, so the server REFUSES a bare launch rather
     * than downgrading it. A row for such a family must therefore lead to a
     * model step instead of launching on Enter - otherwise the picker
     * offers an action the server will reject.
     *
     * Missing reads as false, matching familyPickable: an older server does
     * not ship the field.
     * @param {string} name family name
     * @param {Array} families family summaries from the API
     * @returns {boolean}
     */
    function familyNeedsModel(name, families) {
        var match = (families || []).filter(function (f) { return f.name === name; })[0];
        return !!(match && match.needs_model);
    }

    /**
     * Build the picker's wrapper-step nav items, grouped by family.
     *
     * EXACTLY one item per wrapper, in family order. The family heading is
     * carried ON the first item of each group (`groupLabel`) rather than
     * being an item of its own, so the list stays a pure sequence of
     * selectable things and every index/arrow/type-ahead path in
     * providers.js is unchanged. A single-family install (every install
     * today) gets no headings at all, since one heading over the whole
     * list is noise.
     * @param {Array} wrappers configured wrappers, all families
     * @param {Array} families family summaries from the API
     * @returns {Array} nav items of type 'wrapper'
     */
    function buildWrapperItems(wrappers, families) {
        var order = familyOrder(wrappers, families);
        var hasWrappers = function (name) {
            return (wrappers || []).some(function (w) { return wrapperFamily(w) === name; });
        };
        // A group appears if it has wrappers, OR it is a pickable family
        // with none - in which case it contributes ONE pinned row. Before
        // this, a reserved family was launchable over the API
        // (agent_type: "codex") but unreachable from the picker unless the
        // user first authored a wrapper for it, so the two surfaces
        // disagreed about what could be launched.
        var groupsPresent = order.filter(function (name) {
            return hasWrappers(name) || familyPickable(name, families);
        });
        var multi = groupsPresent.length > 1;
        var items = [];
        groupsPresent.forEach(function (name) {
            var heading = multi ? familyLabel(name, families) : null;
            var inFamily = (wrappers || []).filter(function (w) { return wrapperFamily(w) === name; });
            if (!inFamily.length) {
                // Pinned family row. No model step: a reserved family runs
                // one fixed command and does not take an OpenRouter model
                // id, exactly as AgentWrapper.accepts_model=false means for
                // a wrapper. Handing one over would arrive at the CLI as a
                // prompt argument.
                var needsModel = familyNeedsModel(name, families);
                items.push({
                    type: 'family',
                    agentType: name,
                    label: familyLabel(name, families),
                    // A needs_model family advertises the model step, so the
                    // row reads "models >" and Enter goes there rather than
                    // launching something the server will refuse.
                    acceptsModel: needsModel,
                    needsModel: needsModel,
                    groupLabel: heading,
                });
                return;
            }
            // A wrapper inherits its FAMILY's needs_model. `cldl` is a
            // wrapper in the `local` family, so the pinned-family branch
            // above never runs for it - and without this the picker asked
            // only `accepts_model`, which is true for both `cldor` and
            // `cldl` and says nothing about WHICH catalog to open. That
            // sent every LM Studio launch to the OpenRouter list.
            // accepts_model = "takes a model id"; needsModel = "the id has
            // to come from the local box". Two questions, both required.
            var famNeedsModel = familyNeedsModel(name, families);
            inFamily.forEach(function (w, i) {
                items.push({
                    type: 'wrapper',
                    wrapperId: w.id,
                    label: w.default ? (w.label + ' (default)') : w.label,
                    acceptsModel: !!w.accepts_model,
                    needsModel: famNeedsModel,
                    familyLabel: familyLabel(name, families),
                    groupLabel: (i === 0) ? heading : null,
                });
            });
        });
        return items;
    }

    root.ProviderGroups = {
        wrapperFamily: wrapperFamily,
        familyOrder: familyOrder,
        familyLabel: familyLabel,
        familyPickable: familyPickable,
        familyNeedsModel: familyNeedsModel,
        buildWrapperItems: buildWrapperItems,
    };
})(typeof window !== 'undefined' ? window : globalThis);
