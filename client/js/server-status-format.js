/**
 * Formatting and markup for the server-status panel.
 * ----------------------------------------------------------------------
 * SPLIT FROM THE CONTROLLER ON PURPOSE. Everything here is a pure
 * function of the snapshot the server sent, so tests/test_server_status_
 * panel.node.mjs can drive it with no DOM at all, and the controller next
 * door keeps only the things that need one (fetch, open, close, wire).
 *
 * THE THIRD OUTCOME IS THE WHOLE JOB OF THIS FILE. Every section of the
 * payload carries `available` and `error`. A section that could not be
 * measured renders the words "cannot determine" and its reason - never a
 * blank, never a zero, never a dash that reads like "nothing to report".
 * A memory row showing 0 bytes used because vm_stat was missing is the
 * exact false-green the server side is built to avoid, and throwing it
 * away in the renderer would undo all of it.
 *
 * NOTHING HERE IS A PILL. The settings screen was flattened at the user's
 * explicit request - no capsules, no fully-rounded badges - and this
 * panel matches it. Ownership and state are shown as plain uppercase
 * words, not as chips. If you add a control, declare its `border-radius`
 * (and its `width`/`height`): styles.css has a bare `button` element rule
 * setting all three, and a class only beats an element selector for the
 * properties it actually declares.
 *
 * SESSION NAMES ARE UNTRUSTED. They come from tmux and from user input.
 * Every one of them goes through esc() before it reaches markup, and the
 * literal name travels back to the click handler in a data attribute -
 * it is never interpolated into anything executable.
 */

console.log('[ServerStatusFormat Module] Loading...');

