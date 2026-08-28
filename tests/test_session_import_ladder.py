"""The evidence ladder that decides OURS / THEIRS / UNKNOWN for one session.

Every test here exists because getting the corresponding rule wrong
reintroduces the defect the ladder was built to correct. The two most
important are the tier-2 and tier-5/6 tests: both guard against a future
"improvement" that makes the ladder decide more often by trusting
something that is not evidence.
"""

from __future__ import annotations

import pytest

from src.core.session_import_ladder import (
    INADMISSIBLE_TIERS,
    LADDER_OURS,
    LADDER_THEIRS,
    LADDER_UNKNOWN,
    REASON_COULD_NOT_EVALUATE,
    REASON_NO_EVIDENCE,
    TIER_UNEVALUATED,
    LadderEvidence,
    LiveSession,
    TierOutcome,
    classify,
    collect_hints,
    decide,
)

SOCK_NAME = "cloude_ses_1a2b3c4d"


def _evidence(**kw):
    """Build a LadderEvidence with every field explicitly defaulted.

    Inputs: kw - overrides for any LadderEvidence field.
    Output: LadderEvidence - defaults are "everything readable, nothing found".
    """
    base = dict(
        owned_tmux_names=frozenset(),
        created_pipe_slugs=frozenset(),
        ext_pipe_names=frozenset(),
        origin_markers={},
        origin_probe_failures=frozenset(),
        stage_a_boundary_epoch=None,
        project_roots=(),
    )
    base.update(kw)
    return LadderEvidence(**base)


def _session(name=SOCK_NAME, epoch=1000, working_dir=None):
    """One live tmux session under test."""
    return LiveSession(tmux_name=name, epoch=epoch, working_dir=working_dir)


# ---- tier 1 ---------------------------------------------------------------

def test_tier1_owned_set_hit_is_ours():
    v = decide(_session(), _evidence(owned_tmux_names=frozenset({SOCK_NAME})))
    assert v.verdict == LADDER_OURS
    assert v.reason == "owned_set"


def test_tier1_unreadable_owned_set_is_could_not_evaluate_not_no_evidence():
    """An unreadable owned set is NOT the same as an empty one.

    Today's bug is exactly this collapse: _load_session_metadata logs and
    carries on with an empty set, so "we could not read it" and "you own
    nothing" produce identical behaviour.
    """
    v = decide(_session(), _evidence(owned_tmux_names=None))
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_COULD_NOT_EVALUATE
    assert "owned_set" in v.unevaluated


# ---- tier 2 - INADMISSIBLE ------------------------------------------------

def test_tier2_ext_pipe_alone_never_produces_theirs():
    """THE REGRESSION GUARD. An ext_ pipe is the app's own verdict, and
    that verdict was produced by the bug. It may never decide anything."""
    ev = _evidence(ext_pipe_names=frozenset({SOCK_NAME}))
    v = decide(_session(), ev)
    assert v.verdict != LADDER_THEIRS
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_NO_EVIDENCE
    assert all(t.tier != 2 for t in v.tiers)


def test_tier2_cannot_be_expressed_as_a_tier_outcome_at_all():
    """A future edit that adds tier 2 to the ladder fails HERE, loudly."""
    assert 2 in INADMISSIBLE_TIERS
    with pytest.raises(ValueError):
        TierOutcome(tier=2, name="ext_pipe", result="hit")


def test_both_pipes_is_ours_on_tier3_and_records_the_readopt_history():
    """The one admissible use of an ext_ pipe: it explains history only.

    The proof is the CREATED pipe (tier 3). The ext_ pipe adds the reason
    text and nothing else.
    """
    ev = _evidence(
        created_pipe_slugs=frozenset({"ses_1a2b3c4d"}),
        ext_pipe_names=frozenset({SOCK_NAME}),
    )
    v = decide(_session(), ev)
    assert v.verdict == LADDER_OURS
    assert v.reason == "created_pipe"
    assert v.readopted is True


# ---- tier 3 ---------------------------------------------------------------

def test_tier3_created_pipe_maps_to_the_auto_named_session():
    ev = _evidence(created_pipe_slugs=frozenset({"ses_1a2b3c4d"}))
    v = decide(_session(), ev)
    assert v.verdict == LADDER_OURS
    assert v.reason == "created_pipe"
    assert v.readopted is False


def test_tier3_cannot_map_a_user_typed_name_and_says_so():
    """The documented asymmetry: the created pipe is keyed on the internal
    session id, so a user-typed tmux name has nothing to map back to."""
    ev = _evidence(created_pipe_slugs=frozenset({"ses_1a2b3c4d"}))
    v = decide(_session(name="cloude_scrolltest"), ev)
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_NO_EVIDENCE


def test_tier3_unreadable_log_directory_is_could_not_evaluate():
    v = decide(_session(), _evidence(created_pipe_slugs=None))
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_COULD_NOT_EVALUATE
    assert "created_pipe" in v.unevaluated


# ---- tier 4 - epoch gated -------------------------------------------------

def test_tier4_marker_after_the_stage_a_boundary_is_ours():
    ev = _evidence(
        origin_markers={SOCK_NAME: "created"}, stage_a_boundary_epoch=500
    )
    v = decide(_session(epoch=900), ev)
    assert v.verdict == LADDER_OURS
    assert v.reason == "origin_marker"


