"""The matcher must be able to say NO, and these tests are the proof.

A matcher that always finds something is worse than useless: attaching
the wrong conversation to a session silently resumes a stranger's context
into the user's pane, which is strictly worse than leaving the row empty.
So the negative controls here are not an afterthought - they are the
point. Every abstention case is asserted explicitly, including the one
where the transcript genuinely does not exist.
"""

from __future__ import annotations

from src.core.session_uuid_backfill import (
    KIND_FILL,
    KIND_REPLACE,
    OUTCOME_AMBIGUOUS,
    OUTCOME_COLLIDES,
    OUTCOME_CONFIDENT,
    OUTCOME_NO_CANDIDATE,
    OUTCOME_SOUND,
    UUID_ABSENT,
    UUID_PHANTOM,
    UUID_PRESENT,
    RowFacts,
    TranscriptFacts,
    classify_row,
    signal_families,
)
from src.core.session_uuid_backfill_rules import propose_for_row, sibling_uuid_map

NOW = 1_788_700_000.0
DIR = "-Users-x-Proj"
WD = "/Users/x/Proj"


def _t(uuid: str, **kw) -> TranscriptFacts:
    """Build a transcript that is in the right directory and recently active.

    Inputs: uuid (str), plus TranscriptFacts overrides.
    Output: TranscriptFacts.
    """
    base = dict(
        path=f"/p/{DIR}/{uuid}.jsonl",
        project_dir=DIR,
        recorded_cwd=WD,
        first_ts=NOW - 10_000,
        last_ts=NOW - 100,
        mtime=NOW - 100,
    )
    base.update(kw)
    return TranscriptFacts(uuid=uuid, **base)


def _row(**kw) -> RowFacts:
    """Build a row whose pane is live and whose directory is WD.

    Inputs: RowFacts overrides.
    Output: RowFacts.
    """
    base = dict(
        row_id=1,
        working_dir=WD,
        tmux_name="cloude_Proj",
        tmux_created_epoch=int(NOW - 5_000),
    )
    base.update(kw)
    return RowFacts(**base)


def _propose(row, transcripts, **kw):
    """Run the matcher with the directory listing supplied, not probed.

    Inputs: row (RowFacts), transcripts (list), plus propose_for_row kwargs.
    Output: Proposal.
    """
    kw.setdefault("claims", {})
    kw.setdefault("now", NOW)
    kw.setdefault("existing_dirs", [DIR])
    return propose_for_row(row, transcripts, **kw)


# ---------------------------------------------------------------------------
# THE NEGATIVE CONTROL. A matcher that cannot refuse is not a matcher.
# ---------------------------------------------------------------------------


def test_no_candidate_when_the_transcript_genuinely_does_not_exist():
    """A row whose conversation is not on disk gets NOTHING invented for it.

    THE NEGATIVE CONTROL required by the brief: feed the matcher a row
    whose transcript genuinely does not exist and prove it reports no
    candidate rather than reaching for the nearest file.
    """
    row = _row(working_dir="/Users/x/NoSuchProject", tmux_name="cloude_Ghost")
    proposal = _propose(row, [_t("aaaa"), _t("bbbb")], existing_dirs=[DIR])

    assert proposal.outcome == OUTCOME_NO_CANDIDATE
    assert proposal.proposed_uuid is None
    assert proposal.writable is False


def test_no_candidate_when_there_are_no_transcripts_at_all():
    """An empty corpus produces an abstention, never a fabrication."""
    proposal = _propose(_row(), [])
    assert proposal.outcome == OUTCOME_NO_CANDIDATE
    assert proposal.proposed_uuid is None


def test_a_probe_transcript_is_never_a_match():
    """Claude Code's own liveness probes are excluded outright."""
    proposal = _propose(_row(), [_t("probe", is_probe=True)])
    assert proposal.outcome == OUTCOME_NO_CANDIDATE


# ---------------------------------------------------------------------------
# Three row classes, because counting two hides the dangerous one.
# ---------------------------------------------------------------------------