(function () {
    'use strict';

    /** What a section renders when it could not be measured. */
    var UNKNOWN = 'cannot determine';

    /**
     * Escape a string for interpolation into HTML text or a double-quoted
     * attribute. Deliberately string-only, with no DOM dependency, so the
     * node test exercises the real function rather than a stand-in.
     *
     * @param {*} value - anything; null and undefined become ''.
     * @returns {string} escaped text.
     */
    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Human-readable byte count, base 1024.
     *
     * @param {number|null|undefined} bytes
     * @returns {string} e.g. "31.2 gb", or UNKNOWN for a non-number.
     */
    function bytes(value) {
        if (typeof value !== 'number' || !isFinite(value) || value < 0) return UNKNOWN;
        var units = ['b', 'kb', 'mb', 'gb', 'tb'];
        var n = value;
        var i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
    }

    /**
     * Human-readable duration.
     *
     * @param {number|null|undefined} seconds
     * @returns {string} e.g. "2d 3h", "14m", or UNKNOWN.
     */
    function duration(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) {
            return UNKNOWN;
        }
        var s = Math.floor(seconds);
        var d = Math.floor(s / 86400);
        var h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60);
        if (d > 0) return d + 'd ' + h + 'h';
        if (h > 0) return h + 'h ' + m + 'm';
        if (m > 0) return m + 'm';
        return s + 's';
    }

    /**
     * How long ago a unix timestamp was, relative to now.
     *
     * @param {number|null|undefined} epochSeconds
     * @param {number} [nowSeconds] - injectable for tests.
     * @returns {string} e.g. "3h 2m ago", or UNKNOWN.
     */
    function since(epochSeconds, nowSeconds) {
        if (typeof epochSeconds !== 'number' || epochSeconds <= 0) return UNKNOWN;
        var now = typeof nowSeconds === 'number'
            ? nowSeconds
            : Math.floor(Date.now() / 1000);
        var delta = now - epochSeconds;
        if (delta < 0) return UNKNOWN;
        return duration(delta) + ' ago';
    }

    /**
     * One label/value line.
     *
     * @param {string} label - lowercase field name.
     * @param {string} value - already-escaped or plain text.
     * @param {string} [extraClass] - modifier for the value cell.
     * @returns {string} HTML.
     */
    function line(label, value, extraClass) {
        var cls = 'server-status-value' + (extraClass ? ' ' + extraClass : '');
        return '<div class="server-status-line">'
            + '<span class="server-status-label">' + esc(label) + '</span>'
            + '<span class="' + cls + '">' + esc(value) + '</span>'
            + '</div>';
    }

    /**
     * The could-not-evaluate line for a whole section.
     *
     * @param {object|null} section - a payload section.
     * @returns {string} HTML, or '' when the section IS available.
     */
    function unavailableLine(section) {
        if (section && section.available) return '';
        var why = (section && section.error) || 'no reading was returned';
        return line(UNKNOWN, why, 'server-status-value--unknown');
    }

    /**
     * Wrap a titled section.
     *
     * @param {string} title - lowercase heading.
     * @param {string} body - inner HTML.
     * @returns {string} HTML.
     */
    function section(title, body) {
        return '<section class="server-status-section">'
            + '<h3 class="server-status-heading">' + esc(title) + '</h3>'
            + body + '</section>';
    }

    /**
     * The server-process section: uptime, bind address, deployed commit.
     *
     * `lan_reachable` is stated in words rather than as a raw bind
     * address because that is the fact with a consequence: on 0.0.0.0
     * every device on the network can reach this app and its own login
     * is the only thing in the way.
     *
     * @param {object} server - snapshot.server.
     * @returns {string} HTML.
     */
    function renderServer(server) {
        var s = server || {};
        if (!s.available) return section('server', unavailableLine(s));
        var commit = s.commit || {};
        var commitText = commit.available
            ? commit.sha + (commit.dirty ? ' (uncommitted changes)' : '')
            : UNKNOWN + ': ' + (commit.error || 'no reading');
        var reach = s.lan_reachable
            ? 'yes, every device on this network can reach it'
            : 'no, this machine only';
        return section('server', ''
            + line('uptime', duration(s.uptime_seconds))
            + line('bound to', s.host + ':' + s.port)
            + line('on the network', reach)
            + line('commit', commitText)
            + line('process id', String(s.pid == null ? UNKNOWN : s.pid))
            + line('python', s.python_version || UNKNOWN));
    }

    /**
     * The host section: identity, uptime, memory, disk, load.
     *
     * Load is printed against the core count on the same line, because a
     * load of 8 means nothing without knowing whether there are 4 cores
     * or 16.
     *
     * @param {object} snapshot - the whole payload.
     * @returns {string} HTML.
     */
    function renderHost(snapshot) {
        var snap = snapshot || {};
        var host = snap.host || {};
        var mem = snap.memory || {};
        var disk = snap.disk || {};
        var load = snap.load || {};

        var body = host.available
            ? line('machine', host.hostname + ' (' + host.os + ')')
                + line('host uptime', duration(host.uptime_seconds))
            : unavailableLine(host);

        body += mem.available
            ? line('memory', bytes(mem.used_bytes) + ' of ' + bytes(mem.total_bytes)
                + ' used (' + mem.used_percent + '%), ' + bytes(mem.available_bytes) + ' free')
            : line('memory', UNKNOWN + ': ' + (mem.error || 'no reading'),
                'server-status-value--unknown');

        body += disk.available
            ? line('disk ' + disk.path, bytes(disk.free_bytes) + ' free of '
                + bytes(disk.total_bytes) + ' (' + disk.used_percent + '% used)')
            : line('disk', UNKNOWN + ': ' + (disk.error || 'no reading'),
                'server-status-value--unknown');

        body += load.available
            ? line('load', load.load_1 + ', ' + load.load_5 + ', ' + load.load_15
                + ' across ' + load.cpu_count + ' cores')
            : line('load', UNKNOWN + ': ' + (load.error || 'no reading'),
                'server-status-value--unknown');

        return section('host', body);
    }

    /**
     * The release section: what this install is, and whether it is current.
     *
     * THE SHAPE IS `GET /api/v1/version`, defined by
     * src/api/version_routes.py and rendered nowhere else:
     *
     *   { version, update: { status, current_version, latest_version,
     *     remote, checked_at, reason, upgrade_command } }
     *
     * `status` is one of the three strings `current`, `update_available`
     * or `unknown`, and `checked_at` is unix seconds for when the
     * comparison last actually RAN.
     *
     * A TRANSPORT FAILURE arrives instead as `{available: false, error}`,
     * synthesised by the panel's fetchRelease when the call itself never
     * landed. That is a fourth thing the server cannot report about
     * itself, and it renders the same way `unknown` does.
     *
     * THREE OUTCOMES, AND THE THIRD IS THE POINT. A cached "up to date"
     * with no timestamp is indistinguishable from a fresh one, and an
     * offline check that renders as "up to date" is a lie with a long
     * shelf life. So: `update_available` names the newer tag, `current`
     * says up to date, and ANY other value - including a missing update
     * block or an unrecognised string - says "could not check" with
     * whatever reason there is. Unrecognised falls to the honest state on
     * purpose; a new server status this client has never heard of is
     * exactly the case where it does not know the answer.
     *
     * NO ONE-CLICK UPGRADE. The command is rendered as copyable text on
     * purpose. An unattended upgrade can pull the claude binary out from
     * under a running agent session, and the user decides when that
     * happens.
     *
     * @param {object|null} release - the `/version` payload, the
     *   `{available: false, error}` marker, or null when it could not be
     *   fetched at all.
     * @param {number} [nowSeconds] - injectable clock for tests.
     * @returns {string} HTML.
     */
    function renderRelease(release, nowSeconds) {
        var r = release || {};
        if (r.available === false) {
            return section('release', line('release', UNKNOWN + ': '
                + (r.error || 'the version check is not available on this server'),
                'server-status-value--unknown'));
        }
        var u = r.update || {};
        var body = line('running', r.version || u.current_version || UNKNOWN);

        if (u.status === 'update_available') {
            body += line('status', 'update available: '
                + (u.latest_version || 'a newer release'),
                'server-status-value--attention');
        } else if (u.status === 'current') {
            body += line('status', 'up to date');
        } else {
            body += line('status', 'could not check: '
                + (u.reason || 'no comparison was made'),
                'server-status-value--unknown');
        }

        body += (typeof u.checked_at === 'number' && u.checked_at > 0)
            ? line('checked', since(u.checked_at, nowSeconds))
            : line('checked', UNKNOWN + ': no check has been recorded',
                'server-status-value--unknown');

        if (u.upgrade_command) {
            body += '<div class="server-status-line">'
                + '<span class="server-status-label">upgrade with</span>'
                + '<code class="server-status-command">' + esc(u.upgrade_command) + '</code>'
                + '</div>';
        }
        return section('release', body);
    }

    /**
     * The claude CLI section: what every session on this box will launch.
     *
     * @param {object} cli - snapshot.claude_cli.
     * @returns {string} HTML.
     */
    function renderClaudeCli(cli) {
        var c = cli || {};
        if (!c.available) return section('claude cli', unavailableLine(c));
        return section('claude cli',
            line('version', c.version) + line('path', c.path));
    }

    /**
     * Ownership as a plain word. Never a badge, never a pill.
     *
     * @param {boolean|null|undefined} createdByCloude - the SERVER's
     *   verdict, taken verbatim. Undefined/null means the server could
     *   not tell us, which is NOT the same as "not ours".
     * @returns {string} 'cloudecode', 'external' or 'owner unknown'.
     */
    function ownershipWord(createdByCloude) {
        if (createdByCloude === true) return 'cloudecode';
        if (createdByCloude === false) return 'external';
        return 'owner unknown';
    }

    /**
     * One tmux session row, with its kill control.
     *
     * The literal session name rides back to the handler on
     * `data-session-name` and the session id (when the session is open in
     * this app) on `data-session-id`, exactly as the launcher's rows do.
     * Neither is ever placed anywhere it could be executed.
     *
     * @param {object} row - one entry of snapshot.tmux.sessions.
     * @param {number} [nowSeconds] - injectable clock for tests.
     * @returns {string} HTML.
     */
    function renderSessionRow(row, nowSeconds) {
        var r = row || {};
        var name = r.name || '';
        var meta = [
            'created ' + since(r.created_at_epoch, nowSeconds),
            (r.pane_cols || 0) + 'x' + (r.pane_rows || 0),
            ownershipWord(r.created_by_cloude),
        ];
        if (r.open_in_app) meta.push('open in cloudecode');
        if (r.attached_clients > 0) {
            meta.push(r.attached_clients
                + (r.attached_clients === 1 ? ' client attached' : ' clients attached'));
        }
        return '<div class="server-status-session" data-session-row="' + esc(name) + '">'
            + '<div class="server-status-session-main">'
            + '<div class="server-status-session-name">' + esc(name) + '</div>'
            + '<div class="server-status-session-meta">' + esc(meta.join(' · ')) + '</div>'
            + '<div class="server-status-session-dir">' + esc(r.working_dir || '') + '</div>'
            + '</div>'
            + '<button type="button" class="server-status-kill"'
            + ' data-kill-name="' + esc(name) + '"'
            + (r.session_id ? ' data-kill-id="' + esc(r.session_id) + '"' : '')
            + ' data-kill-attached="' + esc(r.attached_clients || 0) + '"'
            + ' data-kill-open="' + (r.open_in_app ? '1' : '0') + '"'
            + ' title="close session" aria-label="close session ' + esc(name) + '">'
            + 'close</button>'
            + '</div>';
    }

    /**
     * The tmux section: socket health, scrollback ceiling, session list.
     *
     * A socket with no server running is NOT an error and does not
     * render as one - it is the ordinary state before the first session,
     * and the server reports it as a measured fact.
     *
     * @param {object} tmux - snapshot.tmux.
     * @param {number} [nowSeconds] - injectable clock for tests.
     * @returns {string} HTML.
     */
    function renderTmux(tmux, nowSeconds) {
        var t = tmux || {};
        if (!t.available) {
            return section('tmux', line('socket', t.socket || UNKNOWN)
                + unavailableLine(t));
        }
        var body = line('socket', t.socket)
            + line('tmux server', t.server_running ? 'running' : 'not running')
            + line('scrollback limit', t.history_limit == null
                ? UNKNOWN + ': tmux did not report history-limit'
                : t.history_limit + ' lines',
                t.history_limit == null ? 'server-status-value--unknown' : '');

        var sessions = t.sessions || [];
        body += line('open sessions', String(sessions.length));
        for (var i = 0; i < sessions.length; i++) {
            body += renderSessionRow(sessions[i], nowSeconds);
        }
        return section('tmux', body);
    }

    /**
     * The whole panel body.
     *
     * @param {object} snapshot - the payload from GET /server/status.
     * @param {number} [nowSeconds] - injectable clock for tests.
     * @param {object|null} [release] - the release payload, fetched
     *   separately because it is owned by the packaging work.
     * @returns {string} HTML.
     */
    function renderBody(snapshot, nowSeconds, release) {
        var snap = snapshot || {};
        return renderRelease(release, nowSeconds)
            + renderServer(snap.server)
            + renderTmux(snap.tmux, nowSeconds)
            + renderHost(snap)
            + renderClaudeCli(snap.claude_cli);
    }

    window.ServerStatusFormat = {
        UNKNOWN: UNKNOWN,
        esc: esc,
        bytes: bytes,
        duration: duration,
        since: since,
        line: line,
        ownershipWord: ownershipWord,
        renderServer: renderServer,
        renderHost: renderHost,
        renderClaudeCli: renderClaudeCli,
        renderRelease: renderRelease,
        renderSessionRow: renderSessionRow,
        renderTmux: renderTmux,
        renderBody: renderBody
    };
})();

console.log('[ServerStatusFormat Module] Exported as window.ServerStatusFormat');
