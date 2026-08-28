"""DISPLAY-time resolution of ``agent_type`` into an agent family.

Split out of ``src/core/agent_families.py`` on purpose, and kept in its own
module rather than folded back in - not just documented apart. That file's
``get_family`` / ``resolve_agent_type`` are LAUNCH-TIME resolvers: something
has to run when a session launches, so an unresolvable ``agent_type`` MUST
fall back to a real, runnable family (``DEFAULT_FAMILY``). This module's
``resolve_family_for_display`` is the opposite contract: a status pill must
never claim a fact nobody measured, so an unresolvable value here returns
``(None, "unknown")`` - never the launch fallback.

This is the project's THREE-OUTCOME RULE (see repo CLAUDE.md) applied to
one field: pass, fail, could-not-determine, and the third state must never
collapse into either of the other two. Read ``resolve_family_for_display``'s
docstring for the full outcome table before changing this file, and read
``get_family``'s docstring in ``agent_families.py`` before "simplifying"
the two back into one function - they answer different questions and a
future merge would silently reintroduce the bug this module fixes.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from src.core.agent_families import AGENT_FAMILY_BY_NAME, DEFAULT_FAMILY, AgentFamily

# Every value ``resolve_family_for_display`` can return as its ``source``.
# Kept as a tuple (not just documented in prose) so a test can assert the
# display field never carries a string outside this set.
FAMILY_SOURCE_WRAPPER = "wrapper"
FAMILY_SOURCE_RESERVED_NAME = "reserved_name"
FAMILY_SOURCE_FINGERPRINT = "fingerprint"
FAMILY_SOURCE_DERIVED_DEEPEST = "derived_deepest"
FAMILY_SOURCE_UNKNOWN = "unknown"

DISPLAY_FAMILY_SOURCES: Tuple[str, ...] = (
    FAMILY_SOURCE_WRAPPER,
    FAMILY_SOURCE_RESERVED_NAME,
    FAMILY_SOURCE_FINGERPRINT,
    FAMILY_SOURCE_DERIVED_DEEPEST,
    FAMILY_SOURCE_UNKNOWN,
)


def resolve_family_for_display(
    agent_type: Optional[str],
    wrappers: List,
    *,
    from_fingerprint: bool = False,
) -> Tuple[Optional[AgentFamily], str]:
    """Resolve ``agent_type`` into a family for DISPLAY, honestly.

    DISPLAY-TIME RESOLVER - the counterpart to ``get_family`` /
    ``resolve_agent_type`` in ``src/core/agent_families.py``, and
    deliberately NOT merged with them. Those two exist to make sure a
    session always has something to run; this one exists to make sure a
    pill never claims a fact nobody measured. When the input cannot be
    resolved this returns ``(None, "unknown")`` - never
    ``AGENT_FAMILY_BY_NAME[DEFAULT_FAMILY]``. A caller that wants a value
    to launch with must call ``get_family`` / ``resolve_agent_type``
    instead; this function is not a drop-in replacement for either.

    Description of the five ``source`` outcomes, in resolution order:
      1. ``"reserved_name"`` - ``agent_type`` (case-insensitively) equals
         one of ``AGENT_FAMILY_NAMES`` directly, with no wrapper consulted.
         The stored value literally IS a family name - a fact, not a
         guess. (Broader than ``RESERVED_FAMILY_NAMES``: that set exists
         to keep a wrapper ID from colliding with ``"claude"`` at
         *launch* time - see ``agent_families``'s module docstring - and
         is irrelevant to whether ``"claude"`` is a meaningful value to
         *display*.)
      2. ``"wrapper"`` - ``agent_type`` matches a configured wrapper's id,
         and that wrapper carries an explicit, non-blank ``family``. The
         strongest kind of fact: a user-named launch choice.
      3. ``"derived_deepest"`` - ``agent_type`` matches a configured
         wrapper's id, but the wrapper itself has no ``family`` recorded
         (a raw dict from a config read before the ``family`` field
         existed - see ``wrappers_for_family``'s docstring for the same
         case). The family is still resolvable, but only by walking past
         the wrapper to the additive-default convention, one layer deeper
         than a direct field read - hence "derived", and "deepest"
         because there is nowhere further to walk. Render this
         differently from a plain "wrapper" fact: it is a real answer,
         reached by inference rather than by reading a stored value.
      4. ``"fingerprint"`` - the caller passes ``from_fingerprint=True``
         (meaning ``agent_type`` was produced by scanning adopted-session
         scrollback in ``src/core/agent_fingerprint.py``, not chosen at
         launch) AND that scan produced a value matching a known family
         name. A heuristic guess that happened to land on a real family -
         render it visibly as a guess, never identically to a stored fact.
      5. ``"unknown"`` - none of the above. ``agent_type`` is missing,
         blank, or a string that matches neither a wrapper id nor a
         family name (e.g. a wrapper that has since been deleted from
         config). Returns ``(None, "unknown")``. THIS is the outcome that
         used to silently render as a confident "claude" pill; it must
         never collapse into any of the other four.

    Inputs:
      agent_type (str | None) - the stored/requested value, same shape as
        ``resolve_agent_type`` accepts (a wrapper id OR a bare family
        name); case-insensitive.
      wrappers (list) - every configured wrapper, ``AgentWrapper`` objects
        or AgentWrapper-shaped dicts (same dual-type contract as
        ``wrappers_for_family``).
      from_fingerprint (bool) - True iff the caller already knows this
        ``agent_type`` value was produced by scrollback fingerprinting
        rather than an explicit launch/config choice. The resolver has no
        way to infer this on its own - a fingerprinted value that landed
        on "codex" is textually identical to an explicit "codex" launch -
        so the caller must say so. Defaults to False (the common case:
        launched or config-derived).
    Output:
      (AgentFamily | None, str) - the resolved family (or None when it
      could not be determined) and one of ``DISPLAY_FAMILY_SOURCES``.
    Example:
      resolve_family_for_display("deleted-wrapper-id", []) -> (None, "unknown")
      resolve_family_for_display("codex", []) -> (codex family, "reserved_name")
    """
    normalized = (agent_type or "").strip().lower()
    if not normalized:
        return None, FAMILY_SOURCE_UNKNOWN

    if normalized in AGENT_FAMILY_BY_NAME:
        family = AGENT_FAMILY_BY_NAME[normalized]
        if from_fingerprint:
            return family, FAMILY_SOURCE_FINGERPRINT
        return family, FAMILY_SOURCE_RESERVED_NAME

    for w in wrappers:
        if isinstance(w, dict):
            wid = w.get("id")
            has_family_key = "family" in w
            family_name = w.get("family") or None
        else:
            wid = getattr(w, "id", None)
            has_family_key = hasattr(w, "family")
            family_name = getattr(w, "family", None) or None
        if wid != normalized:
            continue

        if family_name is not None and family_name in AGENT_FAMILY_BY_NAME:
            family = AGENT_FAMILY_BY_NAME[family_name]
            if from_fingerprint:
                return family, FAMILY_SOURCE_FINGERPRINT
            return family, FAMILY_SOURCE_WRAPPER

        if not has_family_key or not family_name:
            # Pre-migration wrapper shape: no explicit family recorded.
            # Still resolvable via the additive-default convention
            # (wrappers_for_family applies the same rule), but only by
            # walking one layer deeper than a direct field read.
            family = AGENT_FAMILY_BY_NAME[DEFAULT_FAMILY]
            if from_fingerprint:
                return family, FAMILY_SOURCE_FINGERPRINT
            return family, FAMILY_SOURCE_DERIVED_DEEPEST

        # A wrapper matched, but its recorded family is not a family this
        # build knows about (e.g. config written by a newer version). We
        # found the wrapper and still cannot answer - that is "unknown",
        # not "wrapper".
        return None, FAMILY_SOURCE_UNKNOWN

    return None, FAMILY_SOURCE_UNKNOWN
