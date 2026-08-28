"""Pure list operations for ``agents.wrappers`` - no I/O, no Settings.

Extracted from ``src/config.py`` (feat/universal-wrappers), which was over
the repo's 500-line budget before this feature added to it. Every function
here takes the raw wrapper list read out of config.json and returns the new
list; ``Settings`` keeps only the read-validate-write plumbing around them.
That split is what makes the family rules testable without touching a disk
or constructing a ``Settings``.

DEFAULT IS A PER-FAMILY FLAG. Each family resolves its own default
independently (see ``src/core/agent_families.resolve_agent_type`` and
``Settings.get_agent_command``), so every mutation below scopes its
default handling to one family. Clearing the flag list-wide - which is what
this code did when wrappers were claude-only - would strip claude's default
the moment a codex wrapper was made default, silently dropping claude back
to its legacy ``claude_command``.

Raw dicts, not ``AgentWrapper`` objects, because every caller is mid-way
through a read-modify-write of the on-disk block and re-validates the
merged result afterwards.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from src.core.agent_families import DEFAULT_FAMILY, wrappers_for_family


class WrapperNotFoundError(ValueError):
    """No wrapper with the requested id exists in the list."""


class DuplicateWrapperError(ValueError):
    """A wrapper with that id already exists."""


class ReservedWrapperIdError(ValueError):
    """The id collides with a reserved agent type / family name."""


def family_of(wrapper: Dict) -> str:
    """The family a raw wrapper dict belongs to.

    Description: a wrapper written before the field existed (an unmigrated
      or hand-edited config.json) is treated as ``claude``, matching
      ``AgentWrapper.family``'s own default.
    Inputs: wrapper (dict) - AgentWrapper-shaped dict.
    Output: str - family name.
    """
    return wrapper.get("family") or DEFAULT_FAMILY


def find_index(wrappers: List[Dict], wrapper_id: str) -> Optional[int]:
    """Index of the wrapper with this id, or None.

    Inputs: wrappers (list[dict]); wrapper_id (str).
    Output: int | None.
    """
    return next((i for i, w in enumerate(wrappers) if w.get("id") == wrapper_id), None)


def clear_family_default(
    wrappers: List[Dict], family_name: str, skip_index: Optional[int] = None
) -> None:
    """Clear ``default`` on every wrapper of one family, in place.

    Inputs:
      wrappers (list[dict]) - full wrapper list, mutated in place.
      family_name (str) - family whose flags to clear.
      skip_index (int | None) - index to leave untouched, used by
        ``update`` so the entry being replaced is not cleared before its
        own new value is written.
    Output: None.
    """
    for i, w in enumerate(wrappers):
        if i == skip_index:
            continue
        if family_of(w) == family_name:
            w["default"] = False


def add(wrappers: List[Dict], new_wrapper: Dict, reserved_ids) -> List[Dict]:
    """Append a wrapper, enforcing id uniqueness and reserved names.

    Description: a reserved id (a family name like ``shell``) is refused
      because ``resolve_agent_type`` checks reserved names BEFORE any
      wrapper lookup, so such a wrapper could never launch - accepting it
      would silently create dead config.
    Inputs:
      wrappers (list[dict]) - existing list (not mutated).
      new_wrapper (dict) - already pydantic-validated wrapper dump.
      reserved_ids (Container[str]) - reserved family names.
    Output: list[dict] - the new list.
    Raises: DuplicateWrapperError; ReservedWrapperIdError.
    """
    wrapper_id = new_wrapper["id"]
    if wrapper_id in reserved_ids:
        raise ReservedWrapperIdError(
            f"'{wrapper_id}' is a reserved agent type, not usable as a wrapper id"
        )
    result = [dict(w) for w in wrappers]
    if find_index(result, wrapper_id) is not None:
        raise DuplicateWrapperError(f"Wrapper '{wrapper_id}' already exists")
    if new_wrapper.get("default"):
        clear_family_default(result, family_of(new_wrapper))
    result.append(dict(new_wrapper))
    return result


def update(wrappers: List[Dict], wrapper_id: str, new_wrapper: Dict) -> List[Dict]:
    """Replace one wrapper's fields in place (id immutable).

    Inputs:
      wrappers (list[dict]) - existing list (not mutated).
      wrapper_id (str) - id of the entry to replace.
      new_wrapper (dict) - validated replacement; its id must match.
    Output: list[dict] - the new list.
    Raises: ValueError (id mismatch); WrapperNotFoundError.
    """
    if new_wrapper["id"] != wrapper_id:
        raise ValueError(
            "wrapper id cannot be changed via update; delete and re-add instead"
        )
    result = [dict(w) for w in wrappers]
    idx = find_index(result, wrapper_id)
    if idx is None:
        raise WrapperNotFoundError(f"Wrapper '{wrapper_id}' not found")
    if new_wrapper.get("default"):
        clear_family_default(result, family_of(new_wrapper), skip_index=idx)
    result[idx] = dict(new_wrapper)
    return result


def delete(wrappers: List[Dict], wrapper_id: str) -> List[Dict]:
    """Remove a wrapper, promoting a same-family sibling if it was default.

    Description: promotes the first REMAINING wrapper of the SAME family,
      not simply the first in the whole list - promoting across families
      would give one family a default while leaving another with none,
      which then silently falls through to that family's static command.
    Inputs: wrappers (list[dict]); wrapper_id (str).
    Output: list[dict] - the new list (may be empty).
    Raises: WrapperNotFoundError.
    """
    result = [dict(w) for w in wrappers]
    idx = find_index(result, wrapper_id)
    if idx is None:
        raise WrapperNotFoundError(f"Wrapper '{wrapper_id}' not found")
    removed = result.pop(idx)
    if removed.get("default"):
        siblings = wrappers_for_family(result, family_of(removed))
        if siblings:
            siblings[0]["default"] = True
    return result


def set_default(wrappers: List[Dict], wrapper_id: str) -> List[Dict]:
    """Make one wrapper its family's default, clearing that family only.

    Inputs: wrappers (list[dict]); wrapper_id (str).
    Output: list[dict] - the new list.
    Raises: WrapperNotFoundError.
    """
    result = [dict(w) for w in wrappers]
    idx = find_index(result, wrapper_id)
    if idx is None:
        raise WrapperNotFoundError(f"Wrapper '{wrapper_id}' not found")
    family_name = family_of(result[idx])
    for w in wrappers_for_family(result, family_name):
        w["default"] = (w.get("id") == wrapper_id)
    return result
