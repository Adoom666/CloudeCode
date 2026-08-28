# Testing Cloude Code on a real iOS Simulator

Verified 2026-08-16 against deployed commit `8ac32fc` at `http://10.0.1.150:8000/`,
on iPhone 16e / iOS 26.1 / Safari 26.1, macOS host with Xcode 26.

This exists because desktop responsive emulation cannot show you the three
things that actually break on a phone: which font WebKit really picks, what
`env(safe-area-inset-*)` resolves to, and how `100vh` relates to the real
visible viewport. Everything below is the shortest path to those numbers.

## 0. One-time host setup

```sh
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -downloadPlatform iOS          # long, several GB
xcrun simctl list devicetypes | grep iPhone
```

`xcode-select -p` must print the Xcode path, not
`/Library/Developer/CommandLineTools`. The CLT do not ship `simctl` at all.

## 1. Boot a device

```sh
xcrun simctl list devices available | grep iPhone
xcrun simctl boot "iPhone 16e"            # or the UDID
open -a Simulator                         # optional, only if you want to watch
xcrun simctl bootstatus <UDID> -b         # blocks until boot completes
```

Never `sleep 30` and hope. `bootstatus -b` is the poll. Even after it returns,
SpringBoard needs a few more seconds before it will accept a URL open (see the
gotcha below).

Pick a device that matches the class of bug you are chasing. iPhone 16e gives
390x844 points at DPR 3, which is the common phone case. A Pro Max is a
different story for the header overflow bug because it has more width.

## 2. GOTCHA: CoreSimulator stale service

Symptom:

```
Framework version (1051.9.4) does not match existing job version (932.2)
```

This means a `CoreSimulatorService` started under a different (usually older,
CommandLineTools-era) Xcode is still resident. Fix:

```sh
killall -9 com.apple.CoreSimulator.CoreSimulatorService
```

Then re-run the boot. This is safe; launchd restarts the service from the
currently selected Xcode on the next `simctl` call. Do this any time `simctl`
behaves as though a device does not exist that `simctl list` clearly shows.

## 3. GOTCHA: `openurl` times out right after boot

```
An error was encountered processing the command (domain=NSPOSIXErrorDomain, code=60):
Simulator device failed to open http://... Operation timed out
```

The device is booted but Safari has never been launched, so there is no
receiver for the open. Launch Safari first, then open the URL:

```sh
xcrun simctl launch <UDID> com.apple.mobilesafari
sleep 5
xcrun simctl openurl <UDID> "http://10.0.1.150:8000/"
```

Retrying `openurl` alone also eventually works, but launching Safari first is
deterministic.

## 4. Screenshots

```sh
xcrun simctl io <UDID> screenshot /path/shot.png
```

Writes a full-resolution PNG (1170x2532 on a 16e, i.e. real DPR 3 pixels). This
always works and needs nothing else running. Use it as the visual record for
any layout claim.

## 5. The Claude iOS Simulator MCP does NOT work here

`mcp__Claude_Code_iOS_Simulator__control` returns `No booted simulator found`
even while `xcrun simctl list devices` reports `Booted`. This is not a
CoreSimulator problem and killing the service does not fix it. The Claude
Desktop renderer process reports its own feature gate:

```
"iosSimulator": {"status":"unsupported",
                 "reason":"iOS Simulator is disabled by its rollout flag"}
```

So the tool is gated off client-side. Confirm it yourself with:

```sh
ps aux | grep 'Claude Helper (Renderer)' | grep -o '"iosSimulator":{[^}]*}'
```

Related: this host resolves feature flags through `featuregates.org`, which the
LAN Pi-hole sinkholes. A blocked flag service can leave rollout-gated features
reading as disabled. If the MCP ever needs to work, that is the first thing to
check. Until then, drive the simulator with `simctl` plus `safaridriver`, as
below, and say so in any report rather than implying the MCP worked.

## 6. Getting a real JS console: safaridriver against the simulator

This is the important part and it is not widely known. `safaridriver` can drive
Safari inside an iOS Simulator, and unlike desktop Safari automation it needs
**no `safaridriver --enable`** and no sudo.

