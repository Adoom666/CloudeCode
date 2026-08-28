// tray-status.js - decide what the menu-bar icon should look like.
//
// WHY THIS FILE EXISTS
//
// The tray icon is the only part of this app visible without clicking
// anything, so it is the only place a problem can announce itself. Until now
// it was a single fixed glyph: identical whether the server was healthy, dead,
// or unreachable. The user asked for three signals in it - server state,
// sessions needing attention, and an available update.
//
// THE THREE-OUTCOME RULE IS THE WHOLE POINT OF THIS MODULE.
//
// An icon that looks healthy when the app could not reach the server is
// exactly the false green this project has spent its time eliminating. So
// "could not determine" is a first-class state here with its OWN appearance
// (a hollow ring, never a filled dot, never the plain healthy glyph). A
// filled dot means "I measured this and it is true". A hollow ring means "I
// have no settled measurement" - either I could not ask, or the thing has
// not finished happening yet. Absence of a dot means "I measured this and
// there is nothing to report". Those are three different claims and they get
// three different pictures.
//
// THE GLYPH IS NEVER DIMMED. It used to be, for starting/crashed/stopped,
// and that dimming was carrying real meaning. It has all been moved into the
// dot's fill and hue - see the state table in scripts/generate-tray-icons.py
// for the two axes and why there are only two. Undimming without moving it
// would have made "stopped" pixel-identical to "ok".
//
// A NOTE ON WHICH SIGNAL DRIVES THE ICON
//
// Only ONE unknown escalates the icon to the unknown state: an
// undeterminable SESSION list. That is deliberate, and it is not a collapse
// of the third outcome. The icon answers one question - "does something need
// your attention right now" - and the session list is the input to that
// question. The update check is a different question, it routinely cannot
// reach GitHub on an offline laptop, and letting it pin the icon to "unknown"
// forever would turn the icon into furniture: a warning that never clears is
// not a monitor.
//
// Nothing is silently dropped, though. describeSignals() below reports every
// signal's own three-state verdict separately, and the caller renders all of
// them into the tooltip and the menu. The icon prioritises; the text tells
// the whole truth.

'use strict';

const path = require('path');

/**
 * Session activity statuses that mean a human is needed.
 *
 * Mirrors the server's vocabulary in src/core/session_status.py:
 *   question        - a Notification/PermissionRequest hook fired and nothing
 *                     has resolved it. Claude is literally waiting on him.
 *   finished_unread - a Stop hook landed and he has not looked yet.
 *   dead            - the session's pane is gone.
 *
 * "unknown" is deliberately NOT here. An unknown session status is a
 * measurement failure, not an attention signal, and it is routed to the
 * unknown outcome instead so it cannot masquerade as a definite alarm.
 *
 * @type {ReadonlyArray<string>}
 */
const ATTENTION_STATUSES = Object.freeze(['question', 'finished_unread', 'dead']);

/**
 * The session status meaning the server could not resolve it.
 * @type {string}
 */
const STATUS_UNKNOWN = 'unknown';

/**
 * Every tray state this module can return, most urgent first. Exported so a
 * test can assert the asset table covers all of them rather than trusting a
 * hand-maintained second list.
 * @type {ReadonlyArray<string>}
 */
const TRAY_STATES = Object.freeze([
  'crashed',
  'attention',
  'unknown',
  'starting',
  'stopped',
  'update',
  'ok',
]);

/**
 * Count the sessions that need a human, and the ones whose status the server
 * could not resolve.
 *
 * @param {Array<{activity_status?: string}>} sessions - Rows from
 *   GET /api/v1/sessions/list.
 * @returns {{attention: number, unknown: number, total: number}} Counts.
 */
function countSessionSignals(sessions) {
  const rows = Array.isArray(sessions) ? sessions : [];
  let attention = 0;
  let unknown = 0;

  for (const row of rows) {
    const status = row && row.activity_status;
    if (ATTENTION_STATUSES.includes(status)) attention += 1;
    else if (status === STATUS_UNKNOWN || !status) unknown += 1;
  }

  return { attention, unknown, total: rows.length };
}

