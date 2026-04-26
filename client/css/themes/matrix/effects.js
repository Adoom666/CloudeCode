// Matrix theme — falling katakana background.
// Mounts a fixed full-viewport <canvas> at z-index: -1.
// Throttled to ~30fps desktop / ~15fps mobile.
// Pauses on document.visibilitychange (hidden → suspend RAF).
// Refuses to mount entirely under prefers-reduced-motion: reduce.
// Public API: init({ themeContext }), destroy().

let canvas = null;
let ctx = null;
let rafId = null;
let resizeHandler = null;
let visHandler = null;
let lastFrameAt = 0;
let columns = [];
let fontSize = 16;
let colCount = 0;

const GLYPHS = (() => {
  const out = [];
  // Katakana block (U+30A0 - U+30FF) — pull the visually dense ones.
  for (let cp = 0x30a0; cp <= 0x30ff; cp++) out.push(String.fromCharCode(cp));
  // Digits + a few latin letters for variety (the original film mixes them in).
  for (let i = 0; i < 10; i++) out.push(String(i));
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach((c) => out.push(c));
  return out;
})();

function pickGlyph() {
  return GLYPHS[(Math.random() * GLYPHS.length) | 0];
}

function resize() {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(window.innerWidth * dpr);
  canvas.height = Math.floor(window.innerHeight * dpr);
  canvas.style.width = window.innerWidth + "px";
  canvas.style.height = window.innerHeight + "px";
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);
  fontSize = window.innerWidth < 768 ? 13 : 16;
  colCount = Math.max(1, Math.floor(window.innerWidth / fontSize));
  columns = new Array(colCount);
  for (let i = 0; i < colCount; i++) {
    columns[i] = Math.random() * (window.innerHeight / fontSize);
  }
  ctx.font = `${fontSize}px "SF Mono", "Menlo", monospace`;
  ctx.textBaseline = "top";
}

function tick(now) {
  rafId = requestAnimationFrame(tick);
  const isMobile = window.innerWidth < 768;
  const minDelta = isMobile ? 1000 / 15 : 1000 / 30;
  if (now - lastFrameAt < minDelta) return;
  lastFrameAt = now;

  // Translucent black overlay creates the trailing fade.
  ctx.fillStyle = "rgba(0, 0, 0, 0.06)";
  ctx.fillRect(0, 0, window.innerWidth, window.innerHeight);

  ctx.fillStyle = "#00ff41";
  for (let i = 0; i < colCount; i++) {
    const x = i * fontSize;
    const y = columns[i] * fontSize;
    const glyph = pickGlyph();
    // Occasional bright head-glyph for sparkle.
    if (Math.random() < 0.015) {
      ctx.fillStyle = "#ccffcc";
      ctx.fillText(glyph, x, y);
      ctx.fillStyle = "#00ff41";
    } else {
      ctx.fillText(glyph, x, y);
    }
    columns[i]++;
    if (y > window.innerHeight && Math.random() > 0.975) {
      columns[i] = 0;
    }
  }
}

function startLoop() {
  if (rafId != null) return;
  lastFrameAt = 0;
  rafId = requestAnimationFrame(tick);
}

function stopLoop() {
  if (rafId != null) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
}

export function init() {
  if (canvas) return; // idempotent
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return; // honor user preference; CSS fallback handles the static look
  }
  canvas = document.createElement("canvas");
  canvas.id = "matrix-rain";
  Object.assign(canvas.style, {
    position: "fixed",
    inset: "0",
    width: "100vw",
    height: "100vh",
    zIndex: "-1",
    pointerEvents: "none",
    background: "#000000",
  });
  document.body.appendChild(canvas);
  ctx = canvas.getContext("2d", { alpha: false });

  resize();
  resizeHandler = () => resize();
  window.addEventListener("resize", resizeHandler, { passive: true });

  visHandler = () => (document.hidden ? stopLoop() : startLoop());
  document.addEventListener("visibilitychange", visHandler);

  if (!document.hidden) startLoop();
}

export function destroy() {
  stopLoop();
  if (resizeHandler) {
    window.removeEventListener("resize", resizeHandler);
    resizeHandler = null;
  }
  if (visHandler) {
    document.removeEventListener("visibilitychange", visHandler);
    visHandler = null;
  }
  if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
  canvas = null;
  ctx = null;
  columns = [];
}

export default { init, destroy };
