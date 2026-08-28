"""Regression proofs for D1 (the probe classifier) and D2/D3 (the parser).

Each test names the defect it closes and asserts the failure CANNOT
RECUR, not merely that today's inputs happen to work. Where a behavioural
test would only catch inputs somebody thought of, the proof is structural
instead: the D1 default-safety property and the D2 field-order property
are both asserted against the code, because the real risk in both is a
future edit rather than a future input.
"""

from __future__ import annotations

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
from src.core.tmux_listing import (
    REASON_CONNECT_FAILED,
    REASON_NO_SERVER,
    STDERR_CONNECT_FAILED,
    STDERR_NO_SERVER,
    STDERR_UNRECOGNISED,
    classify_listing_failure,
    classify_tmux_stderr,
    looks_like_no_server,
)
from src.core.tmux_listing_parse import (
    FIELD_SEPARATOR,
    LISTING_FORMAT,
    parse_listing_row,
    resolve_ownership,
)

# ===========================================================================
# D1 - only ONE connect errno is an answer. Everything else is a non-answer.
# ===========================================================================

#: Measured by running tmux 3.7b against each condition on this machine.
#: The adversary measured byte-identical text on 3.5a.
MEASURED_CONNECT_FAILURES = (
    "error connecting to /tmp/x/sock (Permission denied)",
    "error connecting to /tmp/x/sock (Socket operation on non-socket)",
    "error connecting to /very/long/path/sock (File name too long)",
)

#: Also measured: the ONLY connect errno that genuinely means no server,
#: plus the three plain-language forms that carry no errno at all.
MEASURED_NO_SERVER = (
    "error connecting to /tmp/x/absent (No such file or directory)",
    "no server running on /tmp/x/sock",
    "failed to connect to server",
    "no current server",
)


@pytest.mark.parametrize("stderr_text", MEASURED_CONNECT_FAILURES)
def test_a_connect_error_that_is_not_ENOENT_is_NOT_an_answer(stderr_text):
    """D1: a socket we could not reach must never render as zero sessions.

    Each of these is tmux saying "I could not look". Classifying them as
    a complete answer of zero walked the first-run import straight
    through its gate and stamped a one-way latch over the user's entire
    session history, with no error on any screen.
    """
    assert classify_tmux_stderr(stderr_text) == STDERR_CONNECT_FAILED
    assert looks_like_no_server(stderr_text) is False
    listing = classify_listing_failure(1, stderr_text)
    assert listing.ok is False
    assert listing.sessions == []
    assert listing.reason == REASON_CONNECT_FAILED
    assert listing.detail == stderr_text


@pytest.mark.parametrize("stderr_text", MEASURED_NO_SERVER)
def test_a_genuine_absent_server_still_IS_a_complete_answer_of_zero(stderr_text):
    """D1, the other half: the fix must not create a never-clearing unknown.

    A tmux server exits when its last session ends, so "no server" is the
    normal steady state of a machine with nothing running. If the fix
    collapsed these into CANNOT DETERMINE, the first-run import would
    stay pending forever on exactly the machines with nothing to import,
    and the notice would become furniture.

    Measured on tmux 3.7b: a server killed cleanly AND a server killed
    with SIGKILL both leave the socket file on disk and both report
    "no server running", NOT a connect error. So restricting the connect
    branch to ENOENT cannot strand the common dead-server case.
    """
    assert classify_tmux_stderr(stderr_text) == STDERR_NO_SERVER
    assert looks_like_no_server(stderr_text) is True
    listing = classify_listing_failure(1, stderr_text)
    assert listing.ok is True
    assert listing.sessions == []
    assert listing.reason == REASON_NO_SERVER


@pytest.mark.parametrize(
    "stderr_text",
    [
        "",
        "   ",
        "lost server",
        "error connecting to /tmp/x/sock",  # connect error, NO errno at all
        "no such file or directory",  # the errno alone, no connect context
    ],
)
def test_an_UNRECOGNISED_stderr_defaults_to_could_not_evaluate(stderr_text):
    """D1: the default must be the third outcome, never the confident one.

    This is the property that makes the fix survive a future tmux
    release. An errno the classifier has never seen, a connect line whose
    parentheses are missing or empty, or a message from a version that
    reworded everything - all degrade to "we did not look", which
    RETRIES. The alternative degradation, a confident zero, is PERMANENT.
    """
    assert classify_tmux_stderr(stderr_text) == STDERR_UNRECOGNISED
    assert looks_like_no_server(stderr_text) is False
    assert classify_listing_failure(1, stderr_text).ok is False


