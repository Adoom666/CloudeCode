# Theme audio credits

Every file here is CC0 (Creative Commons Zero, public domain dedication), which
requires no attribution. We record it anyway because provenance is what lets a
future maintainer re-verify the license, re-download a source, or re-encode at a
different bitrate without re-doing this research from scratch.

All clips were transcoded and loudness-normalized with a two-pass ffmpeg
`loudnorm` to **-24 LUFS integrated, -2 dBTP** so no clip is jarringly louder
than another. They loop under a working terminal, so the target is deliberately
quiet. Looping is handled in code (`themeAudio.js` sets `el.loop = true`); the
files are not gapless-mastered.

## Two formats: .m4a is the one that plays

Every clip ships **twice**, as `.m4a` (AAC-LC) and `.ogg` (Vorbis). The manifests
point `src` at the m4a and `srcFallback` at the ogg.

The ogg-only original was silent on iOS. Measured on iPhone 16e, iOS 26.1 /
Safari 26.1, 2026-08-16: `canPlayType('audio/ogg; codecs=vorbis')` returns
`"probably"`, and the element then fails to load with
`MEDIA_ERR_SRC_NOT_SUPPORTED` (code 4). WebKit recognises the container and
cannot decode the codec, so a `canPlayType` probe is worthless here and the
format order is hard-coded instead.

The m4a set was encoded from the ogg (one extra lossy generation, inaudible at
this playback level) with ffmpeg's native `aac` encoder at 80 kbps stereo,
56 kbps for the one mono clip - roughly the bitrate the oggs already sat at, so
the two sets are the same total size (12.1 MiB ogg, 12.2 MiB m4a). Note that
this ffmpeg build has **no libvorbis**, which is why the ogg encode originally
needed `vorbis-tools`/`oggenc`; re-encoding the m4a set needs neither.

| File | Source work | Author | License | Source page |
|---|---|---|---|---|
| `post-apocalyptic-wastelands.m4a` + `post-apocalyptic-wastelands.ogg` | Post Apocalyptic Wastelands [Loop Ready].ogg | Juhani Junkala (OGA user `SubspaceAudio`) | CC0 | https://opengameart.org/content/horror-atmosphere |
| `cyber-city.m4a` + `cyber-city.ogg` | busy_cyberworld.ogg | TinyWorlds | CC0 | https://opengameart.org/content/scifi-city-ambient-loop |
| `scifi-drone.m4a` + `scifi-drone.ogg` | Sci-fi Ambient Drone | LookIMadeAThing | CC0 | https://freesound.org/people/LookIMadeAThing/sounds/534018/ |
| `dead-ship.m4a` + `dead-ship.ogg` | MyVeryOwnDeadShip.ogg | yd | CC0 | https://opengameart.org/content/background-space-track |
| `observing-the-star.m4a` + `observing-the-star.ogg` | ObservingTheStar.ogg | yd | CC0 | https://opengameart.org/content/another-space-background-track |
| `out-there.m4a` + `out-there.ogg` | OutThere.ogg | yd | CC0 | https://opengameart.org/content/space-music-out-there |
| `dungeon-ambience.m4a` + `dungeon-ambience.ogg` | dungeon_ambient_1.ogg | JaggedStone | CC0 | https://opengameart.org/content/loopable-dungeon-ambience |
| `forest-ambience.m4a` + `forest-ambience.ogg` | Forest_Ambience_0.mp3 | FGResources | CC0 | https://opengameart.org/content/cc0-background-ambience |

## Source notes

- `dead-ship` and `observing-the-star` were extracted from the LMMS
  project zips (`projects.zip`, `ObservingTheStar.zip`) that those two pages
  attach instead of a bare .ogg.
- `scifi-drone` was encoded from Freesound's **public high-quality MP3
  preview**, not the 90.6 MB source WAV. Freesound puts the original file
  behind an account login; the preview is the same CC0 work, served publicly
  and at full length (2:45). This means one extra lossy generation. It is
  inaudible at the volume this plays at, but if someone later wants a clean
  master, download the WAV with a Freesound account and re-run the encode.

## Shared pool, not per-theme copies

Eight clips (in two formats) cover 23 themes. They live in one flat directory
and each `theme.json` points at the shared URLs. Per-theme directories would duplicate
megabytes of identical audio for no benefit.
