/**
 * Session listing STATE - turning a failed session probe into a verdict
 * the UI can render honestly.
 *
 * WHY THIS EXISTS. The sidebar used to `catch` a failed
 * `GET /sessions/attachable`, log it, and carry on with an empty array -
 * which rendered "no other conversations". That sentence is a claim about
 * the world, and the app had just failed to learn anything about the
 * world. It is the same false green the home screen already fixed for its
 * own list, and the sidebar sitting beside it saying the opposite is
 * worse than either alone: two surfaces, same data, contradicting each
 * other on screen at the same time.
 *
 * THREE OUTCOMES, never two:
 *   ok            the probe answered. Zero rows means zero sessions.
 *   unavailable   the probe did not answer. Zero rows means NOTHING, and
 *                 the list says CANNOT DETERMINE instead of counting.
 *   (there is no third stored state - "partially answered" is `ok` with
 *   fewer rows, because a probe that answered is a probe that answered.)
 *
 * The reason/detail derivation is shared with the home screen's own
 * treatment on purpose: the server ships a structured 503 body
 * ({listing_ok, listing_reason, listing_detail, message}) and BOTH
 * surfaces must repeat the server's verdict rather than invent a parallel
 * one. The MARKUP is not shared - a 320px docked bar cannot render the
 * home screen's wide attention card - but the words in it come from here.
 *
 * Must load BEFORE session-sidebar.js runs.
 */

console.log('[SessionListingState Module] Loading...');

(function () {
    /**
     * Description: HTML-escape a value for interpolation into innerHTML.
     * Inputs: value (any).
     * Output: string.
     */
    function esc(value) {
        const div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML;
    }

    /**
     * Description: derive a machine-readable reason token from a rejected
     *   API call, preferring the server's own `listing_reason` over
     *   anything the browser could guess.
     * Inputs: err (Error) - the rejection, possibly carrying `.detail`.
     *   status (number|null) - HTTP status, or 0/null for a transport
     *   failure.
     * Output: string - e.g. 'tmux_missing', 'timeout', 'unauthorized',
     *   'http_500', 'network_error'.
     * Example: SessionListingState.reasonFromError(err, 503) // 'timeout'
     */
    function reasonFromError(err, status) {
        const d = err && err.detail;
        if (d && typeof d === 'object' && typeof d.listing_reason === 'string' && d.listing_reason) {
            return d.listing_reason;
        }
        if (status === 401) return 'unauthorized';
        if (typeof status === 'number' && status > 0) return `http_${status}`;
        return 'network_error';
    }

    /**
     * Description: derive the one-sentence human explanation shown to the
     *   user. Never returns an empty string - a blank cell explains
     *   nothing, and "I could not measure this" is only actionable when it
     *   says what it could not measure.
     * Inputs: err (Error), status (number|null).
     * Output: string.
     * Example: SessionListingState.detailFromError(err, 0)
     *   // 'the server could not be reached'
     */
    function detailFromError(err, status) {
        const d = err && err.detail;
        if (d && typeof d === 'object') {
            if (typeof d.listing_detail === 'string' && d.listing_detail) return d.listing_detail;
            if (typeof d.message === 'string' && d.message) return d.message;
        }
        if (status === 401) return 'sign in again to see your sessions';
        if (status === 503) return 'the server could not read the tmux session list';
        if (typeof status === 'number' && status > 0) return `the server answered HTTP ${status}`;
        if (err && typeof err.message === 'string' && err.message) return err.message;
        return 'the server could not be reached';
    }

    /**
     * Description: build the state object a failed probe produces.
     * Inputs: err (Error), status (number|null).
     * Output: object - {ok: false, reason, detail}.
     */
    function fromError(err, status) {
        return {
            ok: false,
            reason: reasonFromError(err, status),
            detail: detailFromError(err, status),
        };
    }

    /**
     * Description: the CANNOT DETERMINE block the sidebar renders in place
     *   of a row list. It deliberately carries NO controls: an action
     *   against a session whose existence could not be confirmed either
     *   does nothing or does something to the wrong session, and offering
     *   it would claim knowledge the app does not have.
     * Inputs: listing (object|null) - {ok, reason, detail}.
     * Output: string - HTML, or '' when the listing is fine.
     * Example: SessionListingState.attentionHtml({ok: false, reason: 'timeout', detail: 'x'})
     */
    function attentionHtml(listing) {
        if (!listing || listing.ok) return '';
        const reason = esc(listing.reason || 'probe_error');
        const detail = esc(listing.detail || 'the server could not be reached');
        return (
            `<div class="session-sidebar-attention" role="status" ` +
            `data-listing-ok="0" data-listing-reason="${reason}">` +
            '<div class="session-sidebar-attention__head">NEEDS ATTENTION</div>' +
            '<div class="session-sidebar-attention__title">CANNOT DETERMINE which sessions exist</div>' +
            `<div class="session-sidebar-attention__detail">${detail} (${reason})</div>` +
            '</div>'
        );
    }

    window.SessionListingState = {
        reasonFromError, detailFromError, fromError, attentionHtml, esc,
    };
    console.log('[SessionListingState Module] Exported as window.SessionListingState');
})();
