"""Is the conversation a row NAMES actually still on disk?

THE INCIDENT THIS EXISTS FOR, 2026-09-07. A restart of session row 42
resumed ``c33e4ce2-17ea-4320-aa28-36204c4d13c5`` because that is the
``claude_session_uuid`` the row carried. No transcript with that id exists
anywhere under ``~/.claude/projects``. ``claude --resume <unknown-uuid>``
prints its refusal and exits immediately, so tmux was handed a pane that
died on the first tick, while ``rebind_instance`` had already stamped the
row ``lifecycle='running'`` and the route had already answered
``201 conversation='resumed'``. A dead pane reported as a running,
resumed conversation is the exact false green this codebase is built
against, and the owner spent the evening looking for a session that had
never started.

THE CHECK IS A FILE STAT, NOT AN INFERENCE. Claude Code stores one
conversation as ``<corpus>/<project-slug>/<uuid>.jsonl``. Either that
file is there or it is not, and asking is cheap enough to do before every
resume.

WHY IT DOES NOT TRUST THE SLUG. ``claude_transcript_correlate.slugify_project_dir``
maps ``/`` and ``.`` to ``-`` and nothing else, but Claude Code also maps
the SPACE in ``Mobile Documents`` and the ``~`` in ``com~apple~CloudDocs``.
Measured on the owner's box 2026-09-07: the real directory is
``-Users-jsugamele-Library-Mobile-Documents-com-apple-CloudDocs-Sync-Development-CloudeCode``
while the slug function produces ``...Mobile Documents-com~apple~CloudDocs...``,
which names nothing. A presence probe built on that slug alone would
answer ABSENT for every session in the owner's iCloud tree and refuse
every restart on the box - a repair strictly worse than the defect.

So the slug is used only as a FAST PATH, and the authority is a scan of
the corpus root's immediate subdirectories for ``<uuid>.jsonl``. That is
one ``stat`` per project directory (about 200 on the live corpus, against
19,065 files), so it is bounded by project count and not by transcript
count.

THREE OUTCOMES, AND THE THIRD IS NOT A FLAVOUR OF THE OTHER TWO. A
missing corpus root, an unreadable one, or a blank uuid answer
:data:`CONVERSATION_UNCHECKED`. Callers must treat that as "do not
refuse": not having looked is not evidence of absence, and turning a
could-not-evaluate into a refusal would break restart on any machine
whose corpus lives somewhere this module was not told about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

#: DEFINITE POSITIVE. A ``<uuid>.jsonl`` was found in the corpus, and its
#: path is on the result. Safe to hand to ``claude --resume``.
CONVERSATION_PRESENT = "present"

#: DEFINITE NEGATIVE. The corpus was readable and scanned, and no
#: ``<uuid>.jsonl`` is in it. ``--resume`` WILL fail, so no caller may
#: report this as a resumed conversation.
CONVERSATION_ABSENT = "absent"

#: COULD NOT EVALUATE. No uuid to look for, no corpus root on this box, or
#: the root could not be listed. Never folded into either outcome above,
#: and specifically never into :data:`CONVERSATION_ABSENT`: a caller that
#: refused on this would refuse on every machine with a relocated corpus.
CONVERSATION_UNCHECKED = "unchecked"

#: A canonical Claude session uuid. Anything else is not looked for at
#: all - a truncated or shell-mangled value would otherwise glob against
#: whatever it happened to prefix-match. Same rule, same reason, as
#: ``claude_resume_argv._UUID_RE``.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class ConversationPresence:
    """What the corpus says about one Claude session uuid.

    Description: the return of :func:`conversation_presence`. Frozen
      because it reports a measurement of the filesystem, not a plan.

      - ``outcome``: one of the three constants above.
      - ``path``: the transcript found, on :data:`CONVERSATION_PRESENT`
        only.
      - ``detail``: a plain sentence naming why, for the two outcomes
        that are not PRESENT. Written to be shown to a user verbatim.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    outcome: str
    path: Optional[Path] = None
    detail: Optional[str] = None

    @property
    def missing(self) -> bool:
        """True ONLY on a measured absence.

        Description: the one-line test a refusal may be built on.
          Deliberately False for :data:`CONVERSATION_UNCHECKED`, so
          "could not look" can never be spelled the same way as
          "looked, and it is gone".
        Inputs: n/a.
        Output: bool.
        Example: conversation_presence('abc').missing
        """
        return self.outcome == CONVERSATION_ABSENT


def conversation_presence(
    claude_session_uuid: Optional[str],
    *,
    working_dir: Optional[str] = None,
    corpus_root: Optional[Path] = None,
) -> ConversationPresence:
    """Report whether a Claude conversation is still on this machine.

    Description: the preflight for anything about to run
      ``claude --resume <uuid>``. Tries the working directory's own
      project slug first (one stat), then scans the corpus root's
      immediate subdirectories, because the slug function does not cover
      every character Claude Code rewrites (see the module docstring).
    Inputs: claude_session_uuid (str | None) - the conversation to look
      for. working_dir (str | None) - the session's directory, used only
      to build the fast-path slug; a wrong or absent one costs a scan,
      never a wrong answer. corpus_root (Path | None) - the transcript
      corpus; defaults to ``~/.claude/projects``.
    Output: ConversationPresence.
    Example:
      conversation_presence('2629dba5-...', working_dir='/x').outcome
    """
    from src.core.claude_transcript_correlate import (
        default_projects_dir,
        slugify_project_dir,
    )

    if not claude_session_uuid or not _UUID_RE.match(str(claude_session_uuid)):
        return ConversationPresence(
            CONVERSATION_UNCHECKED,
            detail=(
                "no canonical claude session id to look for, so the "
                "transcript corpus was not searched"
            ),
        )

    root = Path(corpus_root) if corpus_root is not None else default_projects_dir()
    filename = f"{claude_session_uuid}.jsonl"

    if working_dir:
        fast = root / slugify_project_dir(str(working_dir)) / filename
        try:
            if fast.is_file():
                return ConversationPresence(CONVERSATION_PRESENT, path=fast)
        except OSError as exc:
            # An unreadable candidate is not an absence. Fall through to
            # the scan, which gets its own chance to answer.
            logger.debug(
                "conversation_presence_fast_path_unreadable",
                path=str(fast),
                error=str(exc),
            )

    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        # THE CORPUS IS NOT WHERE WE LOOKED. Could-not-evaluate, and it
        # must stay that way: a machine with a relocated corpus would
        # otherwise have every resume refused.
        return ConversationPresence(
            CONVERSATION_UNCHECKED,
            detail=(
                f"the transcript corpus at {root} could not be read "
                f"({exc.strerror or exc}), so whether this conversation "
                "still exists could not be determined"
            ),
        )

    for child in children:
        candidate = child / filename
        try:
            if candidate.is_file():
                return ConversationPresence(CONVERSATION_PRESENT, path=candidate)
        except OSError:
            # One unreadable project directory is not the whole corpus.
            continue

    return ConversationPresence(
        CONVERSATION_ABSENT,
        detail=(
            f"no transcript for claude session {claude_session_uuid} "
            f"exists under {root}, so it cannot be resumed"
        ),
    )
