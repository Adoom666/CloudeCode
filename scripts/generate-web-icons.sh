#!/bin/bash
# Generate the web-app icon set (apple-touch-icon + manifest icons) from the
# single 1024px source already used for the macOS menu-bar app.
#
# One source, one look: the home-screen icon on a phone is the SAME artwork
# as the desktop app icon rather than a second thing to keep in sync.
#
# Sizes and why each exists:
#   180  apple-touch-icon. iOS uses this for "Add to Home Screen". iOS does
#        NOT read SVG here, so a PNG is mandatory. iOS also has no concept of
#        transparency here - a transparent pixel renders BLACK on some iOS
#        versions - so this one is flattened onto the app's own dark
#        background colour (client/manifest.webmanifest background_color)
#        rather than shipped with alpha.
#   192  web app manifest baseline (Android/Chrome install prompt). Kept
#        transparent: "any" purpose icons are composited by the launcher,
#        which already handles alpha correctly.
#   512  web app manifest large / splash. Same transparent treatment as 192.
#   64/header-icon.png, 128/header-icon@2x.png  in-app header brand mark
#        (client/index.html #header-icon, client/js/app.js
#        HEADER_BRAND_ICON_URL / HEADER_BRAND_ICON_URL_2X). Same transparent,
#        edge-to-edge treatment as 192/512 so it reads correctly on both
#        light and dark theme headers. 1x/2x pair, not a single size, so the
#        1.2em header box stays sharp on retina displays. fix/real-app-icon-art
#        replaced the old client/assets/cloude-icon.svg hand-drawn
#        approximation with these, cropped straight from the source PNG.
#   512-maskable  Android adaptive-icon variant, purpose "maskable". Adaptive
#        icons are cropped to a shape (circle, squircle, rounded square...)
#        chosen by the OEM launcher, keeping only a centered safe zone
#        (~66% of the canvas). The plain 512 art fills ~98% of the canvas
#        width, so reusing it (the previous approach) let the cloud's outer
#        lobes get clipped. This variant re-scales the source into that safe
#        zone and flattens it onto the background colour, which is also what
#        a maskable icon is expected to do (the mask, not transparency, is
#        what shows the surrounding shape).
#
# Rerun after changing macOS/assets/AppIcon-1024.png. Output is committed:
# the server has no image pipeline and must not grow one just to ship a
# handful of static files. Pillow is already a project dependency
# (requirements.txt) so the flatten/rescale step uses it instead of adding a
# new one.
#
# Usage: scripts/generate-web-icons.sh

set -euo pipefail

REPO_ROOT="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/macOS/assets/AppIcon-1024.png"
OUT_DIR="$REPO_ROOT/client/assets/icons"
PYTHON_BIN="$REPO_ROOT/venv/bin/python3"

if [ ! -f "$SRC" ]; then
    echo "generate-web-icons: source not found: $SRC" >&2
    exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

# sips ships with macOS; no Homebrew dependency for the plain resizes.
if ! command -v sips >/dev/null 2>&1; then
    echo "generate-web-icons: sips not found (macOS only)" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# Transparent, edge-to-edge resizes for the manifest "any" icons.
for size in 192 512; do
    sips -s format png -z "$size" "$size" "$SRC" \
        --out "$OUT_DIR/icon-${size}.png" >/dev/null
    echo "generate-web-icons: wrote icon-${size}.png"
done

# Transparent, edge-to-edge resizes for the in-app header brand mark.
# 64px (1x) and 128px (2x) cover the header's 1.2em box sharply at every
# display density.
sips -s format png -z 64 64 "$SRC" --out "$OUT_DIR/header-icon.png" >/dev/null
echo "generate-web-icons: wrote header-icon.png"
sips -s format png -z 128 128 "$SRC" --out "$OUT_DIR/header-icon@2x.png" >/dev/null
echo "generate-web-icons: wrote header-icon@2x.png"

# apple-touch-icon (180, flattened) and the maskable 512 (flattened + safe
# zone) both need real alpha compositing, not just a resize, so they go
# through Pillow rather than sips.
"$PYTHON_BIN" - "$SRC" "$OUT_DIR" <<'PYEOF'
import sys
from PIL import Image

src_path, out_dir = sys.argv[1], sys.argv[2]

# Matches client/manifest.webmanifest background_color / theme_color, the
# app's default (Claude theme) page background.
BG = (0x0a, 0x0a, 0x0a, 0xff)

# Android's safe zone for maskable icons is a centered circle at ~66% of the
# canvas. Scaling the source content to 60% of canvas width leaves margin so
# every OEM mask shape (circle, squircle, rounded square) keeps the full
# mark.
MASKABLE_SAFE_FRACTION = 0.60

src = Image.open(src_path).convert("RGBA")


def flatten(im: Image.Image, size: int) -> Image.Image:
    """Resize im to size x size and composite it onto an opaque BG canvas."""
    resized = im.resize((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), BG)
    canvas.alpha_composite(resized)
    return canvas.convert("RGB")


# apple-touch-icon: same edge-to-edge scale as the transparent variants, just
# flattened so iOS never has to guess at a transparent pixel.
apple = flatten(src, 180)
apple.save(f"{out_dir}/icon-180.png")
print("generate-web-icons: wrote icon-180.png (flattened)")

# Maskable 512: crop to the visible mark, scale it into the safe zone, paste
# centered on an opaque BG canvas.
bbox = src.getbbox()
cropped = src.crop(bbox)
cw, ch = cropped.size
canvas_size = 512
target = int(canvas_size * MASKABLE_SAFE_FRACTION)
scale = target / max(cw, ch)
new_w, new_h = max(1, round(cw * scale)), max(1, round(ch * scale))
mark = cropped.resize((new_w, new_h), Image.LANCZOS)

canvas = Image.new("RGBA", (canvas_size, canvas_size), BG)
paste_x = (canvas_size - new_w) // 2
paste_y = (canvas_size - new_h) // 2
canvas.alpha_composite(mark, (paste_x, paste_y))
canvas.convert("RGB").save(f"{out_dir}/icon-512-maskable.png")
print("generate-web-icons: wrote icon-512-maskable.png (safe-zone, flattened)")
PYEOF

echo "generate-web-icons: done ($OUT_DIR)"
