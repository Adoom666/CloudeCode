/**
 * Setup / upgrade wizard client.
 *
 * A separate file rather than an inline block because the app's CSP is
 * `script-src 'self'` - an inline script is refused outright, silently as far
 * as the page is concerned, which would render a permanently empty wizard.
 *
 * It renders whatever GET /api/v1/setup/state returns and does not decide
 * anything itself. In particular it does NOT decide which mode to show: a
 * client that could choose "first run" would be a client that could ask for
 * the unauthenticated path. The server decides, this draws.
 *
 * A 401 from that endpoint is not an error state, it is the upgrade-review
 * mode saying "log in first", and it is rendered as the TOTP step.
 */

/** Endpoint returning everything the wizard renders. @type {string} */
const STATE_URL = '/api/v1/setup/state';

/**
 * Key under which the main web client stores its access token.
 *
 * MUST match client/js/api.js. Using a different key here does not fail
 * loudly - it just means the wizard never sees the session the user already
 * has, so the upgrade-review mode demands a TOTP code from somebody who is
 * already logged in, and the token it stores afterwards is invisible to the
 * rest of the app. tests/test_setup_signal.node.mjs pins the two together.
 *
 * @type {string}
 */
const TOKEN_KEY = 'claude_tunnel_token';

/**
 * Read the access token the main web client stored, if any.
 *
 * @returns {string|null} The token, or null when none is stored.
 */
function storedToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch (err) {
    // Private-mode or blocked storage. Not having a token is a real answer.
    return null;
  }
}

/**
 * Build request headers, attaching a bearer token when one is available.
 *
 * @param {object} [extra] - Additional headers to merge in.
 * @returns {object} Headers for fetch().
 */
function headers(extra) {
  const out = Object.assign({ Accept: 'application/json' }, extra || {});
  const token = storedToken();
  if (token) out.Authorization = 'Bearer ' + token;
  return out;
}

/**
 * Escape text for safe interpolation into markup.
 *
 * Config values are the user's own, but they land in innerHTML, and "it is
 * his own data" is how stored-XSS bugs get written.
 *
 * @param {*} value - Anything renderable.
 * @returns {string} HTML-safe text.
 */
function esc(value) {
  const div = document.createElement('div');
  div.textContent = value === undefined ? '' : String(value);
  return div.innerHTML;
}

/**
 * Render a value the way a human can read it.
 *
 * @param {*} value - A config value.
 * @returns {string} Pretty JSON, or a plain marker for absent values.
 */
function renderValue(value) {
  if (value === null || value === undefined) return '(not set)';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}

/**
 * Draw one setup check row, honouring the three-outcome rule.
 *
 * `passed === null` means the fact could not be evaluated. It gets its own
 * glyph and its own colour; collapsing it into a tick or a cross would be
 * reporting a verdict nobody measured.
 *
 * @param {{key: string, title: string, passed: (boolean|null), detail: string}} check
 * @returns {string} Markup for the row.
 */
function renderCheck(check) {
  let cls = 'unknown';
  let glyph = '?';
  if (check.passed === true) { cls = 'pass'; glyph = 'Y'; }
  else if (check.passed === false) { cls = 'fail'; glyph = 'N'; }
  return (
    '<div class="check" data-check="' + esc(check.key) + '" data-passed="' +
    (check.passed === null ? 'unknown' : String(check.passed)) + '">' +
    '<span class="mark ' + cls + '">' + glyph + '</span>' +
    '<span><strong>' + esc(check.title) + '</strong><br>' +
    '<span class="muted">' + esc(check.detail) + '</span></span></div>'
  );
}

/**
 * Draw one configuration item needing a decision.
 *
 * Each item states four things, which is precisely what the dialog this page
 * replaces did not: what the setting is, what his value is, what the shipped
 * default is, and what happens on each choice.
 *
 * @param {object} item - One entry from plan.items.
 * @param {number} index - Position, used to group the radio buttons.
 * @returns {string} Markup for the item card.
 */
function renderItem(item, index) {
  const name = 'decision-' + index;
  const takeNew = item.can_take_new_default
    ? '<label class="choice"><input type="radio" name="' + esc(name) +
      '" value="take_new" data-path="' + esc(item.path) + '">' +
      'Take the new default<span class="consequence">' +
      esc(item.if_you_take_the_new_default) + '</span></label>'
    : '';
  return (
    '<div class="card item" data-path="' + esc(item.path) + '" data-outcome="' +
    esc(item.outcome) + '">' +
    '<h3>' + esc(item.path) + '</h3>' +
    '<p><strong>' + esc(item.headline) + '</strong></p>' +
    '<p class="muted">' + esc(item.what_it_means) + '</p>' +
    '<div class="values">' +
    '<div class="value-box"><h4>Your value</h4><pre>' +
    esc(renderValue(item.yours)) + '</pre></div>' +
    '<div class="value-box"><h4>Shipped default</h4><pre>' +
    esc(renderValue(item.shipped_default)) + '</pre></div>' +
    '</div>' +
    '<div class="choices">' +
    '<label class="choice"><input type="radio" name="' + esc(name) +
    '" value="keep" data-path="' + esc(item.path) + '" checked>' +
    'Keep mine<span class="consequence">' + esc(item.if_you_keep_yours) +
    '</span></label>' + takeNew +
    '</div></div>'
  );
}

