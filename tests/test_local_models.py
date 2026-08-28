"""The LM Studio probe: three states, and bounds in both directions."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.config import ProvidersConfig
from src.core.local_models import (
    LOCAL_NOT_CONFIGURED,
    LOCAL_REACHABLE,
    LOCAL_UNREACHABLE,
    fetch_local_models,
    is_embedding_model,
    to_payload,
)


class _Resp(io.BytesIO):
    """A urlopen-shaped context manager over a fixed body."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _opener(body: bytes):
    """An opener returning ``body``, ignoring the url."""
    def _open(url, timeout=None):
        return _Resp(body)
    return _open


def _raising(exc):
    """An opener that raises."""
    def _open(url, timeout=None):
        raise exc
    return _open


def _models(*ids):
    """An OpenAI-shaped model list body."""
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


# --- the three states --------------------------------------------------------


def test_an_unset_address_is_NOT_CONFIGURED_not_unreachable():
    """THE DISTINCTION THIS MODULE EXISTS FOR.

    They are constantly conflated and they mean opposite things: one says
    go check the machine, the other says go set the address. Not-configured
    is also the state EVERY install is in until someone opts in, so
    rendering it as unreachable would make an alarming row the normal
    reading and train people to ignore it.
    """
    r = fetch_local_models("")
    assert r.state == LOCAL_NOT_CONFIGURED
    assert r.reachable is False
    assert "config.json" in (r.detail or "")


def test_a_refused_connection_is_UNREACHABLE():
    r = fetch_local_models("x:1", opener=_raising(urllib.error.URLError("refused")))
    assert r.state == LOCAL_UNREACHABLE
    assert r.models == []
    assert r.host == "x:1"


def test_a_good_answer_is_REACHABLE_with_models():
    r = fetch_local_models("x:1", opener=_opener(_models("qwen/qwen3-8b", "llama-3")))
    assert r.state == LOCAL_REACHABLE
    assert r.reachable is True
    assert r.models == ["qwen/qwen3-8b", "llama-3"]


def test_reachable_is_false_for_BOTH_non_ok_states():
    """Which is exactly why `state` exists and is what the UI branches on."""
    assert fetch_local_models("").reachable is False
    assert fetch_local_models(
        "x:1", opener=_raising(OSError("nope"))
    ).reachable is False


# --- it never raises ---------------------------------------------------------


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("dns"),
    OSError("socket"),
    TimeoutError("slow"),
    ValueError("weird url"),
])
def test_every_transport_failure_becomes_a_state(exc):
    """The caller is an endpoint that must answer 200 with a state."""
    r = fetch_local_models("x:1", opener=_raising(exc))
    assert r.state == LOCAL_UNREACHABLE


def test_a_non_json_body_is_unreachable_not_a_crash():
    r = fetch_local_models("x:1", opener=_opener(b"<html>nope</html>"))
    assert r.state == LOCAL_UNREACHABLE
    assert "not JSON" in (r.detail or "")


def test_a_json_body_of_the_wrong_shape_is_unreachable():
    """Answered, but not with a model list. Not an empty list.

    An empty list means "this box is serving nothing". A wrong shape means
    "whatever is on that port, it is not LM Studio". Reporting the second
    as the first would send the user looking for missing models on a
    machine that is not running the product at all.
    """
    r = fetch_local_models("x:1", opener=_opener(b'{"object":"list"}'))
    assert r.state == LOCAL_UNREACHABLE
    assert "OpenAI-shaped" in (r.detail or "")


def test_a_genuinely_empty_list_is_REACHABLE():
    r = fetch_local_models("x:1", opener=_opener(_models()))
    assert r.state == LOCAL_REACHABLE
    assert r.models == []


# --- bounds ------------------------------------------------------------------


def test_an_oversized_body_is_refused_rather_than_parsed():
    """A body over the cap is abandoned, not streamed into memory."""
    r = fetch_local_models("x:1", opener=_opener(b"x" * 4096), max_bytes=1024)
    assert r.state == LOCAL_UNREACHABLE
    assert "larger than" in (r.detail or "")


def test_a_body_exactly_at_the_cap_is_still_parsed():
    """The read takes one byte PAST the cap so at-the-limit is not
    indistinguishable from over-the-limit."""
    body = _models("m")
    r = fetch_local_models("x:1", opener=_opener(body), max_bytes=len(body))
    assert r.state == LOCAL_REACHABLE


