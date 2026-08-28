/**
 * Toast Manager
 * ----------------------------------------------------------------------
 * Renders toast notifications surfaced by the server over the WebSocket
 * (toast.new / toast.ack frames) AND backfilled via the REST endpoint
 * on session attach. Per-toast accent color is sourced from the server's
 * project-theme resolution and applied as `style="--toast-accent: <hex>"`
 * on the toast element - the CSS picks it up as a left border.
 *
 * WHAT THESE TOASTS ARE, because it determines everything below: they are
 * NOT ephemeral snackbars. There is no dwell timer anywhere in this file
 * and there deliberately still is not one. A toast is a server-backed
 * record that survives a reload, is re-delivered by the attach backfill,
 * and disappears only when ACKED. An auto-dismiss would have to either
 * auto-ack (silently destroying a PermissionRequest the user never saw)
 * or hide-without-acking (a ghost that returns on the next attach). Both
 * are worse than a tall stack. Hover-to-pause is likewise moot with no
 * timer to pause.
 *
 * THE PILE-UP, and where it comes from. Every emitter is a Claude Code
 * hook (src/core/claude_hooks.py TOAST_EVENTS) and exactly one of the
 * three is chatty: `Stop` fires once per assistant turn, always with the
 * literal title "Your turn". A ten-turn session is ten identical cards
 * nobody acked. `Notification` is occasional; `PermissionRequest` is rare
 * and is the one the user actually has to act on.
 *
 * SO THE STACKING POLICY IS THREE RULES, each declared rather than
 * inferred:
 *
 *   1. COALESCE repeats into one card with a count. The pre-existing
 *      dedupe is by `id` only - that covers the backfill/WS race and
 *      nothing else. Ten `Stop` events are ten distinct ids saying one
 *      thing. `COALESCE_KEY` below declares, per kind, what "the same
 *      thing" means, and PermissionRequest is deliberately excluded.
 *   2. CAP the number of visible cards and put the rest behind ONE
 *      overflow row that states how many it is holding and the worst
 *      severity in there. Nothing is dropped and nothing is auto-acked:
 *      an overflowed toast is still unacked on the server, still in
 *      `_byId`, still counted on screen, and one click from view.
 *   3. TIER by severity. High-severity groups sort to the top AND are
 *      exempt from the cap entirely - the cap exists to suppress noise,
 *      and a blocking permission prompt is not noise. So a
 *      PermissionRequest can never be the thing hidden behind "+7 more".
 *
 * NEVER SILENTLY LOSE SOMETHING THE USER NEEDED. Every suppression path
 * above stays reachable: the cap holds a live, counted, one-click set and
 * exempts high severity; a coalesced card carries every member id and
 * acks all of them on dismiss, so no member is orphaned server-side; and
 * nothing in this file expires on a timer.
 *
 * Dismiss flow:
 *   - User clicks x  -> fade-out animation -> POST to
 *     /api/v1/toasts/<id>/ack for EVERY member of the group -> server
 *     broadcasts toast.ack -> other tabs dismiss in lockstep (with
 *     syncToServer=false to skip the round-trip).
 *   - Server-driven ack (from another browser) -> dismiss(id, {syncToServer:
 *     false}) - no echo back to the server.
 *
 * No localStorage cross-tab sync; the WS broadcast is the source of truth
 * for ack propagation.
 */

console.log('[Toast Module] Loading...');

/**
 * Severity per toast kind. Drives sort order, cap exemption, and the
 * ARIA live-region politeness of the rendered card.
 *
 * PermissionRequest is HIGH because it is blocking: Claude is stopped
 * until the user answers. Notification is MEDIUM: Claude is waiting on
 * input but the card itself is not the decision. Stop is LOW: "your
 * turn" is information the terminal in front of the user already shows.
 */
const TOAST_SEVERITY = {
  PermissionRequest: 3,
  Notification: 2,
  Stop: 1,
};
const SEVERITY_DEFAULT = 2; // an unknown future kind is not assumed harmless

