"""The individual tier evaluators of the session-attribution ladder.

Split out of :mod:`src.core.session_import_ladder` so neither file grows
past this project's 500-line ceiling, and so the RULE that combines the
tiers stays readable next to the rule about what each one may claim.

Every function here returns a :class:`TierOutcome` with THREE possible
results and never raises for a data problem: ``hit`` (this tier proves
the session is ours), ``miss`` (this tier was evaluated and found
nothing) and ``unevaluated`` (this tier could not be measured at all).
The third is not a flavour of the second. A tier that could not be
measured propagates upward and stops the ladder reporting "we looked and
found nothing" about a measurement that never happened.

THERE IS NO TIER 2 IN THIS FILE, AND THAT IS THE POINT. See the ladder
module's docstring: an ``ext_`` pipe records the app's own verdict, which
the bug produced, and :class:`TierOutcome` refuses to be constructed with
``tier=2`` at all.
"""

from __future__ import annotations

from typing import Optional

from src.core.session_import_ladder_types import (
    ORIGIN_MARKER_CREATED,
    SESSION_PREFIX,
    TIER_HIT,
    TIER_MISS,
    TIER_UNEVALUATED,
    LadderEvidence,
    LiveSession,
    TierOutcome,
)


def _tier1_owned_set(
    session: LiveSession, evidence: LadderEvidence
) -> TierOutcome:
    """Tier 1: the persisted owned set names this session.

    Description: our file, in our directory, written by us - not forgeable
      by anything outside the app. An UNREADABLE owned set is UNEVALUATED
      and not a miss, because today's defect is precisely that collapse.
    Inputs: session (LiveSession). evidence (LadderEvidence).
    Output: TierOutcome.
    """
    if evidence.owned_tmux_names is None:
        return TierOutcome(
            tier=1,
            name="owned_set",
            result=TIER_UNEVALUATED,
            detail="the owned-set file could not be read",
        )
    try:
        hit = session.tmux_name in evidence.owned_tmux_names
    except (OSError, ValueError, TypeError) as exc:
        return TierOutcome(
            tier=1,
            name="owned_set",
            result=TIER_UNEVALUATED,
            detail=f"the owned set could not be searched: {exc}",
        )
    return TierOutcome(
        tier=1,
        name="owned_set",
        result=TIER_HIT if hit else TIER_MISS,
        detail="named in the persisted owned set" if hit else "not in the owned set",
    )


def created_pipe_slug_for(tmux_name: str) -> Optional[str]:
    """Map a live tmux name back to the created-pipe slug, or None.

    Description: the created pipe is ``tmux_<slug>.pipe`` where slug is
      the INTERNAL session id, and the tmux name is ``cloude_<slug>``
      ONLY when the app auto-named it. A user-typed name overrides the
      derivation, so no slug can be recovered from it - this returns None
      and the tier misses. That asymmetry is documented in the design doc
      and is the reason some sessions are unrecoverable rather than a gap
      to be papered over with a guess.
    Inputs: tmux_name (str).
    Output: str | None.
    Example: created_pipe_slug_for('cloude_ses_1a2b3c4d')  # 'ses_1a2b3c4d'
    """
    if not tmux_name.startswith(SESSION_PREFIX):
        return None
    slug = tmux_name[len(SESSION_PREFIX):]
    return slug or None


def _tier3_created_pipe(
    session: LiveSession, evidence: LadderEvidence
) -> TierOutcome:
    """Tier 3: a ``tmux_<slug>.pipe`` exists whose slug is this session.

    Description: the app writes that filename ONLY when it created the
      backend, so its presence is a statement about authorship it made
      about itself at creation time - unlike tier 2, which is a statement
      it made about a session it had already misclassified.
    Inputs: session (LiveSession). evidence (LadderEvidence).
    Output: TierOutcome.
    """
    if evidence.created_pipe_slugs is None:
        return TierOutcome(
            tier=3,
            name="created_pipe",
            result=TIER_UNEVALUATED,
            detail="the log directory could not be read",
        )
    slug = created_pipe_slug_for(session.tmux_name)
    if slug is None:
        return TierOutcome(
            tier=3,
            name="created_pipe",
            result=TIER_MISS,
            detail="the name carries no recoverable slug (user-typed name)",
        )
    try:
        hit = slug in evidence.created_pipe_slugs
    except (OSError, ValueError, TypeError) as exc:
        return TierOutcome(
            tier=3,
            name="created_pipe",
            result=TIER_UNEVALUATED,
            detail=f"the pipe set could not be searched: {exc}",
        )
    return TierOutcome(
        tier=3,
        name="created_pipe",
        result=TIER_HIT if hit else TIER_MISS,
        detail=(
            f"tmux_{slug}.pipe exists, which only the create path writes"
            if hit
            else f"no tmux_{slug}.pipe"
        ),
    )


def _tier4_origin_marker(
    session: LiveSession, evidence: LadderEvidence
) -> TierOutcome:
    """Tier 4: the tmux env marker ``CLOUDECODE_ORIGIN``, EPOCH GATED.

    Description: the marker only proves anything on a session created
      AFTER Stage A shipped on this install. On anything older it is
      evidence of nothing and is IGNORED - not trusted, and not quietly
      treated as absent either: the miss says why. When the install's
      Stage-A boundary cannot be determined the tier is UNEVALUATED
      rather than assumed valid.

      A session with NO marker misses regardless of the boundary. There
      is nothing to date, so an unknown boundary is not a failed
      measurement - collapsing that into UNEVALUATED would put every
      session on every pre-Stage-A install into could-not-evaluate and
      drown the real signal.
    Inputs: session (LiveSession). evidence (LadderEvidence).
    Output: TierOutcome.
    """
    name = "origin_marker"
    if session.tmux_name in evidence.origin_probe_failures:
        return TierOutcome(
            tier=4,
            name=name,
            result=TIER_UNEVALUATED,
            detail="the tmux environment could not be read for this session",
        )
    marker = evidence.origin_markers.get(session.tmux_name)
    if marker != ORIGIN_MARKER_CREATED:
        return TierOutcome(
            tier=4,
            name=name,
            result=TIER_MISS,
            detail=(
                "no CLOUDECODE_ORIGIN=created in the session environment"
                if marker is None
                else f"CLOUDECODE_ORIGIN is {marker!r}, not 'created'"
            ),
        )
    if evidence.stage_a_boundary_epoch is None:
        return TierOutcome(
            tier=4,
            name=name,
            result=TIER_UNEVALUATED,
            detail=(
                "a CLOUDECODE_ORIGIN marker is present but this install's "
                "Stage-A boundary is unknown, so the marker cannot be dated "
                "and is INADMISSIBLE rather than assumed valid"
            ),
        )
    if session.epoch is None:
        return TierOutcome(
            tier=4,
            name=name,
            result=TIER_UNEVALUATED,
            detail=(
                "a marker is present but the session has no creation epoch, "
                "so it cannot be placed relative to the Stage-A boundary"
            ),
        )
    if int(session.epoch) < int(evidence.stage_a_boundary_epoch):
        return TierOutcome(
            tier=4,
            name=name,
            result=TIER_MISS,
            detail=(
                f"a marker is present but the session epoch {session.epoch} "
                f"predates this install's Stage-A boundary "
                f"{evidence.stage_a_boundary_epoch}, so it is IGNORED"
            ),
        )
    return TierOutcome(
        tier=4,
        name=name,
        result=TIER_HIT,
        detail="CLOUDECODE_ORIGIN=created, stamped after this install's Stage-A boundary",
    )
