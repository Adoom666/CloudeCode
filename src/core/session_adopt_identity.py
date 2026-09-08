"""What to call a session the user just opened: its stored id, or a new one.

The pure decision behind ``SessionManager.adopt_external_session``'s very
first line. It used to be a literal::

    adopted_id = f"adopted:{name}"

and that literal is a defect whenever the session ALREADY HAS A ROW.

WHAT THE RE-MINT ACTUALLY BREAKS, because "the id looks different" badly
undersells it. ``get_env_for_spawn`` injects ``CLOUDECODE_SESSION_ID``
into the pane's environment at ``new-session`` time, so the agent running
inside that pane presents its CREATE-TIME id on every hook POST for the
rest of its life, and the hook token it carries is bound to that id.
Registering the same live session under a second, invented
``adopted:<name>`` id does not rename anything - it leaves the agent
presenting an id the app now has no token for, and
``validate_hook_token`` answers False. Measured on the owner's box
2026-09-08: one session re-minted this way produced **94 hook POSTs
answered 403** in four minutes, none of them retryable from the agent's
side. Note the status - **403, not 410**. A reader grepping for the
stale-session code finds nothing and concludes the hook path is healthy.

THE LADDER IS NOT A SECOND LADDER. ``resolve_session_id`` from
``session_boot_readopt_plan`` is imported and reused verbatim, so
adopt-on-open and the boot re-adopt cannot disagree about what one live
session is called. Inventing a parallel resolver here is how the two
paths would drift into naming the same pane two different things.

WHAT IS STILL DERIVED, and must stay that way. A genuinely external
session - one with no row for its instance triple - has no stored id to
recover, and ``adopted:<name>`` remains exactly right for it. So does a
session whose creation epoch could not be read: with no epoch there is no
instance triple, ``session_store.get_instance`` returns None BY CONTRACT,
and a row that merely shares the NAME describes some other, historical
process. Guessing there would attach a stranger's row to a live pane,
which is a worse failure than a fresh id.

A RECOVERED ID CARRIES A CREDENTIAL; A DERIVED ONE DOES NOT. That is the
whole reason :attr:`AdoptIdentity.rekeyed` exists as a field rather than
being re-derived by the caller from a string prefix. The caller mints a
hook token for a derived id and MUST NOT mint one for a recovered id -
minting rotates the token, and rotating the token is precisely the 403
this module exists to stop. See ``SessionManager._keep_hook_token``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.core.session_boot_readopt_plan import (
    ID_SOURCE_DERIVED,
    derived_id_for,
    resolve_session_id,
)

__all__ = [
    "AdoptIdentity",
    "resolve_adopt_identity",
]


@dataclass(frozen=True)
class AdoptIdentity:
    """The id one adoption should register under, and where it came from.

    Description: ``id_source`` is one of the ``ID_SOURCE_*`` values from
      ``session_boot_readopt_plan`` - reused, not redefined.
      ``rekeyed`` is the operationally important bit: True means this id
      was RECOVERED from durable state and the live agent may already be
      presenting it with a token bound to it, so the caller must not mint
      a fresh token over the top. False means the id was DERIVED here and
      nothing else in the world is using it yet.
    Inputs (constructor): session_id (str) - the id to register under.
      id_source (str). rekeyed (bool).
    Output: an AdoptIdentity.
    Example: resolve_adopt_identity(...).rekeyed
    """

    session_id: str
    id_source: str
    rekeyed: bool


def resolve_adopt_identity(
    *,
    name: str,
    epoch: Optional[int],
    row_lookup: Callable[[str, Optional[int]], Optional[Dict[str, Any]]],
    hook_names: Dict[str, str],
) -> AdoptIdentity:
    """Decide whether an adoption re-keys to a stored id or mints a new one.

    Description: pure - no tmux, no sqlite, no event loop. The caller
      supplies the instance-triple read as ``row_lookup`` (in production
      ``session_store.get_instance``, the SAME lookup the boot re-adopt
      plan uses) so this whole decision is testable without a socket.

      Two guards ahead of the ladder, and both answer "derive" rather
      than guess. A missing epoch identifies no instance at all. A
      missing row means nothing durable describes this pane, which is
      the ordinary true-external adoption and not a failure.
    Inputs: name (str) - literal tmux session name. epoch (int | None) -
      the instance's ``#{session_created}``, as measured by the adopt
      persistence step; None when the listing could not supply one.
      row_lookup (callable) - ``(name, epoch) -> sessions row | None``.
      hook_names (dict[str, str]) - session_id -> tmux name, as restored
      by ``SessionManager._load_hook_tokens``.
    Output: AdoptIdentity.
    Example: resolve_adopt_identity(name='cloude_a', epoch=1000,
                 row_lookup=f, hook_names={'ses_1': 'cloude_a'}).session_id
             # 'ses_1'
    """
    if epoch is None:
        return AdoptIdentity(derived_id_for(name), ID_SOURCE_DERIVED, False)

    row = row_lookup(name, epoch)
    if row is None:
        return AdoptIdentity(derived_id_for(name), ID_SOURCE_DERIVED, False)

    session_id, id_source = resolve_session_id(name, row, hook_names)
    # A ladder that lands back on ``adopted:<name>`` recovered nothing,
    # whatever rung reported it. Reading ``rekeyed`` off the SOURCE
    # rather than off the string keeps that honest: a row whose
    # ``legacy_session_id`` happened to be an ``adopted:`` id is still a
    # recovered id, and a derived one is still derived.
    return AdoptIdentity(
        session_id, id_source, id_source != ID_SOURCE_DERIVED
    )