def test_classify_row_separates_absent_present_and_phantom():
    """A recorded uuid with no transcript is its OWN class, not 'has one'."""
    assert classify_row(_row(), []) == UUID_ABSENT
    assert classify_row(_row(recorded_uuid="u"), ["u"]) == UUID_PRESENT
    assert classify_row(_row(recorded_uuid="u"), ["other"]) == UUID_PHANTOM


def test_a_sound_row_is_left_entirely_alone():
    """A row whose uuid has a transcript is never re-proposed."""
    row = _row(recorded_uuid="aaaa")
    proposal = _propose(row, [_t("aaaa")])
    assert proposal.outcome == OUTCOME_SOUND
    assert proposal.writable is False


# ---------------------------------------------------------------------------
# Evidence, and the bar it has to clear.
# ---------------------------------------------------------------------------


def test_pane_argv_is_decisive_even_among_many_neighbours():
    """The running process naming its own conversation settles the question."""
    transcripts = [_t("aaaa"), _t("bbbb"), _t("cccc")]
    proposal = _propose(_row(), transcripts, argv_uuid="bbbb")

    assert proposal.outcome == OUTCOME_CONFIDENT
    assert proposal.proposed_uuid == "bbbb"
    assert proposal.kind == KIND_FILL
    assert "pane_argv" in proposal.evidence
    assert proposal.writable is True


def test_two_correlated_location_signals_do_not_clear_the_bar():
    """`project_dir` and `recorded_cwd` answer ONE question, not two.

    FAILS BEFORE THE FIX: raw signals were counted, so directory
    agreement corroborated by itself read as two independent facts and
    promoted one of three transcripts to "confident". Caught on the
    owner's live data, on his archived CloudeCode row.
    """
    lonely = _t("aaaa", last_ts=None, mtime=None)
    proposal = _propose(_row(), [lonely])

    assert signal_families(["project_dir", "recorded_cwd"]) == {"location"}
    assert proposal.outcome == OUTCOME_AMBIGUOUS
    assert proposal.writable is False


def test_two_independent_families_do_clear_the_bar():
    """Location plus timing is two different questions agreeing."""
    proposal = _propose(_row(), [_t("aaaa")])
    assert proposal.outcome == OUTCOME_CONFIDENT
    assert proposal.proposed_uuid == "aaaa"


def test_a_tie_between_two_qualified_candidates_is_never_broken():
    """Recency, size and order are all refused as tiebreakers."""
    proposal = _propose(_row(), [_t("aaaa"), _t("bbbb", last_ts=NOW - 1)])
    assert proposal.outcome == OUTCOME_AMBIGUOUS
    assert set(proposal.candidates) == {"aaaa", "bbbb"}
    assert proposal.writable is False


def test_evidence_without_a_winner_is_ambiguous_not_absent():
    """`too little evidence` and `nothing here` are different claims.

    FAILS BEFORE THE FIX: a directory holding 45 plausible transcripts
    reported "no transcript on disk could be this row", which is a false
    negative stated with confidence - the same failure mode as a false
    match, pointed the other way.
    """
    thin = [_t(u, last_ts=None, mtime=None) for u in ("aaaa", "bbbb")]
    proposal = _propose(_row(), thin)

    assert proposal.outcome == OUTCOME_AMBIGUOUS
    assert len(proposal.candidates) == 2


# ---------------------------------------------------------------------------
# The UNIQUE index as an adversarial oracle.
# ---------------------------------------------------------------------------


def test_a_uuid_another_row_claims_is_refused_and_reported_as_a_pair():
    """A collision proves the proposal wrong, so it is never written.

    ``claude_session_uuid`` is UNIQUE, so two rows cannot both hold it.
    This module is not entitled to decide which row loses, so it reports
    the pair and writes nothing.
    """
    row = _row(row_id=7, recorded_uuid="phantom")
    proposal = _propose(row, [_t("aaaa")], claims={"aaaa": 4}, argv_uuid="aaaa")

    assert proposal.outcome == OUTCOME_COLLIDES
    assert proposal.collides_with == 4
    assert proposal.proposed_uuid == "aaaa"
    assert proposal.writable is False


def test_a_row_does_not_collide_with_its_own_claim():
    """A row already holding the right uuid is sound, not self-colliding."""
    row = _row(row_id=7, recorded_uuid="aaaa")
    proposal = _propose(row, [_t("aaaa")], claims={"aaaa": 7})
    assert proposal.outcome == OUTCOME_SOUND


