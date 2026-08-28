"""Print exactly what ``GET /api/v1/themes`` serialises, as JSON.

Companion to ``scripts/audio-end-to-end-proof.mjs``. The point is to read
the RESPONSE rather than the manifests on disk: for the whole audio outage
the files were correct and the response was not, and reading the files back
would have confirmed the wrong half.

Goes through the real FastAPI route and the real ``response_model`` via
TestClient, so anything the model drops is dropped here too.

Usage:
    venv/bin/python3 scripts/dump_themes_payload.py > /tmp/themes.json
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dump_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dump_logs_"))
# feat/state-directory - keep this hermetic. Without it, any code path
# that reaches Settings.get_state_dir() would create/write into the real
# ~/Library/Application Support/CloudeCode on the machine running this.
os.environ.setdefault("CLOUDE_STATE_DIR", tempfile.mkdtemp(prefix="cc_dump_state_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth


def main() -> int:
    """Dump the themes payload to stdout.

    :returns: process exit code; 1 if the endpoint did not return 200.
    """
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    resp = TestClient(app).get("/api/v1/themes")
    if resp.status_code != 200:
        print(f"themes endpoint returned {resp.status_code}", file=sys.stderr)
        return 1

    json.dump(resp.json(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
