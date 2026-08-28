/**
 * The quit sequence: hold the quit open until the server is actually down.
 *
 * WHY THIS IS A MODULE AND NOT FOUR LINES IN main.js
 *
 * It was four lines in main.js, and they were wrong:
 *
 *     app.on('before-quit', async () => {
 *       if (statsUpdateInterval) clearTimeout(statsUpdateInterval);
 *       if (serverManager) await serverManager.stop();
 *     });
 *
 * Electron does not await an async `before-quit` listener. It calls the
 * function, receives a pending promise, discards it, and continues quitting.
 * So everything after the first `await` inside stop() ran in a race with the
 * main process's own teardown. On 2026-08-25 that race was observed going
 * BOTH ways on the same machine and the same build across different restarts:
 * sometimes the Python server died with the app, sometimes it outlived it,
 * got reparented to launchd (ppid 1), kept serving port 8000, and was adopted
 * four hours later by a NEWER version of the app. Nondeterministic, not
 * conditional - which is why "it usually works" was never evidence.
 *
 * The correct Electron shape is to defer: `preventDefault()` the first quit,
 * do the asynchronous work, then quit again for real. That is easy to write
 * and easy to write wrongly - an unguarded `preventDefault()` re-enters its
 * own handler on the second `app.quit()` and defers forever, turning a leaked
 * server into an app that cannot be quit at all.
 *
 * main.js cannot be loaded outside a running Electron, so anything left in it
 * can only be checked by reading its source text. Putting the sequencing here
 * - with no imports and every collaborator injected - means the ORDERING can
 * be driven and observed in a plain node test: that the quit is deferred,
 * that it is re-issued only after the stop resolves, that it is re-issued
 * exactly once, and that a second quit does not stop the server twice.
 *
 * @module quit-sequence
 */

/**
 * Build the `before-quit` listener.
 *
 * The returned function is stateful by design: it remembers whether the
 * deferred teardown has already run, which is what stops the re-issued quit
 * from re-entering it.
 *
 * Failure policy: if the teardown throws, the app STILL QUITS. A user who
 * pressed Cmd-Q and got an app that refuses to die because its cleanup threw
 * is worse off than one whose cleanup failed, and the error is reported
 * rather than swallowed. The one thing that must not happen is a silent
 * hang.
 *
 * @param {object} deps - Injected collaborators.
 * @param {() => Promise<any>} deps.teardown - Does the actual shutdown work
 *   (clear timers, stop the server). Awaited exactly once.
 * @param {() => void} deps.quit - Re-issues the quit once teardown resolves.
 * @param {(msg: string) => void} [deps.log] - Progress reporting.
 * @param {(msg: string, err: Error) => void} [deps.onError] - Called when
 *   teardown rejects. The quit proceeds regardless.
 * @returns {(event: {preventDefault: () => void}) => Promise<void>} The
 *   listener to hand to `app.on('before-quit', ...)`.
 *
 * @example
 * app.on('before-quit', createQuitHandler({
 *   teardown: async () => { clearTimeout(t); await serverManager.stop(); },
 *   quit: () => app.quit(),
 * }));
 */
function createQuitHandler({ teardown, quit, log, onError }) {
  if (typeof teardown !== 'function') {
    throw new TypeError('createQuitHandler requires a teardown function');
  }
  if (typeof quit !== 'function') {
    throw new TypeError('createQuitHandler requires a quit function');
  }
  const note = typeof log === 'function' ? log : () => {};
  const report = typeof onError === 'function' ? onError : () => {};

  // Three states, not two. `idle` -> `running` -> `done`. The middle one
  // exists because Electron can deliver a second before-quit while the first
  // teardown is still in flight (Cmd-Q twice, or Quit from the menu during a
  // slow stop). That second one must ALSO be deferred - letting it through
  // would quit the app out from under the teardown, which is the original
  // bug arriving by a different door - but it must not start a second
  // teardown.
  let phase = 'idle';

  return async function onBeforeQuit(event) {
    if (phase === 'done') {
      // The teardown has finished. This is our own re-issued quit (or a
      // later one); let it through untouched.
      return;
    }

    if (event && typeof event.preventDefault === 'function') {
      event.preventDefault();
    }

    if (phase === 'running') {
      note('Quit already in progress; waiting for the server to stop.');
      return;
    }

    phase = 'running';
    note('Quitting: stopping the server before the app goes away...');
    try {
      await teardown();
    } catch (err) {
      report('Teardown failed during quit; quitting anyway.', err);
    }
    phase = 'done';
    note('Teardown complete; quitting.');
    quit();
  };
}

module.exports = { createQuitHandler };