# ---------------------------------------------------------------------------
# FILL and REPLACE are different acts with different bars.
# ---------------------------------------------------------------------------


def test_a_phantom_is_never_overwritten_on_timing_evidence_alone():
    """Overwriting a recorded uuid needs the pane's own argv.

    A missing transcript is not proof the recorded value is wrong - the
    file can have been deleted, archived, or not yet flushed - so the
    weaker signals may never destroy what the row currently claims.
    """
    row = _row(recorded_uuid="phantom")
    proposal = _propose(row, [_t("aaaa")])

    assert proposal.row_class == UUID_PHANTOM
    assert proposal.outcome == OUTCOME_AMBIGUOUS
    assert proposal.writable is False


def test_a_phantom_is_replaced_only_on_the_panes_own_argv():
    """With argv evidence and no collision, a replacement is proposed."""
    row = _row(recorded_uuid="phantom")
    proposal = _propose(row, [_t("aaaa")], argv_uuid="aaaa")

    assert proposal.outcome == OUTCOME_CONFIDENT
    assert proposal.kind == KIND_REPLACE
    assert proposal.proposed_uuid == "aaaa"


# ---------------------------------------------------------------------------
# Siblings: the cwd spelling trap's signature.
# ---------------------------------------------------------------------------


def test_sibling_rows_are_found_across_two_cwd_spellings(tmp_path):
    """One session split across two rows, one per spelling, is detected."""
    home = tmp_path / "home"
    real = home / "Library" / "Dev" / "Media"
    real.mkdir(parents=True)
    link = home / "Development"
    link.symlink_to(home / "Library" / "Dev")

    left = RowFacts(row_id=4, working_dir=str(real), recorded_uuid="real-uuid")
    right = RowFacts(row_id=7, working_dir=str(link / "Media"))

    assert sibling_uuid_map([left, right])[7] == {"real-uuid"}
    assert sibling_uuid_map([left, right])[4] == set()


def test_a_row_is_never_its_own_sibling():
    """Self-corroboration is not corroboration."""
    row = RowFacts(row_id=1, working_dir=WD, recorded_uuid="u")
    assert sibling_uuid_map([row])[1] == set()


def test_timing_alone_does_not_make_a_transcript_a_candidate():
    """A busy file in an unrelated project is not weak evidence, it is none.

    FAILS BEFORE THE ANCHOR GATE: a row whose project directory does not
    exist still "matched" every transcript whose write time happened to
    overlap its pane's lifetime, which turned the negative control into
    an ambiguous verdict instead of an honest refusal.
    """
    row = _row(working_dir="/Users/x/Elsewhere")
    proposal = _propose(row, [_t("aaaa")], existing_dirs=[DIR])

    assert proposal.outcome == OUTCOME_NO_CANDIDATE
    assert proposal.candidates == []


def test_a_title_match_alone_does_not_anchor_either():
    """Titles repeat across projects; a name is corroboration, not an anchor."""
    row = _row(working_dir="/Users/x/Elsewhere", title="Agent - Proj")
    proposal = _propose(
        row, [_t("aaaa", name="Agent - Proj")], existing_dirs=[DIR]
    )
    assert proposal.outcome == OUTCOME_NO_CANDIDATE


def test_a_single_weak_but_anchored_candidate_is_ambiguous_not_absent():
    """One transcript in the right directory, nothing else known.

    ISOLATES THE SPLIT. The transcript IS anchored to the row - it sits
    in the row's project directory - so "no transcript on disk could be
    this row" would be a lie. It just cannot be told apart from the
    neighbours it may have, so the honest answer is ambiguous.

    FAILS BEFORE THE FIX: every winner-less outcome was reported as
    no_candidate, which stated a false negative with full confidence.
    """
    anchored_only = _t(
        "aaaa", recorded_cwd=None, last_ts=None, mtime=None
    )
    proposal = _propose(_row(), [anchored_only])

    assert proposal.outcome == OUTCOME_AMBIGUOUS
    assert proposal.candidates == ["aaaa"]
    assert proposal.writable is False
