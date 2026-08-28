"""The response security headers, as data, so a test harness can serve them too.

WHY THIS MODULE EXISTS. The Content-Security-Policy used to be a string
literal inside src/main.py's csp_headers middleware, which meant it existed
in exactly one place that only ever ran inside a live uvicorn. Every pixel
verifier in scripts/ drives a plain static file server instead
(http.server.SimpleHTTPRequestHandler), and a static file server sends no
CSP at all. So the entire class of CSP-dependent defects was invisible to
every one of those harnesses BY CONSTRUCTION - not skipped, not tolerated,
structurally unrepresentable.

That is not hypothetical. #logoutBtn shipped `onclick="App.logout()"` for
roughly four months after `script-src 'self'` landed. `script-src 'self'`
forbids inline event handlers, so the click ran nothing, threw nothing and
rejected nothing. The finding agent's first harness was a static server and
the whole logout flow worked end to end under it, because with no CSP the
inline handler is perfectly legal.

Holding the policy here, as data, lets scripts/lib_csp_static_server.py
serve the SAME headers the application serves. It is a single source of
truth on purpose: a harness that carried its own copy of the policy would
drift from the app, and a harness whose policy has drifted is measuring a
condition production does not have.

Do NOT hand-copy these values anywhere. Import them.
"""

from __future__ import annotations

# Policy rationale for a local / LAN-only SPA. Each directive is deliberate;
# read the reasoning before widening one.
#
# - `default-src 'self'`   lock everything to same-origin by default.
# - `script-src 'self'`    no inline and no eval; all JS ships from /static.
#                          xterm.js and its addons are vendored under
#                          client/vendor/xterm/ and served same-origin, so no
#                          CDN host is needed. THIS DIRECTIVE IS WHAT KILLS
#                          INLINE EVENT HANDLERS. Adding 'unsafe-inline' here
#                          would silently resurrect that whole defect class
#                          and needs its own argument.
# - `style-src 'self' 'unsafe-inline'`
#                          xterm's webgl and fit addons write inline style
#                          attributes onto DOM nodes they manage; without the
#                          concession the terminal renders blank.
# - `connect-src 'self' ws: wss:`
#                          the terminal stream is same-origin WebSocket; the
#                          schemes are allowed so a future named tunnel with a
#                          different scheme can still connect.
# - `img-src 'self' data:` data: URIs carry QR codes and emoji SVGs.
# - `font-src 'self' data:`
#                          xterm embeds its icon font as a data: URI.
# - `frame-ancestors 'none'`
#                          clickjack defense; this app is never meant to be
#                          iframed.
CSP_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("script-src", "'self'"),
    ("style-src", "'self' 'unsafe-inline'"),
    ("connect-src", "'self' ws: wss:"),
    ("img-src", "'self' data:"),
    ("font-src", "'self' data:"),
    ("frame-ancestors", "'none'"),
)


def build_csp(script_src_extra: str = "") -> str:
    """Render the policy string, optionally widening script-src.

    Inputs:
        script_src_extra (str): extra source expressions appended to
            script-src, space separated, already quoted where the CSP
            grammar requires it (for example "'sha256-abc...'"). Empty
            string means the production policy verbatim.
    Output:
        str: a complete Content-Security-Policy header value.

    The ONLY intended caller of the non-empty form is the static test
    server, which hashes a harness page's own bootstrap <script> blocks so
    the harness can boot under the real policy. It deliberately cannot
    grant 'unsafe-hashes', so inline EVENT HANDLERS stay forbidden there
    exactly as they are in production - which is the entire point of
    serving the policy in a harness at all.

    Example:
        build_csp("'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='")
    """
    parts = []
    for name, value in CSP_DIRECTIVES:
        if name == "script-src" and script_src_extra:
            parts.append("%s %s %s" % (name, value, script_src_extra.strip()))
        else:
            parts.append("%s %s" % (name, value))
    return "; ".join(parts) + ";"


#: The exact policy every application response carries.
CONTENT_SECURITY_POLICY: str = build_csp()

#: Every hardening header, CSP included, as one mapping. A harness that
#: stamps this dict is serving what production serves.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