@pytest.mark.parametrize(
    "stderr_text",
    [
        "error connecting to /tmp/x (Some Errno From tmux 4.0)",
        "error connecting to /tmp/x ()",
        "error connecting to /tmp/x (Connection refused)",
    ],
)
def test_an_errno_OUTSIDE_the_allowlist_is_a_connect_failure(stderr_text):
    """D1: an unrecognised errno lands on the SAFE side of the split.

    It is reported as ``connect_failed`` rather than ``unrecognised``,
    which is a more useful label for the log, but the property that
    matters is identical for both: ``ok=False``. The allowlist is what
    guarantees that - membership is required to be an ANSWER, so an errno
    nobody has measured cannot become one by default.
    """
    assert classify_tmux_stderr(stderr_text) == STDERR_CONNECT_FAILED
    assert looks_like_no_server(stderr_text) is False
    assert classify_listing_failure(1, stderr_text).ok is False


def test_a_socket_path_containing_parentheses_does_not_confuse_the_errno():
    """D1: the errno is parsed anchored to the END, because paths are free-form."""
    text = "error connecting to /tmp/my (weird) dir/sock (Permission denied)"
    assert classify_tmux_stderr(text) == STDERR_CONNECT_FAILED
    ok_text = "error connecting to /tmp/my (weird) dir/s (No such file or directory)"
    assert classify_tmux_stderr(ok_text) == STDERR_NO_SERVER


def test_the_no_server_ALLOWLIST_is_exactly_one_errno():
    """D1, STRUCTURAL: allowlist, never blocklist, and it must stay tiny.

    A behavioural test only catches errnos somebody enumerated. The
    safety property is that the set of errnos treated as an ANSWER is a
    closed allowlist of one, so anything new is a non-answer by
    construction. If a future edit turns this into a blocklist, or adds
    an errno without a measurement behind it, this fails.
    """
    from src.core.tmux_listing import _NO_SERVER_CONNECT_ERRNOS

    assert _NO_SERVER_CONNECT_ERRNOS == frozenset({"no such file or directory"})


def test_looks_like_no_server_is_TRUE_for_exactly_one_of_three_outcomes():
    """D1, STRUCTURAL: the bool face must not re-widen the classifier.

    ``looks_like_no_server`` is the compatibility shim, and the original
    defect lived in it. It must be a pure projection of the three-valued
    classifier onto "is it NO_SERVER", so it cannot drift back into
    matching a bare prefix.
    """
    for verdict, sample in (
        (STDERR_NO_SERVER, "no server running on /x"),
        (STDERR_CONNECT_FAILED, "error connecting to /x (Permission denied)"),
        (STDERR_UNRECOGNISED, "lost server"),
    ):
        assert classify_tmux_stderr(sample) == verdict
        assert looks_like_no_server(sample) is (verdict == STDERR_NO_SERVER)


# ===========================================================================
# D2 - the instance triple must not be forgeable from a session name.
# ===========================================================================


def test_the_forged_line_from_the_demonstration_is_REFUSED():
    """D2: the adversary's measured tmux line must no longer parse.

    A session created as ``tmux new-session -s 'victim|1755000000|1'``
    printed ``victim|1755000000|1|1787070480|1`` under the old format.
    Under the new field order that line cannot validate at all: its first
    field is not a ``$<digits>`` session id. The forgery is not detected
    by a blocklist, it is unrepresentable.
    """
    assert parse_listing_row("victim|1755000000|1|1787070480|1") is None


def test_a_pipe_in_a_session_name_survives_VERBATIM():
    """D2: the name is the LAST field and a bounded split keeps it whole.

    The correct reading of a pipe-bearing name is the whole name, not a
    truncation and not a rejection - a project legitimately called
    ``api|prod`` must work.
    """
    row = parse_listing_row("$3|1755000000|2|api|prod")
    assert row == {
        "session_id": "$3",
        "created_at_epoch": 1755000000,
        "window_count": 2,
        "name": "api|prod",
    }


@pytest.mark.parametrize(
    "hostile_name",
    [
        "a|b",
        "a|b|c|d|e",
        "|leading",
        "trailing|",
        "$9|1|1",  # a name shaped like an entire valid row
        "  spaced  |  name  ",
        "unicode |namé| 中文",  # non-ASCII, no emoji (house style)
    ],
)
def test_no_session_name_can_alter_the_fields_in_front_of_it(hostile_name):
    """D2, the general property: the leading fields are tmux's alone.

    Whatever the name contains, the three tmux-generated fields parse to
    the values tmux emitted. This is the security claim stated as a test
    over a family of hostile names rather than one specimen.
    """
    line = f"$7|1787070480|4|{hostile_name}"
    row = parse_listing_row(line)
    assert row is not None
    assert row["session_id"] == "$7"
    assert row["created_at_epoch"] == 1787070480
    assert row["window_count"] == 4
    assert row["name"] == hostile_name


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "garbage",
        "$3|1755000000|2",  # too few fields: no name
        "3|1755000000|2|name",  # session id missing its $
        "$x|1755000000|2|name",  # session id not numeric
        "$3|notanepoch|2|name",
        "$3|1755000000|notanint|name",
        "$3|1755000000|2|",  # empty name
        "$3|-1|2|name",  # negative is not what tmux emits
    ],
)
def test_a_row_that_does_not_VALIDATE_is_refused_not_guessed(line):
    """D2: three outcomes for a parser too - parsed, absent, never a guess.

    The old parser coerced an unreadable epoch to 0 and carried on, which
    manufactured an instance triple out of a line it had not understood.
    """
    assert parse_listing_row(line) is None


