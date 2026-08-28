"""The opt-in trace: off by default, safe when on, never the fault."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core import debug_trace


@pytest.fixture()
def traced(tmp_path, monkeypatch):
    """Tracing ON, writing into tmp_path."""
    monkeypatch.setenv("CLOUDE_DEBUG", "1")
    monkeypatch.setenv("CLOUDE_DEBUG_DIR", str(tmp_path))
    monkeypatch.setattr(debug_trace, "_trace_path", lambda: tmp_path / "trace.jsonl")
    return tmp_path / "trace.jsonl"


def _lines(path):
    """Parsed trace records."""
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# --- off by default ----------------------------------------------------------


def test_it_is_off_unless_explicitly_enabled(monkeypatch):
    """The normal state. A debug facility that is on by default is a log."""
    monkeypatch.delenv("CLOUDE_DEBUG", raising=False)
    assert debug_trace.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_usual_truthy_spellings_all_work(monkeypatch, value):
    """Nobody should have to guess which word this flag wants."""
    monkeypatch.setenv("CLOUDE_DEBUG", value)
    assert debug_trace.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsey_spellings_stay_off(monkeypatch, value):
    monkeypatch.setenv("CLOUDE_DEBUG", value)
    assert debug_trace.enabled() is False


def test_writing_while_disabled_produces_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDE_DEBUG", raising=False)
    monkeypatch.setattr(debug_trace, "_trace_path", lambda: tmp_path / "trace.jsonl")
    debug_trace.trace("should.not.appear", a=1)
    assert not (tmp_path / "trace.jsonl").exists()


# --- it records what you asked for -------------------------------------------


def test_a_record_carries_the_event_and_fields(traced):
    debug_trace.trace("hook.lifecycle.received", event_kind="SessionStart", ok=True)
    rows = _lines(traced)
    assert len(rows) == 1
    assert rows[0]["event"] == "hook.lifecycle.received"
    assert rows[0]["event_kind"] == "SessionStart"
    assert rows[0]["ok"] is True
    assert "ts" in rows[0] and "pid" in rows[0]


# --- secrets -----------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "token", "CLOUDECODE_HOOK_TOKEN", "api_key", "jwt_secret",
    "password", "Authorization", "cookie",
])
def test_secret_shaped_keys_are_never_written_in_full(traced, key):
    """THE POINT OF THE REDACTION.

    This file exists to be pasted into a conversation while debugging, so
    a token in it is a token in a transcript. Redaction is by KEY NAME,
    not by inspecting the value, so a credential is caught even when it
    looks innocuous.
    """
    debug_trace.trace("x", **{key: "SUPERSECRETVALUE"})
    body = traced.read_text()
    assert "SUPERSECRETVALUE" not in body
    assert "fp=" in body and "len=16" in body


def test_a_nested_secret_is_redacted_too(traced):
    debug_trace.trace("x", outer={"inner": {"auth_token": "NESTEDSECRET"}})
    assert "NESTEDSECRET" not in traced.read_text()


def test_the_fingerprint_answers_is_it_the_same_one(traced):
    """Which is the only question worth asking about a token in a log.

    Length is included because a WRONG-SHAPED secret is a common bug and
    length alone often names it.
    """
    a = debug_trace.fingerprint("same")
    b = debug_trace.fingerprint("same")
    c = debug_trace.fingerprint("different")
    assert a == b and a != c
    assert a.startswith("len=4 fp=")


def test_a_non_secret_value_is_written_plainly(traced):
    """Redacting everything would make the trace useless."""
    debug_trace.trace("x", url="http://127.0.0.1:8000/api/v1/hooks/claude-event")
    assert "127.0.0.1:8000" in traced.read_text()


# --- it must never become the fault ------------------------------------------


def test_an_unwritable_destination_does_not_raise(monkeypatch):
    """This runs inside request handlers and a subprocess spawn path."""
    monkeypatch.setenv("CLOUDE_DEBUG", "1")
    monkeypatch.setattr(debug_trace, "_trace_path", lambda: Path("/proc/nope/x.jsonl"))
    debug_trace.trace("x", a=1)  # must not raise


def test_an_unserializable_value_does_not_raise(traced):
    debug_trace.trace("x", obj=object())
    assert _lines(traced), "the record should still land, rendered via default=str"


def test_a_resolution_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("CLOUDE_DEBUG", "1")

    def boom():
        raise RuntimeError("no state dir")

    monkeypatch.setattr(debug_trace, "_trace_path", boom)
    debug_trace.trace("x", a=1)  # must not raise


# --- bounded -----------------------------------------------------------------


def test_a_very_long_string_is_truncated_with_a_marker(traced):
    """A trace line that scrolls for a screen is one nobody reads."""
    debug_trace.trace("x", blob="z" * 5000)
    row = _lines(traced)[0]
    assert "truncated 5000 chars" in row["blob"]
    assert len(row["blob"]) < 1000


# --- redaction must not eat the evidence -------------------------------------


def test_a_field_that_merely_contains_a_hint_is_not_redacted(traced):
    """THE TRACER HID ITS OWN MOST USEFUL FIELD.

    The first rule was ``"key" in key.lower()``, so ``payload_keys`` - a
    list of field NAMES, the single most diagnostic thing the hook trace
    records - came out as a fingerprint. A tracer that hides the evidence
    is worse than one that is merely noisy, and this one hid it while
    looking like it was working.
    """
    debug_trace.trace("x", payload_keys=["session_id", "source"])
    body = traced.read_text()
    assert "session_id" in body and "source" in body
    assert "fp=" not in body


def test_a_word_that_happens_to_contain_a_hint_is_safe(traced):
    debug_trace.trace("x", monkey="not a secret", keyboard="also fine")
    body = traced.read_text()
    assert "not a secret" in body and "also fine" in body


@pytest.mark.parametrize("key", ["api_key", "x-api-key", "auth.token", "JWT_SECRET"])
def test_real_credentials_are_still_redacted(traced, key):
    """The bias stays toward redacting."""
    debug_trace.trace("x", **{key: "REALSECRET"})
    assert "REALSECRET" not in traced.read_text()


def test_a_non_string_value_under_a_secret_name_is_kept(traced):
    """A count or a bool is not a credential.

    Fingerprinting one destroys the only information the line carried and
    protects nothing.
    """
    debug_trace.trace("x", token_count=42, has_key=True)
    body = traced.read_text()
    assert '"token_count": 42' in body
    assert '"has_key": true' in body
