#!/usr/bin/env python3
"""Synthesise a long claude transcript jsonl for the lab session ONLY.

Writes into the claude project dir of ALTSB_LAB_DIR, a scratch directory,
so real projects are untouched. Numbered lines make scroll depth
measurable: line N is literally "TRANSCRIPT LINE N".
"""
import json
import os
import uuid
from datetime import datetime, timezone

#: The scratch directory the lab claude session runs in. Override with
#: ALTSB_LAB_DIR; claude derives its project dir by replacing every "/"
#: and "." in the path with "-".
CWD = os.environ.get(
    "ALTSB_LAB_DIR", os.path.expanduser("~/Scratch/llmScratch/altscreen-lab")
)
PROJ = os.path.expanduser(
    "~/.claude/projects/" + CWD.replace("/", "-").replace(".", "-")
)
SESSION = "aaaaaaaa-0000-4000-8000-000000000001"
TURNS = 120


def stamp() -> str:
    """Current UTC timestamp in the format the transcript files use."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main() -> None:
    """Write the synthetic transcript file."""
    os.makedirs(PROJ, exist_ok=True)
    path = os.path.join(PROJ, SESSION + ".jsonl")
    prev = None
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(1, TURNS + 1):
            for role in ("user", "assistant"):
                uid = str(uuid.uuid4())
                text = "TRANSCRIPT LINE %04d %s marker" % (
                    (i * 2 - 1) if role == "user" else i * 2, role
                )
                rec = {
                    "parentUuid": prev,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": CWD,
                    "sessionId": SESSION,
                    "version": "2.1.199",
                    "gitBranch": "master",
                    "type": role,
                    "message": (
                        {"role": "user", "content": text}
                        if role == "user"
                        else {
                            "role": "assistant",
                            "model": "claude-opus-4-5",
                            "content": [{"type": "text", "text": text}],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    ),
                    "uuid": uid,
                    "timestamp": stamp(),
                }
                fh.write(json.dumps(rec) + "\n")
                prev = uid
    print(path)


if __name__ == "__main__":
    main()