def test_the_deadline_is_passed_to_the_opener():
    seen = {}

    def _open(url, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = url
        return _Resp(_models("m"))

    fetch_local_models("h:1", deadline=2.5, opener=_open)
    assert seen["timeout"] == 2.5
    assert seen["url"] == "http://h:1/v1/models"


# --- embedders ---------------------------------------------------------------


@pytest.mark.parametrize("mid", [
    "text-embedding-nomic-embed-text-v1.5", "bge-large-en", "gte-small",
    "e5-mistral-7b", "some-EMBEDDING-model",
])
def test_embedders_are_filtered_out(mid):
    """They cannot hold a conversation, so offering one offers a broken
    session."""
    assert is_embedding_model(mid)
    r = fetch_local_models("x:1", opener=_opener(_models(mid, "qwen3-8b")))
    assert r.models == ["qwen3-8b"]


def test_a_chat_model_is_not_mistaken_for_an_embedder():
    assert not is_embedding_model("qwen/qwen3-8b")
    assert not is_embedding_model("llama-3.1-8b-instruct")


# --- the address validator ---------------------------------------------------


@pytest.mark.parametrize("good", ["10.0.1.5:1234", "localhost:1234", "[::1]:1234"])
def test_a_valid_host_port_survives(good):
    assert ProvidersConfig(local_host=good).local_host == good


@pytest.mark.parametrize("bad", [
    "http://x:1234", "x:1234/path", "x", "x:0", "x:99999", "::1:1234",
    "x:1234;rm -rf /", "x:1234 && id", "user@x:1234", "x:1234?q=1",
    "`id`:1234", "$(id):1234",
])
def test_a_hostile_or_malformed_host_drops_to_NOT_CONFIGURED(bad):
    """Dropped to empty, never to a guess.

    This value reaches a shell environment and an outbound fetch target.
    Failing soft is right - one bad field must not brick startup - but it
    must fail soft to NOT CONFIGURED, which is honest, rather than to a
    default address, which would make the app probe a stranger's box.
    """
    assert ProvidersConfig(local_host=bad).local_host == ""


def test_the_default_is_empty_rather_than_a_guessed_address():
    """A guessed default would make 'unreachable' the normal state for
    everyone who does not run LM Studio, and would point the app at some
    other network's machine."""
    assert ProvidersConfig().local_host == ""


# --- payload -----------------------------------------------------------------


def test_the_payload_carries_state_and_the_boolean():
    body = to_payload(fetch_local_models("x:1", opener=_opener(_models("m"))))
    assert body["state"] == LOCAL_REACHABLE
    assert body["reachable"] is True
    assert body["models"] == ["m"]
    assert body["host"] == "x:1"


# --- the cldl example wrapper ------------------------------------------------


def test_the_cldl_base_url_has_no_v1_suffix():
    """THE BUG THIS EXISTS TO KEEP FIXED, found only end to end.

    The Anthropic SDK appends "/v1/messages" to ANTHROPIC_BASE_URL itself.
    A base URL ending in /v1 therefore produces /v1/v1/messages, which LM
    Studio answers 200 with a 65-byte {"error": "Unexpected endpoint or
    method"} - JSON that is not a Message.

    What makes it worth a test rather than a comment is how it PRESENTS.
    Claude Code reports it as "API returned an empty or malformed response
    (HTTP 200) ... 0 stream events received", which reads like a streaming
    fault or an intercepting proxy. Every layer underneath was verified
    working at the time: the model list, a non-streaming Messages call
    that returned real content, a streaming call emitting correct
    Anthropic SSE event names, and the same again with tools and a system
    prompt. The one wrong path segment looked like a transport problem.
    """
    from src.core.agent_wrappers import EXAMPLE_WRAPPERS

    cldl = [w for w in EXAMPLE_WRAPPERS if w["id"] == "cldl"][0]
    assert 'ANTHROPIC_BASE_URL="http://${host}"' in cldl["script"]
    assert "/v1\"" not in cldl["script"], (
        "the base URL carries a /v1 suffix again; the SDK adds its own"
    )


def test_the_cldl_wrapper_refuses_rather_than_guesses():
    """Both inputs fail loudly instead of defaulting.

    A guessed host would connect somewhere the user did not choose; a
    guessed model would run something they did not pick. Both are the
    silent-wrong-answer shape, so both use ${VAR:?message}.
    """
    from src.core.agent_wrappers import EXAMPLE_WRAPPERS

    cldl = [w for w in EXAMPLE_WRAPPERS if w["id"] == "cldl"][0]
    assert "${CLDL_HOST:?" in cldl["script"]
    assert "${1:?" in cldl["script"]


def test_the_cldl_wrapper_accepts_a_model():
    """It takes the model as $1, so the picker must offer the model step."""
    from src.core.agent_wrappers import EXAMPLE_WRAPPERS

    cldl = [w for w in EXAMPLE_WRAPPERS if w["id"] == "cldl"][0]
    assert cldl["accepts_model"] is True
    assert cldl["family"] == "local"
