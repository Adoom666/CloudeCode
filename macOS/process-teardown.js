/**
 * Terminating a process and PROVING it terminated.
 *
 * WHY THIS MODULE EXISTS, AND WHY IT IS SEPARATE FROM server-manager.js
 *
 * On 2026-08-25 a Cloude Code v1.0.2 was quit at some point in the morning and
 * its Python server was still serving port 8000 four hours later: pid 5491,
 * PARENT PID 1. A child that outlives its parent is reparented to launchd, so
 * ppid 1 is the signature of an orphan, not of anything deliberate. The next
 * version of the app then found that healthy listener and adopted it, and a
 * v1.0.3 bundle spent four hours running v1.0.2's code.
 *
 * The precondition for that whole chain is a quit that does not reliably kill
 * the server, and the reason it did not is one line in server-manager.js:
 *
 *     this.process.kill('SIGTERM');
 *     await sleep(3000);
 *     if (this.process && !this.process.killed) {   // <- never true
 *       this.process.kill('SIGKILL');
 *     }
 *
 * `ChildProcess.killed` does not mean "the child is dead". Node's own
 * documentation is explicit: it means a signal was successfully SENT. The
 * SIGTERM two lines above sets it, so the SIGKILL branch is unreachable. The
 * escalation existed only as text. A uvicorn that declines to exit on SIGTERM
 * - holding a websocket, mid-request, waiting on a tmux child - was never
 * escalated against, and simply outlived the app that spawned it.
 *
 * That is a false claim of the most ordinary kind: the code reported a
 * completed teardown having measured nothing about the process. So this
 * module never asks a JavaScript object whether a process is dead. It asks
 * the KERNEL, with signal 0, and it keeps asking until it gets an answer or
 * runs out of the time it was given.
 *
 * It lives in its own file with NO imports for two reasons. It is the piece
 * that has to be right, and server-manager.js requires `electron` and `axios`
 * at module scope, which makes everything in it unloadable outside a running
 * Electron and therefore testable only by reading its source text. A rule
 * that can only be checked by grepping for it is not covered - see
 * tests/test_process_teardown.node.mjs, which spawns real processes and reads
 * the verdict off the kernel.
 */

/**
 * Whether a pid currently exists, according to the kernel.
 *
 * Signal 0 performs the permission and existence checks without delivering
 * anything. The three answers it can produce are all different and are all
 * kept apart here:
 *
 *   * no error  -> the process exists and we may signal it.
 *   * EPERM     -> the process EXISTS and we may not signal it. Only a live
 *                  process can produce EPERM, so this is ALIVE. Folding it
 *                  into "dead" is how a running server gets reported stopped.
 *   * ESRCH     -> no such process. This is the only "dead".
 *
 * @param {number} pid - Process id to test.
 * @returns {boolean} True when the process exists.
 *
 * @example
 * isAlive(process.pid); // true
 */
function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM means it is there and out of reach. That is alive.
    return err.code === 'EPERM';
  }
}

/**
 * Poll until a pid is gone, or until the budget is spent.
 *
 * @param {number} pid - Process id to watch.
 * @param {number} budgetMs - Total time to wait.
 * @param {number} pollMs - Interval between checks.
 * @returns {Promise<boolean>} True if the process is gone, false on timeout.
 */
async function waitForExit(pid, budgetMs, pollMs) {
  const deadline = Date.now() + budgetMs;
  // Check once before sleeping: a process that exited instantly should not
  // cost a full poll interval.
  while (isAlive(pid)) {
    if (Date.now() >= deadline) return false;
    const remaining = deadline - Date.now();
    await new Promise((resolve) =>
      setTimeout(resolve, Math.min(pollMs, Math.max(remaining, 1)))
    );
  }
  return true;
}

