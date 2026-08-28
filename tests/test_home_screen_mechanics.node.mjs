// Node test for the home-screen mechanics round (feat/home-screen-mechanics):
// the fold/unfold fix, the slimmed project row, the renamed projects
// section, the header help control, and the restructured add menu.
//
// WHAT THIS ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. Every assertion
// below reads either the actual HTML string a render function wrote, the
// actual state of a DOM node a handler touched, or the actual text of the
// shipped stylesheet - never a state object produced along the way. That
// rule exists here for a specific reason: this project shipped a feature
// with 282 green state assertions that rendered zero pixels.
//
// REAL PIXELS ARE MEASURED ELSEWHERE, NOT SIMULATED HERE. This repo has
// no bundled layout engine (no jsdom, no package.json), so a Node process
// cannot compute a box. scripts/verify_home_mechanics.py drives
// tests/manual/home-mechanics-geometry-harness.html in a REAL headless
// Chromium at 430x900 and measures getBoundingClientRect() and painted
// PIXELS. Verified numbers, 2026-08-19, fixture as shipped in that
// harness, viewport asserted from window.innerWidth == 430:
//
//   expanded  .project-node[cloudecode] height=181.16
//             .project-node__sessions height=66.00
//             children cloude_b h=31.00 top=453.16 bottom=484.16 insideParent=true
//                      cloude_a h=31.00 top=488.16 bottom=519.16 insideParent=true
//             .project-description height=20.16
//   collapsed .project-node__sessions height=0, every child height=0,
//             .project-description height=0, node height=87.00 (< 181.16)
//   after a re-render, still 0 / 0 / aria-expanded="false"
//   re-expanded back to 66.00 with both children insideParent=true
//   fill measured by sampling one painted pixel over a black page and
//   again over a white one: 81.2% (codex), 81.6% (legacy_windows),
//   81.6% (dracula) - so ~18.5% of the animation still shows through
//   help control 20x20 at right=418 in a 430-wide header; the panel
//   measures 0 height closed, 1377.77 open
//
// CORRECTION 2026-08-24: the line above used to read "the panel is still
// the FIRST child of .launchpad-container". That stopped being true at
// 28d698b, which put the attribution-prompt slot ahead of it. The claim
// that matters was never "first child", it was "nothing above it adds
// height", and the empty slot measures 0x0 with display:none - verified
// in pixels by scripts/verify_attribution_prompt.py::measure_none.
//
// Run with: node tests/test_home_screen_mechanics.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {
    ROOT, LAUNCHPAD_SRC, STYLES, INDEX,
    test, results, ruleBody, el, loadLaunchpad, renderProjects,
} from './lib-home-mechanics.mjs';

// =====================================================================
// ITEM 38 - the fold actually folds, and it folds the right element.
// =====================================================================

await test('ITEM 38: the fold resolves its target from the NODE, not from the toggle sibling', async () => {
    // The exact shape the real renderer produces for a project node: the
    // toggle is nested inside `.project-node__row`, so its next sibling
    // is the `.project-item` card. The old handler walked to that sibling,
    // found the wrong class, and silently changed nothing.
    const sessions = el('project-node__sessions');
    const description = el('project-description');
    const card = el('project-item', { children: [description] });
    const toggle = el('project-node__toggle', { attrs: { 'aria-expanded': 'true', 'data-node-key': 'project:p' } });
    const row = el('project-node__row', { children: [toggle, card] });
    const node = el('project-node', { children: [row, sessions] });

    const { lp } = loadLaunchpad();
    const ok = lp._applyProjectNodeCollapsed(toggle, true);
    assert.equal(ok, true, 'the handler must report that it found a node');
    assert.equal(sessions.style.display, 'none', 'the sessions container must actually be hidden');
    assert.equal(description.style.display, 'none', 'the description must actually be hidden');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');

    lp._applyProjectNodeCollapsed(toggle, false);
    assert.equal(sessions.style.display, '', 'unfolding must actually restore the sessions');
    assert.equal(description.style.display, '', 'unfolding must actually restore the description');
    assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(node.classes.includes('project-node'), true);
});

