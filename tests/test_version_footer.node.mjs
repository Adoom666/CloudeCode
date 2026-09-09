// Node-based tests for client/js/version-footer.js - the small grey app
// version, shown at the foot of the session sidebar and in the home
// screen's bottom bar.
//
// WHY THIS FILE EXISTS. Two placements read from one function
// (versionSpanHtml), which is the whole point of the component: if the
// sidebar and the home bar ever computed the version text separately,
// they could drift into showing two different strings for the one app.
// This suite guards three things:
//
//   1. THE TWO PLACEMENTS SHARE THE SAME RENDER CALL. session-sidebar-rows.js
//      and launchpad.js are checked, by source, to both call into
//      window.VersionFooter rather than building their own markup.
//   2. THE VERSION IS READ, NOT INVENTED. It comes from the
//      `cloude-app-version` meta tag - the same value src/core/version.py's
//      resolve_version() produces server-side - and is read at most once.
//   3. AN UNRESOLVED VERSION IS NAMED, NOT SILENT. An empty or missing
//      meta tag renders "version unknown" in the same markup shape as a
//      real version, never a blank string and never a stale one.
//
// Run with: node tests/test_version_footer.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;
const queue = [];

/** Queue one named assertion block. Inputs: name, fn. Output: void. */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Run every queued test in order. Inputs: none. Output: Promise<void>. */
async function runQueue() {
    for (const [name, fn] of queue) {
        try {
            await fn();
            passes++;
            console.log(`ok - ${name}`);
        } catch (err) {
            failures++;
            console.error(`NOT OK - ${name}`);
            console.error(err && err.stack ? err.stack : err);
        }
    }
}

/** Read one repo file as text. Inputs: ...parts (string). Output: string. */
function repoFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
}

/**
 * A `document` double carrying exactly one meta tag, and counting how
 * many times it was asked for it - so the cache can be proven to read
 * the DOM at most once.
 *
 * Inputs: content (string|null) - the meta tag's `content` attribute, or
 *   null to simulate the tag being entirely absent.
 * Output: object - {document, queryCount: () => number}.
 */
function fakeDocument(content) {
    let calls = 0;
    const meta = content === null ? null : {
        getAttribute: (k) => (k === 'content' ? content : null),
    };
    const document = {
        querySelector: (sel) => {
            calls++;
            return sel === 'meta[name="cloude-app-version"]' ? meta : null;
        },
    };
    return { document, queryCount: () => calls };
}

/**
 * Load version-footer.js into a fresh sandbox around the given document
 * double.
 * Inputs: doc (object|undefined) - a `document` double; a throwing one
 *   simulates a blocked DOM read.
 * Output: object - the exported window.VersionFooter.
 */
function loadModule(doc) {
    const context = { console, document: doc };
    vm.createContext(context);
    vm.runInContext(repoFile('client', 'js', 'version-footer.js'), context);
    return context.VersionFooter;
}

// ---- the resolved case ---------------------------------------------------

test('a resolved version renders as the shared span, unescaped content escaped', () => {
    const { document } = fakeDocument('v1.0.35');
    const VF = loadModule(document);
    assert.equal(
        VF.versionSpanHtml(),
        '<span class="version">v1.0.35</span>',
    );
});

test('an extra class joins onto the shared "version" class, never replaces it', () => {
    const { document } = fakeDocument('v1.0.35');
    const VF = loadModule(document);
    assert.equal(
        VF.versionSpanHtml('sidebar-thing'),
        '<span class="version sidebar-thing">v1.0.35</span>',
    );
});

test('the meta tag is read AT MOST ONCE, however many times the span is rendered', () => {
    const { document, queryCount } = fakeDocument('v1.0.35');
    const VF = loadModule(document);
    VF.versionSpanHtml();
    VF.versionSpanHtml();
    VF.readVersionText();
    assert.equal(queryCount(), 1, 'the DOM must be queried once and cached');
});

test('resetCache forces a fresh read - test-only escape hatch', () => {
    const { document, queryCount } = fakeDocument('v1.0.35');
    const VF = loadModule(document);
    VF.readVersionText();
    VF.resetCache();
    VF.readVersionText();
    assert.equal(queryCount(), 2);
});

test('a version string is HTML-escaped before interpolation', () => {
    const { document } = fakeDocument('v1.0.0"><script>x</script>');
    const VF = loadModule(document);
    const html = VF.versionSpanHtml();
    assert.ok(!html.includes('<script>'), html);
    assert.ok(html.includes('&lt;script&gt;'), html);
});

// ---- the unresolved case: named, never blank -----------------------------

test('an EMPTY meta tag renders "version unknown", not a blank span', () => {
    const { document } = fakeDocument('');
    const VF = loadModule(document);
    assert.equal(
        VF.versionSpanHtml(),
        `<span class="version">${VF.UNKNOWN_TEXT}</span>`,
    );
    assert.notEqual(VF.UNKNOWN_TEXT, '', 'the fallback itself must not be blank');
});

test('a MISSING meta tag renders the same honest fallback as an empty one', () => {
    const { document } = fakeDocument(null);
    const VF = loadModule(document);
    assert.equal(VF.readVersionText(), '');
    assert.ok(VF.versionSpanHtml().includes(VF.UNKNOWN_TEXT));
});