def test_tier4_marker_on_a_pre_stage_a_epoch_is_IGNORED_not_trusted():
    """THE REGRESSION GUARD. A marker on a session that predates the
    Stage-A upgrade is evidence of nothing."""
    ev = _evidence(
        origin_markers={SOCK_NAME: "created"}, stage_a_boundary_epoch=2000
    )
    v = decide(_session(epoch=900), ev)
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_NO_EVIDENCE
    marker = [t for t in v.tiers if t.name == "origin_marker"][0]
    assert marker.result != "hit"
    assert "predates" in (marker.detail or "")


def test_tier4_is_inadmissible_when_the_boundary_cannot_be_determined():
    """Not assumed valid. A marker we cannot date is a could-not-evaluate."""
    ev = _evidence(
        origin_markers={SOCK_NAME: "created"}, stage_a_boundary_epoch=None
    )
    v = decide(_session(epoch=900), ev)
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_COULD_NOT_EVALUATE
    assert "origin_marker" in v.unevaluated


def test_no_marker_and_no_boundary_is_plain_no_evidence():
    """An absent boundary must not turn every session on a pre-Stage-A
    install into could-not-evaluate. With no marker there is nothing to
    date, so the tier is a genuine MISS."""
    v = decide(_session(), _evidence(stage_a_boundary_epoch=None))
    assert v.reason == REASON_NO_EVIDENCE


def test_tier4_probe_failure_is_could_not_evaluate():
    ev = _evidence(origin_probe_failures=frozenset({SOCK_NAME}))
    v = decide(_session(), ev)
    assert v.reason == REASON_COULD_NOT_EVALUATE
    assert "origin_marker" in v.unevaluated


# ---- tiers 5 and 6 - hints only -------------------------------------------

def test_tier5_name_shape_alone_never_decides():
    """THE REGRESSION GUARD. cloude_ses_<hex> only the app generates - and
    a user can type it. It orders the UNKNOWN list; it never writes origin."""
    v = decide(_session(name="cloude_ses_deadbeef"), _evidence())
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_NO_EVIDENCE


def test_tier6_cwd_in_a_project_root_alone_never_decides():
    ev = _evidence(project_roots=("/Users/x/Development",))
    v = decide(_session(working_dir="/Users/x/Development/thing"), ev)
    assert v.verdict == LADDER_UNKNOWN
    assert v.reason == REASON_NO_EVIDENCE


def test_decide_cannot_even_see_the_hints():
    """Structural: hints are produced by a DIFFERENT function whose output
    is not an input to decide(). A future edit that folds them in has to
    change this signature, and this test."""
    import inspect

    params = set(inspect.signature(decide).parameters)
    assert params == {"session", "evidence"}
    assert "hints" not in params


def test_hints_are_words_not_a_score():
    hints = collect_hints(
        _session(name="cloude_ses_deadbeef", working_dir="/r/x"),
        _evidence(project_roots=("/r",)),
    )
    assert any("auto-generated" in h for h in hints)
    assert any("project" in h for h in hints)
    assert all(isinstance(h, str) for h in hints)


# ---- classify: the whole list --------------------------------------------

def test_classify_unknown_list_is_exactly_what_the_ladder_could_not_resolve():
    ev = _evidence(created_pipe_slugs=frozenset({"ses_1a2b3c4d"}))
    report = classify(
        [_session(), _session(name="cloude_typed", epoch=2)], ev
    )
    assert [v.tmux_name for v in report.ours] == [SOCK_NAME]
    assert [v.tmux_name for v in report.unknown] == ["cloude_typed"]
    assert report.theirs == []


def test_classify_separates_no_evidence_from_could_not_evaluate():
    ev = _evidence(owned_tmux_names=None)
    report = classify([_session()], ev)
    assert report.unknown_no_evidence == []
    assert [v.tmux_name for v in report.unknown_unevaluated] == [SOCK_NAME]


def test_an_error_resolving_one_session_does_not_land_it_in_no_evidence():
    """A catch-all UNKNOWN would absorb the error silently. It must be
    reported as could-not-evaluate, naming what could not be measured."""

    class Exploding(frozenset):
        def __contains__(self, item):
            raise OSError("boom")

    ev = _evidence(owned_tmux_names=Exploding())
    report = classify([_session()], ev)
    assert report.unknown_no_evidence == []
    v = report.unknown_unevaluated[0]
    assert v.reason == REASON_COULD_NOT_EVALUATE
    assert "owned_set" in v.unevaluated
    bad = [t for t in v.tiers if t.name == "owned_set"][0]
    assert bad.result == TIER_UNEVALUATED
    assert "boom" in (bad.detail or "")


def test_unattributed_records_carry_name_epoch_hints_and_reason():
    report = classify([_session(name="cloude_ses_deadbeef", epoch=77)], _evidence())
    rec = report.unattributed_records()[0]
    assert set(rec) == {"tmux_name", "epoch", "hints", "reason"}
    assert rec["tmux_name"] == "cloude_ses_deadbeef"
    assert rec["epoch"] == 77
    assert rec["reason"] == REASON_NO_EVIDENCE
    assert rec["hints"]