await test('ITEM 38: a toggle outside any project node reports FAILURE, it does not pretend to have folded', async () => {
    const orphan = el('project-node__toggle', { attrs: { 'aria-expanded': 'true' } });
    const { lp } = loadLaunchpad();
    assert.equal(lp._applyProjectNodeCollapsed(orphan, true), false,
        'three outcomes: "I could not find the node" is not the same as "I folded it"');
});

await test('ITEM 38: the fold does not depend on element ORDER inside the node', async () => {
    // Same parts, sessions container FIRST. A sibling-walk implementation
    // is order-sensitive; addressing from the node root is not.
    const sessions = el('project-node__sessions');
    const toggle = el('project-node__toggle', { attrs: { 'aria-expanded': 'true' } });
    const row = el('project-node__row', { children: [el('project-item'), toggle] });
    el('project-node', { children: [sessions, row] });
    const { lp } = loadLaunchpad();
    lp._applyProjectNodeCollapsed(toggle, true);
    assert.equal(sessions.style.display, 'none');
});

await test('ITEM 38: collapse state is re-applied on every render, for BOTH foldable parts', async () => {
    const fixture = {
        projects: [{ id: 1, name: 'proj', path: '/p', description: 'a description' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [{ name: 'cloude_a', created_by_cloude: true, created_at_epoch: 1, window_count: 1, status: 'idle' }],
        attribution: [{ tmux_name: 'cloude_a', project_id: 1, project_attribution: 'derived_deepest' }],
    };
    const first = renderProjects(fixture);
    assert.ok(first.html.includes('aria-expanded="true"'));
    assert.ok(!/class="project-description" style="display:none;"/.test(first.html));

    first.lp._collapsedProjectNodes.add('project:proj');
    first.lp.renderProjectList();
    const html = first.projectList.innerHTML;
    assert.ok(html.includes('aria-expanded="false"'), 'the toggle must render collapsed');
    assert.ok(/project-node__sessions[^>]*style="display:none;"/.test(html),
        'the sessions container must render collapsed');
    assert.ok(/class="project-description" style="display:none;"/.test(html),
        'the description must render collapsed too, not spring back open');
});

// =====================================================================
// ITEM 43 - slim rows.
// =====================================================================

await test('ITEM 43: an empty description renders NOTHING, not the words "no description"', async () => {
    const { html } = renderProjects({
        projects: [{ id: 1, name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
    });
    assert.ok(!html.includes('no description'),
        'the filler line is a full row of type that says nothing');
    assert.ok(!html.includes('class="project-description"'),
        'and no empty element is left behind to keep costing height');
});

await test('ITEM 43: a whitespace-only description counts as empty', async () => {
    const { html } = renderProjects({
        projects: [{ id: 1, name: 'proj', path: '/p', description: '   ' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
    });
    assert.ok(!html.includes('class="project-description"'));
});

await test('ITEM 43: a project with a description and no sessions is still foldable', async () => {
    const { html } = renderProjects({
        projects: [{ id: 1, name: 'proj', path: '/p', description: 'something to fold' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
    });
    assert.ok(html.includes('project-node__toggle'), 'it has something to fold, so it gets a control');
    assert.ok(!html.includes('project-node__count'),
        'but no count chip: a bare "0" would be a claim about sessions');
});

await test('ITEM 43: a project with nothing to fold gets no fold control at all', async () => {
    const { html } = renderProjects({
        projects: [{ id: 1, name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
    });
    assert.ok(!html.includes('project-node__toggle'));
});

await test('a description is HTML-escaped on the way into the row', async () => {
    const { html } = renderProjects({
        projects: [{ id: 1, name: 'proj', path: '/p', description: '<img src=x onerror=1>' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
    });
    assert.ok(!html.includes('<img src=x'), 'a description is user text and must not reach the DOM as markup');
    assert.ok(html.includes('&lt;img src=x'));
});

// =====================================================================
// ITEM 42 - it is the project list, not a recency list.
// =====================================================================

await test('ITEM 42: the section is called "projects"', async () => {
    const start = LAUNCHPAD_SRC.indexOf('id="projects-section-toggle"');
    assert.ok(start !== -1, 'expected the projects section toggle');
    const chunk = LAUNCHPAD_SRC.slice(start, start + 400);
    assert.ok(/>\s*projects\s*</.test(chunk), 'the heading text must read "projects"');
    assert.ok(!/recent projects/.test(chunk), 'and must not still say "recent projects"');
});

// =====================================================================
// ITEM 48 - the help CONTROL moves; the help PANEL does not.
// =====================================================================

// SUPERSEDED BY ITEM 61b, and rewritten rather than deleted so the
// history stays readable. This assertion used to require the help button
// to be the LAST child of `.controls`, i.e. the top-right corner of the
// header. The user revised that instruction: he wants the control beside
// the centred title instead. The panel it toggles is unchanged and is
// still asserted below, which was always the substantive half of item 48.
//
// The right-hand cluster is now explicitly asserted NOT to contain it,
// because putting it back there is not merely a style regression: the
// `.header--home` flank arithmetic sizes `#header-home-spacer` to mirror
// `.controls`, and that token budgets TWO inline controls, so a third one
// there pushes the title off centre.
await test('ITEM 61b: the help control sits beside the title, not in the controls cluster', async () => {
    const btnIdx = INDEX.indexOf('id="launchpad-help-btn"');
    assert.ok(btnIdx !== -1, 'the help button is gone entirely');

    const h1 = INDEX.match(/<h1 id="appTitle">[\s\S]*?<\/h1>/);
    assert.ok(h1, '#appTitle is gone');
    assert.ok(h1[0].includes('id="launchpad-help-btn"'),
        'the help button must be a child of #appTitle so it rides the title centring');
    assert.ok(h1[0].indexOf('id="header-title-text"') < h1[0].indexOf('id="launchpad-help-btn"'),
        'and must come after the title text, i.e. to its right');

    const controls = INDEX.match(/<div class="controls">[\s\S]*?\n        <\/div>/);
    assert.ok(controls, '.controls block is gone');
    assert.ok(!controls[0].includes('id="launchpad-help-btn"'),
        'the help button must NOT be back in the right-hand controls cluster');
});

await test('ITEM 48: nothing above the help panel may add HEIGHT to the launchpad container', async () => {
    // WHAT THIS ASSERTS AND WHY IT CHANGED. It used to demand that the
    // <details class="adopt-disclosure"> be the literally first tag inside
    // .launchpad-container. That was a MARKUP PROXY for the thing that
    // actually matters, which is that nothing above the help panel pushes
    // the home screen down. The proxy broke on a legitimate change:
    // 28d698b added <div id="attribution-prompt"> as the first child, a
    // slot that is EMPTY in the healthy case and carries
    // `.attribution-prompt-slot:empty { display: none }`, so it costs
    // exactly zero height. The old assertion failed on an app that was
    // correct, which is the same defect class as a false green pointed
    // the other way.
    // So: only elements on this allow-list may precede the disclosure,
    // and each one must be able to PROVE it collapses when empty. The
    // rendered-pixel half of this claim is measured, not inferred, by
    // scripts/verify_attribution_prompt.py::measure_none, which reads
    // the empty slot's getBoundingClientRect() and requires a 0x0 box.
    const ZERO_COST_SLOTS = [
        { cls: 'attribution-prompt-slot', css: 'client/css/attribution-prompt.css' },
    ];
    const containerTag = '<div class="launchpad-container">';
    const containerIdx = LAUNCHPAD_SRC.indexOf(containerTag);
    const detailsIdx = LAUNCHPAD_SRC.indexOf('<details class="adopt-disclosure">', containerIdx);
    assert.ok(detailsIdx !== -1, 'the disclosure must still be rendered by launchpad.js');
    const between = LAUNCHPAD_SRC
        .slice(containerIdx + containerTag.length, detailsIdx)
        .replace(/<!--[\s\S]*?-->/g, '');
    const tags = [...between.matchAll(/<([a-zA-Z][\w-]*)([^>]*)>/g)];
    for (const [, tag, attrs] of tags) {
        const slot = ZERO_COST_SLOTS.find((s) => attrs.includes(s.cls));
        assert.ok(slot,
            `<${tag}> renders between .launchpad-container and the help panel and is `
            + 'not a declared zero-cost slot. Anything here pushes the whole home '
            + 'screen down. Add it to ZERO_COST_SLOTS with a :empty collapse rule, '
            + 'or move it below the disclosure.');
        const css = fs.readFileSync(path.join(ROOT, slot.css), 'utf8');
        const rule = new RegExp(
            `\\.${slot.cls}:empty\\s*\\{[^}]*display:\\s*none`, 'm');
        assert.match(css, rule,
            `.${slot.cls} sits above the help panel, so it must collapse when empty - `
            + `expected a ".${slot.cls}:empty { display: none }" rule in ${slot.css}`);
    }
});

await test('ITEM 48: exactly ONE help control - the in-pane summary is taken out of the layout', async () => {
    const body = ruleBody(STYLES, '#launchpad-screen .adopt-disclosure > summary');
    assert.match(body, /display:\s*none/,
        'the summary stays in the markup (it is what makes it a disclosure) but must not paint');
    const hidden = ruleBody(STYLES, '#launchpad-help-btn');
    assert.match(hidden, /display:\s*none/, 'the header control is off by default');
    const shown = ruleBody(STYLES, '.header--home #launchpad-help-btn');
    assert.match(shown, /display:\s*inline-flex/,
        'and turned on by the home-screen header class, so no screen-switch code has to remember it');
});

await test('ITEM 48: the header control is wired to the same disclosure, not to a copy', async () => {
    assert.ok(LAUNCHPAD_SRC.includes('bindHeaderHelpToggle()'),
        'init() must wire the control');
    const start = LAUNCHPAD_SRC.indexOf('bindHeaderHelpToggle() {');
    const body = LAUNCHPAD_SRC.slice(start, start + 900);
    assert.ok(body.includes("querySelector('#launchpad-screen .adopt-disclosure')"),
        'it must resolve the live disclosure at click time, because renderLaunchpadUI replaces it');
    assert.ok(body.includes('details.open = next'), 'and toggle that element, not a clone of it');
});

await test('ITEM 48: a missing header control is reported, not silently ignored', async () => {
    const { lp } = loadLaunchpad();
    assert.equal(lp.bindHeaderHelpToggle(), false,
        'three outcomes: "the control was not there" is its own answer');
});

// =====================================================================
// ITEMS 51/52/53 - the add menu.
// =====================================================================

await test('ITEMS 51/52/53: the menu items, in order, as rendered', async () => {
    const menuStart = LAUNCHPAD_SRC.indexOf('<div class="new-fab__menu"');
    const menuEnd = LAUNCHPAD_SRC.indexOf('</div>', LAUNCHPAD_SRC.indexOf('new-console', menuStart));
    // Comments are stripped first: prose ABOUT an old label is not the
    // old label, and a check that cannot tell them apart is not a check.
    const menu = LAUNCHPAD_SRC.slice(menuStart, menuEnd).replace(/<!--[\s\S]*?-->/g, '');
    const actions = [...menu.matchAll(/data-action="([a-z-]+)"/g)].map((m) => m[1]);
    const labels = [...menu.matchAll(/class="new-fab__label">([^<]+)</g)].map((m) => m[1].trim());
    assert.deepEqual(actions.slice(0, 2), ['new-claude-project', 'new-session']);
    assert.deepEqual(labels.slice(0, 2), ['new claude project', 'new session']);
    assert.ok(!actions.includes('clone-github'),
        'ITEM 53: clone from github is an option inside the new-project flow, not a peer of it');
    assert.ok(!menu.includes('create new project'),
        'ITEM 51: the old unexplained name must be gone');
});

await test('ITEM 51: the top item uses the real app icon FILE, and no mark is redrawn', async () => {
    const start = LAUNCHPAD_SRC.indexOf('data-action="new-claude-project"');
    const item = LAUNCHPAD_SRC.slice(start, LAUNCHPAD_SRC.indexOf('</button>', start));
    assert.ok(item.includes('/static/assets/icons/header-icon.png'),
        'it must point at the shipped asset the header already uses');
    assert.ok(!/<path\s/.test(item), 'and must not contain a hand-drawn path');
    assert.ok(fs.existsSync(path.join(ROOT, 'client', 'assets', 'icons', 'header-icon.png')),
        'the asset it points at has to exist');
    assert.ok(fs.existsSync(path.join(ROOT, 'client', 'assets', 'icons', 'header-icon@2x.png')));
});

await test('ITEM 53: clone from github is reachable from INSIDE the new claude project flow', async () => {
    const start = LAUNCHPAD_SRC.indexOf('async startNewClaudeProject() {');
    assert.ok(start !== -1);
    const body = LAUNCHPAD_SRC.slice(start, LAUNCHPAD_SRC.indexOf('\n    }', start));
    assert.ok(body.includes("key: 'clone'") && body.includes('showCloneFromGithubModal()'),
        'the clone flow must be an option of this one, routed into the existing handler');
    assert.ok(body.includes('createNewSession()'), 'and "start empty" into the existing create flow');
});

await test('ITEM 52: with ZERO projects, new session says so instead of opening an empty picker', async () => {
    const { lp } = loadLaunchpad();
    lp.projects = [];
    lp.projectsListingOk = true;
    let opts = null;
    lp._showChoiceModal = async (o) => { opts = o; return null; };
    let selected = false;
    lp.selectProject = async () => { selected = true; };
    await lp.startSessionInExistingProject();
    assert.ok(opts, 'something must be shown');
    assert.equal(opts.items.length, 0, 'there is nothing to pick from');
    assert.match(opts.emptyMessage, /no claude projects yet/i);
    assert.equal(selected, false, 'and nothing may be launched');
});

await test('ITEM 52: a FAILED project fetch is never reported as "you have no projects"', async () => {
    const { lp } = loadLaunchpad();
    lp.projects = [];
    lp.projectsListingOk = false;
    let opts = null;
    lp._showChoiceModal = async (o) => { opts = o; return null; };
    await lp.startSessionInExistingProject();
    assert.match(opts.emptyMessage, /CANNOT DETERMINE/,
        'an unread list is a third outcome, not an empty one');
    assert.equal(opts.emptyKind, 'unknown');
    assert.ok(!/no claude projects yet/i.test(opts.emptyMessage));
});

await test('a FAILED GET /projects latches the listing as unread, so nothing can call it empty', async () => {
    const { lp, win } = loadLaunchpad();
    assert.equal(lp.projectsListingOk, null,
        'it starts as "never asked", which is neither "read" nor "failed"');
    win.API.getProjects = async () => { throw new Error('simulated fetch failure'); };
    lp.loadProjectPresence = async () => {};
    lp.loadProjectAuthority = async () => {};
    lp.loadRunningSessions = async () => {};
    lp.loadRecentSessions = async () => {};
    lp.renderProjectList = () => {};
    lp.showError = () => {};
    await lp.loadProjects();
    assert.equal(lp.projectsListingOk, false,
        'a failed fetch has to be recorded, or an empty list reads as a measured answer');

    // And the honest path still latches true.
    const second = loadLaunchpad();
    second.win.API.getProjects = async () => [{ name: 'p', path: '/p' }];
    second.lp.loadProjectPresence = async () => {};
    second.lp.loadProjectAuthority = async () => {};
    second.lp.loadRunningSessions = async () => {};
    second.lp.loadRecentSessions = async () => {};
    second.lp.renderProjectList = () => {};
    await second.lp.loadProjects();
    assert.equal(second.lp.projectsListingOk, true);
});

await test('ITEM 52: new session offers existing projects and refuses the unusable ones', async () => {
    const { lp } = loadLaunchpad();
    lp.projects = [
        { name: 'good', path: '/good' },
        { name: 'gone', path: '/gone' },
        { name: 'unknown', path: '/unknown' },
    ];
    lp.projectsListingOk = true;
    lp.projectPresence = new Map([
        ['/good', { id: 1, raw_path: '/good', presence: 'present' }],
        ['/gone', { id: 2, raw_path: '/gone', presence: 'missing' }],
        ['/unknown', { id: 3, raw_path: '/unknown', presence: 'unreachable', presence_detail: 'volume asleep' }],
    ]);
    let opts = null;
    lp._showChoiceModal = async (o) => { opts = o; return 'good'; };
    let opened = null;
    lp.selectProject = async (p) => { opened = p; };
    await lp.startSessionInExistingProject();
    assert.equal(opts.items.length, 3, 'every project stays VISIBLE, including the broken ones');
    const byKey = Object.fromEntries(opts.items.map((i) => [i.key, i]));
    assert.equal(byKey.good.disabled, false);
    assert.equal(byKey.gone.disabled, true);
    assert.match(byKey.gone.reason, /MISSING/);
    assert.equal(byKey.unknown.disabled, true);
    assert.match(byKey.unknown.reason, /CANNOT DETERMINE - volume asleep/);
    assert.equal(opened.name, 'good', 'choosing a usable one opens it');
});

await test('ITEM 52: new session NEVER creates a project', async () => {
    const start = LAUNCHPAD_SRC.indexOf('async startSessionInExistingProject() {');
    const body = LAUNCHPAD_SRC.slice(start, LAUNCHPAD_SRC.indexOf('\n    }\n', start));
    assert.ok(!body.includes('createNewSession(') && !body.includes('showProjectNameModal('),
        'it adds a session to an existing project and nothing else');
});

await test('the choice modal renders its rows and its empty state as real markup', async () => {
    const { lp, body } = loadLaunchpad();
    lp._showChoiceModal({
        title: 'new claude project',
        items: [
            { key: 'empty', label: 'start empty', sub: 'a fresh working folder' },
            { key: 'clone', label: 'clone from github', sub: 'from an existing repository' },
        ],
    });
    const overlay = body.children[body.children.length - 1];
    assert.ok(overlay.innerHTML.includes('start empty'));
    assert.ok(overlay.innerHTML.includes('clone from github'));
    assert.ok(overlay.innerHTML.includes('data-choice-index="0"'));
    assert.ok(overlay.innerHTML.includes('folder-picker-item'));

    lp._showChoiceModal({ title: 'new session', items: [], emptyMessage: 'nothing here', emptyKind: 'unknown' });
    const empty = body.children[body.children.length - 1];
    assert.ok(empty.innerHTML.includes('folder-picker-empty--unknown'));
    assert.ok(empty.innerHTML.includes('nothing here'));
    assert.ok(!empty.innerHTML.includes('folder-picker-item'), 'no rows may be drawn when there are none');
});

// =====================================================================
// ITEM 37 - one colour per edge.
// =====================================================================

await test('ITEM 37: the project card declares ONE uniform border and no border-left override', async () => {
    const body = ruleBody(STYLES, '.project-item');
    assert.match(body, /border:\s*1px solid var\(--color-border\)/);
    assert.ok(!/border-left:/.test(body),
        'a 3px left border against a 1px ring is what miters into the two-tone corner bleed');
    assert.match(body, /box-shadow:\s*inset 3px 0 0 var\(--color-accent\)/,
        'the accent rail survives as an inset shadow, which is clipped by the radius rather than mitered');
});

await test('ITEM 37: the presence states move their rail onto the same shadow, keeping two DIFFERENT colours', async () => {
    const missing = ruleBody(STYLES, '.project-item.project-presence-missing');
    const unreachable = ruleBody(STYLES, '.project-item.project-presence-unreachable');
    assert.match(missing, /box-shadow:\s*inset 3px 0 0 var\(--color-danger\)/);
    assert.match(unreachable, /box-shadow:\s*inset 3px 0 0 var\(--color-warning\)/);
    assert.ok(!/border-left-color/.test(missing) && !/border-left-color/.test(unreachable),
        'nothing may still be painting the old border rail');
    assert.notEqual(missing, unreachable, 'MISSING and CANNOT DETERMINE must never look the same');
});

await test('ITEM 37: hover re-declares the rail, because box-shadow is one property and not a list', async () => {
    const hover = ruleBody(STYLES, '.project-item:hover');
    assert.match(hover, /box-shadow:\s*inset 3px 0 0 var\(--color-accent\),/,
        'a hover glow that forgets the rail would make the accent edge blink on hover');
});

await test('ITEM 37: a themed session row paints NO edge layer, on either surface', async () => {
    // This assertion has moved twice, and both moves were the same
    // correction: an edge is not where session identity belongs.
    //
    // It first asserted a `.launchpad-container` override in styles.css
    // that cancelled a 3px rail for the home card alone. The rail then
    // went from both surfaces and the override with it, so it became "one
    // ring, both surfaces, no rail". The ring is now gone too, because the
    // row's border is what `[data-active="1"]` uses to say "this is the
    // session you are in" - and when a session is pinned to the host
    // theme, the ring and the selection border are the same colour, so a
    // themed row read as the selected one.
    //
    // What it asserts now is the whole point: session-theme-tint.css
    // declares nothing on either row's BOX. The cue is a swatch element.
    const tint = fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'session-theme-tint.css'), 'utf8');
    const liveTint = tint.split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('/*'))
        .join('\n');
    assert.ok(!/\.session-sidebar-row\[data-session-theme\]/.test(liveTint),
        'nothing may select the sidebar ROW by its theme any more - the row box '
        + 'is selection, and the theme has its own element');
    assert.ok(!/\.running-session-row\[data-session-theme\]/.test(liveTint),
        'and the same for the home card, or the collision has only been moved');
    assert.ok(!/box-shadow/.test(liveTint),
        'no box-shadow at all: a ring is an edge, and an edge is what this '
        + 'change is removing');
    assert.match(liveTint, /\.session-theme-swatch\s*\{/,
        'the cue has to be somewhere, or the tint was deleted rather than moved');
    const liveStyles = STYLES.split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('/*'))
        .join('\n');
    assert.ok(
        !/\.launchpad-container\s+\.running-session-row\[data-session-theme\]\s*\{/
            .test(liveStyles),
        'the home-screen override cancelled a rail that no longer exists; a rule '
        + 'that restates what it overrides is a second place to forget to change');
});

// =====================================================================
// ITEM 41 - an 80 percent fill, declared from theme tokens.
// =====================================================================

await test('ITEM 41: every home surface fills to 80 percent of the theme PAGE colour', async () => {
    for (const selector of ['.project-item', '.running-session-row', '.project-session-row']) {
        const body = ruleBody(STYLES, selector);
        assert.match(body, /background-color:\s*color-mix\(in srgb, var\(--color-bg, #1e1e1e\) 80%, transparent\)/,
            `${selector} must declare the 80 percent fill`);
        assert.ok(!/^\s*background:\s/m.test(body),
            `${selector} must not use the background shorthand, which would discard one of the two layers`);
    }
});

await test('ITEM 41: the accent tint rides as an IMAGE layer so the fill survives it', async () => {
    for (const selector of ['.project-item', '.running-session-row', '.project-session-row']) {
        const body = ruleBody(STYLES, selector);
        assert.match(body, /background-image:\s*linear-gradient\(var\(--color-accent-bg-soft\), var\(--color-accent-bg-soft\)\)/);
    }
});

await test('ITEM 41: hover changes the TINT only, never the fill', async () => {
    for (const selector of ['.running-session-row:hover', '.project-session-row:hover']) {
        const body = ruleBody(STYLES, selector);
        assert.match(body, /background-image:\s*linear-gradient\(/,
            `${selector} must tint, not repaint`);
        assert.ok(!/background:\s*rgba/.test(body),
            `${selector} used the shorthand, which drops the row back to a 14 percent fill on hover`);
    }
    const item = ruleBody(STYLES, '.project-item:hover');
    assert.match(item, /background-color:\s*color-mix\(in srgb, var\(--color-bg-hover, #323235\) 80%, transparent\)/);
});

await test('ITEM 41: the fill is a THEME token, not a hardcoded colour', async () => {
    const body = ruleBody(STYLES, '.project-item').replace(/\/\*[\s\S]*?\*\//g, '');
    const hardcoded = body.match(/#[0-9a-fA-F]{6}/g) || [];
    assert.deepEqual(hardcoded, ['#1e1e1e'],
        'the only literal allowed is the var() fallback, so a light theme fills light and a dark theme dark');
});

const { passes, failures } = results();
console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
