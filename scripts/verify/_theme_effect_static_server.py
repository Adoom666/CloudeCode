"""Static server that reproduces the app's asset layout without its auth.

WHY THIS EXISTS. Measuring whether a theme effect is visible needs the REAL
paint stack: the real index.html, the real stylesheet order (styles.css before
ios-chrome.css matters - that ordering is what caused the occlusion bug), and
the real registry.js applying the real manifests. Booting src.main to get that
would require generating TOTP and JWT secrets, which a measurement script has
no business doing. Serving client/ with the same URL shape gives an identical
paint stack and needs no credentials.

What it serves:
  /                  client/index.html
  /static/<path>     client/<path>
  /api/v1/themes     manifests built from the on-disk theme.json files, each
                     marked source="builtin" so registry.js's effects consent
                     gate is bypassed exactly as it is for bundled themes
  /api/<anything>    an empty JSON array, so app.js fails soft rather than
                     throwing before Themes is exported

Not a production server and never used as one: it binds 127.0.0.1 only, and is
imported solely by measure-theme-effect-visibility.py.

Usage: python3 _theme_effect_static_server.py <port>
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = os.path.join(REPO_ROOT, "client")
THEMES = os.path.join(CLIENT, "css", "themes")

MIME = {
    ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
    ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".webmanifest": "application/manifest+json", ".map": "application/json",
    ".mjs": "text/javascript", ".ico": "image/x-icon",
}


def manifests():
    """Every bundled theme manifest, marked as builtin.

    Returns:
        list[dict]: theme.json contents, each with source="builtin" and a
        guaranteed "id" key.
    """
    out = []
    for name in sorted(os.listdir(THEMES)):
        path = os.path.join(THEMES, name, "theme.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["source"] = "builtin"
        manifest.setdefault("id", name)
        out.append(manifest)
    return out


class Handler(BaseHTTPRequestHandler):
    """Serves the client tree and a synthetic themes endpoint."""

    def log_message(self, *args):
        """Silence the default per-request logging."""

    def _send(self, code, body, ctype):
        """Write one response.

        Args:
            code (int): HTTP status.
            body (bytes): Response body.
            ctype (str): Content-Type header value.
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        """Route one GET to the client tree or the synthetic API."""
        path = self.path.split("?")[0]
        if path == "/api/v1/themes":
            self._send(200, json.dumps(manifests()).encode(), "application/json")
            return
        if path.startswith("/api/"):
            self._send(200, b"[]", "application/json")
            return
        if path in ("/", "/index.html"):
            target = os.path.join(CLIENT, "index.html")
        elif path.startswith("/static/"):
            target = os.path.join(CLIENT, *path[len("/static/"):].split("/"))
        else:
            target = os.path.join(CLIENT, *path.lstrip("/").split("/"))
        target = os.path.normpath(target)
        # Path traversal guard: everything served must stay under client/.
        if not target.startswith(CLIENT) or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain")
            return
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as fh:
            self._send(200, fh.read(), MIME.get(ext, "application/octet-stream"))


def main():
    """Serve until killed. Port comes from argv[1]."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5057
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
