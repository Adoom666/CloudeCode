# vendored xterm.js bundle

`xterm.css`, `xterm.js`, `xterm-addon-fit.js`, `xterm-addon-webgl.js` and
`xterm-addon-unicode11.js` are unmodified files pulled straight from
jsDelivr at the pinned versions below and committed as-is. There is no
build step here - unlike `client/vendor/codemirror/`, these ship from
their npm packages as plain browser files, so there is nothing to bundle.

## why vendored instead of CDN-loaded

`client/index.html` previously loaded all five of these from
`cdn.jsdelivr.net`. On desktop that CDN is fast and typically warm-cached,
so the failure mode below never surfaces there. On the user's phone
(Brave), Shields blocks or delays third-party requests by default,
including CDN-hosted JS and CSS. When `xterm.css` is missing or arrives
late, xterm's internal character-measurement element - the hidden DOM node
it uses to compute cell width/height - never gets the right metrics.
Backend pane dimensions still come out correct (they are computed
independently), so the terminal LOOKS fine from the server's point of view
while the on-device render is garbled. Serving these five files from this
app's own origin removes third-party-CDN reachability from the picture
entirely: no CDN request, no Shields interaction, no race between script
load and stylesheet load. Same reasoning that put CodeMirror under
`client/vendor/`, see that directory's `VERSION.md`.

## pinned versions (fetched 2026-08-16)

| file | package | version | source URL |
|---|---|---|---|
| xterm.css | xterm | 5.3.0 | https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css |
| xterm.js | xterm | 5.3.0 | https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js |
| xterm-addon-fit.js | xterm-addon-fit | 0.8.0 | https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js |
| xterm-addon-webgl.js | xterm-addon-webgl | 0.16.0 | https://cdn.jsdelivr.net/npm/xterm-addon-webgl@0.16.0/lib/xterm-addon-webgl.js |
| xterm-addon-unicode11.js | xterm-addon-unicode11 | 0.6.0 | https://cdn.jsdelivr.net/npm/xterm-addon-unicode11@0.6.0/lib/xterm-addon-unicode11.js |

## sha256

```
832f3f2c603b43ad4351ff04970150cc7a873014276db126a6065c6dd81e4872  xterm.css
f0aea0f75f48559013ae6643c2479dd737d26da42d5524e6d2b70915ae6523c7  xterm.js
10f3194c5f17c1786fb7d5db865c1ec8539b6736a318063fd38bdaaf7c46848f  xterm-addon-fit.js
0c9c48c9391c4cee816eacf95699dbde97e8cc8f191e87f3a571e73d214c8df8  xterm-addon-webgl.js
ab10d83642883e5e17ea741cd5b6e5f8c0f6a06e3271f2f0c0e043be4fc5e738  xterm-addon-unicode11.js
```

## updating

Dated snapshot, not auto-updated. To bump, run
`scripts/xterm-vendor/fetch.sh <xterm-ver> <fit-ver> <webgl-ver> <unicode11-ver>`
(or edit the pinned versions inside the script and run it with no args),
then update the version table and sha256 block above to match what it
prints.
