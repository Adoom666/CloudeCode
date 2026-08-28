"""A static test server that stamps the APPLICATION's real security headers.

THE BLIND SPOT THIS CLOSES. Every pixel verifier under scripts/ drives
http.server.SimpleHTTPRequestHandler rooted at the repo, because the manual
harnesses under tests/manual/ are not routes the FastAPI app serves - they
are pages that fetch the shipped markup and mount it so geometry can be
measured. That arrangement is right, and it had one structural consequence
nobody had priced in: a plain static server sends NO Content-Security-Policy,
so no CSP-dependent defect can exist in that harness. Not "usually passes" -
cannot be represented.

The codebase then proved the class is real and shippable. #logoutBtn carried
`onclick="App.logout()"` for roughly four months after `script-src 'self'`
landed. `script-src 'self'` forbids inline event handlers, so every click ran
nothing, threw nothing, rejected nothing, and left the element present, sized,
visible and unobstructed. The first harness built to chase it was a static
server and the entire logout flow worked end to end under it - correctly, for
that server, because with no CSP the inline handler is legal. A harness that
cannot reproduce the production security context cannot falsify a
security-context defect.

WHAT THIS DOES.
1. Serves `src.security_headers.SECURITY_HEADERS` on every response - the
   same dict src/main.py stamps, imported rather than copied, so the harness
   cannot drift away from production.
2. Boots harness pages anyway. `script-src 'self'` would otherwise block a
   harness's own inline bootstrap <script> and the page would never become
   ready. Each .html response gets its OWN policy in which every inline
   <script> block in THAT document is allowed by sha256 hash.
3. Refuses to grant `'unsafe-hashes'`. That is the load-bearing line in this
   file. Hashes without `'unsafe-hashes'` cover inline SCRIPT BLOCKS only;
   inline EVENT HANDLER attributes stay forbidden exactly as in production.
   So the harness boots, and the defect class still fails.

WHAT IT CANNOT DO. A CSP violation is reported to the page, not to the
server, so serving the header is only half of a check - the driving script
has to collect the violations and fail on them. Use `collector_init_script()`
and `violations()` below for that; a harness that stamps the header and never
reads the violations has changed nothing it can observe.

Usage:
    from lib_csp_static_server import serve, collector_init_script, violations
    httpd, port = serve(ROOT)
    page.add_init_script(collector_init_script())
    ...
    for v in violations(page): ...
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import re
import socketserver
import sys
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.security_headers import SECURITY_HEADERS, build_csp  # noqa: E402

#: Matches an inline <script> block, i.e. one with no src= attribute. The
#: negative lookahead on `[^>]*src` is what keeps external scripts out: those
#: are already allowed by 'self' and hashing them would be meaningless.
_INLINE_SCRIPT = re.compile(
    rb"<script(?![^>]*\ssrc\s*=)[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

#: The name the browser-side collector parks its violation list under.
VIOLATION_GLOBAL = "__cspViolations"


def script_hashes(html: bytes) -> list[str]:
    """Hash every inline <script> body in one HTML document.

    Inputs:
        html (bytes): the raw document as it will be sent on the wire. It
            must be the exact bytes - CSP hashes the script body verbatim,
            so re-encoding or reformatting invalidates them.
    Output:
        list[str]: CSP source expressions, e.g. ["'sha256-...='", ...].
    """
    out = []
    for body in _INLINE_SCRIPT.findall(html):
        digest = hashlib.sha256(body).digest()
        out.append("'sha256-%s'" % base64.b64encode(digest).decode("ascii"))
    return out


class CSPStaticHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler that stamps the app's security headers, quietly.

    HTML responses are read and sent by this class rather than delegated,
    because the per-document script-src hashes cannot be computed without
    the body. Everything else falls through to SimpleHTTPRequestHandler with
    the headers injected in end_headers().
    """

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        return

    def translate_path(self, path: str) -> str:
        """Map /static/ onto client/, the way src/main.py mounts it.

        Inputs: path (str) - the request path.
        Output: str - a filesystem path.

        src/main.py mounts client/ at /static, so every asset URL in the
        shipped markup is /static/... . A server rooted at the bare repo
        404s all of them, and an <img> that 404s is still present in the
        DOM - precisely the check that passes while the user looks at a
        broken icon. This lets a harness assert naturalWidth and mean it.
        """
        if path.startswith("/static/"):
            path = "/client/" + path[len("/static/"):]
        return super().translate_path(path)

    def _stamp(self, csp: str | None = None) -> None:
        """Emit every security header, optionally overriding the CSP.

        Inputs: csp (str | None) - a per-document policy, or None for the
            production policy verbatim.
        Output: None.
        """
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and csp:
                self.send_header(name, csp)
            else:
                self.send_header(name, value)

    def end_headers(self) -> None:  # noqa: D102
        if not getattr(self, "_headers_already_stamped", False):
            self._stamp()
        super().end_headers()

    def do_GET(self) -> None:  # noqa: D102, N802
        path = Path(self.translate_path(self.path))
        if path.is_file() and path.suffix.lower() in (".html", ".htm"):
            self._send_html(path)
            return
        super().do_GET()

    def _send_html(self, path: Path) -> None:
        """Send one HTML file under a policy that permits only ITS inline scripts.

        Inputs: path (Path) - resolved file on disk.
        Output: None.
        """
        body = path.read_bytes()
        extra = " ".join(script_hashes(body))
        self._headers_already_stamped = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # NOTE the absence of 'unsafe-hashes'. Without it these hashes admit
        # inline <script> BLOCKS and nothing else, so inline event handler
        # attributes remain as dead here as they are in production. Adding it
        # would re-open the exact hole this file exists to keep shut.
        self._stamp(build_csp(extra))
        self.end_headers()
        self.wfile.write(body)