/**
 * Draw the exposure banner: which address is actually in force.
 *
 * Shows the effective address, not the configured one. During setup those
 * differ, and showing the configured value would be presenting an aspiration
 * as a fact.
 *
 * @param {object} exposure - The exposure block from the state response.
 * @returns {string} Markup.
 */
function renderExposure(exposure) {
  // Three outcomes, and the third gets its own banner rather than rendering
  // "Listening on null". An unmeasured bind is reported as unknown by the
  // server, and the configured address must not be substituted for it here -
  // that substitution is the defect this whole path exists to prevent.
  if (!exposure.effective_host) {
    return '<div class="banner" id="exposure-banner">' +
      'The address this server is listening on could not be determined. ' +
      'Your configured address is <code>' + esc(exposure.configured_host) +
      '</code>, which is what was requested and not proof of what is ' +
      'reachable. Restart the server from the menu bar to settle this.' +
      '</div>';
  }
  const cls = exposure.locked_down ? 'banner' : 'banner ok';
  let text = 'Listening on <code>' + esc(exposure.effective_host) + '</code>. ';
  if (exposure.locked_down) {
    text += 'Locked to this machine until setup finishes. Your configured ' +
      'address <code>' + esc(exposure.configured_host) +
      '</code> takes effect after setup completes and the server restarts.';
  } else {
    text += 'This is the address you configured.';
  }
  return '<div class="' + cls + '" id="exposure-banner">' + text + '</div>';
}

/**
 * Draw the TOTP login step shown when the wizard is protected.
 *
 * @returns {string} Markup.
 */
function renderLogin() {
  return (
    '<div class="banner" id="login-required">This instance is already set up, ' +
    'so the wizard is behind your authenticator.</div>' +
    '<div class="card"><h3>Enter your 6-digit code</h3>' +
    '<p class="muted">Same code you use to sign in to Cloude Code.</p>' +
    '<input type="text" id="totp-code" inputmode="numeric" autocomplete="one-time-code" ' +
    'maxlength="6" placeholder="000000">' +
    '<div class="actions"><button id="login-btn">Unlock</button></div>' +
    '<p class="muted" id="login-error"></p></div>'
  );
}

/**
 * Exchange a TOTP code for an access token and reload the wizard.
 *
 * @returns {Promise<void>}
 */
async function submitLogin() {
  const field = document.getElementById('totp-code');
  const errorLine = document.getElementById('login-error');
  errorLine.textContent = '';
  try {
    const resp = await fetch('/api/v1/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: (field.value || '').trim() })
    });
    const data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) {
      errorLine.textContent = data.detail || ('Login failed (HTTP ' + resp.status + ').');
      return;
    }
    // AuthTokenResponse carries access_token, with `token` kept as a
    // deprecated alias for pre-Item-5 clients. Prefer the real field.
    const token = data.access_token || data.token;
    if (!token) {
      errorLine.textContent = 'The server accepted the code but returned no token.';
      return;
    }
    window.localStorage.setItem(TOKEN_KEY, token);
    load();
  } catch (err) {
    errorLine.textContent = 'Could not reach the server: ' + err.message;
  }
}

/**
 * Collect the per-item choices currently selected on the page.
 *
 * @returns {Array<{path: string, choice: string}>} One entry per item.
 */
function collectDecisions() {
  const picked = document.querySelectorAll('.choices input[type=radio]:checked');
  return Array.prototype.map.call(picked, function (input) {
    return { path: input.getAttribute('data-path'), choice: input.value };
  });
}

/**
 * Apply the selected decisions and re-render.
 *
 * @returns {Promise<void>}
 */
async function applyDecisions() {
  const status = document.getElementById('apply-status');
  status.textContent = 'Applying...';
  try {
    const resp = await fetch('/api/v1/setup/apply', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ decisions: collectDecisions() })
    });
    const data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) {
      status.textContent = 'Failed: ' + (data.detail || ('HTTP ' + resp.status));
      return;
    }
    status.textContent = data.changed && data.changed.length
      ? 'Updated ' + data.changed.join(', ') + '. Backup: ' + data.backup
      : 'Nothing needed changing.';
    load();
  } catch (err) {
    status.textContent = 'Could not reach the server: ' + err.message;
  }
}

/**
 * Finish setup, then tell the user plainly what did and did not just change.
 *
 * The bind address does NOT move here. uvicorn binds once at startup, so the
 * configured address only applies after a restart. Saying "done" without
 * saying that would leave him believing he is reachable on an address he is
 * not, which is the whole failure mode this wizard was built to avoid.
 *
 * @returns {Promise<void>}
 */
