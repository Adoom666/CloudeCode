"""Reading a label back: one exact key, one weak key, and the difference.

WHY THERE ARE TWO READS AND NOT ONE. Every surface that renders a
session's name needs the label for a tmux session it has in hand. Some
of those callers hold the creation epoch (a tmux listing row carries
``created_at_epoch``; the attribution prompt carries ``epoch``) and some
do not (a toast is recorded from a hook event with only a session id).

``label_for_instance`` is for the first group. It keys on the FULL
instance triple, so the answer is either this session's label or None -
it cannot be another session's label, ever.

``label_for_name`` is for the second group. It is keyed on the name
alone, which is weaker, and the point of the tests below is to bound
exactly HOW weak. The rule it must obey: the NEWEST row for that name
decides, and if that row carries no title the answer is None. The
previous shape searched PAST title-less rows for an older row that had
one, so a dead predecessor could lend its label to a live session that
had never been named - a wrong string presented to a human as identity.
That is the one behaviour these tests exist to forbid.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_label_reads.py -v
"""

from __future__ import annotations

import sys

from tests.lifecycle_helpers import ROOT, SOCKET, add_row, conn  # noqa: F401

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402

from src.core.session_label import label_for_instance, label_for_name


def _title(conn, uuid, title):
    """Set a row's title directly. Inputs: conn, uuid (str), title (str).

    Output: None.
    """
    conn.execute(
        "UPDATE sessions SET title = ? WHERE session_uuid = ?", (title, uuid)
    )


def test_an_exact_instance_read_answers_that_instances_label(conn):
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "Media Compression")
    assert (
        label_for_instance(conn, socket=SOCKET, name="cloude_a", epoch=1000)
        == "Media Compression"
    )


def test_an_exact_instance_read_refuses_a_different_epoch(conn):
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "Media Compression")
    assert (
        label_for_instance(conn, socket=SOCKET, name="cloude_a", epoch=2000)
        is None
    )


def test_an_exact_instance_read_with_no_epoch_answers_none(conn):
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "Media Compression")
    assert (
        label_for_instance(conn, socket=SOCKET, name="cloude_a", epoch=None)
        is None
    )


def test_the_name_read_answers_the_newest_rows_label(conn):
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "the old one")
    add_row(conn, uuid="u2", name="cloude_a", epoch=2000)
    _title(conn, "u2", "the live one")
    assert label_for_name(conn, socket=SOCKET, name="cloude_a") == "the live one"


def test_a_dead_predecessor_never_lends_its_label_to_an_unnamed_successor(conn):
    """THE ONE THIS FILE EXISTS FOR.

    An older instance of the same tmux name was labelled; the live one
    never was. The live one has no label, so the honest answer is None -
    which every surface renders as the tmux name. Answering "the old
    one" would put a stranger's name in a tab title.
    """
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "the old one")
    add_row(conn, uuid="u2", name="cloude_a", epoch=2000)
    assert label_for_name(conn, socket=SOCKET, name="cloude_a") is None


def test_an_empty_title_counts_as_no_label(conn):
    add_row(conn, uuid="u1", name="cloude_a", epoch=1000)
    _title(conn, "u1", "   ")
    assert label_for_name(conn, socket=SOCKET, name="cloude_a") is None
    assert (
        label_for_instance(conn, socket=SOCKET, name="cloude_a", epoch=1000)
        is None
    )


def test_an_unknown_name_answers_none_rather_than_raising(conn):
    assert label_for_name(conn, socket=SOCKET, name="cloude_nope") is None
    assert (
        label_for_instance(conn, socket=SOCKET, name="cloude_nope", epoch=1)
        is None
    )


def test_a_label_carrying_punctuation_reads_back_verbatim(conn):
    """The point of the feature: the label takes what a human typed.

    Spaces, ``:``, ``.``, ``"`` and ``$`` were all rejected by the old
    rename validator because the value was handed to tmux. It is not any
    more, so all of them must survive a store-and-read round trip
    unchanged.
    """
    hairy = 'client: acme v2.1 "prod" $rate'
    add_row(conn, uuid="u1", name="cloude_client_acme", epoch=1000)
    _title(conn, "u1", hairy)
    assert (
        label_for_instance(
            conn, socket=SOCKET, name="cloude_client_acme", epoch=1000
        )
        == hairy
    )
    assert label_for_name(conn, socket=SOCKET, name="cloude_client_acme") == hairy
