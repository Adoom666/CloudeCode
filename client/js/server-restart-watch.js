/**
 * ServerRestartWatch - "the connection to the server went away and came
 * back" detector.
 *
 * WHY THIS EXISTS: terminal.js already recovers from three specific WS
 * close codes. 4401/4400 (auth failed / malformed auth header) trigger a
 * token refresh, and 4404 ("server does not know this session_id")
 * triggers a re-resolve by tmux name. A real SERVER RESTART produces
 * NONE of them: the socket dies with an ordinary abnormal-close code
 * (1006/1001/1012/1013...) while the old process is going down, and the
 * new process is not listening yet, so nothing on the wire ever says
 * 4404. The generic bounded retry then burns every attempt against a
 * port that is not answering and the user has to hard-refresh.
 *
 * The missing case is "the socket closed for some other reason AND the
 * server is not reachable right now". This module owns exactly that:
 * probing the unauthenticated ``/health`` endpoint, and waiting for it
 * to answer with backoff and a hard overall ceiling so it can never spin
 * forever. It deliberately knows NOTHING about sessions, tmux, or
 * re-adoption - the caller (terminal.js) reuses its existing
 * ``_attemptReconnectByName()`` machinery once this reports the server
 * is reachable again.
 *
 * WHAT IT DOES NOT CLAIM: from inside a browser a failed probe means
 * "we cannot reach the server", nothing more. A server restart, a dead
 * upstream proxy and a dropped uplink are indistinguishable at this
 * layer, so this module never asserts "the server restarted" - the
 * status copy in SERVER_RESTART_STATUS says only what is actually known.
 * The ONE case that IS distinguishable is the client's own link being
 * down: ``navigator.onLine === false`` is a reliable negative, so
 * ``isClientOffline()`` splits that out, suppresses probes that could
 * not possibly succeed, and pauses the overall budget rather than
 * spending it on a machine with no network.
 *
 * Every timing/IO dependency is constructor-injectable so the logic is
 * testable in a plain Node vm sandbox (tests/test_restart_reconnect.node.mjs).
 */

/**
 * Tunables for the health-poll loop. Frozen so no caller can mutate the
 * shared defaults; per-instance overrides go through the constructor.
 *
 * healthPath      - unauthenticated liveness endpoint (src/main.py :/health)
 * firstDelayMs    - delay before the second probe
 * maxDelayMs      - backoff ceiling per individual wait
 * backoffFactor   - multiplier applied to the delay after each failed probe
 * ceilingMs       - overall budget; past this we stop and tell the user.
 *                   Time spent with the client offline is NOT charged
 *                   to it, because that is not the server's silence.
 * probeTimeoutMs  - per-probe abort timeout, so a hung socket cannot stall
 * offlineRecheckMs - how often navigator.onLine is re-read while offline
 */
const SERVER_RESTART_WATCH_DEFAULTS = Object.freeze({
    healthPath: '/health',
    firstDelayMs: 500,
    maxDelayMs: 5000,
    backoffFactor: 1.6,
    ceilingMs: 120000,
    probeTimeoutMs: 4000,
    offlineRecheckMs: 1000,
});

/**
 * WS close codes that already have a dedicated recovery path in
 * terminal.js and must NOT be re-handled as a possible outage.
 * 4400 = auth header present but empty (src/api/websocket.py :185). A
 *        direct sibling of 4401: the server ANSWERED and rejected the
 *        handshake, so it is provably not an outage. It keeps the
 *        pre-existing bounded id-based retry rather than a health wait.
 * 4401 = auth failure (refresh then reconnect).
 * 4404 = unknown session_id (re-resolve by tmux name).
 */
const RESTART_EXCLUDED_CLOSE_CODES = Object.freeze([4400, 4401, 4404]);

/**
 * Outcomes of waitForServer(). Callers switch on these instead of on
 * booleans, because "aborted" and "timeout" need different UI.
 */
const SERVER_RESTART_RESULT = Object.freeze({
    UP: 'up',
    ABORTED: 'aborted',
    TIMEOUT: 'timeout',
});

/**
 * Status-line strings for the outage path. Centralized here so the
 * banner text has one home and "waiting for the server" can never be
 * confused with the ordinary "reconnecting" state.
 *
 * The copy is deliberately about REACHABILITY, not about a restart. The
 * client cannot see why the server went silent, so it does not claim to:
 * OFFLINE is the one state it can assert (navigator says the link is
 * down), WAITING is the honest "no answer, cause unknown".
 */