/**
 * Terminate a process and confirm from the kernel that it is gone.
 *
 * SIGTERM first, always: the server gets its chance to flush connections and
 * close its tmux handles. SIGKILL only once SIGTERM has been OBSERVED to have
 * failed, never on a timer alone, and the result says which one was needed so
 * a routine escalation is visible in the log rather than assumed.
 *
 * There are five outcomes and none of them is a guess:
 *
 *   * `already-gone`  - ESRCH on the first signal. Nothing was killed and the
 *                       result does not pretend otherwise.
 *   * `sigterm`       - it exited within the grace period.
 *   * `sigkill`       - it did not, SIGKILL was sent, and it is now gone.
 *   * `still-running` - SIGKILL was sent and the pid is STILL there. Rare
 *                       (an uninterruptible wait), and reported as a failure,
 *                       because the caller's next decision - whether the port
 *                       is free - depends on the truth here.
 *   * `not-permitted` - EPERM. We can neither kill it nor prove it dead.
 *                       CANNOT DETERMINE, and `terminated` is false.
 *   * `no-pid`        - nothing usable was passed. Note that NEGATIVE pids are
 *                       process GROUPS to kill(2); accepting one would signal
 *                       every process in the group, which can include us.
 *
 * @param {number} pid - Process id to terminate.
 * @param {object} [opts] - Timing knobs.
 * @param {number} [opts.graceMs=3000] - How long SIGTERM gets before SIGKILL.
 * @param {number} [opts.killMs=2000] - How long SIGKILL gets to take effect.
 * @param {number} [opts.pollMs=100] - Interval between liveness checks.
 * @returns {Promise<{terminated: boolean, outcome: string, escalated: boolean,
 *   waitedMs: number}>} What happened, measured.
 *
 * @example
 * const r = await terminateProcess(5491);
 * // { terminated: true, outcome: 'sigkill', escalated: true, waitedMs: 3012 }
 */
async function terminateProcess(pid, opts = {}) {
  const graceMs = Number.isFinite(opts.graceMs) ? opts.graceMs : 3000;
  const killMs = Number.isFinite(opts.killMs) ? opts.killMs : 2000;
  const pollMs = Number.isFinite(opts.pollMs) ? opts.pollMs : 100;
  const started = Date.now();

  if (!Number.isInteger(pid) || pid <= 0) {
    return {
      terminated: false,
      outcome: 'no-pid',
      escalated: false,
      waitedMs: 0,
    };
  }

  try {
    process.kill(pid, 'SIGTERM');
  } catch (err) {
    if (err.code === 'ESRCH') {
      return {
        terminated: true,
        outcome: 'already-gone',
        escalated: false,
        waitedMs: Date.now() - started,
      };
    }
    if (err.code === 'EPERM') {
      return {
        terminated: false,
        outcome: 'not-permitted',
        escalated: false,
        waitedMs: Date.now() - started,
      };
    }
    throw err;
  }

  if (await waitForExit(pid, graceMs, pollMs)) {
    return {
      terminated: true,
      outcome: 'sigterm',
      escalated: false,
      waitedMs: Date.now() - started,
    };
  }

  // SIGTERM was observed to have failed. THIS is the branch the shipped code
  // could never reach.
  try {
    process.kill(pid, 'SIGKILL');
  } catch (err) {
    if (err.code === 'ESRCH') {
      // It exited between the last poll and this signal.
      return {
        terminated: true,
        outcome: 'sigterm',
        escalated: false,
        waitedMs: Date.now() - started,
      };
    }
    if (err.code === 'EPERM') {
      return {
        terminated: false,
        outcome: 'not-permitted',
        escalated: true,
        waitedMs: Date.now() - started,
      };
    }
    throw err;
  }

  const gone = await waitForExit(pid, killMs, pollMs);
  return {
    terminated: gone,
    outcome: gone ? 'sigkill' : 'still-running',
    escalated: true,
    waitedMs: Date.now() - started,
  };
}

module.exports = { terminateProcess, isAlive, waitForExit };
