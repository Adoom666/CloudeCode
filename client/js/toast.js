/**
 * Toast Manager (v0.7.0 Part 2)
 * ----------------------------------------------------------------------
 * Renders toast notifications surfaced by the server over the WebSocket
 * (toast.new / toast.ack frames) AND backfilled via the REST endpoint
 * on session attach. Per-toast accent color is sourced from the server's
 * project-theme resolution and applied as `style="--toast-accent: <hex>"`
 * on the toast element — the CSS picks it up as a left border.
 *
 * Dismiss flow:
 *   - User clicks ×  → fade-out animation → DELETE-equivalent POST to
 *     /api/v1/toasts/<id>/ack → server broadcasts toast.ack → other tabs
 *     dismiss in lockstep (with syncToServer=false to skip the round-trip).
 *   - Server-driven ack (from another browser) → dismiss(id, {syncToServer:
 *     false}) — no echo back to the server.
 *
 * No localStorage cross-tab sync; the WS broadcast is the source of truth
 * for ack propagation.
 */

console.log('[Toast Module] Loading...');

class ToastManager {
  constructor(containerId = 'toast-container') {
    this.containerId = containerId;
    /** id -> { toast: <server-shape>, el: HTMLElement } */
    this._byId = new Map();
  }

  /**
   * Resolve the container element. Late-bound so the class can be
   * constructed before DOMContentLoaded — we look it up at first use.
   * Returns null if absent (e.g. on a page that doesn't include the
   * container markup); callers MUST guard for null.
   */
  _container() {
    return document.getElementById(this.containerId);
  }

  /**
   * Add a toast to the UI.
   * @param {object} toast - shape: { id, session_id, kind, title, body,
   *   color, created_at, acknowledged }
   */
  add(toast) {
    if (!toast || !toast.id) return;
    if (this._byId.has(toast.id)) return; // dedupe — backfill + WS race
    if (toast.acknowledged) return; // server says already done; don't show
    const container = this._container();
    if (!container) return;

    const el = document.createElement('div');
    el.className = 'toast toast--entering';
    el.dataset.toastId = toast.id;
    el.dataset.kind = toast.kind || '';
    if (toast.color) {
      // Inline custom property — CSS picks it up via var(--toast-accent).
      el.style.setProperty('--toast-accent', toast.color);
    }
    el.setAttribute('role', 'status');

    const title = document.createElement('div');
    title.className = 'toast__title';
    title.textContent = toast.title || '(untitled)';
    el.appendChild(title);

    if (toast.body) {
      const body = document.createElement('div');
      body.className = 'toast__body';
      body.textContent = toast.body;
      el.appendChild(body);
    }

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'toast__dismiss';
    dismissBtn.setAttribute('aria-label', 'Dismiss notification');
    dismissBtn.textContent = '×';
    dismissBtn.addEventListener('click', () => {
      this.dismiss(toast.id, { syncToServer: true });
    });
    el.appendChild(dismissBtn);

    container.appendChild(el);
    this._byId.set(toast.id, { toast, el });

    // Trigger entry animation on next frame so the CSS transition fires.
    requestAnimationFrame(() => {
      el.classList.remove('toast--entering');
    });
  }

  /**
   * Remove a toast from the UI, optionally syncing the ack to the server.
   * @param {string} toastId
   * @param {object} [opts]
   * @param {boolean} [opts.syncToServer=true] - POST /toasts/<id>/ack.
   *   Set false when this dismiss was triggered BY a server toast.ack
   *   frame (i.e. another browser already acked, we're just rendering).
   */
  dismiss(toastId, { syncToServer = true } = {}) {
    const entry = this._byId.get(toastId);
    if (!entry) return;
    const { toast, el } = entry;
    this._byId.delete(toastId);

    el.classList.add('toast--dismissing');
    setTimeout(() => {
      if (el && el.parentNode) {
        el.parentNode.removeChild(el);
      }
    }, 220); // slightly longer than the CSS transition (200ms)

    if (syncToServer && toast && toast.session_id) {
      // Fire-and-forget; the server-side broadcast will reach OTHER tabs.
      // Our own UI already removed the toast above; no need for the
      // server's response.
      if (window.API && typeof window.API.ackToast === 'function') {
        window.API.ackToast(toastId, toast.session_id).catch((err) => {
          // 404 / 500: log only — the local UI is already updated; a
          // failed server ack will simply re-deliver the toast on the
          // next attach backfill, which is acceptable degraded behavior.
          console.warn('[Toast] ack failed', err && err.message);
        });
      }
    }
  }

  /**
   * Bulk dismiss every tracked toast for a given session id. Called
   * when a session is destroyed so the user doesn't see ghost toasts
   * referencing a dead session. Does NOT sync to the server — the
   * session is gone, the toasts are gone with it server-side.
   */
  dismissBySession(sessionId) {
    if (!sessionId) return;
    for (const [id, entry] of Array.from(this._byId.entries())) {
      if (entry.toast && entry.toast.session_id === sessionId) {
        this.dismiss(id, { syncToServer: false });
      }
    }
  }

  /**
   * Drop all UI state without ack. Used on full logout / page tear-down
   * paths where the server-side ack is irrelevant.
   */
  clearAll() {
    for (const id of Array.from(this._byId.keys())) {
      this.dismiss(id, { syncToServer: false });
    }
  }

  /**
   * Backfill from a list of server-shape toasts (e.g. on session attach).
   * Each is fed through ``add`` which dedupes by id — safe to call twice.
   */
  backfill(toasts) {
    if (!Array.isArray(toasts)) return;
    // Server returns newest-first; render OLDEST-first so the visual
    // stacking order matches add-order (newest on top of the pile).
    for (let i = toasts.length - 1; i >= 0; i--) {
      this.add(toasts[i]);
    }
  }
}

// Singleton export — matches the pattern used by API, TerminalController.
window.ToastManager = new ToastManager();
console.log('[Toast Module] Exported as window.ToastManager:', window.ToastManager);