```sh
safaridriver -p 4444 &

curl -s -X POST http://127.0.0.1:4444/session \
  -H 'Content-Type: application/json' \
  -d '{"capabilities":{"alwaysMatch":{
        "browserName":"safari",
        "platformName":"iOS",
        "safari:useSimulator":true,
        "safari:deviceUDID":"<UDID>"}}}'
```

That returns a `sessionId`. From then on:

```sh
SID=<sessionId>

# navigate
curl -s -X POST http://127.0.0.1:4444/session/$SID/url \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://10.0.1.150:8000/"}'

# run JS and get the value back
curl -s -X POST http://127.0.0.1:4444/session/$SID/execute/sync \
  -H 'Content-Type: application/json' \
  -d '{"script":"return JSON.stringify({w:innerWidth,h:innerHeight});","args":[]}'

# real touch gesture (this is a genuine touch, not a synthetic scroll)
curl -s -X POST http://127.0.0.1:4444/session/$SID/actions \
  -H 'Content-Type: application/json' \
  -d '{"actions":[{"type":"pointer","id":"f1",
        "parameters":{"pointerType":"touch"},
        "actions":[{"type":"pointerMove","duration":0,"x":195,"y":500},
                   {"type":"pointerDown","button":0},
                   {"type":"pointerMove","duration":250,"x":195,"y":180},
                   {"type":"pointerUp","button":0}]}]}'

# clean up
curl -s -X DELETE http://127.0.0.1:4444/session/$SID
```

Notes:
- `safaridriver` drives its own automation window. `xcrun simctl io ... screenshot`
  still captures it, so screenshots and JS measurements agree.
- The automation session starts with a clean profile, so it is always logged out.
- Quoting JSON through the shell is miserable. Write the script to a file and
  build the request body with `python3 -c 'import json,sys; print(json.dumps(
  {"script": sys.stdin.read(), "args": []}))' < script.js`.

## 7. The measurements worth taking

Helper used throughout, because `getComputedStyle` will not resolve `env()` or
viewport units for you unless something is actually using them:

```js
function probe(v){
  var d=document.createElement('div');
  d.style.cssText='position:fixed;top:0;left:0;height:'+v+';visibility:hidden';
  document.body.appendChild(d);
  var x=getComputedStyle(d).height; d.remove(); return x;
}
probe('env(safe-area-inset-top)');
probe('100vh'); probe('100dvh'); probe('100svh'); probe('100lvh');
```

### Which monospace face WebKit really picked

Do not read the CSS. Measure it. A monospace face renders `iiiiiiiiii` and
`MMMMMMMMMM` at identical widths; a proportional fallback does not.

```js
function w(f){
  var s=document.createElement('span');
  s.style.cssText='position:absolute;visibility:hidden;white-space:pre;'+
                  'font-size:40px;font-family:'+f;
  s.textContent='iiiiiiiiii'; document.body.appendChild(s);
  var a=s.getBoundingClientRect().width;
  s.textContent='MMMMMMMMMM';
  var b=s.getBoundingClientRect().width; s.remove(); return [a,b];
}
w('var(--font-mono)'); w('ui-monospace'); w("'SF Mono'"); w('serif');
```

Result on iOS 26.1 (2026-08-16):

| family | i x10 | M x10 | verdict |
|---|---|---|---|
| `var(--font-mono)` | 247.27 | 247.27 | monospace, correct |
| `ui-monospace` | 247.27 | 247.27 | monospace |
| `'SF Mono'` | 111.14 | 355.67 | **not resolved**, fell back to proportional |
| `Menlo` | 240.83 | 240.83 | monospace |
| `serif` (control) | 111.14 | 355.67 | proportional |

`'SF Mono'` produces byte-identical widths to the serif control, which is the
proof that the family name does not exist on iOS. This is exactly why
`--font-mono` must lead with `ui-monospace`: xterm measures a cell from the
first resolvable family, and a proportional face makes every column wrong.
Any future edit that moves `ui-monospace` off the front of that list must be
re-checked with this exact test.

