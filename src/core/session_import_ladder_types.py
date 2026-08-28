"""Value types and constants for the session-attribution evidence ladder.

Split out of :mod:`src.core.session_import_ladder` for this project's
500-line ceiling. The one piece of BEHAVIOUR that lives here rather than
with the rules is :meth:`TierOutcome.__post_init__`, and it lives here
deliberately: refusing to construct an inadmissible tier is a property of
the value, so no caller anywhere can route around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple


#: Verdicts. Exactly three, and the third is not a flavour of the others.
LADDER_OURS = "ours"
LADDER_THEIRS = "theirs"
LADDER_UNKNOWN = "unknown"

#: Why an UNKNOWN is unknown. The distinction is the point of the split.
REASON_NO_EVIDENCE = "no_admissible_evidence"
REASON_COULD_NOT_EVALUATE = "could_not_evaluate"

#: One tier's result. UNEVALUATED is the third outcome at tier level and
#: propagates upward: a session with any unevaluated tier can never be
#: reported as "we looked and found nothing".
TIER_HIT = "hit"
TIER_MISS = "miss"
TIER_UNEVALUATED = "unevaluated"

#: Tiers that may NEVER appear in a verdict. See the module docstring.
INADMISSIBLE_TIERS: Tuple[int, ...] = (2,)

#: The tmux namespace prefix every session on our socket carries. Kept
#: here rather than imported from tmux_backend so this module stays free
#: of that import graph; a test pins the two together.
SESSION_PREFIX = "cloude_"

#: The app's auto-generated session-name form, ``cloude_ses_<8 hex>``.
#: TIER 5 ONLY - a hint, never a verdict. A user can type this.
AUTO_NAME_RE = re.compile(r"^cloude_ses_[0-9a-f]{8}$")

#: The marker value tier 4 accepts. Anything else is not our stamp.
ORIGIN_MARKER_CREATED = "created"


@dataclass(frozen=True)
class LiveSession:
    """One live tmux session on our socket, as the ladder sees it.

    Inputs (constructor): tmux_name (str). epoch (int | None) -
      ``#{session_created}``; None means the instance cannot be dated,
      which makes tier 4's epoch gate unevaluable rather than passable.
      working_dir (str | None) - tier 6 hint input only.
    Output: a LiveSession instance.
    """

    tmux_name: str
    epoch: Optional[int] = None
    working_dir: Optional[str] = None


@dataclass(frozen=True)
class TierOutcome:
    """What one tier measured, for one session.

    Description: refuses construction for an inadmissible tier. That is
      the structural half of the tier-2 rule - the prose half is in the
      module docstring, and prose does not fail a build.
    Inputs (constructor): tier (int). name (str) - stable token used in
      reasons and in the unevaluated list. result (str) - TIER_HIT,
      TIER_MISS or TIER_UNEVALUATED. detail (str | None) - what was
      measured, or what could not be.
    Output: a TierOutcome instance.
    Raises: ValueError - tier is in INADMISSIBLE_TIERS.
    Example: TierOutcome(tier=1, name='owned_set', result=TIER_HIT)
    """

    tier: int
    name: str
    result: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.tier in INADMISSIBLE_TIERS:
            raise ValueError(
                f"tier {self.tier} is INADMISSIBLE and may never appear in a "
                "verdict: it records the app's own verdict, produced by the "
                "bug this import exists to correct. See the module docstring."
            )


@dataclass(frozen=True)
class LadderEvidence:
    """Everything the ladder is allowed to reason from, already gathered.

    Description: every "could not read it" is a None or an explicit
      failure set, NEVER an empty collection - an empty owned set and an
      unreadable one are the exact pair whose collapse causes today's
      bug.
    Inputs (constructor): owned_tmux_names (frozenset | None) - tier 1;
      None means the owned-set file could not be read. created_pipe_slugs
      (frozenset | None) - tier 3; slugs parsed out of
      ``tmux_<slug>.pipe``; None means the log directory could not be
      read. ext_pipe_names (frozenset) - tier 2, recorded for history and
      admissible for nothing. origin_markers (Mapping[str, str]) - tier 4;
      tmux name -> ``CLOUDECODE_ORIGIN`` value actually read.
      origin_probe_failures (frozenset) - names whose env probe could not
      be run. stage_a_boundary_epoch (int | None) - the unix epoch at or
      after which a tier-4 marker is admissible on this install; None
      means CANNOT DETERMINE and makes tier 4 inadmissible.
      project_roots (tuple[str, ...]) - tier 6 hint input only.
    Output: a LadderEvidence instance.
    """

    owned_tmux_names: Optional[frozenset] = None
    created_pipe_slugs: Optional[frozenset] = None
    ext_pipe_names: frozenset = frozenset()
    origin_markers: Mapping[str, str] = field(default_factory=dict)
    origin_probe_failures: frozenset = frozenset()
    stage_a_boundary_epoch: Optional[int] = None
    project_roots: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionVerdict:
    """The ladder's answer for one session, with its whole working shown.

    Inputs (constructor): tmux_name (str). epoch (int | None). verdict
      (str) - LADDER_OURS, LADDER_THEIRS or LADDER_UNKNOWN. reason (str) -
      the winning tier's name on OURS, otherwise REASON_NO_EVIDENCE or
      REASON_COULD_NOT_EVALUATE. tiers (tuple[TierOutcome, ...]) - every
      tier evaluated, in order. hints (tuple[str, ...]) - tier 5/6
      sentences, display only. unevaluated (tuple[str, ...]) - names of
      the tiers that could not be measured. readopted (bool) - a created
      pipe AND an ext pipe both exist, so this was ours and later
      re-adopted.
    Output: a SessionVerdict instance.
    """

    tmux_name: str
    epoch: Optional[int]
    verdict: str
    reason: str
    tiers: Tuple[TierOutcome, ...] = ()
    hints: Tuple[str, ...] = ()
    unevaluated: Tuple[str, ...] = ()
    readopted: bool = False

    @property
    def could_not_evaluate(self) -> bool:
        """True when at least one tier could not be measured."""
        return self.reason == REASON_COULD_NOT_EVALUATE

    def as_unattributed_record(self) -> Dict[str, Any]:
        """The ``session_import_unattributed`` record for this session.

        Inputs: none.
        Output: dict with exactly tmux_name, epoch, hints, reason - the
          shape the design doc names, and the shape the prompt renders.
        """
        return {
            "tmux_name": self.tmux_name,
            "epoch": self.epoch,
            "hints": list(self.hints),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LadderReport:
    """Every verdict from one run, bucketed so no bucket can absorb another.

    Inputs (constructor): verdicts (tuple[SessionVerdict, ...]).
    Output: a LadderReport instance.
    """

    verdicts: Tuple[SessionVerdict, ...] = ()

    @property
    def ours(self) -> List[SessionVerdict]:
        """Sessions tiers 1-4 proved we created."""
        return [v for v in self.verdicts if v.verdict == LADDER_OURS]

    @property
    def theirs(self) -> List[SessionVerdict]:
        """Sessions proved to belong to someone else.

        Description: empty under every current tier, by construction, and
          reported as its own bucket so it can never be silently merged
          into UNKNOWN if an admissible THEIRS tier is ever added.
        """
        return [v for v in self.verdicts if v.verdict == LADDER_THEIRS]

    @property
    def unknown(self) -> List[SessionVerdict]:
        """Everything the ladder could not resolve, both reasons."""
        return [v for v in self.verdicts if v.verdict == LADDER_UNKNOWN]

    @property
    def unknown_no_evidence(self) -> List[SessionVerdict]:
        """We looked at every tier and none of them hit."""
        return [v for v in self.unknown if v.reason == REASON_NO_EVIDENCE]

    @property
    def unknown_unevaluated(self) -> List[SessionVerdict]:
        """We could not look. NEVER folded into the bucket above."""
        return [v for v in self.unknown if v.could_not_evaluate]

    def unattributed_records(self) -> List[Dict[str, Any]]:
        """Records for ``meta.session_import_unattributed``, in order."""
        return [v.as_unattributed_record() for v in self.unknown]
