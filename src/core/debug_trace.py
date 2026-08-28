"""A loud, opt-in trace for the paths that fail silently.

WHY THIS EXISTS, STATED AS THE INCIDENT THAT CAUSED IT. Claude's
SessionStart hook delivered nothing for every session on a real install.
The hook was registered, it ran, it exited 0, the endpoint was reachable,
the environment was correct, and ``claude_session_uuid`` was never
written. Finding it took an hour of bisecting a shell one-liner by hand,
because EVERY layer reported success and none of them recorded what it
actually saw. The one variable that mattered - a trailing ``&`` that
orphaned curl before it delivered - was invisible from every log in the
system.

WHAT THIS IS FOR, AND WHAT IT IS NOT. It is for the paths where a failure
looks exactly like a success: a hook that fires and delivers nothing, a
spawn whose environment is subtly stale, a lookup that returns an empty
list because it read the wrong attribute. Those are not exceptional paths
and they raise nothing, so ordinary error logging never sees them. This
is not a replacement for ``logger.info`` on real events.

DESIGN, AND THE REASONS.

  OFF BY DEFAULT, ONE SWITCH. ``CLOUDE_DEBUG=1``. Not a log level: a level
  is a blunt global that also unmutes every per-second poller, which is
  what made the production log unreadable and is why the filtering bound
  logger was added in the first place. This is a separate axis so it can
  be verbose about six interesting call sites without touching the rest.

  ITS OWN FILE, NEVER THE APP'S STDOUT. Under launchd the app's stdout is
  launchd.log, which is rotated by newsyslog and whose fd the process
  holds across rotation - so a burst of debug output there is both
  unbounded and, after a rotation, invisible. This writes to
  ``<state_dir>/debug/trace.jsonl``, which nothing else touches and which
  can be deleted at any time.

  IT MUST NEVER BECOME THE FAULT. Every entry point swallows its own
  errors. A tracer that can break the thing it is tracing is worse than
  no tracer, and this one runs inside request handlers and a subprocess
  spawn path.

  SECRETS ARE REDACTED BY KEY NAME, and the redaction is deliberately
  aggressive: this file exists to be pasted into a conversation while
  debugging, so a token in it is a token in a transcript. Anything whose
  key contains token/secret/key/password/authorization is replaced with a
  LENGTH and a fingerprint, which is what you actually need to answer "is
  it the same one?" without ever printing it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

#: Key substrings whose values are never written in full.
_SECRET_HINTS = ("token", "secret", "key", "password", "authorization", "cookie")

#: Cap on any single rendered value. A trace line that scrolls for a
#: screen is a trace line nobody reads.
_MAX_VALUE_CHARS = 600

#: Cap on the file. Past this it is rotated ONCE to ``.1`` and restarted,
#: so an overnight debug session cannot fill a disk and the newest data is
#: always the data you get.
_MAX_BYTES = 8 * 1024 * 1024

_lock = threading.Lock()


def enabled() -> bool:
    """Whether tracing is on.

    Description: read on EVERY call rather than cached, so turning it on
      does not require a restart - you can export it, restart just the
      thing you are debugging, and get output. The read is a dict lookup;
      it is not worth caching to save.
    Output: bool.
    Example: if enabled(): trace("hook.received", ...)
    """
    raw = os.environ.get("CLOUDE_DEBUG")
    if raw is None:
        # THE PACKAGED APP DOES NOT INHERIT AN ARBITRARY ENV VAR. It is a
        # GUI .app whose server subprocess is spawned with a constructed
        # environment, so `export CLOUDE_DEBUG=1` in a shell - or even
        # `launchctl setenv` - does not reach it. Measured: the flag was
        # set and no trace file appeared.
        #
        # ``.env`` IS how this app is configured, and Settings already
        # reads it, so the flag is read from there as well. Without this
        # the whole facility is unreachable in exactly the deployment it
        # was built to debug.
        try:
            from src.config import settings

            raw = getattr(settings, "cloude_debug", None)
        except Exception:
            raw = None
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def fingerprint(value: Any) -> str:
    """A short, stable, non-reversible tag for a secret.

    Description: answers "is this the same value as that one?" - which is
      the only question worth asking about a token in a debug log - while
      printing none of it. Length is included because a WRONG-SHAPED
      secret is a common bug (a 31-character ``op://`` reference used as a
      40-character API token, for instance) and length alone often names
      it.
    Inputs: value (Any).
    Output: str, e.g. 'len=43 fp=9c1f4a2b'.
    Example: fingerprint("hunter2")  # 'len=7 fp=...'
    """
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"len={len(text)} fp={digest}"


def _is_secret(key: str) -> bool:
    """Whether a field name suggests its value must not be printed.

    Description: matched on WORD BOUNDARIES, not bare substrings. The
      first version used ``"key" in low``, which redacted ``payload_keys``
      - a list of field NAMES, the single most diagnostic thing the hook
      trace records - into a fingerprint. A tracer that hides the evidence
      is worse than one that is merely noisy, and it hid it while looking
      like it was working.

      The bias is still toward redacting: a name like ``api_key`` or
      ``x-api-key`` still matches. What no longer matches is a word that
      merely CONTAINS one of the hints as a fragment.
    Inputs: key (str).
    Output: bool.
    Example: _is_secret("payload_keys")  # False
    """
    parts = re.split(r"[^a-z0-9]+", str(key).lower())
    return any(part in _SECRET_HINTS for part in parts if part)


def scrub(payload: Any) -> Any:
    """Recursively redact secret-shaped fields and cap long values.

    Description: redaction is by KEY NAME, not by value inspection, so a
      credential is redacted even when it happens to look innocuous.
    Inputs: payload (Any) - dict, list, or scalar.
    Output: the same shape, safe to write down.
    Example: scrub({"token": "abc"})  # {'token': 'len=3 fp=...'}
    """
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            # A NON-STRING VALUE IS NOT A CREDENTIAL. A list of field
            # names, a count, a bool - fingerprinting those destroys the
            # only information the line carried and protects nothing.
            secret = _is_secret(key) and isinstance(value, str)
            out[str(key)] = fingerprint(value) if secret else scrub(value)
        return out
    if isinstance(payload, (list, tuple)):
        return [scrub(v) for v in payload]
    if isinstance(payload, str) and len(payload) > _MAX_VALUE_CHARS:
        return payload[:_MAX_VALUE_CHARS] + f"...<truncated {len(payload)} chars>"
    return payload


def _trace_path() -> Optional[Path]:
    """Where the trace file lives, or None if it cannot be resolved."""
    try:
        from src.config import settings

        base = Path(settings.get_state_dir()) / "debug"
    except Exception:
        override = os.environ.get("CLOUDE_DEBUG_DIR")
        if not override:
            return None
        base = Path(override)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / "trace.jsonl"


def trace(event: str, **fields: Any) -> None:
    """Write one trace line. Never raises, never blocks on anything real.

    Description: a no-op when tracing is off, which is the normal state,
      so call sites do not need to guard. Fields are scrubbed before they
      are written - see :func:`scrub`.
    Inputs: event (str) - a dotted name, e.g. 'hook.received'.
      **fields - anything JSON-serializable; secrets are redacted by key.
    Output: None.
    Example: trace("hook.received", event_kind="SessionStart", ok=True)
    """
    if not enabled():
        return
    try:
        path = _trace_path()
        if path is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "pid": os.getpid(),
        }
        record.update(scrub(fields))
        line = json.dumps(record, default=str, sort_keys=True) + "\n"
        with _lock:
            # Rotate ONCE at the cap. Keeping one generation means an
            # overnight run cannot fill a disk, and the file you open is
            # always the newest data rather than the oldest.
            try:
                if path.exists() and path.stat().st_size + len(line) > _MAX_BYTES:
                    path.replace(path.with_suffix(".jsonl.1"))
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        # A tracer that can break what it traces is worse than none. This
        # runs inside request handlers and a subprocess spawn path.
        return