const SERVER_RESTART_STATUS = Object.freeze({
    WAITING: 'No answer from server, retrying...',
    OFFLINE: 'No network connection, waiting...',
    BACK: 'Server reachable, restoring session...',
    UNREACHABLE: 'Cannot reach server',
});

class ServerRestartWatch {
    /**
     * Build a watch bound to one origin.
     *
     * Inputs: options (object, optional) - any key of
     *   SERVER_RESTART_WATCH_DEFAULTS, plus the injectable seams
     *   `fetchImpl` (function), `setTimeoutImpl` (function),
     *   `clearTimeoutImpl` (function), `nowImpl` (function -> number),
     *   `origin` (string).
     * Output: ServerRestartWatch.
     * Example: new ServerRestartWatch({ ceilingMs: 60000 })
     */
    constructor(options = {}) {
        const cfg = Object.assign({}, SERVER_RESTART_WATCH_DEFAULTS, options || {});

        this.healthPath = cfg.healthPath;
        this.firstDelayMs = cfg.firstDelayMs;
        this.maxDelayMs = cfg.maxDelayMs;
        this.backoffFactor = cfg.backoffFactor;
        this.ceilingMs = cfg.ceilingMs;
        this.probeTimeoutMs = cfg.probeTimeoutMs;
        this.offlineRecheckMs = cfg.offlineRecheckMs;

        const g = (typeof globalThis !== 'undefined') ? globalThis : {};
        this._fetch = cfg.fetchImpl
            || (typeof g.fetch === 'function' ? g.fetch.bind(g) : null);
        this._setTimeout = cfg.setTimeoutImpl || ((fn, ms) => setTimeout(fn, ms));
        this._clearTimeout = cfg.clearTimeoutImpl || ((id) => clearTimeout(id));
        this._now = cfg.nowImpl || (() => Date.now());
        // Reads the client's own link state. `navigator.onLine === false`
        // is the only reliable negative a browser gives us; anything else
        // (missing navigator, true) is treated as "assume we can reach
        // the network and let the probe decide".
        this._isOnline = cfg.onlineImpl || (() => {
            const nav = (typeof g.navigator !== 'undefined') ? g.navigator : null;
            return !(nav && nav.onLine === false);
        });
        this._origin = (typeof cfg.origin === 'string')
            ? cfg.origin
            : ((typeof window !== 'undefined' && window.location && window.location.origin)
                ? window.location.origin : '');

        // True while waitForServer() is running. Callers use it to make
        // the watch single-flight instead of stacking loops.
        this.active = false;
    }

    /**
     * Is this close code one we should treat as a possible OUTAGE (the
     * server, the path to it, or our own link)? True for everything
     * EXCEPT the codes the server itself sent us as a deliberate
     * rejection - those prove the server answered - and except a
     * null/undefined code (no signal to act on).
     *
     * Deliberately NOT called "isRestartCloseCode": the code alone
     * cannot tell a restart from a dropped uplink. The probe, plus
     * isClientOffline(), is what narrows it afterwards.
     *
     * Inputs: closeCode (number|null|undefined) - WS CloseEvent.code.
     * Output: boolean.
     * Example: ServerRestartWatch.isOutageCloseCode(1006) === true
     */
    static isOutageCloseCode(closeCode) {
        if (typeof closeCode !== 'number') return false;
        return RESTART_EXCLUDED_CLOSE_CODES.indexOf(closeCode) === -1;
    }

    /**
     * Is the CLIENT's own network link down? This is the one half of the
     * ambiguity a browser can resolve: when navigator reports offline,
     * the failure is local and a /health probe cannot possibly succeed,
     * so we must neither send it nor conclude anything about the server.
     *
     * Inputs: none.
     * Output: boolean - true only when the client is provably offline.
     * Example: if (watch.isClientOffline()) showOfflineBanner();
     */
    isClientOffline() {
        try {
            return this._isOnline() === false;
        } catch (err) {
            // A broken seam is not evidence of being offline.
            console.debug('[RestartWatch] online check failed:', err && err.message);
            return false;
        }
    }

    /**
     * Absolute URL of the health endpoint for this origin.
     *
     * Inputs: none.
     * Output: string.
     * Example: "https://box.local/health"
     */
    healthUrl() {
        return `${this._origin}${this.healthPath}`;
    }

