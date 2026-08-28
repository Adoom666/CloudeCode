# vendored CodeMirror 6 bundle

`codemirror-bundle.js` is a single minified IIFE built from the CodeMirror 6
packages below, produced by the (not-shipped) build workspace at
`scripts/codemirror-vendor/` in this repo. It exposes one global,
`window.CodeMirrorBundle`, with `createEditor()` and `languageForPath()` -
see `scripts/codemirror-vendor/src/entry.js` for the actual surface.

Vendored, not CDN-loaded, because this app's CSP is `script-src 'self'`
(see `src/main.py`) and vendoring is the only way to load a real editor
without weakening that policy.

The build script must NOT pass esbuild's `--global-name`. That flag wraps
the output as `var CodeMirrorBundle = (() => { ... })()`, and because
`entry.js` sets `window.CodeMirrorBundle` itself instead of exporting, the
IIFE returns undefined and the `var` overwrites the object that was just
set - the global is then present but undefined, with no error anywhere.
That shipped in the 2026-08-14 build and made every file click in the
config editor fail with "Cannot read properties of undefined (reading
'createEditor')". Rebuilt 2026-08-15 without the flag; the two builds are
byte-identical apart from that 21-byte wrapper prefix.

## pinned versions (built 2026-08-15)

| package | version |
|---|---|
| @codemirror/state | 6.7.1 |
| @codemirror/view | 6.43.8 |
| @codemirror/commands | 6.10.4 |
| @codemirror/language | 6.12.4 |
| @codemirror/lang-markdown | 6.5.2 |
| @codemirror/lang-json | 6.0.2 |
| @codemirror/lang-python | 6.2.1 |
| @codemirror/lang-javascript | 6.2.5 |
| @codemirror/legacy-modes | 6.5.3 (shell/bash mode - no dedicated `@codemirror/lang-shell` package exists) |
| @codemirror/search | 6.7.1 |
| @codemirror/autocomplete | 6.20.3 (transitive dep of lang-python; not wired up/enabled) |
| @lezer/highlight | 1.2.3 |

Bundle size: 592KB minified (single file, no separate CSS asset - CM6
ships its styling as inline `EditorView.theme`/`baseTheme` calls, not
external stylesheets).

Language modes included: markdown, json, python, javascript (also covers
.cjs/.mjs), shell/bash (via legacy-modes). No `@codemirror/lang-html` or
`@codemirror/lang-css` are wired into `languageForPath()` even though
`lang-markdown` pulls them in transitively for embedded-code-block
highlighting - not a standalone mode this app exposes.

## updating

This is a deliberate, dated snapshot - not auto-updated. To bump:

```
cd scripts/codemirror-vendor
npm install --save-exact @codemirror/<pkg>@<version> ...
npm run build
```

Then update the version table above and re-check the bundle size (flag to
the user if it crosses roughly 1MB - it was not expected to grow much from
592KB for a point-release bump).
