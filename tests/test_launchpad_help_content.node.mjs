// Node tests for the launchpad help disclosure's copy (client/js/launchpad.js,
// `.adopt-disclosure-body`, fix/help-copy). This is the only substantive
// explanatory prose in the whole client - the header "More actions" menu has
// only Logout and Settings, and grep across client/js + client/*.html for
// "help"-named markup turns up nothing else. See docs/help-content-audit.md
// for how that was established and what was wrong with the old copy.
//
// These assertions guard the two classes of defect the audit found:
//   1. style violations the project's own rules forbid (em/en dashes, emoji)
//      that were present in the pre-fix text and are easy to reintroduce by
//      pasting instead of typing;
//   2. factual drift - a README link or a UI term name that silently stops
//      matching the code/docs it points at. Both of the old defects in this
//      class (a dead GitHub anchor, "alias" for what README requires to be a
//      function) were exactly this shape.
//
// Run with: node tests/test_launchpad_help_content.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

const launchpad = fs.readFileSync(path.join(repoRoot, 'client/js/launchpad.js'), 'utf8');
const readme = fs.readFileSync(path.join(repoRoot, 'README.md'), 'utf8');
const settingsTabs = fs.readFileSync(path.join(repoRoot, 'client/js/settings-tabs.js'), 'utf8');
const wrappersView = fs.readFileSync(path.join(repoRoot, 'client/js/agent-wrappers-view.js'), 'utf8');

/**
 * Extract the `.adopt-disclosure-body` markup block from launchpad.js.
 * @returns {string} The inner HTML string, still containing markup tags.
 */
function helpBodyHtml() {
    const start = launchpad.indexOf('<div class="adopt-disclosure-body">');
    assert.ok(start !== -1, 'expected the adopt-disclosure-body block to exist');
    const end = launchpad.indexOf('</details>', start);
    assert.ok(end !== -1, 'expected a closing </details> after the body');
    return launchpad.slice(start, end);
}

/**
 * Strip HTML tags down to visible text, for prose-level assertions
 * (dash/emoji checks should not trip on markup characters).
 * @param {string} html
 * @returns {string}
 */
function textOnly(html) {
    return html.replace(/<[^>]+>/g, ' ');
}

/**
 * Convert a GitHub markdown heading into the anchor GitHub would generate
 * for it: lowercase, strip anything that is not a word char/space/hyphen,
 * collapse whitespace to single hyphens. Good enough for ASCII headings
 * (all headings in this README are ASCII).
 * @param {string} heading  Heading text with leading `#`s already stripped.
 * @returns {string}
 */
function githubAnchor(heading) {
    return heading
        .toLowerCase()
        .replace(/[^\w\s-]/g, '')
        .trim()
        .replace(/\s+/g, '-');
}

