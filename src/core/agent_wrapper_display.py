"""Which WRAPPER a session was launched through, for display.

The sibling of ``src/core/agent_family_display.py``, and deliberately a
second module rather than a second return value on that one. A family and
a wrapper answer different questions about the same session:

    family   what KIND of agent is in the pane - claude, codex, shell
    wrapper  the exact configured script that started it - "claude (chrome)"

One is a category with five members; the other is a user-authored row in
``agents.wrappers`` that can be renamed, retitled or deleted at any time.
Collapsing them would make the pill lie in the two cases that matter: a
session fingerprinted as ``claude`` has a family and no wrapper, and a
session launched through a wrapper that has since been deleted has a
recorded wrapper id and nothing left to name it with.

WHY A FINGERPRINT CAN NEVER NAME A WRAPPER. ``agent_fingerprint`` reads
scrollback and answers with a bare family token ("claude", "codex"). It
has no way to tell ``claude-chrome`` from ``claude-skip-permissions``:
both print the same banner. So ``from_fingerprint=True`` returns NO
WRAPPER here, unconditionally, before anything is looked up. A wrapper
pill is a claim about a stored launch choice; there is no honest dashed
version of it.

WHY THERE IS NO "DERIVE THE FAMILY FROM THE COMMAND" RUNG. ``AgentWrapper``
carries an explicit, validated ``family`` field (see
``src/core/agent_wrappers.py``): it defaults to ``claude``, refuses an
unknown value at validation time, and the v2->v3 migration writes it onto
every pre-existing wrapper. A wrapper therefore CANNOT reach this module
without one. Guessing a family from the first word of the script would be
a ladder rung that can never fire - the exact "fallback that cannot fire"
trap this repo's CLAUDE.md names - so ``resolve_family_for_display``'s
existing wrapper rung stays the single source of truth for family, and
this module only ever answers "what is this wrapper called".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class WrapperDisplay:
    """The configured wrapper behind a session, or the absence of one.

    Description: two fields that are always both set or both None. A
      caller renders a pill when ``label`` is non-None and renders
      NOTHING otherwise - never a placeholder, and never the raw
      ``agent_type`` string, which is an internal id the user did not
      choose the spelling of.
    Inputs (constructor): wrapper_id (str | None) - the wrapper's own id
      as configured, not the caller's spelling of it. label (str | None) -
      the human-readable name from config.
    Output: a WrapperDisplay instance.
    Example: WrapperDisplay("claude-chrome", "claude (chrome)")
    """

    wrapper_id: Optional[str] = None
    label: Optional[str] = None


#: The one answer for every "no wrapper to name". A single object so no
#: branch can return a subtly different shape of nothing.
NO_WRAPPER = WrapperDisplay()


def resolve_wrapper_for_display(
    agent_type: Optional[str],
    wrappers: List,
    *,
    from_fingerprint: bool = False,
) -> WrapperDisplay:
    """Name the configured wrapper an ``agent_type`` refers to, or nothing.

    Description: DISPLAY-TIME ONLY, and it never falls back. Three
      outcomes collapse into two renderings on purpose, because the
      distinction between them is already carried by the family pill
      standing next to it:

        a wrapper id that matches config   -> that wrapper's label
        a fingerprinted value              -> NO_WRAPPER (see module doc)
        a bare family name ("shell")       -> NO_WRAPPER; a family is not
                                              a wrapper and naming it as
                                              one would invent a launch
                                              choice nobody made
        a wrapper id config no longer has  -> NO_WRAPPER; the family pill
                                              already reads "unknown
                                              family" for the same row
        blank / None                       -> NO_WRAPPER

      Matching is case-insensitive on a stripped value, the same
      normalisation ``resolve_family_for_display`` applies, so the two
      pills on one row can never disagree about which wrapper matched.
      The label returned is the CONFIGURED one; a wrapper whose label is
      blank falls back to its id so the pill still names something real
      rather than rendering an empty box.
    Inputs:
      agent_type (str | None) - the stored/requested value: a wrapper id
        or a bare family name.
      wrappers (list) - every configured wrapper, ``AgentWrapper`` objects
        or AgentWrapper-shaped dicts (the same dual-type contract
        ``wrappers_for_family`` accepts).
      from_fingerprint (bool) - True iff ``agent_type`` came from a
        scrollback scan rather than a launch or config choice.
    Output: WrapperDisplay - ``NO_WRAPPER`` when nothing can be named.
    Example:
      resolve_wrapper_for_display("claude-chrome", cfg.wrappers)
        -> WrapperDisplay("claude-chrome", "claude (chrome)")
      resolve_wrapper_for_display("claude", cfg.wrappers,
                                  from_fingerprint=True) -> NO_WRAPPER
    """
    if from_fingerprint:
        return NO_WRAPPER

    normalized = (agent_type or "").strip().lower()
    if not normalized:
        return NO_WRAPPER

    for wrapper in wrappers:
        if isinstance(wrapper, dict):
            wid = wrapper.get("id")
            label = wrapper.get("label")
        else:
            wid = getattr(wrapper, "id", None)
            label = getattr(wrapper, "label", None)
        if not wid or str(wid).strip().lower() != normalized:
            continue
        text = (label or "").strip() or str(wid)
        return WrapperDisplay(wrapper_id=str(wid), label=text)

    return NO_WRAPPER
