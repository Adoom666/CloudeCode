# Manual Audio Check - ThemeAudio + GlobalAudioToggle plumbing

Per-theme background-music plumbing (`window.ThemeAudio`) ships alongside a
single global on/off control (`window.GlobalAudioToggle`) that lives in the
bottom bar on every screen that has one - the home screen's `.home-bar` and
the terminal screen's `.info`. This document is the checklist for verifying
the whole path by hand.

Two prior controls are gone and stay gone: a header mute button
(`#audioToggleBtn` / `.header-audio-toggle`, deleted well before this doc was
last updated) and, after that, a per-session "play music" row buried in the
session editor FAB's dropdown menu (`session-editor-menu.js`, deleted when
`globalAudioToggle.js` shipped). If either shows up again in `client/index.html`
or the JS it is a regression - `tests/test_session_theme_menu.node.mjs` and
`tests/test_global_audio_toggle.node.mjs` assert both stay deleted.

## Files involved

- `client/js/themeAudio.js` - singleton (`window.ThemeAudio`), the playback
  engine and its one gate (session in scope AND the global on/off)
- `client/js/themeAudioStatus.js` - turns a `ThemeAudio.getStatus()` snapshot
  into a three-outcome verdict (`playing` / a named reason / `settling`)
- `client/js/globalAudioToggle.js` - the one on/off control (`window.
  GlobalAudioToggle`), one shared DOM button re-parented between bars by
  `app.js`'s `place('auth'|'launchpad'|'terminal')`, one stored key
  (`cloude.audio.enabled`)
- `client/css/global-audio.css` - the button's states (`data-audio-state`)
- `client/js/themes/registry.js` - calls `ThemeAudio.setTheme(manifest.audio
  || null)` from `applyTheme()`
- `client/js/app.js` - calls `GlobalAudioToggle.place(screen)` and
  `.syncForSession()` from `showAuth()` / `showLaunchpad()` / `showTerminal()`
  / `returnToExistingTerminal()`

## Default-state checks (no music files needed)

1. **Hard-refresh browser** (Cmd-Shift-R). On the home screen, `.home-bar`
   shows the audio button in its off state (muted-speaker glyph,
   `data-audio-state="off"`, `aria-pressed="false"`). No audio plays. No
   console errors.
2. **Click the button.** It flips to on (`aria-pressed="true"`). On the home
   screen there is never a session in scope, so the button also shows the
   `on-no-session` colour and its label reads "audio is on, nothing to play
   here" - on, but honestly reporting there is nothing to play yet.
3. **Refresh the page.** The on/off choice persists (`cloude.audio.enabled`
   in localStorage). The button reads the same state it was left in.
4. **Click it again.** Back to off.
5. **Enter a session whose theme declares no `audio` block** (most of them,
   until you add a temporary one per below). The button, now on the
   terminal screen's `.info` bar, shows `on-no-track` if you leave audio on -
   distinct from `on-no-session` and distinct from a real fault.

## Live-audio checks (after adding a temp `audio.src` to one theme)

For testing only - DO NOT commit. Pick a theme (e.g. `client/css/themes/metal/theme.json`)
and add an `"audio"` block at the top level of the JSON:

```json
"audio": {
  "src": "https://www.kozco.com/tech/piano2.wav",
  "volume": 0.3,
  "fadeMs": 1500
}
```

Then restart the server (so the manifest cache refreshes), hard-refresh, and:

6. **Pick the metal theme from the session theme picker while audio is off.**
   Audio loads (network tab shows a GET to the WAV) but does **not** play -
   the global control is off.
7. **Turn the global control on.** Music starts and fades in over `fadeMs`.
   Volume target is `audio.volume` × the master volume (settings panel,
   defaults to 1.0). The button now reads `on-playing`.
8. **Switch to another browser tab.** After a moment, audio pauses
   (`visibilitychange` fired). The button re-paints via the
   `cloude:audio-state` event; the reason names the background pause.
