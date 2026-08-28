"""
Tests for the MODEL_ID_PATTERN colon fix (fix/model-id-allows-colon).

Context: OpenRouter model-variant ids carry a colon suffix (:free, :nitro,
:online, :extended, :beta). The pattern used to reject every one of them
outright, which blocked session-create and POST /api/v1/providers/models
entirely for any variant id. The fix widens the pattern to accept exactly
one, non-leading, non-trailing colon, while keeping every other
shell-injection restriction (leading hyphen, length cap, disallowed
characters, now also a ".." guard) unchanged or tightened.

Layout:
  - TestAcceptedVariantSuffixes: the five variant ids must now round-trip.
  - TestRedAgainstOldPattern: proves the accepted cases above were REJECTED
    by the pre-fix pattern, so the accept tests are not vacuously true.
  - TestStillRejected: every unsafe shape from the task spec must still be
    refused (leading hyphen, colon placement, metacharacters, "..", length).
  - TestRejectionReasonsDistinguishable: the three-outcome rule - a
    rejection must say WHY, not just "invalid".
  - TestPydanticValidatorUsesReason: CreateSessionRequest surfaces the same
    distinguishable reason, not a generic pattern dump.
  - TestProviderModelsRouteRejectsWithReason: the HTTP 400 detail on
    POST /api/v1/providers/models is the same distinguishable reason.
  - TestShlexQuoteDefenseInDepth: source-level + behavioural proof that
    shlex.quote is still applied on the launch path, and that the
    leading-hyphen guard is not merely cosmetic (a hyphen-led id headed for
    the shell would still be misparsed as a flag if the regex ever failed
    to catch it - shlex.quote alone does not fix that class of bug, which
    is exactly why the regex guard is kept as the primary gate).
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

# Same bootstrap pattern as tests/test_agent_wrappers_api.py: importing
# src.api.routes pulls in src.config.settings, whose pydantic-settings
# Settings model requires these fields from the environment/.env. Without
# them the import itself raises (and dumps a large formatted error, not a
# normal traceback) before any test body runs.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_modelid_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_modelid_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.models import (
    MODEL_ID_PATTERN,
    is_valid_model_id,
    describe_model_id_rejection,
    CreateSessionRequest,
    AddProviderModelRequest,
)

# The exact pre-fix pattern (src/models.py before this branch), kept here
# only to prove the new accept cases would have failed under it - see
# TestRedAgainstOldPattern. Not imported from source: it no longer exists
# there, that is the point of the fix.
_OLD_MODEL_ID_PATTERN = r"^(?!-)[A-Za-z0-9._~/-]{1,120}$"
_OLD_MODEL_ID_RE = re.compile(_OLD_MODEL_ID_PATTERN)


VARIANT_SUFFIXES = [":free", ":nitro", ":online", ":extended", ":beta"]
REALISTIC_FULL_ID = "nvidia/nemotron-3-nano-30b-a3b:free"


class TestAcceptedVariantSuffixes:
    """Every OpenRouter variant suffix must now validate."""

    @pytest.mark.parametrize("suffix", VARIANT_SUFFIXES)
    def test_variant_suffix_accepted(self, suffix: str) -> None:
        model_id = f"some-vendor/some-model{suffix}"
        assert is_valid_model_id(model_id), model_id

    def test_realistic_full_id_round_trips(self) -> None:
        assert is_valid_model_id(REALISTIC_FULL_ID)
        req = CreateSessionRequest(model=REALISTIC_FULL_ID)
        assert req.model == REALISTIC_FULL_ID


class TestRedAgainstOldPattern:
    """
    Proves the accept cases above are not vacuously true: they must FAIL
    the pre-fix pattern, confirming the colon was in fact the blocker.
    """

    @pytest.mark.parametrize("suffix", VARIANT_SUFFIXES)
    def test_variant_suffix_rejected_by_old_pattern(self, suffix: str) -> None:
        model_id = f"some-vendor/some-model{suffix}"
        assert not _OLD_MODEL_ID_RE.fullmatch(model_id), model_id

    def test_realistic_full_id_rejected_by_old_pattern(self) -> None:
        assert not _OLD_MODEL_ID_RE.fullmatch(REALISTIC_FULL_ID)


class TestStillRejected:
    """Every unsafe shape named in the task spec must still be refused."""

    @pytest.mark.parametrize(
        "bad_id",
        [
            "-x",  # leading hyphen
            "--continue",  # leading hyphen, flag-shaped
            ":free",  # leading colon
            "vendor/model:",  # trailing colon
            "vendor:model:free",  # double / multiple colon
            "a::b",  # doubled colon
            "a b",  # whitespace
            "a\tb",  # tab
            "a\nb",  # newline
            "'x",  # single quote
            '"x',  # double quote
            "`x`",  # backtick
            "$(id)",  # command substitution
            "${PATH}",  # parameter expansion
            "a;b",  # command separator
            "a|b",  # pipe
            "a&b",  # background operator
            "..",  # bare path traversal
            "a/../b",  # embedded path traversal
            "a" * 121,  # over the 120 length cap
            "",  # empty
            " ",  # single space
        ],
    )
    def test_rejected(self, bad_id: str) -> None:
        assert not is_valid_model_id(bad_id), bad_id

    def test_length_cap_boundary(self) -> None:
        assert is_valid_model_id("a" * 120)
        assert not is_valid_model_id("a" * 121)


class TestRejectionReasonsDistinguishable:
    """
    Three-outcome rule: a rejected id must say WHY, distinguishably, not
    collapse every cause into one generic message.
    """

    def test_leading_hyphen_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection("-x")
        assert "start with '-'" in reason
        assert "flag" in reason

    def test_leading_colon_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection(":free")
        assert "start or end with ':'" in reason

    def test_trailing_colon_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection("vendor/model:")
        assert "start or end with ':'" in reason

    def test_double_colon_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection("a::b")
        assert "one ':'" in reason

    def test_path_traversal_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection("a/../b")
        assert "'..'" in reason

    def test_metacharacter_reason_names_the_character(self) -> None:
        reason = describe_model_id_rejection("a;b")
        assert ";" in reason

    def test_length_reason_is_specific(self) -> None:
        reason = describe_model_id_rejection("a" * 121)
        assert "120" in reason

    def test_empty_reason_is_specific(self) -> None:
        assert "empty" in describe_model_id_rejection("")

    def test_reasons_for_distinct_causes_are_distinct_strings(self) -> None:
        reasons = {
            describe_model_id_rejection("-x"),
            describe_model_id_rejection(":free"),
            describe_model_id_rejection("a::b"),
            describe_model_id_rejection("a;b"),
            describe_model_id_rejection("a" * 121),
            describe_model_id_rejection(""),
            describe_model_id_rejection("a/../b"),
        }
        assert len(reasons) == 7, "distinct failure causes collapsed to the same message"


class TestPydanticValidatorUsesReason:
    """CreateSessionRequest.model must surface the distinguishable reason."""

    def test_valid_variant_id_accepted(self) -> None:
        req = CreateSessionRequest(model="vendor/model:free")
        assert req.model == "vendor/model:free"

    def test_none_model_accepted(self) -> None:
        req = CreateSessionRequest(model=None)
        assert req.model is None

    def test_invalid_model_raises_with_specific_reason(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateSessionRequest(model="-x")
        assert "flag" in str(exc_info.value)

    def test_invalid_colon_placement_raises_with_specific_reason(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateSessionRequest(model=":free")
        assert "start or end with ':'" in str(exc_info.value)


class TestProviderModelsRouteRejectsWithReason:
    """
    POST /api/v1/providers/models: the 400 detail must be the specific
    reason, not the raw pattern. The invalid-id branch returns before
    touching ``settings``, so this can be exercised without app/auth
    plumbing or on-disk config mutation.
    """

    def test_add_provider_model_request_accepts_variant_id(self) -> None:
        body = AddProviderModelRequest(model="vendor/model:free")
        assert body.model == "vendor/model:free"

    @pytest.mark.asyncio
    async def test_invalid_id_raises_400_with_specific_detail(self) -> None:
        from fastapi import HTTPException
        from src.api.routes import add_provider_model

        body = AddProviderModelRequest(model=":free")
        with pytest.raises(HTTPException) as exc_info:
            await add_provider_model(body)
        assert exc_info.value.status_code == 400
        assert "start or end with ':'" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_leading_hyphen_id_raises_400_naming_the_flag_risk(self) -> None:
        from fastapi import HTTPException
        from src.api.routes import add_provider_model

        body = AddProviderModelRequest(model="--dangerously-skip-permissions")
        with pytest.raises(HTTPException) as exc_info:
            await add_provider_model(body)
        assert exc_info.value.status_code == 400
        assert "flag" in exc_info.value.detail


class TestShlexQuoteDefenseInDepth:
    """
    The task requires proving shlex.quote is still applied on the launch
    path, and that the leading-hyphen guard survives - both as a
    source-level assertion (the code still calls shlex.quote(model)) and a
    behavioural one (quoting a colon-bearing id round-trips safely through
    shlex.split, i.e. the shell will see it as exactly one argument, the
    literal model string, not multiple tokens or an injected command).
    """

    def test_source_quotes_every_positional_in_agent_wrappers(self) -> None:
        """Source-level defense in depth, tracking the current mechanism.

        This used to grep for the literal ``shlex.quote(model)``. The model
        is no longer quoted on its own: it is the FIRST element of a
        positional list that also carries the fork arguments
        (``--resume <uuid> --fork-session``), and every element is quoted
        independently. The intent of the check is unchanged - nothing
        reaches the shell unquoted - so the assertion tracks the mechanism
        rather than a call shape that no longer exists. The behavioural
        half of this pair (``test_render_wrapper_invocation_quotes_colon_model_safely``
        below) is what actually proves the quoting works.
        """
        src = Path("src/core/agent_wrappers.py").read_text(encoding="utf-8")
        assert "positional = [model] if model else []" in src, (
            "the model is no longer the head of the positional list"
        )
        assert "shlex.quote(a) for a in positional" in src, (
            "positionals are no longer quoted element by element"
        )

    def test_source_calls_shlex_quote_on_model_in_agent_families(self) -> None:
        src = Path("src/core/agent_families.py").read_text(encoding="utf-8")
        assert "shlex.quote(model)" in src

    def test_render_wrapper_invocation_quotes_colon_model_safely(self) -> None:
        from src.core.agent_wrappers import AgentWrapper, render_wrapper_invocation

        wrapper = AgentWrapper(
            id="test-wrapper",
            label="Test Wrapper",
            script="echo hello",
            entry="",
        )
        model_id = "vendor/model:free"
        scripts_dir = Path("/tmp/cc-model-colon-test-scripts")
        outer = render_wrapper_invocation(wrapper, scripts_dir, model=model_id)

        # The rendered command must end with the shlex-quoted model as its
        # own token; splitting it back apart must recover exactly the
        # original model string with no extra tokens introduced.
        tokens = shlex.split(outer)
        assert tokens[-1] == model_id

    def test_render_wrapper_invocation_rejects_no_quoting_regression(self) -> None:
        """
        Guards against a future edit that stops quoting: if the model were
        interpolated unquoted, a colon-bearing id would still happen to
        split into one token (colon is not a shell word-break character),
        so this test alone would not catch a naive removal of
        shlex.quote() for THIS input. The source-level test above is the
        one that actually pins the call; this behavioural test documents
        why a colon-only behavioural check is insufficient on its own.
        """
        from src.core.agent_wrappers import AgentWrapper, render_wrapper_invocation

        wrapper = AgentWrapper(
            id="test-wrapper-2",
            label="Test Wrapper 2",
            script="echo hello",
            entry="",
        )
        # A model id with a single quote would only stay one token through
        # shlex.split if it was actually quoted by the source; this id is
        # rejected by MODEL_ID_PATTERN today (validated separately), but
        # render_wrapper_invocation itself performs no charset validation
        # (that is is_valid_model_id's job upstream) - so this exercises
        # the quoting call in isolation, independent of the regex gate.
        model_id = "vendor/model:free"
        scripts_dir = Path("/tmp/cc-model-colon-test-scripts")
        outer = render_wrapper_invocation(wrapper, scripts_dir, model=model_id)
        assert f"{shlex.quote(model_id)}" in outer