test('a document that THROWS on read degrades to the fallback, never throws itself', () => {
    const throwingDoc = {
        querySelector: () => { throw new Error('DOM access blocked'); },
    };
    const VF = loadModule(throwingDoc);
    assert.doesNotThrow(() => VF.versionSpanHtml());
    assert.ok(VF.versionSpanHtml().includes(VF.UNKNOWN_TEXT));
});

test('the fallback text is lowercase, matching this app\'s UI voice', () => {
    const { document } = fakeDocument('');
    const VF = loadModule(document);
    assert.equal(VF.UNKNOWN_TEXT, VF.UNKNOWN_TEXT.toLowerCase());
});

test('the unresolved case is NEVER a stale-looking version-shaped string', () => {
    // The honesty requirement this component exists to meet: an
    // unresolved version must not render as if it were a real one.
    const { document } = fakeDocument('');
    const VF = loadModule(document);
    assert.ok(!/^v?\d/.test(VF.UNKNOWN_TEXT), 'fallback must not look like a version number');
});

// ---- the sidebar wrapper ---------------------------------------------------

test('sidebarFooterHtml wraps the SAME span the home bar renders', () => {
    const { document } = fakeDocument('v1.0.35');
    const VF = loadModule(document);
    const span = VF.versionSpanHtml();
    assert.ok(
        VF.sidebarFooterHtml().includes(span),
        'the sidebar footer must contain the exact span versionSpanHtml produces',
    );
    assert.ok(VF.sidebarFooterHtml().startsWith('<div class="version-footer">'));
});

test('the sidebar wrapper also carries the honest fallback', () => {
    const { document } = fakeDocument('');
    const VF = loadModule(document);
    assert.ok(VF.sidebarFooterHtml().includes(VF.UNKNOWN_TEXT));
});

// ---- one component, two real callers --------------------------------------

test('THE SIDEBAR FOOTER ACTUALLY CALLS THE SHARED COMPONENT', () => {
    const rows = repoFile('client', 'js', 'session-sidebar-rows.js');
    assert.ok(
        rows.includes('window.VersionFooter.sidebarFooterHtml()'),
        'session-sidebar-rows.js must render its version line through VersionFooter, not build one itself',
    );
});

test('THE HOME BAR ACTUALLY CALLS THE SHARED COMPONENT', () => {
    const lp = repoFile('client', 'js', 'launchpad.js');
    assert.ok(
        lp.includes('window.VersionFooter.versionSpanHtml()'),
        'launchpad.js must render the home bar chip through VersionFooter, not build one itself',
    );
    assert.ok(
        !lp.includes('meta[name="cloude-app-version"]'),
        'launchpad.js must no longer read the meta tag directly - version-footer.js owns that read',
    );
});

test('BOTH FILES ARE SERVED, IN AN ORDER THAT WORKS', () => {
    const html = repoFile('client', 'index.html');
    assert.ok(html.includes('/static/js/version-footer.js'), 'the module is served');
    assert.ok(html.includes('/static/css/version-footer.css'), 'and so is its CSS');
    const scriptIdx = html.indexOf('/static/js/version-footer.js');
    const launchpadIdx = html.indexOf('/static/js/launchpad.js');
    const rowsIdx = html.indexOf('/static/js/session-sidebar-rows.js');
    assert.ok(scriptIdx > -1 && launchpadIdx > -1 && rowsIdx > -1);
    assert.ok(scriptIdx < launchpadIdx, 'version-footer.js must load before launchpad.js');
    assert.ok(scriptIdx < rowsIdx, 'version-footer.js must load before session-sidebar-rows.js');
});

test('THE VERSION TEXT IS SELECTABLE - no user-select: none anywhere in its CSS', () => {
    const css = repoFile('client', 'css', 'version-footer.css');
    assert.ok(!/user-select\s*:\s*none/i.test(css), css);
});

test('THE VERSION LINE IS NOT INTERACTIVE - no role=button, tabindex or click wiring', () => {
    // Checks for the actual markup/attribute a reader would hit, not the
    // word appearing in a comment explaining that it is absent.
    const js = repoFile('client', 'js', 'version-footer.js');
    assert.ok(!/role=["']button["']/.test(js), js);
    assert.ok(!/\btabindex\s*=/i.test(js), js);
    assert.ok(!/\.tabIndex\s*=/.test(js), js);
    assert.ok(!/addEventListener/.test(js), js);
});

test('the muted colour comes from a CSS custom property, never a hard-coded value', () => {
    const styles = repoFile('client', 'css', 'styles.css');
    const versionRule = styles.match(/^\.version \{[^}]*\}/m);
    assert.ok(versionRule, 'expected a .version rule in styles.css');
    assert.ok(/color:\s*var\(--color-fg-faint\)/.test(versionRule[0]), versionRule[0]);
    const footerCss = repoFile('client', 'css', 'version-footer.css');
    assert.ok(!/color:\s*#[0-9a-f]{3,8}/i.test(footerCss), 'no hard-coded colour in version-footer.css');
});

test('nothing here uses an em-dash, an en-dash, or an emoji', () => {
    for (const f of [['js', 'version-footer.js'], ['css', 'version-footer.css']]) {
        const src = repoFile('client', ...f);
        assert.ok(!/[—–]/.test(src), `${f[1]} carries a dash`);
        assert.ok(
            !/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u.test(src),
            `${f[1]} carries an emoji`,
        );
    }
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