/** Groups at or above this severity are never pushed into overflow. */
const CAP_EXEMPT_SEVERITY = 3;

/** Visible card cap, before the exemption above raises it. */
const CAP_DESKTOP = 3;
const CAP_NARROW = 2; // a phone screen is mostly toast at 3+
const NARROW_QUERY = '(max-width: 640px)'; // matches toast.css's breakpoint

/**
 * What "the same notification, again" means, per kind. Returns null to
 * declare a kind NEVER coalesces.
 *
 * Stop ignores the body on purpose: its body is the tail of the
 * transcript, so two Stops are almost never byte-identical, yet the older
 * one is strictly superseded - only the newest "your turn" carries
 * information. Notification keys on the body because its body IS the
 * message; two different messages are two different things to read.
 * PermissionRequest never coalesces because each one is a distinct
 * decision about a distinct command, and "x2" would hide the second
 * command string - exactly the loss this policy exists to prevent.
 *
 * Inputs: toast (object) - server-shape toast.
 * Output: string|null - the coalesce key, or null for "never coalesce".
 * Example: COALESCE_KEY.Stop({session_id:'s',title:'Your turn'})
 *          -> 's|Stop|Your turn'
 */
const COALESCE_KEY = {
  Stop: (t) => `${t.session_id}|Stop|${t.title || ''}`,
  Notification: (t) => `${t.session_id}|Notification|${t.title || ''}|${t.body || ''}`,
  PermissionRequest: () => null,
};

class ToastManager {
  constructor(containerId = 'toast-container') {
    this.containerId = containerId;
    /** id -> server-shape toast. Insertion-ordered = arrival-ordered. */
    this._byId = new Map();
    /** User expanded the overflow row; the cap is suspended until reset. */
    this._expanded = false;
    /** Set true once the container has had its live-region attrs applied. */
    this._containerPrepared = false;
    this._narrowQuery = null;
    this._bindViewportWatch();
  }

  /**
   * Re-render when the viewport crosses the narrow breakpoint, because
   * the cap differs on either side of it and a stale cap would render a
   * count the overflow row contradicts.
   * Output: None.
   */
  _bindViewportWatch() {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    try {
      this._narrowQuery = window.matchMedia(NARROW_QUERY);
      const onChange = () => this._render();
      if (typeof this._narrowQuery.addEventListener === 'function') {
        this._narrowQuery.addEventListener('change', onChange);
      } else if (typeof this._narrowQuery.addListener === 'function') {
        this._narrowQuery.addListener(onChange); // Safari < 14
      }
    } catch (err) {
      // No matchMedia (test stub, ancient browser): fall back to the
      // desktop cap. Reported, never swallowed silently.
      console.warn('[Toast] viewport watch unavailable:', err && err.message);
    }
  }

  /**
   * Resolve the container element. Late-bound so the class can be
   * constructed before DOMContentLoaded - we look it up at first use.
   * Returns null if absent (e.g. on a page that doesn't include the
   * container markup); callers MUST guard for null.
   */
  _container() {
    const el = document.getElementById(this.containerId);
    if (el && !this._containerPrepared) {
      // Polite by default; an individual high-severity card upgrades
      // itself to role="alert" so a permission prompt interrupts.
      el.setAttribute('aria-live', 'polite');
      el.setAttribute('aria-relevant', 'additions text');
      this._containerPrepared = true;
    }
    return el;
  }

  /**
   * Severity for a toast kind.
   * Inputs: kind (string).
   * Output: number - higher is more urgent.
   */
  _severity(kind) {
    const s = TOAST_SEVERITY[kind];
    return typeof s === 'number' ? s : SEVERITY_DEFAULT;
  }

  /** Output: number - how many cards may render before overflow. */
  _cap() {
    const narrow = !!(this._narrowQuery && this._narrowQuery.matches);
    return narrow ? CAP_NARROW : CAP_DESKTOP;
  }

