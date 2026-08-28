# iOS home-screen app, over plain http

What "make it an ios app style page" actually gets you when the server is
reachable at `http://<tailscale-host>:<port>` and not over TLS.

Short version: the standalone app window works. Offline does not, and
cannot, without TLS.

## Works today, no TLS needed

iOS "Add to Home Screen" reads the `apple-mobile-web-app-*` meta tags
directly from the document. None of that path is gated on a secure
context, so on a plain-http origin you still get:

| Thing | Where it comes from |
|---|---|
| Home-screen icon | `<link rel="apple-touch-icon">` -> `client/assets/icons/icon-180.png` |
| Standalone window (no Safari chrome, no URL bar) | `apple-mobile-web-app-capable=yes` |
| Status bar drawn over the app background | `apple-mobile-web-app-status-bar-style=black-translucent` |
| Home-screen label "Cloude" | `apple-mobile-web-app-title` |
| Edge-to-edge layout, content clear of the notch and home indicator | `viewport-fit=cover` + `env(safe-area-inset-*)` in `client/css/ios-chrome.css` |
| App-coloured status bar / tab bar | `theme-color` |

The web app manifest is served and valid, and iOS 16.4+ does read parts of
it, but the tags above are what actually carry the behaviour on iOS. The
manifest is there because it is the standard, and because Chrome on
Android uses it for real.

## Needs TLS, and is therefore NOT implemented

`window.isSecureContext` is false on a plain-http non-localhost origin.
Everything below is gated on it and would fail at registration time:

- **Service worker.** `navigator.serviceWorker.register()` rejects. No
  service worker is registered anywhere in this repo, on purpose. A
  registration that silently never takes is worse than none: it looks
  like offline support exists.
- **Cache API / offline start-up.** Depends on the service worker.
- **Background sync, push notifications, periodic sync.** Same gate.
- **The Chrome/Android "install app" prompt.** Requires a service worker
  plus a secure origin. Chrome will still let a user "add to home screen"
  manually.

Because there is no service worker, the app needs the server reachable to
start. That is already true of it - it is a terminal front end for a
server on your network - so the practical cost of no offline mode is
close to zero.

## If you later put TLS in front of it

Tailscale can issue a real cert for a `*.ts.net` name (`tailscale cert`),
which makes the origin a secure context with no other change. At that
point a service worker becomes possible. Nothing in the current markup
has to change to get there; adding one is purely additive.

## What is NOT verified

The tags, the manifest route, the icon route and the safe-area CSS are
verified by test and in a desktop browser at phone width. Actually
installing to a real iPhone home screen and confirming the standalone
window and status bar were NOT tested - there is no iPhone in this
environment. The behaviour above is what the markup specifies, not what
was observed on device.

## Regenerating the icons

`scripts/generate-web-icons.sh` resizes `macOS/assets/AppIcon-1024.png`
into `client/assets/icons/`. Rerun it after changing the source art.