async function finishSetup() {
  const status = document.getElementById('finish-status');
  status.textContent = 'Finishing...';
  try {
    const resp = await fetch('/api/v1/setup/finish', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' })
    });
    const data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) {
      status.textContent = 'Not yet: ' + (data.detail || ('HTTP ' + resp.status));
      return;
    }
    const body = document.getElementById('wizard-body');
    body.innerHTML =
      '<div class="banner ' + (data.restart_required ? '' : 'ok') +
      '" id="finish-result"><strong>Setup complete.</strong><br>' +
      esc(data.message) + '</div>' +
      (data.restart_required
        ? '<div class="card"><h3>Restart required</h3><p>Quit and reopen ' +
          'Cloude Code from the menu bar, or use Stop Server then Start ' +
          'Server. Until then this server stays on <code>127.0.0.1</code>.</p></div>'
        : '');
    document.getElementById('wizard-lede').textContent =
      'This instance now requires your authenticator.';
  } catch (err) {
    status.textContent = 'Could not reach the server: ' + err.message;
  }
}

/**
 * Render the whole wizard from a state response.
 *
 * @param {object} state - The parsed GET /api/v1/setup/state body.
 * @returns {void}
 */
function render(state) {
  const firstRun = state.mode === 'first_run';
  document.getElementById('wizard-title').textContent =
    firstRun ? 'Finish setting up Cloude Code' : 'Review your configuration';
  document.getElementById('wizard-lede').textContent = firstRun
    ? 'This instance is not set up yet. Nothing outside this machine can reach it until it is.'
    : 'Your configuration against the defaults shipped with this version. Nothing changes until you choose it.';

  let html = renderExposure(state.exposure);

  html += '<h2>Setup checks</h2><div class="card" id="setup-checks">' +
    state.setup.checks.map(renderCheck).join('') + '</div>';

  const plan = state.plan || {};
  html += '<h2>Configuration</h2>';

  if (plan.unreadable) {
    html += '<div class="banner danger" id="plan-unreadable">' +
      esc(plan.unreadable) + '</div>';
  } else if (!plan.items || plan.items.length === 0) {
    html += '<div class="banner ok" id="plan-clean">Nothing needs a decision. ' +
      'Your configuration matches the shipped defaults everywhere it matters.</div>';
  } else {
    if (!plan.had_base) {
      html += '<div class="card"><p class="muted">No record of the previous ' +
        'defaults exists yet, so anything differing from a shipped default is ' +
        'reported as undeterminable rather than guessed at. Your value is kept ' +
        'in every case. This is noisy exactly once.</p></div>';
    }
    html += plan.items.map(renderItem).join('');
    html += '<div class="actions"><button id="apply-btn">Apply my choices</button>' +
      '<span class="muted" id="apply-status"></span></div>';
  }

  if (plan.adopting && plan.adopting.length) {
    html += '<h2>Arriving automatically</h2><div class="card"><p class="muted">' +
      'You never changed these, so the new defaults apply with no decision ' +
      'needed.</p><pre>' +
      esc(plan.adopting.map(function (a) { return a.path; }).join('\n')) +
      '</pre></div>';
  }

  if (firstRun) {
    html += '<h2>Finish</h2><div class="card"><p>Finishing marks this ' +
      'instance as set up. The wizard then requires your authenticator, and ' +
      'the address you configured takes effect after a restart.</p>' +
      '<div class="actions"><button id="finish-btn">Finish setup</button>' +
      '<span class="muted" id="finish-status"></span></div></div>';
  }

  document.getElementById('wizard-body').innerHTML = html;

  const applyBtn = document.getElementById('apply-btn');
  if (applyBtn) applyBtn.addEventListener('click', applyDecisions);
  const finishBtn = document.getElementById('finish-btn');
  if (finishBtn) finishBtn.addEventListener('click', finishSetup);
}

/**
 * Fetch state and draw, or draw the login step on a 401.
 *
 * @returns {Promise<void>}
 */
async function load() {
  const body = document.getElementById('wizard-body');
  try {
    const resp = await fetch(STATE_URL, { headers: headers() });
    if (resp.status === 401) {
      document.getElementById('wizard-title').textContent = 'Review your configuration';
      document.getElementById('wizard-lede').textContent =
        'This instance is set up, so the wizard needs your authenticator.';
      body.innerHTML = renderLogin();
      document.getElementById('login-btn').addEventListener('click', submitLogin);
      document.getElementById('totp-code').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') submitLogin();
      });
      return;
    }
    if (!resp.ok) {
      body.innerHTML = '<div class="banner danger" id="state-error">The server ' +
        'answered HTTP ' + resp.status + ', so nothing about this instance could ' +
        'be determined. Nothing has been changed.</div>';
      return;
    }
    render(await resp.json());
  } catch (err) {
    body.innerHTML = '<div class="banner danger" id="state-error">Could not reach ' +
      'the server (' + esc(err.message) + '), so nothing about this instance could ' +
      'be determined.</div>';
  }
}

document.addEventListener('DOMContentLoaded', load);