  /**
   * Collapse the live toast set into render groups.
   *
   * Output: Array of { key, toasts: [...], newest, count, severity },
   *   sorted severity-desc then newest-first. `newest` is the toast whose
   *   body and colour the card shows.
   */
  _groups() {
    const order = new Map();
    let idx = 0;
    for (const id of this._byId.keys()) order.set(id, idx++);
    const byKey = new Map();
    const singles = [];
    for (const toast of this._byId.values()) {
      const keyFn = COALESCE_KEY[toast.kind];
      const key = typeof keyFn === 'function' ? keyFn(toast) : null;
      if (!key) {
        singles.push({ key: `id:${toast.id}`, toasts: [toast] });
        continue;
      }
      const existing = byKey.get(key);
      if (existing) existing.toasts.push(toast);
      else byKey.set(key, { key, toasts: [toast] });
    }
    const groups = [...byKey.values(), ...singles].map((g) => {
      // Arrival order is insertion order, so the last member is newest.
      const newest = g.toasts[g.toasts.length - 1];
      return {
        key: g.key,
        toasts: g.toasts,
        newest,
        count: g.toasts.length,
        severity: this._severity(newest.kind),
      };
    });
    // Severity first so an actionable card is never below chatter; then
    // newest-first so the most recent thing in a tier reads at the top.
    groups.sort((a, b) => {
      if (b.severity !== a.severity) return b.severity - a.severity;
      return order.get(b.newest.id) - order.get(a.newest.id);
    });
    return groups;
  }

  /**
   * Add a toast to the UI.
   * @param {object} toast - shape: { id, session_id, kind, title, body,
   *   color, created_at, acknowledged }
   */
  add(toast) {
    if (!toast || !toast.id) return;
    if (toast.acknowledged) return; // server says already done; don't show
    if (!this._container()) return;
    // A KNOWN ID IS EITHER A DUPLICATE OR A SUPERSESSION, AND THE
    // DIFFERENCE IS IN THE CONTENT, NOT THE ID.
    //
    // The server replaces an unacked `Stop` in place and keeps its id
    // (SessionManager.record_toast), then broadcasts the replaced record.
    // So the same id can legitimately arrive again carrying a NEWER body
    // for the same turn-ended event. Returning early on a known id - the
    // old dedupe - kept the card showing the FIRST turn's transcript tail
    // for the rest of the session while the server held the newest.
    //
    // Refreshing in place is the whole handling: `Map.set` on an existing
    // key preserves insertion position, so arrival order (which drives
    // within-tier sort) does not change, the card is not re-created, and
    // the id the user will ack is the id the server holds. The
    // backfill/WS race this branch was written for still resolves to one
    // card, because re-storing identical content renders identically.
    if (this._byId.has(toast.id)) {
      this._byId.set(toast.id, toast);
      this._render();
      return;
    }
    this._byId.set(toast.id, toast);
    this._render();
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
    const toast = this._byId.get(toastId);
    if (!toast) return;
    this._byId.delete(toastId);

    const el = this._cardFor(toastId);
    if (el) {
      el.classList.add('toast--dismissing');
      setTimeout(() => {
        if (el && el.parentNode) el.parentNode.removeChild(el);
        this._render();
      }, 220); // slightly longer than the CSS transition (200ms)
    } else {
      this._render();
    }

    if (syncToServer && toast && toast.session_id) {
      this._ack(toastId, toast.session_id);
    }
  }

  /**
   * Dismiss every member of a coalesced group. Each member is acked
   * individually so no id is orphaned unacked on the server, which would
   * resurrect it on the next attach backfill.
   * Inputs: key (string) - a group key from `_groups()`.
   * Output: None.
   */
  dismissGroup(key) {
    const group = this._groups().find((g) => g.key === key);
    if (!group) return;
    for (const toast of group.toasts) {
      this.dismiss(toast.id, { syncToServer: true });
    }
  }

