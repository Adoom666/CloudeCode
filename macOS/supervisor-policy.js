/**
 * Should a dead server be restarted automatically, and how many times?
 *
 * WHY THIS EXISTS
 *
 * While diagnosing the 2026-08-25 adoption incident the orphaned server was
 * killed, and the app did not respawn one for at least 22 seconds. It stayed
 * down. There is no supervisor loop anywhere in the app, and spawn/adopt only
 * happen inside start(), which nothing calls again after an unexpected exit.
 * So ANY server death is permanent until the user notices the app has gone
 * quiet and clicks Start Server - and nothing tells him it went quiet.
 *
 * THE CALL: v1 auto-recovers, bounded. The argument, both directions.
 *
 * Against: a restart loop thrashes against a server that cannot start - a bad
 * .env, an occupied port, a broken venv - and it can MASK a real fault by
 * turning a hard failure into an intermittent one, which is much harder to
 * diagnose than the hard failure was. Both are real, and both are the reason
 * this is an explicit budget in a testable module rather than a `while (true)`
 * in an exit handler.
 *
 * For: doing nothing is measurably worse than either. The current behaviour is
 * a dead app whose menu bar does not say it is dead, and the recovery action -
 * click Start - is one the app could have taken instantly and correctly. A
 * user cannot act on a failure nobody told him about.
 *
 * So the shape is: at most three attempts, growing delays, and every attempt
 * SURFACED rather than silent. When the budget is spent it gives up, says so,
 * and leaves it to the user. Giving up is a real outcome, not a failure of
 * nerve: three failed starts inside ninety seconds is a fault a person needs
 * to look at, and continuing to retry would only bury it.
 *
 * THE SUBTLE PART is the budget RESET, and it is why recordUp() alone does not
 * clear anything. A crash loop that gets far enough to look healthy for a
 * moment would otherwise reset the budget on every cycle and retry forever -
 * an unbounded loop wearing a bounded loop's clothes, which is the worst of
 * both designs because it looks safe in review. The budget only clears after
 * the server has been continuously up for a real window, and the caller has to
 * come back and say so (noteStillUp) rather than the policy assuming it.
 *
 * No imports and an injectable clock, so the backoff can be MEASURED in a
 * plain node test rather than read. See tests/test_supervisor_backoff.node.mjs.
 *
 * @module supervisor-policy
 */

/** Schedule a restart after `delayMs`. */
const SUPERVISOR_RESTART = 'restart';
/** The budget is spent. Stop, and say so. */
const SUPERVISOR_GAVE_UP = 'gave-up';
/** Not our business: a deliberate stop, or the app is quitting. */
const SUPERVISOR_NOT_SUPERVISED = 'not-supervised';

/** Attempts before giving up. */
const DEFAULT_MAX_ATTEMPTS = 3;
/** Delay before each attempt. Length must be >= maxAttempts. */
const DEFAULT_DELAYS_MS = [5000, 15000, 45000];
/** How long the server must stay up before the budget is considered clear. */
const DEFAULT_HEALTHY_RESET_MS = 60000;

/**
 * Build a supervisor.
 *
 * @param {object} [options] - Tuning and injection.
 * @param {number} [options.maxAttempts=3] - Restart budget.
 * @param {number[]} [options.delaysMs] - Delay per attempt.
 * @param {number} [options.healthyResetMs=60000] - Continuous uptime required
 *   before the budget resets.
 * @param {() => number} [options.now] - Clock, for tests.
 * @returns {{recordDown: Function, recordUp: Function, noteStillUp: Function,
 *   reset: Function, status: Function}} The supervisor.
 *
 * @example
 * const sup = createSupervisor();
 * const d = sup.recordDown({ expected: false });
 * if (d.action === 'restart') setTimeout(start, d.delayMs);
 */