9. **Return to the tab.** Audio resumes and fades back in; the button
   returns to `on-playing`.
10. **Switch to a theme without an `audio` block** (e.g. `claude`, `codex`).
    Current music fades out, then silence. The button now reads `on-no-track`
    - still on, nothing broken, simply nothing declared.
11. **Switch back to the audio-enabled theme.** Music re-loads and resumes
    (fade-in from 0); the button returns to `on-playing`.
12. **Detach the session (back to the home screen) with audio on.** The
    button, now in `.home-bar`, reads `on-no-session` and the engine is
    silent - `app.js`'s `showLaunchpad()` calls `GlobalAudioToggle
    .syncForSession()` with no session in scope, which closes ThemeAudio's
    gate regardless of the stored on/off.
13. **Re-attach the same session from the launchpad's active-session banner
    or the sidebar** (`App.returnToExistingTerminal()`). Music resumes
    without needing another tap - this path used to skip the sync entirely
    (see the 2026-08-19 note in `app.js`); if it stays silent here, that
    regression is back.
14. **Refresh the page while on.** Audio does NOT auto-play (no user gesture
    yet on the fresh page load) - the button still shows on, but the state
    reads `on-settling` briefly, then whatever `ThemeAudioStatus` reports
    once a gesture (a click anywhere that reaches the engine) unlocks
    playback.
15. **Hard-refresh while off.** Button shows off on load; no network request
    for the audio file happens until the theme is applied and the control is
    turned on.

## Edge-case checks

16. **404 / network failure on `audio.src`.** Set `audio.src` to a bogus URL
    (e.g. `https://example.com/nope.mp3`). Apply the theme with the global
    control on. The button reads `on-error` (distinct colour and label from
    `on-no-track`) and the label names "failed to load". Console logs a
    single `ThemeAudio: audio load error` warn - no toast, no UI breakage.
17. **CORS-tainted `MediaElementAudioSourceNode`.** GitHub Release URLs may
    fail the CORS check when piped through `createMediaElementSource`. The
    code detects this on first node construction and falls back permanently
    to bare HTMLAudioElement mode with JS-driven `requestAnimationFrame`
    volume ramps. You'll see a single `ThemeAudio: WebAudio graph failed,
    falling back to element mode` warn. Fades + visibility-pause still work,
    just less precise. The button still reads `on-playing` once audible.
18. **Rapid theme-flipping.** Click through 5 themes in 2 seconds with audio
    on. No stuck audio nodes, no overlapping playback after the last fade
    settles, no AudioContext warnings about too many connections, and the
    button's state never gets stuck on a stale reading (it repaints on every
    `cloude:audio-state` event, not only on its own click).
19. **Resize below 768px (mobile terminal width).** `.info` is `display:
    none` at that width, so the audio button (a child of `.info` on the
    terminal screen) disappears with the whole bar and adds no height back -
    confirm via devtools that `.info`'s computed `height` is `0` and its
    `display` is `none`, not merely that the button looks hidden.

## Failure modes the plumbing handles silently (i.e. never surfaces a crash)

- Missing `audio` field in manifest → `on-no-track` / off, no warn.
- Missing `audio.src` (empty / non-string) → no node created.
- Network 404 / CORS failure → `on-error`, no toast, no UI breakage.
- `AudioContext` unavailable (old browser) → bare-element mode.
- Tab backgrounded for an extended time → audio paused; buffer warm.
- `window.ThemeAudioStatus` missing or throwing → the button reads `unknown`
  (a distinct fourth colour/state, never guessed as on or off).

## Upgrade path

If `<audio loop=true>` gaps become objectionable (Chromium and WebKit still
produce ~50-200ms gaps on loop boundaries in 2026), upgrade `themeAudio.js`
to fetch the asset, decode via `AudioContext.decodeAudioData`, and play via
two-`AudioBufferSourceNode` scheduler with manual `onended` restart. That
path is sample-accurate gapless AND sidesteps the `createMediaElementSource`
CORS taint. Trade-off: full-file memory load and no streaming start.