  /**
   * Fire-and-forget server ack.
   * Inputs: toastId (string), sessionId (string). Output: None.
   */
  _ack(toastId, sessionId) {
    if (window.API && typeof window.API.ackToast === 'function') {
      window.API.ackToast(toastId, sessionId).catch((err) => {
        // 404 / 500: log only - the local UI is already updated; a
        // failed server ack will simply re-deliver the toast on the
        // next attach backfill, which is acceptable degraded behavior.
        console.warn('[Toast] ack failed', err && err.message);
      });
    }
  }

  /**
   * The rendered card currently showing a given toast id, if any.
   * Inputs: toastId (string). Output: HTMLElement|null.
   */
  _cardFor(toastId) {
    const container = this._container();
    if (!container) return null;
    return container.querySelector(`.toast[data-toast-id="${toastId}"]`);
  }

  /**
   * Bulk dismiss every tracked toast for a given session id. Called
   * when a session is destroyed so the user doesn't see ghost toasts
   * referencing a dead session. Does NOT sync to the server - the
   * session is gone, the toasts are gone with it server-side.
   */
  dismissBySession(sessionId) {
    if (!sessionId) return;
    for (const [id, toast] of Array.from(this._byId.entries())) {
      if (toast && toast.session_id === sessionId) {
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
   * Each is fed through ``add`` which dedupes by id - safe to call twice.
   */
  backfill(toasts) {
    if (!Array.isArray(toasts)) return;
    // Server returns newest-first; ingest OLDEST-first so Map insertion
    // order is arrival order, which is what `_arrivalIndex` reads.
    for (let i = toasts.length - 1; i >= 0; i--) {
      this.add(toasts[i]);
    }
  }

  // --------------------------------------------------------------- render

  /**
   * Rebuild the visible card set from the live toast model.
   *
   * Rebuild-from-model rather than imperative append: the cap, the
   * coalesce counts and the overflow row are all functions of the whole
   * set, so any single-element mutation can invalidate all three. A card
   * already on screen for the same group key is REUSED (its count and
   * body updated in place) so an unrelated arrival does not restart every
   * neighbour's entry animation.
   *
   * Output: None.
   */
  _render() {
    const container = this._container();
    if (!container) return;

    const groups = this._groups();
    const cap = this._cap();
    const exemptCount = groups.filter((g) => g.severity >= CAP_EXEMPT_SEVERITY).length;
    // The cap suppresses NOISE. It never hides a blocking prompt, so it
    // is raised to fit every exempt group before it applies to the rest.
    const naturalCap = Math.max(cap, exemptCount);
    // Whether expansion is MEANINGFUL is a question about the natural cap,
    // never about the expanded one: while expanded, nothing is hidden by
    // construction, so asking "is the hidden set empty?" after applying
    // the expansion collapses the row the instant the user opens it.
    if (groups.length <= naturalCap) this._expanded = false;

    const effectiveCap = this._expanded ? groups.length : naturalCap;
    const visible = groups.slice(0, effectiveCap);
    const hidden = groups.slice(effectiveCap);

    const keep = new Set(visible.map((g) => g.key));
    for (const el of Array.from(container.querySelectorAll('.toast'))) {
      if (!keep.has(el.dataset.groupKey) && !el.classList.contains('toast--dismissing')) {
        el.remove();
      }
    }

    let prev = null;
    for (const group of visible) {
      const el = this._renderCard(group, container);
      // Order matters and groups are re-sorted on every render, so place
      // each card explicitly rather than trusting append order.
      if (prev) {
        if (prev.nextSibling !== el) container.insertBefore(el, prev.nextSibling);
      } else if (container.firstChild !== el) {
        container.insertBefore(el, container.firstChild);
      }
      prev = el;
    }

    this._renderOverflow(hidden, container, prev);
  }

  /**
   * Create or update one card.
   * Inputs: group (object) from `_groups()`; container (HTMLElement).
   * Output: HTMLElement - the card.
   */
  _renderCard(group, container) {
    const { newest, count, severity, key } = group;
    // NOT `.toast--dismissing`: a card mid-fade still carries its group
    // key for the 220ms the exit animation runs, so a new toast arriving
    // in that window would reuse the corpse and resurrect a card the user
    // just dismissed, half-faded.
    let el = container.querySelector(
      `.toast[data-group-key="${CSS_ESC(key)}"]:not(.toast--dismissing)`);
    const isNew = !el;
    if (isNew) {
      el = document.createElement('div');
      el.className = 'toast toast--entering';
      el.dataset.groupKey = key;
    }
    el.dataset.toastId = newest.id;
    el.dataset.kind = newest.kind || '';
    el.dataset.severity = String(severity);
    el.dataset.count = String(count);
    if (newest.color) el.style.setProperty('--toast-accent', newest.color);
    // A blocking prompt interrupts the screen reader; chatter does not.
    el.setAttribute('role', severity >= CAP_EXEMPT_SEVERITY ? 'alert' : 'status');

    el.textContent = '';

    const title = document.createElement('div');
    title.className = 'toast__title';
    // Two nodes, not one string: the count is styled separately, and a
    // single textContent would be indistinguishable from a title that
    // literally contains "x3".
    const titleText = document.createElement('span');
    titleText.className = 'toast__title-text';
    titleText.textContent = newest.title || '(untitled)';
    title.appendChild(titleText);
    if (count > 1) {
      const badge = document.createElement('span');
      badge.className = 'toast__count';
      badge.textContent = `×${count}`;
      badge.setAttribute('aria-label', `${count} occurrences`);
      title.appendChild(badge);
    }
    el.appendChild(title);

    // WHICH SESSION THIS IS ABOUT.
    //
    // A toast is not a view of a session, it is a record of a moment. It
    // can arrive for a session that is not on screen, it outlives the
    // session it names, and the attach backfill replays it later - so a
    // card with no session line reads as a card about whatever the user
    // happens to be looking at, which is the wrong session. The server
    // stamps session_label and session_name at record time, when the
    // identity is certainly knowable, and this renders them through the
    // one shared resolver.
    //
    // ITS OWN ELEMENT, never appended to the title. A title that happened
    // to contain the session name would be indistinguishable from this at
    // the .textContent level - which is exactly how a `~~claude` badge
    // shipped through a green suite in this codebase.
    //
    // THE THIRD OUTCOME IS SPOKEN, NOT DROPPED. A toast recorded before
    // the server carried identity has neither field. It says so. Silently
    // omitting the line would be the dishonest option.
    const session = document.createElement('div');
    session.className = 'toast__session';
    const resolved = window.SessionLabel
      ? window.SessionLabel.resolveToast(newest)
      : (newest.session_label || newest.session_name || null);
    session.textContent = resolved
      || (window.SessionLabel ? window.SessionLabel.UNKNOWN : 'unknown session');
    if (!resolved) session.dataset.unknown = '1';
    el.appendChild(session);

    if (newest.body) {
      const body = document.createElement('div');
      body.className = 'toast__body';
      body.textContent = newest.body;
      el.appendChild(body);
    }

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.className = 'toast__dismiss';
    const label = count > 1
      ? `Dismiss ${count} notifications`
      : 'Dismiss notification';
    dismissBtn.setAttribute('aria-label', label);
    dismissBtn.setAttribute('title', label);
    dismissBtn.textContent = '×';
    dismissBtn.addEventListener('click', () => this.dismissGroup(key));
    el.appendChild(dismissBtn);

    if (isNew) {
      container.appendChild(el);
      // Force a style flush BEFORE dropping the entering class. Without
      // it, a burst that appends and un-classes several cards inside one
      // task can leave the browser never having computed the entering
      // state, so the transition sometimes runs and sometimes does not -
      // measured flapping between both on identical input. A reflow read
      // pins it: the entering style is computed, so the transition always
      // runs and the end state is always reached.
      void el.offsetWidth;
      requestAnimationFrame(() => el.classList.remove('toast--entering'));
    }
    return el;
  }

  /**
   * Create, update or remove the single overflow row.
   *
   * The row is the reason the cap is not a data-loss bug: it states how
   * many toasts it is holding and, when the hidden set contains anything
   * above the lowest severity, says so in words. Clicking it suspends the
   * cap so every hidden card renders.
   *
   * Inputs: hidden (array) of groups; container (HTMLElement);
   *   prev (HTMLElement|null) - the last visible card, for placement.
   * Output: None.
   */
  _renderOverflow(hidden, container, prev) {
    let row = container.querySelector('.toast-overflow');
    if (hidden.length === 0 && !this._expanded) {
      if (row) row.remove();
      return;
    }
    if (!row) {
      row = document.createElement('button');
      row.type = 'button';
      row.className = 'toast-overflow';
      row.addEventListener('click', () => {
        this._expanded = !this._expanded;
        this._render();
      });
    }

    if (this._expanded) {
      row.textContent = 'Show fewer';
      row.dataset.hiddenCount = '0';
      row.dataset.worstSeverity = '0';
      row.setAttribute('aria-expanded', 'true');
    } else {
      const toastCount = hidden.reduce((n, g) => n + g.count, 0);
      const worst = hidden.reduce((m, g) => Math.max(m, g.severity), 0);
      row.dataset.hiddenCount = String(toastCount);
      row.dataset.worstSeverity = String(worst);
      row.setAttribute('aria-expanded', 'false');
      row.textContent = '';
      const n = document.createElement('span');
      n.className = 'toast-overflow__count';
      n.textContent = `+${toastCount} more`;
      row.appendChild(n);
      // Naming the worst thing in there is what keeps a suppressed item
      // from being silently lost: it is suppressed, and the screen still
      // says what kind of thing is suppressed.
      const worstLabel = OVERFLOW_SEVERITY_LABEL[worst];
      if (worstLabel) {
        const w = document.createElement('span');
        w.className = 'toast-overflow__worst';
        // Parenthesised because the two spans are read back as one
        // string by a screen reader: "+9 more9 waiting on you" is what
        // bare concatenation produces, and the flex gap only fixes the
        // sighted case.
        w.textContent = '(' + worstLabel(
          hidden.filter((g) => g.severity === worst).reduce((n2, g) => n2 + g.count, 0),
        ) + ')';
        row.appendChild(w);
      }
    }
    row.dataset.severity = row.dataset.worstSeverity;

    if (prev) {
      if (prev.nextSibling !== row) container.insertBefore(row, prev.nextSibling);
    } else if (container.firstChild !== row) {
      container.insertBefore(row, container.firstChild);
    }
  }
}

/**
 * Words for the worst severity sitting in overflow. Severity 3 is absent
 * on purpose: it is cap-exempt and can never be in there, so a label for
 * it would be dead code claiming a case that cannot occur.
 */
const OVERFLOW_SEVERITY_LABEL = {
  2: (n) => (n === 1 ? '1 waiting on you' : `${n} waiting on you`),
};

/**
 * Escape a string for use inside an attribute-selector value.
 *
 * Group keys contain `|` and arbitrary title text from the server, so
 * they cannot be interpolated into a selector raw. CSS.escape is the
 * right tool and exists in every browser this app supports; the fallback
 * is for the DOM stub the node suites run against.
 *
 * Inputs: value (string). Output: string - safe inside "..." in a selector.
 */
function CSS_ESC(value) {
  return String(value).replace(/["\\]/g, '\\$&');
}

// Singleton export - matches the pattern used by API, TerminalController.
window.ToastManager = new ToastManager();
console.log('[Toast Module] Exported as window.ToastManager:', window.ToastManager);
