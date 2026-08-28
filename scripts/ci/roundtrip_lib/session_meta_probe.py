#!/usr/bin/env python3
"""Read or write session_metadata.json using THE INSTALL'S OWN code.

Run with the install directory as CWD and that install's python. It
imports ``src.config`` from there, so the resolver it exercises is
whichever version is currently checked out - v0.8.1's
``LOG_DIRECTORY``-only resolver, or the current version's
``_resolve_state_file`` with its old-location fallback. Nothing here
paraphrases either one, which is the entire point: a harness that
reimplements the thing it is measuring measures its own reimplementation.

Two modes:

  --write ID --name NAME [--owned A --owned B ...]
      Write metadata for one session at whatever path THIS version
      resolves, and report where it landed.

  (no --write)
      Report where THIS version resolves the file, whether it is there,
      and what session it names.

Output is one JSON object on stdout. ``resolved`` is always reported.
``present`` is a measured fact; when it is false, ``session_id`` is null
rather than absent, so a consumer can tell "no file" from "file with no
id" - two different findings.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--write", default=None, help="session id to persist")
    ap.add_argument("--name", default=None, help="tmux session name")
    ap.add_argument("--working-dir", default="/tmp")
    ap.add_argument("--owned", action="append", default=[])
    args = ap.parse_args()

    sys.path.insert(0, str(Path.cwd()))
    from src.config import Settings  # noqa: E402

    s = Settings()
    out = {"label": args.label}

    # v0.8.1 has no get_state_dir(); report the third outcome by name
    # instead of inventing a value for a method that does not exist.
    try:
        out["state_dir"] = str(s.get_state_dir())
    except AttributeError:
        out["state_dir"] = None
        out["state_dir_note"] = "this version has no get_state_dir()"
    out["log_directory"] = s.log_directory

    path = Path(s.get_session_metadata_path())
    out["resolved"] = str(path)

    if args.write:
        from src.models import Session  # noqa: E402

        payload = json.loads(
            Session(
                id=args.write,
                working_dir=args.working_dir,
                tmux_session=args.name,
            ).model_dump_json()
        )
        payload["owned_tmux_sessions"] = sorted(args.owned)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        out["wrote"] = str(path)

    out["present"] = path.exists()
    out["session_id"] = None
    out["owned_tmux_sessions"] = None
    if out["present"]:
        try:
            raw = json.loads(path.read_text())
            out["session_id"] = raw.get("id")
            out["owned_tmux_sessions"] = raw.get("owned_tmux_sessions")
        except (ValueError, OSError) as exc:
            out["read_error"] = str(exc)

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
