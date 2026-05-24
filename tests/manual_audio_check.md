# Manual Audio Check — ThemeAudio plumbing (v0.7.0+)

Per-theme background-music plumbing ships **dormant** in the initial PR. No
existing theme manifest declares an `audio` block, and no music files exist on
the GitHub Release yet. This document is the checklist for verifying the
plumbing once you add the first `audio.src` to a theme.

## Files involved

- `client/js/themeAudio.js` — singleton (`window.ThemeAudio`)
- `client/js/themes/registry.js` — calls `ThemeAudio.setTheme(manifest.audio || null)` from `applyTheme()`
- `client/index.html` — `<button id="audioToggleBtn" class="header-audio-toggle">` in `.header .controls`
- `client/js/app.js` — `App._wireAudioToggle()` binds the click and `App.init()` calls `ThemeAudio.init()`
- `client/css/styles.css` — `.header-audio-toggle` (mirrors `.header-rename-pencil` visual treatment)

## Default-state checks (no music files yet)

1. **Hard-refresh browser** (Cmd-Shift-R) on `http://localhost:8000`. Header
   shows 🔇 in the controls strip. No audio plays. No console errors.
2. **Click 🔇** → icon flips to 🔊, `aria-pressed="true"`, tooltip becomes
   "Mute theme music". Still no audio (no theme has an `audio` block configured).
3. **Refresh page** → mute state persists per localStorage. If you left it
   unmuted, header shows 🔊 on reload — but still no audio plays without
   a configured theme.
4. **Click 🔊** → icon flips back to 🔇, `cloude.audio.muted` in localStorage
   becomes `'true'`.

## Live-audio checks (after adding a temp `audio.src` to one theme)

For testing only — DO NOT commit. Pick a theme (e.g. `client/css/themes/metal/theme.json`)
and add a `"audio"` block at the top level of the JSON:

```json
"audio": {
  "src": "https://www.kozco.com/tech/piano2.wav",
  "volume": 0.3,
  "fadeMs": 1500
}
```

Then restart the server (so the manifest cache refreshes), hard-refresh, and:

5. **Pick the metal theme from the header selector.** Audio loads (network
   tab shows a GET to the WAV) but does **not** play — the page is still muted
   by default.
6. **Click 🔇 → 🔊.** Music starts and fades in over `fadeMs`. Volume target
   is `audio.volume` × global volume (defaults to `audio.volume` since global
   defaults to 1.0 wait — actually `0.3`. So you'll hear `0.3 × 0.3 = 0.09` —
   adjust `audio.volume` to 1.0 for testing if too quiet).
7. **Switch to another browser tab** (Cmd-T then click the new tab). After a
   moment, audio in the cloudecode tab pauses (`visibilitychange` fired).
8. **Return to the cloudecode tab.** Audio resumes and fades back in.
9. **Switch theme to one without an `audio` block** (e.g. `claude`, `codex`).
   Current music fades out over the fadeMs of the *outgoing* track, then silence.
10. **Switch back to the audio-enabled theme.** Music re-loads and resumes
    (fade-in from 0).
11. **Refresh page while unmuted.** Audio does NOT auto-play (no user gesture
    yet on the fresh page load). Header still shows 🔊 because mute state is
    persisted as `false`. First click on the page that triggers any theme
    apply — including the auto-restored theme on auth — will NOT play.
    User must click 🔊 to re-grant the autoplay (click is the gesture).
12. **Hard-refresh with mute=true.** Same as step 11 but icon is 🔇.

## Edge-case checks

13. **404 / network failure on `audio.src`.** Set `audio.src` to a bogus URL
    (e.g. `https://example.com/nope.mp3`). Apply the theme. Console logs a
    single `ThemeAudio: audio load error` warn — no toast, no UI breakage,
    no error cascade. Other themes still work.
14. **CORS-tainted `MediaElementAudioSourceNode`.** GitHub Release URLs may
    fail the CORS check when piped through `createMediaElementSource`. The
    code detects this on first node construction and falls back permanently
    to bare HTMLAudioElement mode with JS-driven `requestAnimationFrame`
    volume ramps. You'll see a single `ThemeAudio: WebAudio graph failed,
    falling back to element mode` warn. Fades + visibility-pause still work,
    just less precise.
15. **Rapid theme-flipping.** Click through 5 themes in 2 seconds. No stuck
    audio nodes, no overlapping playback after the last fade settles, no
    AudioContext warnings about too many connections.

## Failure modes the plumbing handles silently

- Missing `audio` field in manifest → no audio for that theme (no warn).
- Missing `audio.src` (empty / non-string) → no node created.
- Network 404 / CORS failure → silent teardown, no UI surface.
- `AudioContext` unavailable (old browser) → bare-element mode.
- Tab backgrounded for an extended time → audio paused; buffer warm.

## Upgrade path

If `<audio loop=true>` gaps become objectionable (Chromium and WebKit still
produce ~50–200ms gaps on loop boundaries in 2026), upgrade `themeAudio.js`
to fetch the asset, decode via `AudioContext.decodeAudioData`, and play via
two-`AudioBufferSourceNode` scheduler with manual `onended` restart. That
path is sample-accurate gapless AND sidesteps the `createMediaElementSource`
CORS taint. Trade-off: full-file memory load and no streaming start.
