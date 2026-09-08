"""Decide what ONE transcript should become: a session row, or nothing.

THE OWNER'S MODEL, VERBATIM (2026-09-08): "all sessions belong to
projects, the root folder. sessions and projects can be archived not
deleted. archived items are not visible unless the checkbox is checked."
And: "i dont need any test bs but everything real should be accounted
for."

So the default is IMPORT. This module's job is not to find reasons to
skip; it is to decide which project a real conversation belongs to and
what its row should say. There is exactly ONE EXCLUSION RULE - the
scratch paths - and everything else below it is a statement that the
file is not a session at all rather than a policy about one. All three
are NAMED outcomes, never silent drops:

  EXCLUDED  the conversation ran in a scratch directory this app or an
            agent harness created for itself - ``/private/tmp/``,
            ``/tmp/``, ``/var/folders/``. Not a judgement about whether
            the work mattered; those paths are per-run temporary
            directories that no longer exist and can never be a project
            root, so a row pointing at one could never be resumed.
  ABSENT    the file holds no conversation at all. That is measured by
            transcript_import_facts, not decided here.
  NOT A     the file is not a top-level conversation. Measured on the
  SESSION   live corpus 2026-09-08: 364 of 1,167 files carrying a cwd are
            named ``agent-<id>.jsonl`` - a SUBAGENT's run inside somebody
            else's conversation - and 123 distinct parent ids cover 436
            of them. They are components of a session, not sessions.

THE IDENTITY OF A TRANSCRIPT IS ITS FILE STEM, NOT ITS RECORDED
``sessionId``, and getting that backwards is what surfaced the case
above. ``claude --resume <id>`` opens ``<id>.jsonl`` in the project
directory, so the stem is the only id a restart can act on. A subagent
file's records carry the PARENT conversation's ``sessionId``, so keying
on the recorded value would have proposed 364 rows all claiming to be
conversations they are not, most of them colliding on a uuid a parent row
already holds. The stem is classified by
``message_model_serialize.session_ref_scheme`` - the corpus's existing
single spelling of "uuid / agent / opaque" - rather than by a second
pattern invented here.

THE OWNER'S OWN TEST PROJECTS ARE REAL AND ARE IMPORTED. ``fstest``,
``scrolltest``, ``permtest`` under a real home directory, a
``Scratch/llmScratch`` lab - he did that work, in a directory he chose,
and "this looks like a throwaway" is exactly the judgement that would
start quietly deleting his history.

EXISTENCE ON DISK IS NOT THE RULE. A cwd that no longer exists still
imports, under a created archived project. The rule is whether the PATH
is real - a place on this machine the owner worked - not whether the
directory survived. Deleting a folder does not un-have the conversation,
and a rule keyed on ``os.path.isdir`` would silently drop every finished
project. There is a test for exactly this, as a NEGATIVE CONTROL.

PROJECT RESOLUTION, IN TWO STEPS.

  1. MATCH. The existing project whose ``root`` CONTAINS the cwd, deepest
     first. Both sides are canonicalised with ``realpath`` before
     comparing, because ``~/Development`` is a symlink into iCloud on
     this machine and the same directory is spelled two ways - the defect
     that already manufactured two junk project rows. Deepest wins
     because that is what ``derived_deepest`` already means everywhere
     else in this schema; a shallower root containing a deeper one is not
     a tie.
  2. CREATE, ARCHIVED. No match means a project row is created for the
     cwd's ROOT FOLDER, with ``archived_at`` set. The rule for "root
     folder" is: the git top level the cwd sits inside, else the cwd
     itself. Git is asked by walking up for a ``.git`` entry, stopping at
     ``$HOME`` - no subprocess, so the answer is deterministic and a test
     can build one. It matters: 17 transcripts ran in
     ``.../claude_4/frontend`` and belong with the 37 in ``claude_4``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.message_model_serialize import (
    AGENT_SCHEME,
    UUID_SCHEME,
    session_ref_scheme,
)
from src.core.db_models import (
    PROJECT_SOURCE_TRANSCRIPT_IMPORT,
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
    SESSION_LIFECYCLE_SOURCE_IMPORT,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_IMPORTED,
)
from src.core.transcript_import_facts import TranscriptFacts
from src.core.transcript_import_paths import (  # noqa: F401  (re-exported)
    SCRATCH_PREFIXES,
    TITLE_MAX,
    canonical,
    git_top_level,
    is_scratch,
    match_project,
    truncate_title,
)

#: The transcript becomes a session row.
PLAN_IMPORT = "import"

#: A ``sessions`` row already holds this ``claude_session_uuid``. Counted
#: and skipped, never overwritten - the existing row may be a live
#: session whose columns this importer has no business touching.
PLAN_ALREADY_PRESENT = "already_present"

#: The conversation ran in a per-run scratch directory. See the module
#: docstring for the whole rule.
PLAN_EXCLUDED_SCRATCH = "excluded_scratch"

#: The file holds no conversation, or could not be read. Carried through
#: from ``transcript_import_facts`` rather than re-decided here.
PLAN_NOT_A_CONVERSATION = "not_a_conversation"

#: THE FILE IS NOT A TOP-LEVEL SESSION. Its stem is an ``agent-<id>``
#: subagent ref, or a string that names no identity scheme this corpus
#: recognises (an ``audit`` or ``journal`` file). A real transcript, and
#: not one a restart could ever resume. See the module docstring.
PLAN_NOT_A_SESSION_REF = "not_a_session_ref"

#: ANOTHER FILE IN THIS PASS ALREADY CLAIMS THIS uuid. One conversation,
#: one row: the same conversation can have a transcript under two project
#: directories when its working directory was spelled two ways, and both
#: files are real. The pass keeps the one with the most recent activity
#: and reports the other rather than dropping it silently.
PLAN_DUPLICATE_TRANSCRIPT = "duplicate_transcript"

@dataclass(frozen=True)
class ProposedSession:
    """The row one transcript would become, and why.

    Description: frozen because it is a PROPOSAL a human reads before
      ``--apply`` acts on it. Nothing here is written until the operator
      says so, and nothing about a refusal is inferred from a missing
      field.
    Attributes:
        outcome: one of the four ``PLAN_*`` constants.
        claude_session_uuid: the conversation this row IS.
        working_dir: the canonical LONG spelling of the cwd.
        recorded_cwd: the LITERAL cwd the transcript recorded. Kept
            because Claude Code derives its transcript directory from
            that exact string, so a resume has to run there.
        project_root: the project this row belongs to, canonical.
        project_exists: True when a projects row already carries that
            root; False when the import would create one.
        project_rule: how ``project_root`` was chosen - 'matched',
            'git_toplevel' or 'cwd'.
        title: the reconstructed label, or None when nothing named it.
        title_source: 'custom-title', 'first_user_message' or None.
        created_at / last_work_at: from the transcript's own timestamps.
        transcript_path: the file this proposal was built from. Carried
            so the WRITER can classify the conversation's ``kind``
            without the plan having to read the file a second time -
            this function reads nothing off disk and stays that way.
        detail: a plain sentence, on every non-import outcome.
    """

    outcome: str
    claude_session_uuid: str
    working_dir: Optional[str] = None
    recorded_cwd: Optional[str] = None
    project_root: Optional[str] = None
    project_exists: bool = False
    project_rule: Optional[str] = None
    title: Optional[str] = None
    title_source: Optional[str] = None
    created_at: Optional[str] = None
    last_work_at: Optional[str] = None
    transcript_path: Optional[str] = None
    detail: Optional[str] = None

    @property
    def writes(self) -> bool:
        """True only when this proposal would insert a row.

        Inputs: n/a. Output: bool.
        Example: plan.writes
        """
        return self.outcome == PLAN_IMPORT


def _title_for(facts: TranscriptFacts) -> Tuple[Optional[str], Optional[str]]:
    """The label for one conversation, and where it came from.

    Description: a ``custom-title`` outranks everything, because the
      owner typed it - the same precedence ``archive_titles`` uses. The
      first user message is the fallback. Neither means the row gets no
      title, which is a real state and better than a manufactured one.
    Inputs: facts (TranscriptFacts).
    Output: tuple[str | None, str | None] - (title, title_source).
    """
    if facts.custom_title:
        return truncate_title(facts.custom_title), "custom-title"
    text = truncate_title(facts.first_user_text)
    return (text, "first_user_message") if text else (None, None)


def plan_transcript(
    facts: TranscriptFacts,
    *,
    held_uuids: Sequence[str],
    project_roots: Sequence[str],
    home: Optional[str] = None,
) -> ProposedSession:
    """Decide what one transcript becomes.

    Description: the whole decision for one file, in the order the
      outcomes are cheapest to establish. Nothing is written and nothing
      is read from disk except the git walk, so this is the function a
      dry run and an apply BOTH call - the report a human reads and the
      rows that get inserted are built by the same code, which is what
      makes the report trustworthy.
    Inputs: facts (TranscriptFacts) - from
      ``transcript_import_facts.read_transcript_facts``. held_uuids
      (Sequence[str]) - every ``claude_session_uuid`` a sessions row
      already carries. project_roots (Sequence[str]) - CANONICAL roots of
      every existing project, archived ones included. home (str | None) -
      override for tests.
    Output: ProposedSession.
    Example: plan_transcript(f, held_uuids=[], project_roots=[]).outcome
      # 'import'
    """
    # THE STEM, NOT THE RECORDED sessionId. See the module docstring.
    uuid = facts.uuid
    if not facts.is_conversation:
        return ProposedSession(
            outcome=PLAN_NOT_A_CONVERSATION,
            claude_session_uuid=uuid,
            detail=facts.detail,
        )
    scheme = session_ref_scheme(uuid)
    if scheme != UUID_SCHEME:
        return ProposedSession(
            outcome=PLAN_NOT_A_SESSION_REF,
            claude_session_uuid=uuid,
            recorded_cwd=facts.cwd,
            detail=(
                f"the transcript is named {uuid!r}, a {scheme!r}-scheme ref: "
                + (
                    "a subagent's run inside another conversation, not a "
                    "conversation of its own"
                    if scheme == AGENT_SCHEME
                    else "a string naming no identity scheme this corpus "
                    "recognises, so no restart could address it"
                )
            ),
        )
    if uuid in set(held_uuids):
        return ProposedSession(
            outcome=PLAN_ALREADY_PRESENT,
            claude_session_uuid=uuid,
            recorded_cwd=facts.cwd,
            detail=(
                "a sessions row already holds this conversation, so the "
                "import leaves it alone rather than writing over columns "
                "a live session may own"
            ),
        )
    if is_scratch(facts.cwd):
        return ProposedSession(
            outcome=PLAN_EXCLUDED_SCRATCH,
            claude_session_uuid=uuid,
            recorded_cwd=facts.cwd,
            detail=(
                f"the conversation ran in {facts.cwd!r}, a per-run scratch "
                "directory that can never be a project root"
            ),
        )

    working_dir = canonical(facts.cwd) or facts.cwd
    matched = match_project(working_dir, project_roots)
    if matched is not None:
        project_root, rule, exists = matched, "matched", True
    else:
        top = git_top_level(working_dir, home=home)
        project_root = top or working_dir
        rule = "git_toplevel" if top else "cwd"
        exists = False

    title, title_source = _title_for(facts)
    return ProposedSession(
        outcome=PLAN_IMPORT,
        claude_session_uuid=uuid,
        working_dir=working_dir,
        recorded_cwd=facts.cwd,
        project_root=project_root,
        project_exists=exists,
        project_rule=rule,
        title=title,
        title_source=title_source,
        created_at=facts.first_ts,
        last_work_at=facts.last_ts or facts.first_ts,
        transcript_path=facts.path,
    )


#: The exact column values every imported session row carries, apart from
#: the per-row facts. Spelled ONCE so the dry run's report and the apply's
#: INSERT cannot describe different rows.
#:
#: EVERY ABSENT COLUMN IS ABSENT ON PURPOSE. ``tmux_name``,
#: ``tmux_created_epoch``, ``agent_type``, ``agent_family``, ``model``,
#: ``parent_session_id`` and ``fork_kind`` are all left NULL: this app
#: never watched the session, so it knows none of them, and a guess in
#: any one of them would be indistinguishable from a measurement.
#: ``tmux_socket`` is the exception and it is NOT a claim - the column is
#: NOT NULL with DEFAULT 'cloude', so there is no NULL to write; a row
#: with a NULL ``tmux_name`` is already unaddressable on any socket.
IMPORTED_ROW_CONSTANTS: Dict[str, object] = {
    "origin": SESSION_ORIGIN_IMPORTED,
    "lifecycle": SESSION_LIFECYCLE_STOPPED,
    "lifecycle_source": SESSION_LIFECYCLE_SOURCE_IMPORT,
    "project_attribution": SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    "claude_session_uuid_source": SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
}

#: What a project row created by this importer says about itself.
IMPORTED_PROJECT_SOURCE: str = PROJECT_SOURCE_TRANSCRIPT_IMPORT


def summarise(plans: Sequence[ProposedSession]) -> Dict[str, int]:
    """Count each outcome. Every key is present, including the zeroes.

    Description: a missing key and a zero are different claims, and a
      report that omitted an outcome would read as "this never happens"
      rather than "this did not happen in this pass".
    Inputs: plans (Sequence[ProposedSession]).
    Output: dict[str, int].
    Example: summarise([])  # {'import': 0, 'already_present': 0, ...}
    """
    counts = {
        PLAN_IMPORT: 0,
        PLAN_ALREADY_PRESENT: 0,
        PLAN_EXCLUDED_SCRATCH: 0,
        PLAN_NOT_A_CONVERSATION: 0,
        PLAN_NOT_A_SESSION_REF: 0,
        PLAN_DUPLICATE_TRANSCRIPT: 0,
    }
    for plan in plans:
        counts[plan.outcome] = counts.get(plan.outcome, 0) + 1
    return counts


def projects_to_create(plans: Sequence[ProposedSession]) -> List[str]:
    """Every project root the import would create, in a stable order.

    Inputs: plans (Sequence[ProposedSession]).
    Output: list[str] - canonical roots, sorted.
    Example: projects_to_create(plans)  # ['/Users/x/Scratch']
    """
    return sorted(
        {
            plan.project_root
            for plan in plans
            if plan.writes and not plan.project_exists and plan.project_root
        }
    )


def dedupe_by_uuid(plans: Sequence[ProposedSession]) -> List[ProposedSession]:
    """One conversation, one row, when two files claim the same uuid.

    Description: a conversation whose working directory was spelled two
      ways has a transcript under two project directories, and both files
      are real - 7 uuids on the live corpus 2026-09-08. The pass keeps
      the file with the most recent activity, because that is the one
      that holds the later half of the conversation, and reports the
      other as :data:`PLAN_DUPLICATE_TRANSCRIPT` rather than dropping it
      silently.

      ORDER IS PRESERVED for everything else, so a report reads in corpus
      order. Only proposals that would WRITE are considered; a skipped
      one cannot collide with anything.
    Inputs: plans (Sequence[ProposedSession]) - one per transcript file.
    Output: list[ProposedSession] - same length, some outcomes rewritten.
    Example: dedupe_by_uuid(plans)
    """
    winner: Dict[str, int] = {}
    for index, plan in enumerate(plans):
        if not plan.writes:
            continue
        held = winner.get(plan.claude_session_uuid)
        if held is None:
            winner[plan.claude_session_uuid] = index
            continue
        incumbent = plans[held]
        if (plan.last_work_at or "") > (incumbent.last_work_at or ""):
            winner[plan.claude_session_uuid] = index

    keep = set(winner.values())
    out: List[ProposedSession] = []
    for index, plan in enumerate(plans):
        if not plan.writes or index in keep:
            out.append(plan)
            continue
        kept = plans[winner[plan.claude_session_uuid]]
        out.append(
            ProposedSession(
                outcome=PLAN_DUPLICATE_TRANSCRIPT,
                claude_session_uuid=plan.claude_session_uuid,
                recorded_cwd=plan.recorded_cwd,
                detail=(
                    "another transcript in this pass carries the same "
                    f"conversation with later activity ({kept.recorded_cwd!r}), "
                    "so this one is the older spelling of the same session"
                ),
            )
        )
    return out
