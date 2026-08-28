"""Regressions for the tmux ROW delimiter, the stderr classifier anchoring
and the listing locale pin (findings V1, V4 and V6 of the second
adversarial round).

WHY THESE THREE SIT TOGETHER. All three are the same mistake in three
places: a decision made on text that the CALLER controls, using a rule
looser than the one the producer actually follows.

  V1  Python's ``str.splitlines()`` recognises TEN line boundaries.
      tmux rejects only SEVEN of them in a session name. The three it
      accepts - U+0085 NEL, U+2028 LS, U+2029 PS - let one tmux row
      become several parser rows, and the extra rows were entirely
      attacker-chosen, forging the identity triple the ownership badge
      is computed from.
  V4  The no-server markers were tested as a bare substring against the
      whole stderr blob, and tmux echoes the user-configured socket path
      into stderr. A path containing the literal text "no server
      running" turned a real "(Permission denied)" into a confident
      answer of zero sessions.
  V6  ``strerror`` is localised by glibc, so the one-English-string
      errno allowlist would never match under a non-English locale on
      Linux - a check that can never clear.

WHERE THE ASSERTIONS LIVE, WHICH IS THE LESSON FROM THE LAST ROUND. The
previous round's AST proof constrained the FIELD parser while the hole
sat one layer up in the caller. So the structural proof here walks the
BACKEND - the layer that actually chooses a splitter - and not the
parser, which never had the defect and cannot prevent it.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_listing import (
    LISTING_ENV_OVERRIDES,
    STDERR_CONNECT_FAILED,
    STDERR_NO_SERVER,
    STDERR_UNRECOGNISED,
    classify_listing_failure,
    classify_tmux_stderr,
    listing_env,
)
from src.core.tmux_listing_parse import (
    LINE_BOUNDARY_CHARS,
    name_has_line_boundary,
    parse_listing_row,
    resolve_ownership,
    split_listing_rows,
)

#: The three boundaries tmux ACCEPTS in a session name, measured against
#: tmux 3.7b. These are the forgery primitives; the other seven are
#: rejected by tmux itself and cannot reach the parser from a real
#: server.
TMUX_ACCEPTED_BOUNDARIES = ("\x85", " ", " ")

#: A real epoch the datastore legitimately holds for cloude_work.
REAL_EPOCH = 1755000000


# ---------------------------------------------------------------------------
# V1  the row delimiter
# ---------------------------------------------------------------------------


def test_python_and_tmux_disagree_about_line_boundaries() -> None:
    """Pin the divergence this whole finding rests on.

    If a future Python stops splitting on NEL/LS/PS, or the constant is
    edited, this fails and the reasoning in the module docstrings has to
    be revisited rather than silently becoming wrong.
    """
    for char in TMUX_ACCEPTED_BOUNDARIES:
        assert len(f"x{char}y".splitlines()) == 2, (
            f"{char!r} is no longer a Python line boundary; the row "
            "delimiter analysis needs redoing"
        )
        assert char in LINE_BOUNDARY_CHARS


@pytest.mark.parametrize("boundary", TMUX_ACCEPTED_BOUNDARIES)
def test_forged_payload_yields_exactly_one_row(boundary: str) -> None:
    """THE FORGERY, against the real splitter. One tmux line, one row.

    This is the exact payload from the demonstration: a session named
    ``pwn<boundary>$99|<real epoch>|1|cloude_work``. tmux emits ONE line.
    ``splitlines()`` turned it into two, the second forging the whole
    identity triple. ``split_listing_rows`` must yield one.
    """
    name = f"pwn{boundary}$99|{REAL_EPOCH}|1|cloude_work"
    stdout_text = f"$7|1787000000|1|{name}\n"

    assert len(stdout_text.splitlines()) == 2, "the payload must still be live"
    rows = split_listing_rows(stdout_text)
    assert len(rows) == 1, f"one tmux line must be one row, got {rows!r}"


@pytest.mark.parametrize("boundary", TMUX_ACCEPTED_BOUNDARIES)
def test_forged_payload_never_badges_as_owned(boundary: str) -> None:
    """The end-to-end property: the forgery cannot produce an owned row.

    Asserted on the OUTCOME the attack was after - ``created_by_cloude``
    True for a name and epoch the app really owns - rather than on any
    intermediate representation, so a future refactor that changes how
    rows are shaped still has to keep this false.
    """
    owned = {("cloude_work", REAL_EPOCH)}
    name = f"pwn{boundary}$99|{REAL_EPOCH}|1|cloude_work"
    stdout_text = f"$7|1787000000|1|{name}\n"

    badged: List[str] = []
    for row in split_listing_rows(stdout_text):
        parsed = parse_listing_row(row)
        if parsed is None:
            continue
        if resolve_ownership(
            parsed["name"], parsed["created_at_epoch"], owned, None,
            prefix="cloude_",
        ):
            badged.append(parsed["name"])

    assert badged == [], f"forged row badged as ours: {badged!r}"


@pytest.mark.parametrize("boundary", TMUX_ACCEPTED_BOUNDARIES)
def test_boundary_in_name_is_refused_by_the_parser(boundary: str) -> None:
    """Defence in depth: a name carrying a boundary is refused outright.

    Under the correct split the whole injection payload lands INSIDE the
    name field, so this turns the attack from "renders as one session
    with a strange name" into "refused and logged".
    """
    assert name_has_line_boundary(f"a{boundary}b") is True
    assert parse_listing_row(f"$3|1|1|a{boundary}b") is None


def test_multi_row_forgery_collapses_to_one_row() -> None:
    """Scenario C: N forged rows from ONE session must yield one row."""
    sep = " "
    name = (
        f"a{sep}$1|1|1|forged_one{sep}$2|2|1|forged_two"
        f"{sep}$3|3|1|forged_three"
    )
    stdout_text = f"$9|1787000000|1|{name}\n"

    assert len(stdout_text.splitlines()) == 4
    assert len(split_listing_rows(stdout_text)) == 1


def test_legitimate_rows_still_parse() -> None:
    """The fix must not cost the ordinary cases, including pipes in names."""
    stdout_text = "$1|1755000000|2|api|prod\n$2|1755000001|1|plain\n"
    rows = split_listing_rows(stdout_text)
    assert len(rows) == 2
    assert parse_listing_row(rows[0])["name"] == "api|prod"
    assert parse_listing_row(rows[1])["name"] == "plain"


def test_crlf_and_trailing_newline_do_not_create_empty_rows() -> None:
    """A CRLF stream and a trailing newline must not manufacture rows."""
    assert split_listing_rows("$1|1|1|a\r\n$2|2|1|b\r\n") == [
        "$1|1|1|a",
        "$2|2|1|b",
    ]
    assert split_listing_rows("") == []
    assert split_listing_rows("\n\n") == []


def test_no_listing_site_uses_splitlines() -> None:
    """THE STRUCTURAL PROOF, asserted at the layer that enforces it.

    Walks the AST of tmux_backend and fails if any call to
    ``.splitlines()`` takes the stdout of a listing as its receiver. The
    previous round proved a property about the field parser while the
    defect sat in the caller; this asserts against the caller, because
    the caller is what chooses the splitter.

    ``capture_scrollback`` and the pane probe legitimately split real
    multi-line terminal output, so the proof is scoped to the names that
    carry LISTING stdout rather than banning ``splitlines`` outright -
    a ban nobody could keep would just get suppressed.
    """
    source = (ROOT / "src" / "core" / "tmux_backend.py").read_text()
    tree = ast.parse(source)

    listing_stdout_names = {"stdout_text", "raw_lines", "names"}
    offenders: List[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "splitlines":
            continue
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in listing_stdout_names:
            offenders.append(f"line {node.lineno}: {receiver.id}.splitlines()")

    assert offenders == [], (
        "tmux listing stdout must be split with split_listing_rows, not "
        "str.splitlines() - splitlines recognises NEL, LS and PS, which "
        f"tmux permits in a session name. Offenders: {offenders}"
    )


def test_backend_imports_the_row_splitter() -> None:
    """The backend must actually depend on the splitter, not just avoid
    splitlines. Without this, deleting every listing call would pass the
    proof above vacuously."""
    source = (ROOT / "src" / "core" / "tmux_backend.py").read_text()
    assert "split_listing_rows" in source
    assert source.count("split_listing_rows(") >= 3, (
        "all three enumeration sites (list_attachable_sessions, "
        "list_pane_status_all, discover_existing) must use it"
    )


# ---------------------------------------------------------------------------
# V4  the no-server markers must be anchored, and the errno must win
# ---------------------------------------------------------------------------


def test_socket_path_containing_a_marker_cannot_forge_no_server() -> None:
    """THE BYPASS. A real permission error must not read as zero sessions.

    ``session.tmux_socket_name`` is user-configurable and tmux echoes the
    path verbatim into stderr. This is the measured tmux 3.7b string for
    a socket under a directory named ``no server running``.
    """
    stderr = (
        "error connecting to /tmp/s4verify/no server running/sock "
        "(Permission denied)"
    )
    assert classify_tmux_stderr(stderr) == STDERR_CONNECT_FAILED
    listing = classify_listing_failure(1, stderr)
    assert listing.ok is False, "a probe that could not look is not an answer"
    assert listing.reason == "connect_failed"


def test_marker_in_path_with_absent_server_errno_still_reads_no_server() -> None:
    """The anchoring must not break the genuine case it sits next to."""
    stderr = (
        "error connecting to /tmp/no server running/sock "
        "(No such file or directory)"
    )
    assert classify_tmux_stderr(stderr) == STDERR_NO_SERVER
    assert classify_listing_failure(1, stderr).ok is True


def test_newline_in_socket_path_cannot_forge_a_marker_line() -> None:
    """The residual case anchoring alone would miss.

    A socket name containing a newline puts the forged marker at the
    START of its own line, where a line-anchored test would match it.
    The connect-error line still exists and still decides, which is why
    the errno is consulted FIRST.
    """
    stderr = "error connecting to /tmp/evil\nno server running (Permission denied)"
    assert classify_tmux_stderr(stderr) != STDERR_NO_SERVER
    assert classify_listing_failure(1, stderr).ok is False


def test_a_marker_mid_line_does_not_classify_as_no_server() -> None:
    """ANCHORING, isolated from the errno path.

    Written because a differential proof showed the errno-first ordering
    makes the bypass test above pass even with the marker test
    UNANCHORED - the connect branch returns before the markers are ever
    consulted. So anchoring needed its own case: text that mentions a
    marker without starting a line with it, and with no connect-error
    line to short-circuit on.

    Without this, "unanchor the markers" is a surviving mutant.
    """
    for stderr in (
        "unexpected failure: no server running somewhere",
        "tmux said something about failed to connect to server earlier",
        "context: no current server, but that is not what happened here",
    ):
        assert classify_tmux_stderr(stderr) == STDERR_UNRECOGNISED, stderr
        assert classify_listing_failure(1, stderr).ok is False


def test_genuine_no_server_messages_still_classify() -> None:
    """The real tmux wording, at the start of a line, must still answer."""
    for stderr in (
        "no server running on /tmp/tmux-501/cloude",
        "no server running on /tmp/x\n",
        "  no server running on /tmp/x  ",
        "warning: something\nno server running on /tmp/x",
    ):
        assert classify_tmux_stderr(stderr) == STDERR_NO_SERVER, stderr
        assert classify_listing_failure(1, stderr).ok is True


def test_unreadable_and_empty_stderr_never_become_an_answer() -> None:
    """The deliberate default: no guess, on either shape."""
    for stderr in ("", "   ", "something entirely unexpected"):
        assert classify_tmux_stderr(stderr) == STDERR_UNRECOGNISED
        assert classify_listing_failure(1, stderr).ok is False


def test_connect_error_with_no_readable_errno_is_unrecognised() -> None:
    """A connect failure whose cause we cannot read is not an answer."""
    assert classify_tmux_stderr("error connecting to /tmp/x") == (
        STDERR_UNRECOGNISED
    )


def test_localised_errno_does_not_become_a_confident_zero() -> None:
    """V6's failure mode, asserted from the classifier's side.

    A translated strerror must degrade to could-not-look, never to a
    confident zero. The LC_ALL pin below is what stops it arising; this
    asserts that even if it did, the answer is safe.
    """
    for stderr in (
        "error connecting to /tmp/x (Aucun fichier ou dossier de ce type)",
        "error connecting to /tmp/x (Datei oder Verzeichnis nicht gefunden)",
    ):
        assert classify_tmux_stderr(stderr) == STDERR_CONNECT_FAILED
        assert classify_listing_failure(1, stderr).ok is False


# ---------------------------------------------------------------------------
# V6  the listing subprocess runs under a pinned locale
# ---------------------------------------------------------------------------


def test_listing_env_pins_lc_all_to_c() -> None:
    """The errno allowlist is English, so the child must speak English."""
    assert LISTING_ENV_OVERRIDES["LC_ALL"] == "C"
    env = listing_env()
    assert env["LC_ALL"] == "C"


def test_listing_env_inherits_the_rest_and_does_not_mutate_os_environ() -> None:
    """It must be a complete environment, and a copy."""
    marker = "S4_VERIFY_MARKER"
    os.environ[marker] = "present"
    try:
        env = listing_env()
        assert env[marker] == "present", "child must inherit the real env"
        env["LC_ALL"] = "mutated"
        assert listing_env()["LC_ALL"] == "C", "must return a fresh dict"
        assert os.environ.get("LC_ALL") != "mutated"
    finally:
        os.environ.pop(marker, None)


def test_only_the_listing_path_pins_the_locale() -> None:
    """Session-CREATING commands must inherit the real environment.

    What is handed to ``new-session`` becomes the user's shell
    environment; forcing LC_ALL=C there would break their UTF-8
    rendering. So the pin belongs to _run_listing, not _run_tmux_sync.
    """
    source = (ROOT / "src" / "core" / "tmux_backend.py").read_text()
    assert source.count("listing_env()") == 1, (
        "the locale pin must be applied at exactly one site, the listing "
        "path; applying it in _run_tmux_sync would leak into new-session"
    )