/**
 * Decide the tray state from everything currently known.
 *
 * Precedence, most urgent first: crashed, stopped, starting, then for a
 * running server: attention, unknown, update, ok. Server state comes first
 * because when the server is not up, the session and update signals are not
 * merely unknown, they are meaningless.
 *
 * @param {{serverState: string, lastExitUnexpected?: boolean,
 *   sessions?: (Array<object>|null), sessionsReachable?: boolean,
 *   updateStatus?: (string|null),
 *   setupStatus?: (string|null)}} input - Current knowledge.
 *   `setupStatus` is the server's own verdict ('complete', 'incomplete',
 *   'undetermined') or null when it has not been asked. Null is NOT treated
 *   as complete: an unpolled instance is unknown, not healthy.
 *   `sessions` null or `sessionsReachable` false both mean the session list
 *   could not be determined; they are NOT the same as an empty list, which
 *   means the server was asked and genuinely has no sessions.
 * @returns {{state: string, attentionCount: number, unknownCount: number,
 *   sessionCount: number, reason: string}} The chosen state plus the counts
 *   the caller needs for its labels, and a short human reason.
 */
function deriveTrayState(input) {
  const serverState = (input && input.serverState) || 'stopped';
  const sessionsReachable =
    input && input.sessionsReachable !== undefined
      ? Boolean(input.sessionsReachable)
      : Array.isArray(input && input.sessions);
  const counts = countSessionSignals(input && input.sessions);
  const updateStatus = (input && input.updateStatus) || null;
  const setupStatus = (input && input.setupStatus) || null;

  const base = {
    attentionCount: counts.attention,
    unknownCount: counts.unknown,
    sessionCount: counts.total,
  };

  if (serverState === 'stopped') {
    if (input && input.lastExitUnexpected) {
      return { ...base, state: 'crashed', reason: 'the server exited on its own' };
    }
    return { ...base, state: 'stopped', reason: 'the server is stopped' };
  }

  if (serverState === 'starting') {
    return { ...base, state: 'starting', reason: 'the server is starting' };
  }

  if (!sessionsReachable) {
    return {
      ...base,
      state: 'unknown',
      reason: 'the session list could not be read',
    };
  }

  // Setup comes before session attention, and reuses the SAME 'attention'
  // state rather than introducing a second notion of "needs work". An
  // instance that is not set up cannot be judged by its sessions - it has
  // none - and it is pinned to loopback until somebody finishes it, so it is
  // the more urgent of the two.
  if (setupStatus === 'incomplete' || setupStatus === 'undetermined') {
    return {
      ...base,
      state: 'attention',
      reason:
        setupStatus === 'incomplete'
          ? 'setup is not finished'
          : 'setup state could not be determined',
    };
  }

  if (counts.attention > 0) {
    const plural = counts.attention === 1 ? 'session needs' : 'sessions need';
    return {
      ...base,
      state: 'attention',
      reason: `${counts.attention} ${plural} attention`,
    };
  }

  if (counts.unknown > 0) {
    const plural = counts.unknown === 1 ? 'session' : 'sessions';
    return {
      ...base,
      state: 'unknown',
      reason: `${counts.unknown} ${plural} reported an unknown status`,
    };
  }

  if (updateStatus === 'update_available') {
    return { ...base, state: 'update', reason: 'an update is available' };
  }

  return { ...base, state: 'ok', reason: 'running, nothing needs attention' };
}

/**
 * Report each signal's own verdict, so the tooltip and menu can state all
 * three explicitly instead of letting the single icon state stand in for
 * them. This is what keeps the icon's prioritisation from becoming a silent
 * collapse of the third outcome.
 *
 * @param {object} input - Same shape as deriveTrayState's argument.
 * @returns {{server: string, sessions: string, update: string}} One short
 *   phrase per signal. Any of them may say "cannot determine".
 */
