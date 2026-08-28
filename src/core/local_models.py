"""Reading the model list off an LM Studio server, with bounded trust.

WHAT THIS TALKS TO. LM Studio serves an OpenAI-compatible API; the model
list is ``GET http://<host>/v1/models`` returning ``{"data": [{"id": ...}]}``.
Nothing here assumes more than that shape, and anything that does not match
it is reported as unusable rather than guessed at.

THREE STATES, NOT TWO, AND THE THIRD IS THE ONE PEOPLE SKIP.

  configured + reachable      a model list
  configured + unreachable    the box is off, or the address is wrong
  NOT CONFIGURED              nobody has set providers.local_host

The last two are constantly conflated and they mean opposite things to the
person reading the screen. "Unreachable" says go check the machine. "Not
configured" says go set the address. Rendering the second as the first
sends the user to fix something that is not broken, and it is the state
EVERY install is in until someone opts in - so it would be the normal
reading of the row, which trains people to ignore it.

WHY THE FETCH IS BOUNDED IN BOTH DIRECTIONS. This is an outbound request
to an address out of a config file, so it is a resource the app does not
control: a slow-drip responder would otherwise hold a worker forever, and
an enormous body would grow server memory. There is a total deadline, not
just a connect timeout, and the body is read up to a cap and then
abandoned. Neither is a security boundary on its own - the address is
config-only for that reason (see ``ProvidersConfig.local_host``) - they
are there so a misbehaving box degrades to "unreachable" instead of
degrading the app.

EMBEDDING MODELS ARE FILTERED OUT. They cannot hold a conversation, so
offering one in a launch picker is offering a session that will not work.
The filter is a name heuristic and is therefore stated as one: it is
allowed to be wrong, and being wrong here costs a missing row rather than
a broken launch.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

#: Total wall-clock budget for the whole probe, connect plus read.
LOCAL_MODELS_DEADLINE_SECONDS: float = 4.0

#: Hard cap on the response body. LM Studio's list is a few KiB; a megabyte
#: is already absurd, which makes it a safe place to stop reading.
LOCAL_MODELS_MAX_BYTES: int = 1024 * 1024

#: Substrings that mark a model as an embedder rather than a chat model.
#: A HEURISTIC, deliberately: see the module docstring.
_EMBEDDING_MARKERS = ("embed", "embedding", "bge-", "gte-", "e5-", "nomic-embed")

#: The address has not been set. Not an error, and not unreachable.
LOCAL_NOT_CONFIGURED = "not-configured"

#: Configured and answered.
LOCAL_REACHABLE = "reachable"

#: Configured and did not answer, or answered something unusable.
LOCAL_UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class LocalModels:
    """The outcome of one probe.

    - ``state``: LOCAL_NOT_CONFIGURED / LOCAL_REACHABLE / LOCAL_UNREACHABLE.
    - ``models``: chat-capable model ids, embedders removed.
    - ``host``: what was probed, echoed so the UI can name it.
    - ``detail``: why, for the two states that are not simply a list.
    """

    state: str
    models: List[str] = field(default_factory=list)
    host: str = ""
    detail: Optional[str] = None

    @property
    def reachable(self) -> bool:
        """True only for LOCAL_REACHABLE. NOT_CONFIGURED is not reachable."""
        return self.state == LOCAL_REACHABLE


def is_embedding_model(model_id: str) -> bool:
    """Whether a model id looks like an embedder rather than a chat model.

    Description: a name heuristic and nothing more. Being wrong costs a
      missing row in a picker; the alternative, offering an embedder as a
      chat model, costs a session that opens and cannot talk.
    Inputs: model_id (str).
    Output: bool.
    Example: is_embedding_model("text-embedding-nomic-v1")  # True
    """
    low = (model_id or "").lower()
    return any(marker in low for marker in _EMBEDDING_MARKERS)


def _parse_models(payload: Any) -> Optional[List[str]]:
    """Pull chat model ids out of an OpenAI-shaped list response.

    Inputs: payload (Any) - the decoded JSON body.
    Output: list[str] | None - None when the shape is not recognised, which
      is a DIFFERENT fact from an empty list and is reported as one.
    Example: _parse_models({"data": [{"id": "a"}]})  # ['a']
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    ids: List[str] = []
    for entry in data:
        if isinstance(entry, dict):
            mid = entry.get("id")
        elif isinstance(entry, str):
            mid = entry
        else:
            continue
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    return [m for m in ids if not is_embedding_model(m)]


def fetch_local_models(
    host: str,
    *,
    deadline: float = LOCAL_MODELS_DEADLINE_SECONDS,
    max_bytes: int = LOCAL_MODELS_MAX_BYTES,
    opener=None,
) -> LocalModels:
    """Probe an LM Studio server for its chat models.

    Description: never raises. Every failure mode - unset address, refused
      connection, timeout, non-JSON body, unrecognised shape - resolves to
      a named state, because the caller is an endpoint that must answer
      200 with a state rather than fail. See the module docstring for why
      NOT CONFIGURED is its own state.
    Inputs:
      host (str) - ``host:port``, already validated by ProvidersConfig.
      deadline (float) - total seconds for connect plus read.
      max_bytes (int) - stop reading past this.
      opener (Callable | None) - injection point for tests; defaults to
        ``urllib.request.urlopen``.
    Output: LocalModels.
    Example: fetch_local_models("10.0.1.5:1234")
    """
    address = (host or "").strip()
    if not address:
        return LocalModels(
            LOCAL_NOT_CONFIGURED,
            detail=(
                "no LM Studio address is configured; set providers.local_host "
                "in config.json to host:port"
            ),
        )

    url = f"http://{address}/v1/models"
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=deadline) as resp:
            # Read ONE byte past the cap so a body that is exactly at the
            # limit is not silently indistinguishable from one that is over.
            raw = resp.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.info("local_models_unreachable", host=address, error=str(exc))
        return LocalModels(
            LOCAL_UNREACHABLE, host=address, detail=f"{type(exc).__name__}: {exc}"
        )

    if len(raw) > max_bytes:
        return LocalModels(
            LOCAL_UNREACHABLE,
            host=address,
            detail=f"response larger than {max_bytes} bytes; refusing to parse it",
        )
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return LocalModels(
            LOCAL_UNREACHABLE, host=address, detail=f"response was not JSON: {exc}"
        )

    models = _parse_models(payload)
    if models is None:
        return LocalModels(
            LOCAL_UNREACHABLE,
            host=address,
            detail="answered, but not with an OpenAI-shaped model list",
        )
    return LocalModels(LOCAL_REACHABLE, models=models, host=address)


def to_payload(result: LocalModels) -> Dict[str, Any]:
    """Serialize a probe for the API.

    Description: ``state`` is the field to read; ``reachable`` is kept
      alongside it for a client that only wants the boolean, and it is
      False for BOTH not-configured and unreachable - which is exactly why
      ``state`` exists and is the one the UI should branch on.
    Inputs: result (LocalModels).
    Output: dict.
    Example: to_payload(fetch_local_models(""))
    """
    return {
        "state": result.state,
        "reachable": result.reachable,
        "host": result.host,
        "models": list(result.models),
        "detail": result.detail,
    }