def test_the_FORMAT_STRING_puts_the_caller_controlled_field_LAST():
    """D2, STRUCTURAL: the whole safety argument rests on field ORDER.

    A behavioural test cannot catch someone reordering the format string
    back, because the parser and the format would still agree with each
    other while both became forgeable again. This asserts the property
    directly: ``session_name`` is the final field, and every field before
    it is one tmux generates.
    """
    fields = LISTING_FORMAT.split(FIELD_SEPARATOR)
    assert fields[-1] == "#{session_name}", (
        "the caller-controlled session NAME must be the LAST field, or a "
        "name containing the delimiter can forge the fields after it"
    )
    assert "#{session_name}" not in fields[:-1]
    assert fields[:-1] == [
        "#{session_id}",
        "#{session_created}",
        "#{session_windows}",
    ]


def test_the_split_is_BOUNDED_to_the_leading_field_count():
    """D2, STRUCTURAL: an unbounded split would re-open the hole.

    Field order alone is not sufficient - it only works because the split
    stops counting before it reaches the name. This pins the two together
    so neither can be changed without the other.
    """
    from src.core.tmux_listing_parse import _MAXSPLIT

    assert _MAXSPLIT == len(LISTING_FORMAT.split(FIELD_SEPARATOR)) - 1


def test_the_app_cannot_MINT_a_name_containing_the_delimiter():
    """D2, defence in depth: no attacker is needed to break the old parser.

    A project called ``api|prod`` did it by accident. The parser now
    handles such a name correctly, so this is belt-and-braces, but a name
    the app minted itself should never depend on a parser subtlety.
    """
    from src.core.session_manager import _sanitize_tmux_name

    assert FIELD_SEPARATOR not in _sanitize_tmux_name("api|prod")
    assert _sanitize_tmux_name("api|prod") == "api_prod"
    # control characters would split one session across two listing rows
    assert _sanitize_tmux_name("a\nb") == "a b"
    # tab/newline are collapsed to a space by the pre-existing rule 2,
    # not replaced with an underscore; only NON-whitespace controls are.
    assert _sanitize_tmux_name("a\tb") == "a b"
    assert _sanitize_tmux_name("a\x00b") == "a_b"


# ===========================================================================
# D3 - the legacy name set must not defeat a stored epoch.
# ===========================================================================


def test_a_None_epoch_matches_NOTHING():
    """D3: the wildcard is gone, and it is gone at the matcher.

    ``(name, None)`` used to match any epoch for that name, which
    disabled the identity tier for every session the app had created
    since the last restart.
    """
    assert resolve_ownership("foo", 1, {("foo", None)}, None) is False
    assert resolve_ownership("foo", 9999, {("foo", None)}, None) is False


def test_a_stored_epoch_for_the_NAME_is_a_negative_answer_for_other_epochs():
    """D3: tier 2. The DB's epoch-keyed opinion beats the name set.

    This is the exact scenario from the demonstration: the app owned
    ``cloude_work`` at one epoch, it died, the user made his own
    unrelated ``cloude_work``, and the legacy in-memory set still held
    the bare name because the app created the dead one this boot.
    """
    owned = {("cloude_work", 1755000000)}
    legacy = {"cloude_work"}
    assert resolve_ownership("cloude_work", 1787070999, owned, legacy) is False
    assert resolve_ownership("cloude_work", 1755000000, owned, legacy) is True


def test_the_legacy_tier_survives_when_the_DB_is_silent_about_the_name():
    """D3: the fix must not regress freshly-created sessions to external.

    A session created since the last restart has no row yet. The DB has
    no opinion about that NAME at all, so the degraded name-only tier is
    the best evidence available and is correctly used.
    """
    assert resolve_ownership("fresh", 5, {("other", 1)}, {"fresh"}) is True


def test_an_empty_instance_set_is_an_ANSWER_not_a_fallback():
    """D3: empty set means "the app owns nothing"; None means "no opinion"."""
    assert resolve_ownership("cloude_x", 1, set(), None) is False
    assert resolve_ownership("cloude_x", 1, None, None, prefix="cloude_") is True


def test_the_prefix_heuristic_applies_ONLY_when_neither_source_was_supplied():
    """D3: a supplied-but-silent owned set must not reopen the spoofable tier."""
    assert resolve_ownership("cloude_x", 1, None, set(), prefix="cloude_") is False
    assert resolve_ownership("cloude_x", 1, set(), None, prefix="cloude_") is False
    assert resolve_ownership("cloude_x", 1, None, None, prefix="cloude_") is True
