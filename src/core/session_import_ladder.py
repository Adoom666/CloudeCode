"""The evidence ladder: did WE create this tmux session, or did someone else?

Design doc ``docs/session-attribution-import.md``, Stage B. This module is
pure - it reads no disk, runs no tmux, opens no database. Everything it
needs arrives as a :class:`LadderEvidence`, which
``session_import_evidence`` gathers. That split is deliberate: the rules
below are the part that must be auditable, and a rule you can only
exercise by standing up a filesystem does not get exercised.

THREE OUTCOMES, NEVER A DEFAULT. Every live session resolves to OURS,
THEIRS or UNKNOWN. UNKNOWN is not a flavour of THEIRS - the whole defect
this migration corrects is a codebase that turned "we have no record"
into "it belongs to someone else" and badged the user's own work
EXTERNAL. UNKNOWN is further split by REASON, because "we looked and
found nothing" and "we could not look" are different facts:

    REASON_NO_EVIDENCE          every tier was evaluated and none hit
    REASON_COULD_NOT_EVALUATE   at least one tier could not be measured

Both go to the user, but only the second one names a broken measurement.

WHY THERE IS NO PATH TO THEIRS TODAY. Not an omission. Tiers 1, 3 and 4
prove OURS; no admissible tier proves THEIRS, because the only artifact
that ever asserted "external" is tier 2, and tier 2 is inadmissible (see
below). The constant exists so a future admissible THEIRS tier has a name
to return, and :func:`classify` reports the bucket so it can never be
silently folded into another. A test asserts that no current evidence
combination reaches it.

TIER 2 IS INADMISSIBLE AND THIS MODULE ENFORCES IT STRUCTURALLY.
A ``tmux_ext_<name>.pipe`` records the APP'S OWN VERDICT that a session
was external - and that verdict was produced by the bug under
investigation. Reading it as evidence of THEIRS would launder the defect
into the migration built to correct it, and would do so invisibly,
because the file looks like an independent measurement. On the install
this was measured against, the five ``ext_`` pipes map five-for-five onto
the five rows still sitting at ``origin='observed'`` and predate the
database by a day: the DB did not decide those were external, it
inherited a verdict the pre-DB code had already made for the same wrong
reason.

:class:`TierOutcome` REFUSES to be constructed with ``tier=2``. An edit
that "improves" the ladder by trusting ``ext_`` pipes raises instead of
shipping. The one admissible use of an ``ext_`` pipe is the BOTH-PIPES
case - a session with a created pipe AND an ext pipe was created by us
and later re-adopted - and there the proof is the created pipe (tier 3);
the ext pipe only sets :attr:`SessionVerdict.readopted` so the history
can be explained. Never tier 2 alone.

TIERS 5 AND 6 NEVER DECIDE. Name shape and working directory are display
hints that order and annotate the UNKNOWN list. They are produced by
:func:`collect_hints`, whose output is NOT an input to :func:`decide` -
so folding them into the verdict requires changing a signature that a
test pins. Writing tier 5 into ``origin`` would be exactly the invented
verdict this codebase keeps re-learning not to write.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import structlog

from src.core.session_import_ladder_types import (
    AUTO_NAME_RE,
    INADMISSIBLE_TIERS,
    LADDER_OURS,
    LADDER_THEIRS,
    LADDER_UNKNOWN,
    ORIGIN_MARKER_CREATED,
    REASON_COULD_NOT_EVALUATE,
    REASON_NO_EVIDENCE,
    SESSION_PREFIX,
    TIER_HIT,
    TIER_MISS,
    TIER_UNEVALUATED,
    LadderEvidence,
    LadderReport,
    LiveSession,
    SessionVerdict,
    TierOutcome,
)
from src.core.session_import_tiers import (
    _tier1_owned_set,
    _tier3_created_pipe,
    _tier4_origin_marker,
    created_pipe_slug_for,
)

__all__ = [
    "AUTO_NAME_RE",
    "INADMISSIBLE_TIERS",
    "LADDER_OURS",
    "LADDER_THEIRS",
    "LADDER_UNKNOWN",
    "ORIGIN_MARKER_CREATED",
    "REASON_COULD_NOT_EVALUATE",
    "REASON_NO_EVIDENCE",
    "SESSION_PREFIX",
    "TIER_HIT",
    "TIER_MISS",
    "TIER_UNEVALUATED",
    "LadderEvidence",
    "LadderReport",
    "LiveSession",
    "SessionVerdict",
    "TierOutcome",
    "classify",
    "collect_hints",
    "created_pipe_slug_for",
    "decide",
]

logger = structlog.get_logger()


#: Admissible tiers, in the order they are evaluated and in the order a
#: hit wins. Tier 2 is absent BY CONSTRUCTION, not by omission.
_ADMISSIBLE_TIERS = (_tier1_owned_set, _tier3_created_pipe, _tier4_origin_marker)


def collect_hints(
    session: LiveSession, evidence: LadderEvidence
) -> Tuple[str, ...]:
    """Tiers 5 and 6, as SENTENCES. Display only, and never a verdict input.

    Description: the prompt spells these out in words rather than folding
      them into a score, so the user can weigh what we actually saw. This
      function's output is deliberately not a parameter of :func:`decide`.
    Inputs: session (LiveSession). evidence (LadderEvidence).
    Output: tuple[str, ...] - possibly empty.
    Example: collect_hints(LiveSession('cloude_ses_deadbeef'), ev)
    """
    hints: List[str] = []
    if AUTO_NAME_RE.match(session.tmux_name):
        hints.append(
            "its name matches the auto-generated form Cloude Code uses "
            "when you do not type one"
        )
    wd = session.working_dir
    if wd:
        for root in evidence.project_roots:
            if wd == root or wd.startswith(root.rstrip("/") + "/"):
                hints.append(f"it is running in your project folder {root}")
                break
    return tuple(hints)


def decide(session: LiveSession, evidence: LadderEvidence) -> SessionVerdict:
    """Run the admissible ladder for one session. OURS, THEIRS or UNKNOWN.

    Description: the whole decision, and the only place a verdict is
      minted. It takes exactly two parameters and one of them is not
      hints - see the module docstring for why that signature is load
      bearing and pinned by a test.

      A HIT ANYWHERE WINS, even when a higher tier could not be measured:
      proof is proof, and an unmeasured tier cannot contradict it. With
      no hit, an unevaluated tier means REASON_COULD_NOT_EVALUATE and
      only a fully-evaluated all-miss means REASON_NO_EVIDENCE.
    Inputs: session (LiveSession). evidence (LadderEvidence).
    Output: SessionVerdict.
    Example: decide(LiveSession('cloude_a'), LadderEvidence()).verdict
    """
    outcomes: List[TierOutcome] = [fn(session, evidence) for fn in _ADMISSIBLE_TIERS]
    hints = collect_hints(session, evidence)
    try:
        readopted = session.tmux_name in evidence.ext_pipe_names
    except (OSError, ValueError, TypeError):
        readopted = False

    hit = next((o for o in outcomes if o.result == TIER_HIT), None)
    unevaluated = tuple(o.name for o in outcomes if o.result == TIER_UNEVALUATED)

    if hit is not None:
        # The ext_ pipe is admissible for EXPLANATION only, and only
        # alongside a created pipe. It never reaches the verdict.
        readopted = readopted and hit.name == "created_pipe"
        return SessionVerdict(
            tmux_name=session.tmux_name,
            epoch=session.epoch,
            verdict=LADDER_OURS,
            reason=hit.name,
            tiers=tuple(outcomes),
            hints=hints,
            unevaluated=unevaluated,
            readopted=readopted,
        )

    return SessionVerdict(
        tmux_name=session.tmux_name,
        epoch=session.epoch,
        verdict=LADDER_UNKNOWN,
        reason=(
            REASON_COULD_NOT_EVALUATE if unevaluated else REASON_NO_EVIDENCE
        ),
        tiers=tuple(outcomes),
        hints=hints,
        unevaluated=unevaluated,
        readopted=False,
    )


def classify(
    sessions: Sequence[LiveSession], evidence: LadderEvidence
) -> LadderReport:
    """Run the ladder over every live session and bucket the results.

    Inputs: sessions (Sequence[LiveSession]). evidence (LadderEvidence).
    Output: LadderReport.
    Example: classify([LiveSession('cloude_a')], LadderEvidence()).unknown
    """
    verdicts = tuple(decide(s, evidence) for s in sessions)
    report = LadderReport(verdicts=verdicts)
    logger.info(
        "session_import_ladder_classified",
        total=len(verdicts),
        ours=len(report.ours),
        theirs=len(report.theirs),
        unknown_no_evidence=len(report.unknown_no_evidence),
        unknown_could_not_evaluate=len(report.unknown_unevaluated),
    )
    return report