function describeSignals(input) {
  const derived = deriveTrayState(input);
  const serverState = (input && input.serverState) || 'stopped';

  let server = serverState;
  if (derived.state === 'crashed') server = 'crashed';

  let sessions;
  if (serverState !== 'running') {
    sessions = 'cannot determine (server not running)';
  } else if (derived.state === 'unknown' && derived.sessionCount === 0) {
    sessions = 'cannot determine';
  } else if (derived.attentionCount > 0) {
    sessions = `${derived.attentionCount} of ${derived.sessionCount} need attention`;
  } else if (derived.unknownCount > 0) {
    sessions = `${derived.unknownCount} of ${derived.sessionCount} unknown`;
  } else {
    sessions = `${derived.sessionCount} running, none need attention`;
  }

  const rawUpdate = (input && input.updateStatus) || null;
  let update;
  if (rawUpdate === 'update_available') update = 'update available';
  else if (rawUpdate === 'current') update = 'up to date';
  else update = 'cannot determine';

  // Three outcomes, and the third is not spelled like either of the others.
  // `null` here means the server has not been asked yet, which is a different
  // thing from the server answering "I could not tell" (`undetermined`);
  // both are reported, neither is rendered as healthy.
  const rawSetup = (input && input.setupStatus) || null;
  let setup;
  if (rawSetup === 'complete') setup = 'complete';
  else if (rawSetup === 'incomplete') setup = 'not finished';
  else if (rawSetup === 'undetermined') setup = 'cannot determine';
  else setup = 'cannot determine (not polled yet)';

  return { server, sessions, update, setup };
}

/**
 * Build the tray tooltip. Names every signal, including the ones that could
 * not be measured, because a blank cell is not actionable.
 *
 * @param {object} input - Same shape as deriveTrayState's argument.
 * @returns {string} A multi-line tooltip string.
 */
function buildTooltip(input) {
  const signals = describeSignals(input);
  return [
    'Cloude Code',
    `Server: ${signals.server}`,
    `Sessions: ${signals.sessions}`,
    `Update: ${signals.update}`,
    `Setup: ${signals.setup}`,
  ].join('\n');
}

/**
 * Describe the bind row: what address is IN EFFECT, and what to do about it.
 *
 * THE DEFECT THIS EXISTS TO PREVENT, measured on a fresh install. The menu
 * built this row from getBindHost() - CONFIGURATION - and printed it as
 * though it were a measurement. A slow bootstrap meant the server had
 * evaluated setup as incomplete at startup, the lockdown pinned uvicorn to
 * loopback, and uvicorn bound once and never rebound. So the menu said
 * "hosting on 0.0.0.0" about a server nothing off-host could reach. Nothing
 * errored, and the address it printed was entirely plausible.
 *
 * Three outcomes. The one that matters is the third: when the effective bind
 * is unknown, the LABEL carries no address at all. Not the configured one,
 * not a guess, nothing - because a plausible wrong address is worse than an
 * admitted blank, and the reader has no way to tell the two apart. What
 * configuration asks for still goes in the DETAIL, named as a request rather
 * than as the address in force.
 *
 * @param {{effectiveHost: (string|null), configuredHost: (string|null)}}
 *   input - The address the server reported binding (null or empty when it
 *   has not said) and the address configuration asks for.
 * @returns {{known: boolean, applied: boolean, label: string,
 *   detail: string}} `known` is whether the effective bind was measured;
 *   `applied` is whether configuration is actually in force, and is false
 *   whenever it is not known to be.
 *
 * @example
 * describeBind({effectiveHost: '127.0.0.1', configuredHost: '0.0.0.0'}).label
 * // 'Bind: 127.0.0.1 in effect - restart to apply 0.0.0.0'
 */
