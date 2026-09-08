"""Turn a facts bundle into the report a human decides from.

Kept apart from the rules so the matcher never has to know about
formatting, and apart from the script so the whole judgement is testable
without a filesystem, a database or a tmux server.

WHAT THE REPORT MUST MAKE UNMISSABLE, because these are the distinctions
that decide whether a write is safe:

  A FILL and a REPLACE are different acts. Filling a NULL can only add
  information; overwriting a recorded uuid destroys what the row claims
  today. They are counted and listed separately and never summed into one
  "proposals" number.

  A PHANTOM is not a missing uuid. A row claiming a conversation that
  does not exist reads as sound on every surface in the product, so it is
  counted as its own class rather than folded into "has a uuid".

  A COLLISION is a duplicate row pair, not a tie. Two rows describing one
  session - the cwd spelling trap's signature - are reported for a human
  and never merged, resolved or written here.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

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
    Proposal,
    RowFacts,
    TranscriptFacts,
    classify_row,
    parse_epoch,
)
from src.core.session_uuid_backfill_rules import propose_for_row, sibling_uuid_map

#: Exit status meaning "the report was produced". Says nothing about
#: whether anything is writable - an all-abstain report is a successful
#: run.
STATUS_OK = 0

#: Exit status meaning "could not evaluate": the facts bundle was missing
#: something the matcher needs. NEVER a pass. Same convention
#: ``upgrade-verify.sh`` uses, where 2 is not 0.
STATUS_CANNOT_EVALUATE = 2


def _row_facts(raw: dict, *, live_names: Sequence[str]) -> RowFacts:
    """Project one database row onto the narrow view the matcher may use.

    Description: ``pane_window_end`` is None for a row whose pane is
      still live and a bounded epoch otherwise. The bound is the LATEST
      of the row's own activity stamps, which is loose on purpose: the
      window is only ever one weak corroborating signal, so a generous
      bound costs recall in the abstain bucket and can never cause a
      wrong attach.
    Inputs: raw (dict) - a sessions row. live_names (Sequence[str]) -
      tmux session names currently alive.
    Output: RowFacts.
    """
    archived = bool(raw.get("archived_at"))
    tmux_name = raw.get("tmux_name")
    live = bool(tmux_name) and tmux_name in set(live_names) and not archived
    end: Optional[float] = None
    if not live:
        stamps = [
            parse_epoch(raw.get("updated_at")),
            parse_epoch(raw.get("last_work_at")),
            parse_epoch(raw.get("created_at")),
        ]
        known = [s for s in stamps if s is not None]
        end = max(known) if known else None
    return RowFacts(
        row_id=int(raw["id"]),
        working_dir=raw.get("working_dir"),
        tmux_name=tmux_name,
        tmux_created_epoch=raw.get("tmux_created_epoch"),
        recorded_uuid=raw.get("claude_session_uuid"),
        lifecycle=str(raw.get("lifecycle") or "unknown"),
        archived=archived,
        title=raw.get("title") or raw.get("claude_title"),
        pane_window_end=end,
    )


def _transcript_facts(raw: dict) -> TranscriptFacts:
    """Project one scanned transcript onto the matcher's view.

    Inputs: raw (dict).
    Output: TranscriptFacts.
    """
    return TranscriptFacts(
        uuid=raw["uuid"],
        path=raw["path"],
        project_dir=raw.get("project_dir") or "",
        recorded_cwd=raw.get("recorded_cwd"),
        first_ts=raw.get("first_ts"),
        last_ts=raw.get("last_ts"),
        mtime=raw.get("mtime"),
        is_probe=bool(raw.get("is_probe")),
        name=raw.get("name"),
    )


def evaluate(facts: dict) -> Tuple[List[Proposal], Dict[str, int], List[RowFacts]]:
    """Run the matcher over every row in a facts bundle.

    Description: builds the shared context every row's decision depends
      on - who currently claims which uuid, which uuids have a transcript
      on disk, and which rows are siblings - then asks
      :func:`propose_for_row` once per row. No row's verdict can depend
      on the order rows are visited.
    Inputs: facts (dict) - a bundle from the script's ``--emit-facts``.
    Output: tuple - (proposals, class counts, row facts).
    Example: evaluate(bundle)[1]['uuid_phantom']  # 5
    """
    transcripts = [_transcript_facts(t) for t in facts.get("transcripts", [])]
    pane_argv: Dict[str, str] = facts.get("pane_argv", {}) or {}
    rows = [
        _row_facts(r, live_names=list(pane_argv.keys()))
        for r in facts.get("rows", [])
    ]
    claims = {
        r.recorded_uuid: r.row_id for r in rows if r.recorded_uuid
    }
    on_disk = [t.uuid for t in transcripts]
    siblings = sibling_uuid_map(rows)
    existing = facts.get("project_dir_names")
    now = float(facts.get("now") or 0.0)

    counts = {UUID_ABSENT: 0, UUID_PRESENT: 0, UUID_PHANTOM: 0}
    proposals: List[Proposal] = []
    for row in rows:
        counts[classify_row(row, on_disk)] += 1
        proposals.append(
            propose_for_row(
                row,
                transcripts,
                claims=claims,
                argv_uuid=pane_argv.get(row.tmux_name or ""),
                sibling_uuids=siblings.get(row.row_id, set()),
                now=now,
                existing_dirs=existing,
            )
        )
    return proposals, counts, rows


def spelling_split_pairs(rows: Sequence[RowFacts]) -> List[Tuple[int, int, str]]:
    """Row pairs whose working directories are one directory, two spellings.

    Description: the cwd spelling trap's FOOTPRINT, which is not the same
      claim as "these two rows are the same session". Several rows can
      legitimately share one project directory - the owner has four
      Hirschfeld rows - so sharing a directory under two spellings makes
      a pair worth a human's eye, and nothing more. The pairs that are
      genuinely ONE session split in two are the ones the matcher reports
      as :data:`OUTCOME_COLLIDES`, where one row's live pane names a
      conversation the other row already holds.

      REPORTED, NEVER ACTED ON. Merging duplicate rows is a separate,
      separately authorised job with its own backup requirement, and a
      merge done as a side effect of a backfill is exactly the kind of
      confident wrongness this exercise exists to avoid.
    Inputs: rows (Sequence[RowFacts]).
    Output: list[tuple[int, int, str]] - (lower id, higher id, reason).
    Example: spelling_split_pairs(rows)[0]  # (4, 7, '...')
    """
    from src.core.claude_project_dirs import same_directory

    out: List[Tuple[int, int, str]] = []
    ordered = sorted(rows, key=lambda r: r.row_id)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            if not left.working_dir or not right.working_dir:
                continue
            if left.working_dir == right.working_dir:
                continue
            if same_directory(left.working_dir, right.working_dir):
                out.append(
                    (
                        left.row_id,
                        right.row_id,
                        "same directory, two cwd spellings",
                    )
                )
    return out


def render_report(facts: dict, *, as_json: bool = False) -> Tuple[str, int]:
    """Render the dry-run report.

    Inputs: facts (dict). as_json (bool) - machine-readable output.
    Output: tuple[str, int] - the report and an exit status.
    Example: render_report(bundle)[1]  # 0
    """
    if not facts.get("rows"):
        return ("CANNOT EVALUATE: the facts bundle holds no sessions rows.",
                STATUS_CANNOT_EVALUATE)
    if not facts.get("transcripts"):
        return ("CANNOT EVALUATE: the facts bundle holds no transcripts, so "
                "every row would read as no_candidate for the wrong reason.",
                STATUS_CANNOT_EVALUATE)

    proposals, counts, rows = evaluate(facts)
    by_row = {r.row_id: r for r in rows}

    if as_json:
        return (
            json.dumps(
                {
                    "counts": counts,
                    "proposals": [p.__dict__ | {"writable": p.writable} for p in proposals],
                    "spelling_split_pairs": spelling_split_pairs(rows),
                },
                indent=2,
                default=str,
            ),
            STATUS_OK,
        )

    lines: List[str] = []
    lines.append("DRY RUN. Nothing was written.")
    lines.append("")
    lines.append("ROW CLASSES")
    lines.append(f"  no uuid recorded                  {counts[UUID_ABSENT]:>4}")
    lines.append(f"  uuid recorded, transcript EXISTS  {counts[UUID_PRESENT]:>4}")
    lines.append(
        f"  uuid recorded, transcript ABSENT  {counts[UUID_PHANTOM]:>4}"
        "   <- claims a conversation that is not on disk"
    )
    lines.append("")

    writable = [p for p in proposals if p.writable]
    fills = [p for p in writable if p.kind == KIND_FILL]
    replaces = [p for p in writable if p.kind == KIND_REPLACE]
    ambiguous = [p for p in proposals if p.outcome == OUTCOME_AMBIGUOUS]
    none_found = [p for p in proposals if p.outcome == OUTCOME_NO_CANDIDATE]
    collides = [p for p in proposals if p.outcome == OUTCOME_COLLIDES]

    lines.append("PROPOSALS")
    lines.append(f"  confident FILL    (null -> uuid)      {len(fills):>4}  WRITABLE")
    lines.append(f"  confident REPLACE (uuid -> uuid)      {len(replaces):>4}  WRITABLE")
    lines.append(f"  ambiguous, left alone                 {len(ambiguous):>4}")
    lines.append(f"  no candidate at all                   {len(none_found):>4}")
    lines.append(f"  collides with another row's claim     {len(collides):>4}")
    lines.append("")

    def _table(title: str, group: Sequence[Proposal]) -> None:
        if not group:
            return
        lines.append(title)
        lines.append(
            f"  {'row':>4}  {'class':<13} {'proposed uuid':<38} evidence"
        )
        for p in sorted(group, key=lambda x: x.row_id):
            row = by_row.get(p.row_id)
            name = (row.tmux_name or "-") if row else "-"
            lines.append(
                f"  {p.row_id:>4}  {p.row_class:<13} "
                f"{(p.proposed_uuid or '-'):<38} {', '.join(p.evidence) or '-'}"
            )
            lines.append(f"        {name}  {p.detail or ''}"[:160])
        lines.append("")

    _table("WRITABLE - FILL a null", fills)
    _table("WRITABLE - REPLACE a phantom (needs the pane's own argv)", replaces)
    _table("LEFT ALONE - ambiguous, for a human", ambiguous)
    _table("LEFT ALONE - collides: one session, two rows", collides)

    if none_found:
        lines.append("NO CANDIDATE - nothing on disk could be these rows")
        for p in sorted(none_found, key=lambda x: x.row_id):
            row = by_row.get(p.row_id)
            lines.append(
                f"  {p.row_id:>4}  {p.row_class:<13} "
                f"{(row.tmux_name if row else None) or '-'}"
            )
        lines.append("")

    pairs = spelling_split_pairs(rows)
    if pairs:
        confirmed = {
            (min(p.row_id, p.collides_with), max(p.row_id, p.collides_with))
            for p in proposals
            if p.outcome == OUTCOME_COLLIDES and p.collides_with is not None
        }
        lines.append(
            "ROWS SHARING ONE DIRECTORY UNDER TWO CWD SPELLINGS - "
            "reported only, never merged here"
        )
        lines.append(
            "  CONFIRMED means one row's live pane names the conversation "
            "the other row holds:"
        )
        for left, right, why in pairs:
            mark = "CONFIRMED one session" if (left, right) in confirmed else why
            lines.append(f"  rows {left} and {right}: {mark}")
        lines.append("")

    return ("\n".join(lines), STATUS_OK)
