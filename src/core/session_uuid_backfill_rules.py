"""The decision rules that turn evidence into a proposal, or into a refusal.

Separated from :mod:`src.core.session_uuid_backfill` so the data model
stays readable and the rules stay reviewable on their own. Nothing here
touches a filesystem, a process table or a database: every fact arrives
as an argument, which is what lets a test drive the exact situation that
matters - including the one where the right answer is "I do not know".

THE SHAPE OF EVERY DECISION. Gates first, then signals, then a single
verdict. A gate can only REMOVE a candidate and never promote one; a
signal can only strengthen a candidate that already passed every gate.
That ordering is the whole safety property: no amount of tidy-looking
corroboration can resurrect a candidate whose uuid is already claimed or
whose file does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from src.core.claude_project_dirs import (
    candidate_project_dirs,
    project_dir_names,
    same_directory,
)
from src.core.session_uuid_backfill import (
    ANCHOR_FAMILIES,
    CORROBORATING_SIGNALS,
    FILL_REQUIRES_SIGNALS,
    KIND_FILL,
    KIND_REPLACE,
    OUTCOME_AMBIGUOUS,
    OUTCOME_COLLIDES,
    OUTCOME_CONFIDENT,
    OUTCOME_NO_CANDIDATE,
    OUTCOME_SOUND,
    REPLACE_REQUIRES,
    SIGNAL_ACTIVE_IN_WINDOW,
    SIGNAL_PANE_ARGV,
    SIGNAL_PROJECT_DIR,
    SIGNAL_RECORDED_CWD,
    SIGNAL_SIBLING_ROW,
    SIGNAL_TITLE,
    UUID_ABSENT,
    UUID_PHANTOM,
    UUID_PRESENT,
    Proposal,
    RowFacts,
    TranscriptFacts,
    classify_row,
    signal_families,
)

#: Seconds of tolerance either side of a pane's lifetime when asking
#: whether a transcript was written DURING it. Absorbs clock skew between
#: tmux's ``#{session_created}`` and the timestamps Claude stamps, plus
#: the gap between a pane existing and the user typing into it. Kept
#: generous on purpose: this signal is never sufficient alone, so a loose
#: bound costs recall in the ambiguous bucket and cannot cause a wrong
#: attach.
WINDOW_SLACK_SECONDS = 300


def _normalise_title(value: Optional[str]) -> str:
    """Reduce a title to a comparable form.

    Description: lowercases and strips, and drops a trailing
      parenthesised qualifier, because the owner's titles differ only by
      one ("Agent - BHPP" vs "Agent - BHPP (GPO)") and treating those as
      equal would make the weakest signal also the most promiscuous.
      Deliberately does NOT do fuzzy matching - a near miss here is a
      reason to abstain, not to try harder.
    Inputs: value (str | None).
    Output: str - normalised, or '' when there was nothing to compare.
    Example: _normalise_title('Agent - BHPP (GPO)')  # 'agent - bhpp (gpo)'
    """
    return (value or "").strip().lower()


def _in_window(
    transcript: TranscriptFacts, row: RowFacts, *, now: float
) -> bool:
    """Was this transcript written to while the row's pane was alive?

    Description: uses LAST-write evidence, not first-message evidence.
      A resumed conversation's first message predates its pane by months
      - that assumption is what made the pre-existing timing rule return
      nothing for every session on this machine - so the question that
      actually discriminates is whether the file grew during the pane's
      lifetime.
    Inputs: transcript (TranscriptFacts). row (RowFacts). now (float) -
      epoch used as the window end for a pane still running.
    Output: bool - False whenever it cannot be evaluated, since an
      unanswerable question is not corroboration.
    Example: _in_window(t, r, now=1788700000.0)  # True
    """
    if row.tmux_created_epoch is None:
        return False
    end = row.pane_window_end if row.pane_window_end is not None else now
    latest = transcript.last_ts if transcript.last_ts is not None else transcript.mtime
    if latest is None:
        return False
    return (
        row.tmux_created_epoch - WINDOW_SLACK_SECONDS
        <= latest
        <= end + WINDOW_SLACK_SECONDS
    )


def gather_signals(
    transcript: TranscriptFacts,
    row: RowFacts,
    *,
    argv_uuid: Optional[str],
    sibling_uuids: Set[str],
    project_dirs: Sequence[str],
    now: float,
) -> List[str]:
    """Which independent signals link this transcript to this row.

    Description: each signal is a separate question answered against a
      separate fact, which is what makes counting them meaningful. The
      returned list is ordered by :data:`CORROBORATING_SIGNALS` so two
      runs produce identical evidence strings.
    Inputs: transcript (TranscriptFacts). row (RowFacts). argv_uuid
      (str | None) - the uuid read from the pane's own process tree.
      sibling_uuids (Set[str]) - uuids held by rows sharing this row's
      tmux name or directory. project_dirs (Sequence[str]) - bare names
      of every project directory reachable from the row's working dir.
      now (float) - epoch for an open-ended pane window.
    Output: list[str] - zero or more of the ``SIGNAL_*`` constants.
    Example: gather_signals(t, r, argv_uuid='u', sibling_uuids=set(),
        project_dirs=[], now=0.0)  # ['pane_argv']
    """
    found: Set[str] = set()
    if argv_uuid and transcript.uuid == argv_uuid:
        found.add(SIGNAL_PANE_ARGV)
    if transcript.uuid in sibling_uuids:
        found.add(SIGNAL_SIBLING_ROW)
    if transcript.recorded_cwd and same_directory(
        transcript.recorded_cwd, row.working_dir
    ):
        found.add(SIGNAL_RECORDED_CWD)
    if transcript.project_dir and transcript.project_dir in set(project_dirs):
        found.add(SIGNAL_PROJECT_DIR)
    if _in_window(transcript, row, now=now):
        found.add(SIGNAL_ACTIVE_IN_WINDOW)
    row_title = _normalise_title(row.title)
    if row_title and row_title == _normalise_title(transcript.name):
        found.add(SIGNAL_TITLE)
    return [s for s in CORROBORATING_SIGNALS if s in found]


def propose_for_row(
    row: RowFacts,
    transcripts: Sequence[TranscriptFacts],
    *,
    claims: Dict[str, int],
    argv_uuid: Optional[str] = None,
    sibling_uuids: Optional[Set[str]] = None,
    now: float,
    projects_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    existing_dirs: Optional[Sequence[str]] = None,
) -> Proposal:
    """Propose the conversation behind one row, or refuse to.

    Description: the whole matcher for one row. Runs the gates, gathers
      signals, and returns exactly one verdict with its evidence. NEVER
      returns a uuid it is not entitled to write: see
      :data:`FILL_REQUIRES_SIGNALS` and :data:`REPLACE_REQUIRES` for the
      two bars, and :data:`OUTCOME_COLLIDES` for what happens when the
      right answer is already spoken for.
    Inputs: row (RowFacts). transcripts (Sequence[TranscriptFacts]) -
      every top-level transcript on disk. claims (Dict[str, int]) - uuid
      to the row id currently holding it. argv_uuid (str | None) - read
      from the pane's process tree, None when unavailable. sibling_uuids
      (Set[str] | None). now (float). projects_dir (Path | None) and home
      (Path | None) - test overrides forwarded to the directory
      resolution.
    Output: Proposal - always; never None, never an exception.
    Example: propose_for_row(row, [], claims={}, now=0.0).outcome
      # 'no_candidate'
    """
    on_disk = [t.uuid for t in transcripts]
    row_class = classify_row(row, on_disk)
    siblings = sibling_uuids or set()

    if row_class == UUID_PRESENT:
        return Proposal(
            row_id=row.row_id,
            row_class=row_class,
            outcome=OUTCOME_SOUND,
            detail="row already carries a uuid whose transcript exists",
        )

    dirs = project_dir_names(
        candidate_project_dirs(
            row.working_dir,
            projects_dir=projects_dir,
            home=home,
            existing=existing_dirs,
        )
    )

    # GATES. A candidate must exist on disk (guaranteed by iterating
    # transcripts), must not be one of Claude Code's own liveness probes,
    # and must not be claimed by a DIFFERENT row. The last is not a
    # preference: claude_session_uuid is UNIQUE, so a collision proves
    # the proposal wrong for one of the two rows and this module is not
    # entitled to decide which.
    scored: List[tuple] = []
    collided: List[tuple] = []
    for transcript in transcripts:
        if transcript.is_probe:
            continue
        signals = gather_signals(
            transcript,
            row,
            argv_uuid=argv_uuid,
            sibling_uuids=siblings,
            project_dirs=dirs,
            now=now,
        )
        # ANCHOR GATE. A transcript that matches only on timing or only
        # on a title is not a weak candidate, it is not a candidate: it
        # has nothing tying it to THIS pane. Enforcing this here rather
        # than in the scoring keeps "no candidate" honest, which is what
        # makes the abstention meaningful.
        if not signal_families(signals) & ANCHOR_FAMILIES:
            continue
        owner = claims.get(transcript.uuid)
        if owner is not None and owner != row.row_id:
            collided.append((transcript, signals, owner))
            continue
        scored.append((transcript, signals))

    decisive = [pair for pair in scored if SIGNAL_PANE_ARGV in pair[1]]
    if len(decisive) == 1:
        winners = decisive
    else:
        winners = [
            pair
            for pair in scored
            if len(signal_families(pair[1])) >= FILL_REQUIRES_SIGNALS
        ]

    # A collision on the DECISIVE signal is the most important thing this
    # module can report: the pane's own argv names a conversation another
    # row already holds, which is a duplicate row pair, not a tie.
    argv_collision = [c for c in collided if SIGNAL_PANE_ARGV in c[1]]
    if argv_collision and not decisive:
        transcript, signals, owner = argv_collision[0]
        return Proposal(
            row_id=row.row_id,
            row_class=row_class,
            outcome=OUTCOME_COLLIDES,
            proposed_uuid=transcript.uuid,
            evidence=signals,
            collides_with=owner,
            candidates=[transcript.uuid],
            detail=(
                f"the pane's own argv names {transcript.uuid}, which row "
                f"{owner} already claims - a duplicate row pair for a human, "
                "never resolved here"
            ),
        )

    searched = (
        f"searched {len(dirs)} project "
        f"director{'y' if len(dirs) == 1 else 'ies'}"
    )

    # TWO WAYS TO HAVE NO WINNER, AND THEY ARE DIFFERENT CLAIMS. Nothing
    # scored at all means nothing on disk could be this row. Something
    # scored but nothing cleared the bar means the candidates exist and
    # cannot be told apart - which is AMBIGUOUS, not absent. Collapsing
    # the second into the first would report "no transcript could be this
    # row" about a directory holding 45 that plausibly could, and a false
    # negative stated confidently is the same failure as a false match.
    if not winners:
        if not scored:
            return Proposal(
                row_id=row.row_id,
                row_class=row_class,
                outcome=OUTCOME_NO_CANDIDATE,
                detail=(
                    "no transcript on disk carries ANY evidence of being "
                    f"this row's conversation ({searched})"
                ),
            )
        return Proposal(
            row_id=row.row_id,
            row_class=row_class,
            outcome=OUTCOME_AMBIGUOUS,
            candidates=[t.uuid for t, _ in scored],
            evidence=sorted({s for _, sig in scored for s in sig}),
            detail=(
                f"{len(scored)} transcripts carry some evidence but none "
                "carries two INDEPENDENT kinds of it, so none can be told "
                f"apart from its neighbours ({searched})"
            ),
        )

    if len(winners) > 1:
        return Proposal(
            row_id=row.row_id,
            row_class=row_class,
            outcome=OUTCOME_AMBIGUOUS,
            candidates=[t.uuid for t, _ in winners],
            evidence=sorted({s for _, sig in winners for s in sig}),
            detail=(
                f"{len(winners)} transcripts each clear the evidence bar - "
                "refusing to break the tie"
            ),
        )

    transcript, signals = winners[0]
    kind = KIND_FILL if row_class == UUID_ABSENT else KIND_REPLACE

    if kind == KIND_REPLACE and not any(s in signals for s in REPLACE_REQUIRES):
        return Proposal(
            row_id=row.row_id,
            row_class=row_class,
            outcome=OUTCOME_AMBIGUOUS,
            candidates=[transcript.uuid],
            evidence=signals,
            detail=(
                "overwriting a recorded uuid needs the pane's own argv; a "
                "missing transcript is not proof the recorded value is wrong"
            ),
        )

    return Proposal(
        row_id=row.row_id,
        row_class=row_class,
        outcome=OUTCOME_CONFIDENT,
        kind=kind,
        proposed_uuid=transcript.uuid,
        evidence=signals,
        candidates=[transcript.uuid],
        detail=transcript.path,
    )


def sibling_uuid_map(rows: Sequence[RowFacts]) -> Dict[int, Set[str]]:
    """For each row, the uuids held by rows that describe the same session.

    Description: two rows are siblings when they share a tmux name or a
      working directory that resolves to the same real path - which is
      exactly the shape the cwd spelling trap produces (one session split
      across two rows, one per spelling). A sibling's uuid is strong
      corroboration AND the usual sign of a pair a human needs to merge.
      A row is never its own sibling.
    Inputs: rows (Sequence[RowFacts]).
    Output: Dict[int, Set[str]] - row id to sibling uuids.
    Example: sibling_uuid_map([a, b])[a.row_id]  # {'uuid-held-by-b'}
    """
    out: Dict[int, Set[str]] = {r.row_id: set() for r in rows}
    for row in rows:
        for other in rows:
            if other.row_id == row.row_id or not other.recorded_uuid:
                continue
            same_tmux = bool(
                row.tmux_name and other.tmux_name and row.tmux_name == other.tmux_name
            )
            same_dir = same_directory(row.working_dir, other.working_dir)
            if same_tmux or same_dir:
                out[row.row_id].add(other.recorded_uuid)
    return out