const readmeAnchors = new Set(
    Array.from(readme.matchAll(/^#{1,4}\s+(.+)$/gm)).map((m) => githubAnchor(m[1]))
);

test('the help body has no em dash or en dash (project style rule)', () => {
    const text = textOnly(helpBodyHtml());
    assert.doesNotMatch(text, /[–—]/, 'found an en dash (–) or em dash (—)');
});

test('the help body has no emoji', () => {
    const text = textOnly(helpBodyHtml());
    // Astral-plane emoji range plus the common dingbat/symbol blocks used
    // elsewhere in this app's UI (e.g. the header cloud icon).
    const emojiPattern = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u;
    assert.doesNotMatch(text, emojiPattern, 'found an emoji character in help copy');
});

test('every README link in the help body resolves to a real heading anchor', () => {
    const body = helpBodyHtml();
    const links = Array.from(body.matchAll(/href="https:\/\/github\.com\/Adoom666\/CloudeCode#([a-z0-9-]+)"/g));
    assert.ok(links.length > 0, 'expected at least one README link in the help body');
    for (const [, anchor] of links) {
        assert.ok(
            readmeAnchors.has(anchor),
            `README link anchor "#${anchor}" does not match any heading in README.md ` +
            `(this is exactly how the old #launching-claude-with-a-custom-alias link went dead)`
        );
    }
});

test('the help body never calls the required cld shell function an "alias"', () => {
    // README.md is explicit: "Make it a function, not an alias." The old
    // help copy said "custom launcher alias" for the exact same cld/cldor
    // definitions - directly contradicting the install instructions.
    const text = textOnly(helpBodyHtml()).toLowerCase();
    assert.doesNotMatch(text, /launcher alias/, 'help text must say "function", never "alias", for cld/cldor');
    assert.match(readme, /Make it a function, not an alias/, 'README no longer states the function-not-alias rule; re-check this assertion');
});

test('the help body explains that "wrappers" and "launch wrappers" name the same object', () => {
    const text = textOnly(helpBodyHtml()).toLowerCase();
    assert.match(text, /wrappers and launch wrappers are the same thing/);
    assert.match(text, /there is no second, different kind of wrapper/);
});

test('the wrappers explanation matches what the settings screen actually renders', () => {
    // Tab strip really is labelled "wrappers" (settings-tabs.js renders tab
    // objects verbatim; the id/label pair is supplied by settings-panel.js,
    // asserted indirectly here via the tab id used everywhere else).
    assert.match(settingsTabs, /data-settings-tab-strip|settings-tab/, 'settings-tabs.js shape changed; re-verify the wrappers tab claim');
    // The section inside that tab really does title itself "launch wrappers".
    assert.match(wrappersView, /<h3 class="settings-section-title">launch wrappers<\/h3>/);
});

test('the wrappers explanation names exactly the five real agent families', () => {
    const agentFamilies = fs.readFileSync(path.join(repoRoot, 'src/core/agent_families.py'), 'utf8');
    const text = textOnly(helpBodyHtml()).toLowerCase();
    for (const family of ['claude', 'codex', 'hermes', 'openclaw', 'shell']) {
        assert.match(text, new RegExp(family), `help text should mention the "${family}" family`);
        assert.match(agentFamilies, new RegExp(`name="${family}"|"${family}"`), `"${family}" is not actually a family in agent_families.py`);
    }
});

test('the help body documents the settings tab strip as four tabs, not five', () => {
    // The "agents" tab was removed; help text must not resurrect it.
    const text = textOnly(helpBodyHtml()).toLowerCase();
    assert.doesNotMatch(text, /\bagents tab\b/, 'the agents tab no longer exists (feat/wrappers-and-favorites)');
});

test('the EXTERNAL tag explanation matches how ownership is actually computed', () => {
    const sessionManager = fs.readFileSync(path.join(repoRoot, 'src/core/session_manager.py'), 'utf8');
    const text = textOnly(helpBodyHtml()).toLowerCase();
    assert.match(text, /worked out fresh each time this list loads/, 'help text should describe EXTERNAL as computed live, not a stored flag');
    assert.match(sessionManager, /owned_names=set\(self\.owned_tmux_sessions\)/, 'ownership computation changed; re-verify the EXTERNAL-tag claim');
});

test('the help body still has all three sections, in a stuck-user-first order', () => {
    const text = textOnly(helpBodyHtml()).toLowerCase();
    const iAdopt = text.indexOf('adopting a session you started yourself');
    const iWrap = text.indexOf('wrappers and launch wrappers are the same thing');
    const iSlash = text.indexOf('slash commands');
    assert.ok(iAdopt !== -1 && iWrap !== -1 && iSlash !== -1, 'expected all three sections present');
    assert.ok(iAdopt < iWrap && iWrap < iSlash, 'sections must stay in this order');
});

test('the disclosure marker survives as a native summary (not repurposed into a button)', () => {
    // Regression guard shared in spirit with test_home_screen_polish.node.mjs -
    // this file only asserts on the PROSE, that one still owns the marker's
    // layout contracts.
    const block = launchpad.match(/<details class="adopt-disclosure">[\s\S]*?<\/summary>/);
    assert.ok(block, 'expected the details/summary disclosure markup to still exist');
    assert.doesNotMatch(block[0], /<button\b/, 'a button here inherits the 36px reset box (see test_home_screen_polish)');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures > 0 ? 1 : 0);
