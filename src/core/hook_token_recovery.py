"""One-shot recovery for a hook token this server superseded under a live agent.

THE DEFECT THIS EXISTS TO FIX, measured on the owner's box 2026-09-08.
``SessionManager._mint_hook_token`` REPLACES the token held for a session
id. The same token was baked into the tmux pane's environment at
``new-session`` time and is read from there by the agent at hook-fire
time, so it is fixed for the life of that process and CANNOT be re-issued
to it. Mint over a live agent and every hook it sends is answered 403,
forever, with no retry available from its side.

The measured incident, to the millisecond. At 16:16:40.633984Z an adopt
registered the live pane ``cloude_Agent_-_Cloude_Code`` under the derived
id ``adopted:cloude_Agent_-_Cloude_Code`` and minted a token for it. At
16:16:40.763005Z - 130 ms later - the first
``hook_post_rejected_invalid_token`` for that id was logged. 4,325 more
followed over the next four hours and twenty-four minutes, ending only
when the owner restarted the pane by hand at 20:40:23Z and a new process
inherited the current environment. Before 16:16:40 the same id was being
accepted: ``toast_recorded`` at 16:11:28Z and 16:12:56Z are hooks that
passed validation. Nothing about any of it is visible from inside the
pane.

WHY A RING AND NOT A LOOSENED COMPARISON. The token the agent presents is
not wrong - it is the token THIS SERVER MINTED FOR THAT PANE and then
replaced. So the recovery is not "accept something unverified", it is
"recognise our own superseded credential and correct the record to what
the running process actually holds". Nothing else is admissible:

  1. Only a token this process itself superseded for THAT session id can
     match. An arbitrary old value, a guess, or another session's token
     matches nothing.
  2. The superseded entry's tmux name must equal the name currently bound
     to the id. One pane, one credential; a token superseded while bound
     to a different pane is not evidence about this one.
  3. Acceptance CONSUMES the entry and re-binds the store to the token
     the process holds. It is a correction, not a second valid
     credential - the next hook validates through the ordinary path.
  4. The ring is bounded, so credential material cannot accumulate for
     the life of the install.
  5. NOTHING IS EVER MINTED HERE. Minting is what caused this.

The loopback check in front of the hook route is untouched and remains
the layer that matters against anything off-box.

IN MEMORY ONLY, DELIBERATELY, and this is a KNOWN BOUND rather than an
oversight. A superseded token is recoverable only while the process that
superseded it is still running. A server restart drops the ring, which is
correct on both counts: writing superseded credentials to disk would
widen their exposure window past the mistake they exist to correct, and a
restart already has its own answer - ``_load_hook_tokens`` restores the
live token and the boot re-adopt re-keys the pane to the id its agent
presents (``ID_SOURCE_HOOK_TOKEN``). What this cannot repair is a mint
followed by a restart, and that case degrades to the pre-existing
behaviour rather than to anything worse.

THREE OUTCOMES, because "I found no match" and "I had nothing to look in"
are different claims and only the first is evidence. Both refuse; only
one of them says anything about the token presented.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "RECOVERY_ACCEPTED",
    "RECOVERY_NO_MATCH",
    "RECOVERY_UNAVAILABLE",
    "SUPERSEDED_RING_SIZE",
    "RecoveryDecision",
    "SupersededHookTokens",
]

#: A token this server minted for this id, superseded by a later mint,
#: still bound to the same pane, and presented by a process that has no
#: way to have been handed the replacement. Accept once and re-bind.
RECOVERY_ACCEPTED = "accepted"

#: There WAS a ring to search for this id and the presented token is not
#: in it. This is a real negative: the token is not one of ours.
RECOVERY_NO_MATCH = "no_match"

#: Nothing could be evaluated - no superseded token is held for this id,
#: or the pane binding needed to scope the check is unknown. NOT a
#: statement about the token. Refuses exactly as ``no_match`` does; the
#: two are kept apart so a log line cannot claim a check that never ran.
RECOVERY_UNAVAILABLE = "unavailable"

#: How many superseded tokens are retained per session id. Small on
#: purpose: this recovers a mint that landed under a running agent, and
#: a pane accumulating five of those has a different problem.
SUPERSEDED_RING_SIZE = 4


@dataclass(frozen=True)
class RecoveryDecision:
    """What a recovery attempt concluded, and what to do about it.

    Description: ``outcome`` is one of the ``RECOVERY_*`` constants.
      ``token`` is set ONLY on ``RECOVERY_ACCEPTED`` and is the value the
      store must be re-bound to - the one the running process actually
      holds. ``tmux_name`` is the pane the decision was scoped to, for
      the log line; None when no binding was known.
    Inputs (constructor): outcome (str), token (str | None),
      tmux_name (str | None).
    Output: a RecoveryDecision.
    Example: decision.outcome == RECOVERY_ACCEPTED
    """

    outcome: str
    token: Optional[str] = None
    tmux_name: Optional[str] = None

    @property
    def accepted(self) -> bool:
        """Whether the caller may treat this hook as authenticated."""
        return self.outcome == RECOVERY_ACCEPTED


class SupersededHookTokens:
    """A bounded per-session ring of tokens this server minted then replaced.

    Description: the mutable half of the recovery. Kept out of
      ``SessionManager`` so the rule can be tested without a manager, a
      socket or an event loop, and so the manager file does not grow.

      NEVER LOGS OR RETURNS A TOKEN except to the caller that must
      re-bind it. There is no accessor that dumps the ring.
    Inputs (constructor): ring_size (int) - entries retained per id.
    Output: a SupersededHookTokens.
    Example: ring = SupersededHookTokens(); ring.record('ses_a', old,
             tmux_name='cloude_a')
    """

    def __init__(self, ring_size: int = SUPERSEDED_RING_SIZE) -> None:
        if ring_size < 1:
            raise ValueError("ring_size must be at least 1")
        self._ring_size = ring_size
        # session_id -> ordered [(token, tmux_name)], oldest first.
        self._rings: Dict[str, List[Tuple[str, Optional[str]]]] = {}

    def record(
        self,
        session_id: str,
        token: str,
        *,
        tmux_name: Optional[str],
    ) -> None:
        """Remember a token that has just been replaced for ``session_id``.

        Description: called by the minting path with the token it is
          about to overwrite, together with the pane that token was bound
          to at the time. A blank id or token records nothing - there is
          no such credential to recover. Duplicates are collapsed rather
          than stacked, so a repeated mint of the same value cannot
          consume the whole ring.
        Inputs: session_id (str). token (str) - the value being replaced.
          tmux_name (str | None) - the pane it was bound to.
        Output: None.
        Example: ring.record('ses_a', previous, tmux_name='cloude_a')
        """
        if not session_id or not token:
            return
        entries = self._rings.setdefault(session_id, [])
        entries[:] = [e for e in entries if e[0] != token]
        entries.append((token, tmux_name))
        if len(entries) > self._ring_size:
            del entries[: len(entries) - self._ring_size]

    def decide(
        self,
        session_id: str,
        presented: str,
        *,
        current_tmux_name: Optional[str],
    ) -> RecoveryDecision:
        """Judge a token the ordinary validation has already rejected.

        Description: pure with respect to the caller's state - it reads
          the ring and answers, changing nothing. ``consume`` is the
          separate step that applies the decision, so a caller that
          decides not to act leaves the ring untouched.

          The pane scope is a REFUSAL, not a relaxation: an entry whose
          recorded tmux name disagrees with the name currently bound to
          the id describes a different pane and is skipped. An unknown
          binding on either side yields ``RECOVERY_UNAVAILABLE``, because
          a check that could not be scoped is not a check that passed.
        Inputs: session_id (str). presented (str) - the token from the
          request header. current_tmux_name (str | None) - the name the
          store currently binds to this id.
        Output: RecoveryDecision.
        Example: ring.decide('ses_a', tok, current_tmux_name='cloude_a')
        """
        if not session_id or not presented:
            return RecoveryDecision(RECOVERY_UNAVAILABLE)
        entries = self._rings.get(session_id)
        if not entries:
            return RecoveryDecision(RECOVERY_UNAVAILABLE)
        if not current_tmux_name:
            # We hold superseded tokens but cannot tell which pane the id
            # is on, so nothing can be scoped. Say that, do not guess.
            return RecoveryDecision(RECOVERY_UNAVAILABLE)

        scoped = [e for e in entries if e[1] == current_tmux_name]
        if not scoped:
            return RecoveryDecision(
                RECOVERY_UNAVAILABLE, tmux_name=current_tmux_name
            )

        for token, name in scoped:
            # Constant time, same as ``validate_hook_token``. The lengths
            # are equal in practice (token_urlsafe(32) throughout) but a
            # forged input of another length must not short-circuit
            # differently, so the exception path answers no-match too.
            try:
                match = hmac.compare_digest(token, presented)
            except (TypeError, ValueError):
                match = False
            if match:
                return RecoveryDecision(
                    RECOVERY_ACCEPTED, token=token, tmux_name=name
                )
        return RecoveryDecision(
            RECOVERY_NO_MATCH, tmux_name=current_tmux_name
        )

    def consume(self, session_id: str, token: str) -> bool:
        """Drop an accepted entry so the recovery cannot fire twice.

        Description: this is what makes the acceptance ONE-SHOT. Once the
          store has been re-bound, the token the process holds is the
          live one and validates through the ordinary path; leaving the
          entry in the ring would keep a second credential alive for no
          benefit. Returns whether anything was removed, so a caller can
          tell a real consumption from a duplicate that lost the race.
        Inputs: session_id (str). token (str) - the accepted value.
        Output: bool - True when an entry was removed.
        Example: ring.consume('ses_a', accepted_token)
        """
        entries = self._rings.get(session_id)
        if not entries:
            return False
        remaining = [e for e in entries if e[0] != token]
        removed = len(remaining) != len(entries)
        if remaining:
            entries[:] = remaining
        else:
            self._rings.pop(session_id, None)
        return removed

    def forget(self, session_id: str) -> None:
        """Drop every superseded token for one id (session teardown)."""
        self._rings.pop(session_id, None)

    def size(self, session_id: str) -> int:
        """How many superseded tokens are held for an id. Never the values."""
        return len(self._rings.get(session_id) or ())