### The terminal cell width, and the one that is a decoy

Three different "cell widths" are readable at once and only one of them is the
pitch the renderer draws on. Measured 2026-08-16, iPhone 16e, 14px font:

| quantity | value | what it is |
|---|---|---|
| `_core._charSizeService.width` | 8.65625 | the FONT's natural advance. A decoy. |
| `dimensions.css.cell.width` | 8.33333 | `floor(8.65625 * 3) / 3`, what WebGL draws on |
| `css.canvas.width / cols` | 8.32558 | `358 / 43`, the painted pitch |

The WebGL renderer floors the cell to whole device pixels, so 43 columns paint
`358` CSS px inside a `374` px viewport and nothing overflows. Counting ink
runs in a screenshot row confirms it: 43 separate glyph runs spanning
x=8.67..365.33, against an `.xterm-screen` box of 8..366.

Multiplying a column count by `_charSizeService.width` gives 372.1px and
"proves" an overflow that does not exist. Do not use that number. The
invariant that matters, and the one the guard in `client/js/terminal-metrics.js`
enforces, is `cols * dimensions.css.cell.width <= available width`.

The first two or three rows at the right edge ARE covered, by the
`.terminal-tools` chip (`position: absolute; top: 0; right: 0; z-index: 5`),
which is deliberate. Before the tools were folded there were three chips
covering ~150px, which looks exactly like clipped columns in a screenshot.
Check a row further down before concluding anything about column count.

### Horizontal overflow

```js
document.documentElement.scrollWidth, window.innerWidth
```

Equal means clean. Greater means something is pushing the layout wide, and on a
flex header that something is almost always a `white-space: nowrap` item with
the default `min-width: auto`.

### Safe-area insets

All four resolve to `0px` in normal Safari on this device, portrait, even
though `index.html` correctly declares `viewport-fit=cover`. That is not a bug:
in browser mode the browser chrome already occupies the notch and home
indicator strips, so WebKit reports no inset to reclaim. The insets only become
non-zero when the page runs **standalone** (added to the Home Screen) or in
landscape (side insets). So `ios-chrome.css` is provably inert in Safari and
cannot be validated here. To validate it you must install the PWA, which the
simulator can do but only through the Share sheet by hand.

### The three viewport heights

Measured with the toolbar expanded:

| quantity | value |
|---|---|
| `probe('100vh')` | 739px |
| `probe('100lvh')` | 739px |
| `window.innerHeight` | 699px |
| `window.visualViewport.height` | 699px |
| `probe('100dvh')` | 699px |
| `probe('100svh')` | 699px |

`100vh` is 40px (5.7%) taller than the viewport a user can actually see. This
is the class of bug desktop emulation never shows, because on desktop
`100vh === innerHeight`.

`body` already does the right thing (`height: 100vh; height: 100dvh;`), so it
lands on 699. Anything still written in raw `vh` resolves against 739.

### The toolbar cannot collapse in this app

A real touch swipe changes nothing: `scrollY` stays 0. The reason is that
`body` is `overflow: hidden` with `height: 100dvh`, and every screen scrolls in
its own container, so the document is never scrollable and Safari has no reason
to shrink its toolbar. Therefore the collapsed-toolbar numbers are unobtainable
here, and `100dvh` never actually changes at runtime in Safari for this app.

That is worth knowing in both directions: it means the app is immune to the
classic dvh-jitter problem, and it means you cannot use this app to test
collapsed-toolbar behaviour. The dynamic viewport case that *does* occur is the
software keyboard, which shrinks `visualViewport.height` while `innerHeight`
stays put. Test that on a screen with a focused input.

## 8. The app is login-gated, and automation cannot get into a deployed one

`/` returns the full 32KB SPA to an unauthenticated client; the gate is a
client-side screen (`#auth-screen`) plus server-side auth on the APIs. TOTP.
There is no bypass and none should be added.

