// Blade Runner theme — cyan-cobalt rain streaks falling across a rain-slick night.
// Mounts a fixed full-viewport <canvas> at z-index: -1.
// Throttled to ~30fps desktop / ~15fps mobile.
// Pauses on document.visibilitychange (hidden -> suspend RAF).
// Refuses to mount entirely under prefers-reduced-motion: reduce.
// Public API: init({ themeContext }), destroy().

let canvas = null;
let ctx = null;
let rafId = null;
let resizeHandler = null;
let visHandler = null;
let lastFrameAt = 0;
let streaks = [];
let isMobile = false;

function makeStreak(width, height, seed) {
  // Streak: vertical rain line. 1-2px wide, 40-100px long, falls 6-14 px/frame.
  const len = 40 + Math.random() * 60;
  return {
    x: Math.random() * width,
    // If seed: scatter across the whole viewport so first frame is full.
    // Otherwise spawn just above viewport and fall in.
    y: seed ? Math.random() * height : -len - Math.random() * height * 0.5,
    len,
    w: Math.random() < 0.7 ? 1 : 2,
    speed: 6 + Math.random() * 8,
    // Cyan-cobalt with varying alpha — Deakins blue shadow on Cronenweth wet street.
    alpha: 0.18 + Math.random() * 0.42,
  };
}

function resize() {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = window.innerWidth;
  const h = window.innerHeight;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);
  isMobile = w < 768;
  const density = isMobile ? 40 : 80;
  streaks = new Array(density);
  for (let i = 0; i < density; i++) streaks[i] = makeStreak(w, h, true);
}

function tick(now) {
  rafId = requestAnimationFrame(tick);
  const minDelta = isMobile ? 1000 / 15 : 1000 / 30;
  if (now - lastFrameAt < minDelta) return;
  lastFrameAt = now;

  const w = window.innerWidth;
  const h = window.innerHeight;

  // Translucent night-cobalt overlay creates the smear/trail.
  ctx.fillStyle = "rgba(10, 14, 26, 0.35)";
  ctx.fillRect(0, 0, w, h);

  // Draw streaks as cyan-cobalt vertical gradients (head bright, tail fade).
  for (let i = 0; i < streaks.length; i++) {
    const s = streaks[i];
    // Linear gradient along the streak: transparent -> cyan-cobalt head.
    const grad = ctx.createLinearGradient(s.x, s.y, s.x, s.y + s.len);
    grad.addColorStop(0, "rgba(0, 80, 140, 0)");
    grad.addColorStop(0.6, `rgba(0, 140, 200, ${s.alpha * 0.55})`);
    grad.addColorStop(1, `rgba(0, 212, 255, ${s.alpha})`);
    ctx.fillStyle = grad;
    ctx.fillRect(s.x, s.y, s.w, s.len);

    s.y += s.speed;
    if (s.y > h + 8) {
      streaks[i] = makeStreak(w, h, false);
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
  canvas.id = "blade-runner-rain";
  Object.assign(canvas.style, {
    position: "fixed",
    inset: "0",
    width: "100vw",
    height: "100vh",
    zIndex: "-1",
    pointerEvents: "none",
    background: "#0a0e1a",
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
  streaks = [];
}

export default { init, destroy };