function createSupervisor(options = {}) {
  const maxAttempts = options.maxAttempts || DEFAULT_MAX_ATTEMPTS;
  const delaysMs = options.delaysMs || DEFAULT_DELAYS_MS;
  const healthyResetMs = Number.isFinite(options.healthyResetMs)
    ? options.healthyResetMs
    : DEFAULT_HEALTHY_RESET_MS;
  const now = typeof options.now === 'function' ? options.now : () => Date.now();

  let attempts = 0;
  let gaveUp = false;
  let upSince = null;
  let message = 'The server has not needed an automatic restart.';

  /**
   * Note that the server went down.
   *
   * @param {object} [event] - What happened.
   * @param {boolean} [event.expected] - True when the user or the app asked
   *   for the stop. A deliberate stop is never undone, or there would be no
   *   way to turn the server off.
   * @param {boolean} [event.quitting] - True while the app is shutting down.
   *   Respawning here would create precisely the orphan this whole change
   *   exists to prevent.
   * @returns {{action: string, attempt: number, maxAttempts: number,
   *   delayMs: number, message: string}} What to do, and what to say.
   */
  function recordDown(event = {}) {
    upSince = null;

    if (event.expected === true || event.quitting === true) {
      const why = event.quitting
        ? 'The app is quitting.'
        : 'The server was stopped on purpose.';
      message = why;
      return {
        action: SUPERVISOR_NOT_SUPERVISED,
        attempt: attempts,
        maxAttempts,
        delayMs: 0,
        message,
      };
    }

    if (gaveUp || attempts >= maxAttempts) {
      gaveUp = true;
      message =
        `The server has stopped unexpectedly ${maxAttempts} times, so Cloude ` +
        `Code gave up restarting it automatically. Use Start Server once the ` +
        `problem is fixed - the server log will say what went wrong.`;
      return {
        action: SUPERVISOR_GAVE_UP,
        attempt: attempts,
        maxAttempts,
        delayMs: 0,
        message,
      };
    }

    attempts += 1;
    const delayMs = delaysMs[Math.min(attempts - 1, delaysMs.length - 1)];
    message =
      `The server stopped unexpectedly. Restarting it in ` +
      `${Math.round(delayMs / 1000)}s (attempt ${attempts} of ${maxAttempts}).`;
    return {
      action: SUPERVISOR_RESTART,
      attempt: attempts,
      maxAttempts,
      delayMs,
      message,
    };
  }

  /**
   * Note that the server is up again. Deliberately does NOT clear the budget.
   *
   * Coming up is not the same as staying up, and treating them as the same is
   * what turns a bounded retry into an unbounded one.
   *
   * @returns {void}
   */
  function recordUp() {
    upSince = now();
    if (attempts > 0) {
      message =
        `The server was restarted automatically (attempt ${attempts} of ` +
        `${maxAttempts}). It will stop being counted once it has stayed up ` +
        `for ${Math.round(healthyResetMs / 1000)}s.`;
    }
  }

  /**
   * Tell the supervisor the server is STILL up. Clears the budget once the
   * server has been continuously up for the healthy window.
   *
   * Call this from whatever already polls health; it is cheap and idempotent.
   *
   * @returns {boolean} True if the budget was cleared by this call.
   */
  function noteStillUp() {
    if (upSince === null) {
      upSince = now();
      return false;
    }
    if (attempts === 0 && !gaveUp) return false;
    if (now() - upSince < healthyResetMs) return false;
    attempts = 0;
    gaveUp = false;
    message = 'The server recovered and has been stable since.';
    return true;
  }

  /**
   * Clear everything. For an explicit user-initiated start: having given up
   * must never lock the user out of trying again.
   *
   * @returns {void}
   */
  function reset() {
    attempts = 0;
    gaveUp = false;
    upSince = null;
    message = 'The server has not needed an automatic restart.';
  }

  /**
   * Current state, always renderable.
   *
   * @returns {{attempts: number, maxAttempts: number, gaveUp: boolean,
   *   message: string}} What the menu should say.
   */
  function status() {
    return { attempts, maxAttempts, gaveUp, message };
  }

  return { recordDown, recordUp, noteStillUp, reset, status };
}

module.exports = {
  createSupervisor,
  SUPERVISOR_RESTART,
  SUPERVISOR_GAVE_UP,
  SUPERVISOR_NOT_SUPERVISED,
  DEFAULT_MAX_ATTEMPTS,
  DEFAULT_DELAYS_MS,
  DEFAULT_HEALTHY_RESET_MS,
};