What that means for testing: the auth screen is directly measurable, and so is
everything static (CSS, computed fonts, viewport numbers, safe areas), because
all of that is global to the document. The post-login screens are present in
the DOM but hidden.

For a layout measurement only, you can force a screen visible **client side**,
which touches nothing on the server:

```js
document.getElementById('auth-screen').style.display='none';
document.getElementById('terminal-screen').style.display='flex';
document.getElementById('session-sidebar-toggle').classList.remove('hidden');
document.querySelectorAll('.header .hidden')
        .forEach(e=>e.classList.remove('hidden'));
document.getElementById('header-title-text').textContent='some-long-session-name';
```

Always label results from this as a forced local render. It proves CSS layout.
It does not prove anything about live data, websockets, xterm sizing against a
real pty, or anything the server would have sent.

### Getting a REAL live session anyway: run your own instance

A forced render cannot answer anything about xterm sizing or scrollback,
because both need a live pty behind a websocket. The way in is not to defeat
the deployed instance's auth - never do that - but to stand up your own
throwaway one and let it issue you a token:

```sh
cp config.example.json config.json
printf 'HOST=127.0.0.1\nPORT=<free port>\nTOTP_SECRET=<fresh base32>\n'  > .env
printf 'JWT_SECRET=<fresh random>\nAUTH_CONFIG_FILE=./config.json\n'    >> .env
printf 'DEFAULT_WORKING_DIR=<scratch dir>\nLOG_DIRECTORY=/tmp/cloude-x\n'>> .env
chmod 600 .env
venv/bin/python3 -m src.main &
venv/bin/python3 -c "from src.api.auth import create_jwt_token; \
                     print(create_jwt_token(600)[0])"
```

Then in the safaridriver session, before navigating:

```js
localStorage.setItem('claude_tunnel_token', '<the token>');
```

Reload and the app is authenticated. The secrets are yours, generated for this
run, and `.env` plus `config.json` are both gitignored. Never read, copy or log
the deployed instance's secrets to do this.

Two traps that cost real time:

- **`session.tmux_socket_name` in `config.json` did not isolate the sessions**
  on 2026-08-16: sessions still landed on the shared `cloude` socket. Assume
  your test sessions will appear next to the user's, name them so you can tell
  them apart, snapshot `tmux -L cloude ls` before you start, and diff it after
  you clean up.
- `tests/test_session_backend.py` adopt tests are flaky on a busy host
  (`attach_existing: tmux session adopt_itest_... is not alive`). Confirm
  against a stashed tree before blaming your change.

## 9. What the simulator CANNOT reproduce

Be honest about these in any report. They are not small.

- **Brave, which is what the user actually browses on.** The simulator has
  Safari only. Brave on iOS uses the same WebKit engine, so rendering and font
  resolution carry over, but its chrome does not.
- **Brave Shields.** Content blocking, script blocking, and its aggressive
  fingerprinting defences can block requests, alter `navigator` values, and
  break things that work perfectly in Safari. Nothing in the simulator exercises
  this. A "works in the simulator" result says nothing about Shields.
- **Brave's own toolbar geometry.** Brave's bottom bar has different height and
  different collapse behaviour from Safari's, so `innerHeight`,
  `visualViewport.height` and the effective bottom inset all differ from the
  numbers above. Do not quote Safari numbers as Brave numbers.
- **Standalone / Home Screen PWA mode**, without adding it by hand through the
  Share sheet. Until you do, every `env(safe-area-inset-*)` reads 0.
- **Real network conditions.** The simulator uses the Mac's network stack
  directly. No cellular latency, no captive portals, no Tailscale-vs-LAN
  difference in practice.
- **Real touch ergonomics.** A synthesized pointer says a control is 40px; it
  does not say whether a thumb can hit it.
- **Anything the OS gates on real hardware**: camera, biometrics, push
  notifications, Low Power Mode throttling.

## 10. Teardown

```sh
curl -s -X DELETE http://127.0.0.1:4444/session/$SID
kill %1                       # safaridriver
xcrun simctl shutdown <UDID>  # optional; leaving it booted is fine and faster
```