    /**
     * Single liveness probe. Never throws: a rejected fetch IS the
     * signal this class exists to read, so a transport failure is a
     * handled result (false), not a swallowed error. It is still logged
     * at debug level with the reason so a real bug is diagnosable.
     *
     * Inputs: none.
     * Output: Promise<boolean> - true only on an HTTP-ok response.
     * Example: if (await watch.probe()) { /* server is answering *\/ }
     */
    async probe() {
        if (typeof this._fetch !== 'function') return false;

        let controller = null;
        let timer = null;
        const g = (typeof globalThis !== 'undefined') ? globalThis : {};
        if (typeof g.AbortController === 'function') {
            controller = new g.AbortController();
            timer = this._setTimeout(() => {
                try {
                    controller.abort();
                } catch (err) {
                    console.debug('[RestartWatch] probe abort failed:', err && err.message);
                }
            }, this.probeTimeoutMs);
        }

        try {
            const init = { method: 'GET', cache: 'no-store' };
            if (controller) init.signal = controller.signal;
            const res = await this._fetch(this.healthUrl(), init);
            return !!(res && res.ok);
        } catch (err) {
            // Expected while the server is down (TypeError from a refused
            // connection, AbortError from our own timeout). Logged, not
            // hidden, and reported as "not answering".
            console.debug('[RestartWatch] health probe failed:',
                err && (err.name || 'Error'), err && err.message);
            return false;
        } finally {
            if (timer !== null && timer !== undefined) this._clearTimeout(timer);
        }
    }

    /**
     * Poll the health endpoint until it answers, the caller aborts, or
     * the overall ceiling is spent. The FIRST probe fires immediately,
     * so a caller can use this both to detect "server not answering"
     * and to wait it out in one call.
     *
     * While the CLIENT is offline the loop sends no probe at all (it
     * cannot succeed and its failure would say nothing about the server)
     * and the time spent offline is NOT charged against ceilingMs, so a
     * long wifi drop cannot make us conclude the server is unreachable.
     *
     * Inputs: hooks (object, optional) -
     *   shouldAbort: () => boolean - checked before and after every
     *     probe; true means stop (the caller's session went away).
     *   onAttempt: ({attempt, elapsedMs, clientOffline}) => void - fired
     *     each pass so the caller can paint an honest banner; when
     *     clientOffline is true, no probe was sent this pass.
     * Output: Promise<'up'|'aborted'|'timeout'> (SERVER_RESTART_RESULT).
     * Example: await watch.waitForServer({ shouldAbort: () => !alive })
     */
    async waitForServer(hooks = {}) {
        const shouldAbort = (hooks && typeof hooks.shouldAbort === 'function')
            ? hooks.shouldAbort : () => false;
        const onAttempt = (hooks && typeof hooks.onAttempt === 'function')
            ? hooks.onAttempt : () => {};

        const startedAt = this._now();
        let delay = this.firstDelayMs;
        let attempt = 0;
        // Milliseconds spent with no local network. Subtracted from the
        // budget: silence we caused is not silence the server owes us.
        let offlineMs = 0;

        this.active = true;
        try {
            for (;;) {
                if (shouldAbort()) return SERVER_RESTART_RESULT.ABORTED;

                const offline = this.isClientOffline();
                attempt += 1;
                onAttempt({
                    attempt,
                    elapsedMs: (this._now() - startedAt) - offlineMs,
                    clientOffline: offline,
                });

                if (offline) {
                    await this._sleep(this.offlineRecheckMs);
                    offlineMs += this.offlineRecheckMs;
                    continue;
                }

                if (await this.probe()) return SERVER_RESTART_RESULT.UP;
                if (shouldAbort()) return SERVER_RESTART_RESULT.ABORTED;

                if (((this._now() - startedAt) - offlineMs) + delay >= this.ceilingMs) {
                    return SERVER_RESTART_RESULT.TIMEOUT;
                }

                await this._sleep(delay);
                delay = Math.min(Math.round(delay * this.backoffFactor), this.maxDelayMs);
            }
        } finally {
            this.active = false;
        }
    }

    /**
     * Promise-wrapped timer using the injected scheduler.
     *
     * Inputs: ms (number) - milliseconds to wait.
     * Output: Promise<void>.
     */
    _sleep(ms) {
        return new Promise((resolve) => { this._setTimeout(resolve, ms); });
    }
}

ServerRestartWatch.DEFAULTS = SERVER_RESTART_WATCH_DEFAULTS;
ServerRestartWatch.EXCLUDED_CLOSE_CODES = RESTART_EXCLUDED_CLOSE_CODES;
ServerRestartWatch.RESULT = SERVER_RESTART_RESULT;
ServerRestartWatch.STATUS = SERVER_RESTART_STATUS;

// Export the CLASS (not a singleton) - terminal.js owns one instance per
// Terminal so its lifetime matches the session it is recovering.
window.ServerRestartWatch = ServerRestartWatch;
console.log('[ServerRestartWatch Module] Exported as window.ServerRestartWatch');
