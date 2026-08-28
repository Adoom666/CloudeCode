/**
 * May this app stop or restart the server, and what may it CLAIM afterwards?
 *
 * WHY THIS EXISTS
 *
 * The user clicked Restart Server, nothing happened, and he reported the menu
 * item as broken. It was not broken. server-manager.js read:
 *
 *     if (!this.ownedProcess) {
 *       console.log('Server was adopted, not owned - leaving it running.');
 *       this.state = 'stopped';
 *       return;
 *     }
 *
 * The guard is RIGHT and it stays: do not kill what you did not start. Two
 * other things about those four lines were not.
 *
 * 1. The refusal was invisible. It went to a console nobody reads and
 *    returned. From the menu that is indistinguishable from a dead click, so a
 *    correct decision presented as nothing at all got reported as a bug - and
 *    the user went looking for one that did not exist. A refusal must be
 *    visible, and where there is a legitimate way forward it should be offered
 *    rather than left as an exercise in Activity Monitor.
 *
 * 2. It then set `this.state = 'stopped'` about a server that was still
 *    serving. The tray renders that state, so the menu bar reported a stopped
 *    server while every request to it succeeded - a false claim manufactured
 *    by the refusal path itself, in the one component whose entire job is to
 *    say what is going on without being asked.
 *
 * WHAT RESTART SHOULD DO, ARGUED
 *
 * "Refuse" and "take over" are both defensible, so here is the reasoning
 * rather than the conclusion alone.
 *
 * Since the version gate landed (adoption-decision.js), a server can only be
 * ADOPTED if it provably reports this bundle's own version. So the strong form
 * of the objection - "we might be killing a stranger's process" - no longer
 * holds for an adopted server: we know precisely what it is, because we
 * checked before adopting it. What remains true is that we did not start it,
 * and it may be serving someone right now.
 *
 * That is a reason to ASK, not a reason to refuse flatly. Refusing flatly is
 * actively unhelpful here: an adopted server is the leftover from a previous
 * run and therefore the single most likely one to be wedged and to need the
 * restart, and it is the one case where the user has no other button to press.
 *
 * So the policy is: refuse by default, say why in words worth showing, and
 * offer to take ownership. The app never makes that call on its own -
 * `takeOwnership` has to arrive as a literal `true`, so consent cannot ride in
 * on some truthy value that turned up by accident.
 *
 * THE STATE HALF IS THE SUBTLER HALF. A refusal must not invent a verdict
 * about a server it did not measure. Three outcomes: responding means
 * 'running', not responding means 'stopped', and anything else means null -
 * leave the state exactly as it was. Overwriting the state from a guess is the
 * same defect as the original, just better dressed.
 *
 * No imports, so all of this can be driven in a plain node test:
 * server-manager.js requires electron and axios at module scope. See
 * tests/test_adopted_server_refusals_are_visible.node.mjs.
 *
 * @module ownership-policy
 */

/** Actions this policy governs. Anything else is refused rather than allowed. */
const GOVERNED_ACTIONS = ['stop', 'restart'];

/**
 * Decide whether a stop or restart may proceed, and what state a refusal may
 * leave behind.
 *
 * @param {object} [input] - The situation.
 * @param {string} [input.action] - 'stop' or 'restart'. Anything else is
 *   refused: an unrecognised action defaulting to "permitted" is how a typo
 *   turns into a kill.
 * @param {boolean} [input.owned] - Whether this app spawned the server.
 * @param {boolean|null} [input.serverResponding] - Whether the server was just
 *   observed answering. `true`/`false` only; anything else, including
 *   undefined, means it was not measured.
 * @param {boolean} [input.takeOwnership] - Explicit user consent to stop a
 *   server this app did not start. Must be literally `true`.
 * @returns {{permitted: boolean, reason: string, offerTakeOwnership: boolean,
 *   stateAfterRefusal: string|null}} `stateAfterRefusal` of null means CHANGE
 *   NOTHING - it is not a state, it is the absence of a measurement.
 *
 * @example
 * decideLifecycleAction({ action: 'restart', owned: false, serverResponding: true });
 * // { permitted: false, offerTakeOwnership: true, stateAfterRefusal: 'running', ... }
 */
function decideLifecycleAction(input) {
  const source = input || {};
  const action = source.action;
  const owned = source.owned === true;
  const consented = source.takeOwnership === true;

  // Only `true`/`false` are measurements. Everything else - including the
  // extremely common `undefined` from a caller that did not probe - is
  // "nothing was measured", and must not become a state.
  let stateAfterRefusal = null;
  if (source.serverResponding === true) stateAfterRefusal = 'running';
  else if (source.serverResponding === false) stateAfterRefusal = 'stopped';

  if (!GOVERNED_ACTIONS.includes(action)) {
    return {
      permitted: false,
      reason: `Unrecognised server action ${JSON.stringify(action)}.`,
      offerTakeOwnership: false,
      stateAfterRefusal: null,
    };
  }

  if (owned || consented) {
    return {
      permitted: true,
      reason: '',
      offerTakeOwnership: false,
      stateAfterRefusal: null,
    };
  }

  const verb = action === 'stop' ? 'stop' : 'restart';
  return {
    permitted: false,
    offerTakeOwnership: true,
    stateAfterRefusal,
    reason:
      `Cloude Code did not start the server that is running, so it will not ` +
      `${verb} it on its own. It was already running when this app launched ` +
      `and was adopted rather than replaced. Cloude Code can take ownership ` +
      `of it - stopping it and starting its own in its place - but that is ` +
      `your call to make, not the app's.`,
  };
}

module.exports = { decideLifecycleAction, GOVERNED_ACTIONS };