def serve(root: Path) -> tuple[socketserver.TCPServer, int]:
    """Start a background static server that carries the real CSP.

    Inputs: root (Path) - directory to serve, normally the repo root.
    Output: (server, port). Call server.shutdown() when done.

    Example:
        httpd, port = serve(ROOT)
    """
    handler = functools.partial(CSPStaticHandler, directory=str(root))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def collector_init_script() -> str:
    """JS to install before any page script, recording CSP violations.

    Inputs: none.
    Output: str - pass to playwright's page.add_init_script().

    It must be an INIT script, not an evaluate() after load: the violation
    that matters here fires while the document (or injected markup) is being
    parsed, which is long before a post-load listener would exist.
    """
    return (
        "window.%s = [];\n"
        "document.addEventListener('securitypolicyviolation', function (e) {\n"
        "    window.%s.push({\n"
        "        directive: e.effectiveDirective || e.violatedDirective,\n"
        "        blocked: e.blockedURI,\n"
        "        sample: e.sample || '',\n"
        "        line: e.lineNumber || 0,\n"
        "        source: e.sourceFile || ''\n"
        "    });\n"
        "});\n" % (VIOLATION_GLOBAL, VIOLATION_GLOBAL)
    )


def violations(page) -> list[dict]:
    """Read the collected CSP violations off a page.

    Inputs: page - a playwright Page the init script was installed on.
    Output: list[dict] - one entry per violation, possibly empty.

    Returns [] if the collector is absent, which a caller MUST treat as
    "could not evaluate" rather than "clean" - see assert_collector_live().
    """
    return page.evaluate("window.%s || []" % VIOLATION_GLOBAL) or []


def assert_collector_live(page) -> str | None:
    """Prove the collector is installed and can actually fire.

    Inputs: page - a playwright Page.
    Output: None if the collector is proven live, else a reason string.

    POSITIVE CONTROL, and it is not optional. A collector that was never
    installed and a page with no violations return the identical empty list.
    This dispatches a synthetic SecurityPolicyViolationEvent, checks the
    listener caught it, and removes it again, so an empty list afterwards
    means something.
    """
    try:
        ok = page.evaluate(
            "() => {\n"
            "  const before = (window.%s || []).length;\n"
            "  if (!Array.isArray(window.%s)) return 'collector array absent';\n"
            "  const ev = new SecurityPolicyViolationEvent(\n"
            "      'securitypolicyviolation',\n"
            "      {violatedDirective: '__probe__',"
            "       effectiveDirective: '__probe__', blockedURI: 'probe'});\n"
            "  document.dispatchEvent(ev);\n"
            "  const after = window.%s.length;\n"
            "  if (after !== before + 1) return 'listener did not fire';\n"
            "  window.%s.splice(before, 1);\n"
            "  return '';\n"
            "}" % (VIOLATION_GLOBAL, VIOLATION_GLOBAL,
                   VIOLATION_GLOBAL, VIOLATION_GLOBAL)
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is "unknown"
        return "CSP violation collector could not be probed: %s" % exc
    return ok or None


def assert_policy_served(response) -> str | None:
    """Prove the document really arrived carrying a script-src policy.

    Inputs:
        response: the playwright Response returned by page.goto(). A CSP
            header is NOT readable from JS, so it has to be read off the
            wire; asking the document is not an option.
    Output:
        None if `script-src` is in force, else a reason string.

    Same reasoning as the collector control. If the header never arrived,
    every CSP assertion downstream is vacuous, and vacuous must report
    CANNOT DETERMINE rather than pass.
    """
    if response is None:
        return "no response object, so the served policy is unknown"
    try:
        header = response.header_value("content-security-policy")
    except Exception as exc:  # noqa: BLE001
        return "could not read the response headers: %s" % exc
    if not header:
        return ("the harness response carried NO Content-Security-Policy, so "
                "no CSP-dependent defect could have been detected here")
    if "script-src" not in header:
        return "the served policy has no script-src directive: %s" % header
    return None