function describeBind(input) {
  const effective = (input && input.effectiveHost) || null;
  const configured = (input && input.configuredHost) || null;

  if (!effective) {
    return {
      known: false,
      applied: false,
      label: 'Bind: unknown (not measured)',
      detail:
        'The address this server is listening on has not been measured. ' +
        (configured
          ? 'Configuration asks for ' +
            configured +
            ', which is what was requested and not proof of what is ' +
            'reachable.'
          : 'No bind address is configured either.'),
    };
  }

  if (configured && effective !== configured) {
    return {
      known: true,
      applied: false,
      // The address IN FORCE leads. Reading order is the whole point: he
      // read the configured value first and concluded he was on the LAN.
      label:
        'Bind: ' + effective + ' in effect - restart to apply ' + configured,
      detail:
        'Listening on ' +
        effective +
        '. Configuration asks for ' +
        configured +
        ', which needs a server restart to apply - the server binds its ' +
        'socket once at startup and cannot move it.',
    };
  }

  return {
    known: true,
    applied: true,
    label: 'Bind: ' + effective + ' (in effect)',
    detail: 'Listening on ' + effective + ', which is what you configured.',
  };
}

/**
 * Label for the setup/config menu row.
 *
 * A STATUS IS NOT AN INSTRUCTION. The owner's words about the previous
 * version were that it "did mention setup, with warning but its not self
 * explanitory". "Setup incomplete" tells a person the state they are in and
 * leaves them to work out the next move; the bar here is that somebody who
 * has never seen this app knows what to do without asking.
 *
 * The exclamation marker is NOT added here - the caller derives it from the
 * tray state so the icon and the row cannot disagree about urgency.
 *
 * @param {string|null} setupStatus - The server's verdict: 'complete',
 *   'incomplete', 'undetermined', or null when it has not been asked.
 * @returns {string} The row text.
 */
function describeSetupRow(setupStatus) {
  if (setupStatus === 'incomplete') {
    return 'Setup not finished - open to finish it, then restart the server';
  }
  if (setupStatus === 'undetermined' || !setupStatus) {
    return 'Setup state unknown - open Setup and Config to check';
  }
  return 'Setup and Config...';
}

/**
 * Resolve which image file backs a tray state.
 *
 * EVERY state, including the healthy one, resolves to a generated
 * non-template image. That is deliberate and was arrived at by measurement.
 * A coloured status dot cannot survive a template image (AppKit discards the
 * RGB), so the non-healthy states had to be ordinary images. Leaving "ok" on
 * the template path meant the healthy state rendered through a DIFFERENT
 * AppKit path than every other state, and the two paths do not agree on
 * weight: measured in a real menu bar, the template glyph landed at p90
 * luminance 70 while a full-opacity ordinary glyph landed at 166, and
 * "stopped" came out BRIGHTER than "ok". A stopped server was
 * indistinguishable from a healthy one, which is the precise false green this
 * icon exists to prevent. One path for all states makes them consistent by
 * construction rather than by coincidence.
 *
 * The @2x variants sit beside each file and AppKit picks them up from the
 * filename convention, so only the 1x path is returned.
 *
 * @param {string} state - One of TRAY_STATES.
 * @param {boolean} isDarkMenuBar - True when the menu bar is dark, from
 *   nativeTheme.shouldUseDarkColors.
 * @param {string} assetsDir - Absolute path of macOS/assets.
 * @returns {{path: string, isTemplate: boolean}} The image to load and
 *   whether it must be flagged as a template image. isTemplate is always
 *   false; it is still returned so the caller has one place to change if a
 *   future state ever goes back to being a template.
 */
function resolveIconAsset(state, isDarkMenuBar, assetsDir) {
  const appearance = isDarkMenuBar ? 'dark' : 'light';
  return {
    path: path.join(assetsDir, 'tray', `tray-${state}-${appearance}.png`),
    isTemplate: false,
  };
}

module.exports = {
  ATTENTION_STATUSES,
  describeBind,
  describeSetupRow,
  TRAY_STATES,
  countSessionSignals,
  deriveTrayState,
  describeSignals,
  buildTooltip,
  resolveIconAsset,
};
