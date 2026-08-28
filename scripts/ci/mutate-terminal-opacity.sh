#!/bin/bash
# Mutation check for tests/test_terminal_opacity.node.mjs.
#
# A test that passes is only evidence if it can also FAIL. Each mutation
# below reintroduces one form of the bug this feature guards against:
# the terminal going translucent over nothing (three-outcome rule),
# xterm's own background staying opaque despite theme.background carrying
# an alpha, foreground/ANSI colours getting swept into the transform they
# must never touch, terminal.js's construction/theme-swap sites falling
# back to the raw (unwrapped) theme, or the CSS override losing its gate
# and applying even when the animated background cannot be evaluated.
# Every one must turn the suite red. All three mutated files are restored
# on exit, including on failure.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
OPACITY_JS="$ROOT/client/js/terminal-background-opacity.js"
TERM_JS="$ROOT/client/js/terminal.js"
OPACITY_CSS="$ROOT/client/css/terminal-opacity.css"
mutate_arm_trap "$ROOT" "$OPACITY_JS" "$TERM_JS" "$OPACITY_CSS"

survived=0
cannot_determine=0

restore_all() {
  mutate_restore_files
}

# mutate <name> <file> <old||=>||new>
mutate() {
  local name="$1" file="$2" py="$3"
  restore_all
  python3 - "$file" "$py" <<'PY'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:80])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "CANNOT_DETERMINE $name (target moved)"; cannot_determine=1; return; fi
  if mutate_run node "$ROOT/tests/test_terminal_opacity.node.mjs" >/dev/null 2>&1; then
    echo "SURVIVED $name"
    survived=1
  else
    echo "killed    $name"
  fi
}

# --- parseCssColorChannels stops parsing correctly ----------------------
mutate "hex6 regex broken so #rrggbb never parses" "$OPACITY_JS" \
  "var hex6 = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})\$/i.exec(color);||=>||var hex6 = /^#NEVER\$/i.exec(color);"

mutate "hex3 shorthand expansion drops a digit" "$OPACITY_JS" \
  "r: parseInt(hex3[1] + hex3[1], 16),||=>||r: parseInt(hex3[1], 16),"

mutate "rgb()/rgba() regex stops matching" "$OPACITY_JS" \
  "var rgb = /^rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)/i.exec(color);||=>||var rgb = /^NEVER/i.exec(color);"

# --- isThemeEffectVisible loses the three-outcome gate -------------------
mutate "everything reads as visible, including unavailable" "$OPACITY_JS" \
  "return status === 'running' || status === 'paused' || status === 'static';||=>||return true;"

mutate "running stops counting as visible" "$OPACITY_JS" \
  "status === 'running' || status === 'paused'||=>||status === 'NEVER' || status === 'paused'"

mutate "paused stops counting as visible (tab-hidden frame lost)" "$OPACITY_JS" \
  "status === 'paused' || status === 'static';||=>||status === 'NEVER' || status === 'static';"

# --- withTerminalBackgroundOpacity stops respecting the gate/parse --------
mutate "opacity applies even when the effect is not visible" "$OPACITY_JS" \
  "if (!isThemeEffectVisible()) return rawTheme;||=>||if (false) return rawTheme;"

mutate "an unparseable background gets guessed at instead of left alone" "$OPACITY_JS" \
  "if (!channels) return rawTheme;||=>||if (!channels) return Object.assign({}, rawTheme, { background: 'rgba(0,0,0,0.9)' });"

mutate "TERMINAL_BG_OPACITY silently changes from the specified 0.90" "$OPACITY_JS" \
  "var TERMINAL_BG_OPACITY = 0.90;||=>||var TERMINAL_BG_OPACITY = 0.50;"

mutate "foreground gets swept into the opaque-background transform" "$OPACITY_JS" \
  "return Object.assign({}, rawTheme, {||=>||return Object.assign({}, rawTheme, { foreground: '#ff00ff',"

# --- attach()/apply() wiring inside terminal-background-opacity.js -------
mutate "apply() stops writing to term.options.theme" "$OPACITY_JS" \
  "term.options.theme = withTerminalBackgroundOpacity(rawTheme);||=>||void 0;"

mutate "the effect-status observer stops re-applying the last raw theme" "$OPACITY_JS" \
  "observer = new MutationObserver(function () { apply(lastRaw); });||=>||observer = new MutationObserver(function () {});"

mutate "the observer watches the wrong attribute" "$OPACITY_JS" \
  "attributeFilter: ['data-theme-effects']||=>||attributeFilter: ['data-theme-NEVER']"

# --- terminal.js's thin wiring regresses ---------------------------------
mutate "allowTransparency reverts to false (rgba background silently clamps to opaque)" "$TERM_JS" \
  "allowTransparency: true,||=>||allowTransparency: false,"

mutate "the initial theme never gets attach()'d/applied through the module" "$TERM_JS" \
  "if (this._xtermOpacity) this._xtermOpacity.apply(initialXtermTheme);||=>||void 0;"

mutate "a theme swap (session/global theme change) stops going through the module" "$TERM_JS" \
  "if (this._xtermOpacity) this._xtermOpacity.apply(newXtermTheme); else this.term.options.theme = newXtermTheme;||=>||this.term.options.theme = newXtermTheme;"

mutate "the Terminal instance never attaches to the opacity module at all" "$TERM_JS" \
  "window.TerminalBackgroundOpacity.attach(this.term)||=>||null"

# --- CSS override loses its gate ---------------------------------------
mutate "CSS override applies unconditionally (defeats the fallback)" "$OPACITY_CSS" \
  "html[data-theme-effects=\"running\"] #terminal,||=>||#terminal,"

mutate "CSS override also fires on unavailable" "$OPACITY_CSS" \
  "html[data-theme-effects=\"static\"] #terminal {||=>||html[data-theme-effects=\"static\"] #terminal, html[data-theme-effects=\"unavailable\"] #terminal {"

mutate "CSS override sets the wrong property value" "$OPACITY_CSS" \
  "background: transparent;||=>||background: red;"

restore_all
echo
if [ "$survived" -eq 1 ] || [ "$cannot_determine" -eq 1 ]; then
  echo "MUTATION CHECK FAILED: at least one mutation survived or could not be evaluated (see SURVIVED/CANNOT_DETERMINE above)"
  exit 1
fi
echo "MUTATION CHECK PASSED: every mutation was killed"
exit 0
