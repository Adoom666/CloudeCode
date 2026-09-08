"""The failures the session-group feature raises deliberately.

WHY A MODULE OF ITS OWN. The feature is two halves - the GROUPS
(``src/core/session_group_store.py``) and the MEMBERSHIP
(``src/core/session_group_membership.py``) - and both raise the same
vocabulary. Defining it in either one makes the other import it and
closes a cycle; defining it twice is two vocabularies that drift. So it
lives here, and both import from it. ``session_group_store`` re-exports
every name, so ``store.GroupNotFound`` keeps working for the routes.
"""

from __future__ import annotations


class SessionGroupError(Exception):
    """Base for every failure this feature raises deliberately."""


class GroupNotFound(SessionGroupError):
    """No group carries the given uuid."""


class GroupNameInvalid(SessionGroupError):
    """A name was empty after trimming, or longer than the bound."""


class GroupLimitReached(SessionGroupError):
    """This install already holds ``SESSION_GROUP_MAX`` groups."""


class GroupsUnavailable(SessionGroupError):
    """The tables are not present.

    CANNOT DETERMINE, said out loud. This is raised rather than returning
    an empty list because "this install has no groups" and "this database
    predates groups / could not be read" are different facts, and a
    caller that cannot tell them apart will render the second as the
    first - which is the false green this project keeps removing. The
    route turns it into a distinct status, never into ``[]``.
    """


class SessionNotStored(SessionGroupError):
    """A tmux name resolves to no ``sessions`` row, so it has no durable key.

    THE ONE BEHAVIOUR CHANGE OF THE v24 RE-KEY, AND IT IS DELIBERATE.
    While membership keyed on the tmux name, a name with no row behind it
    could still be filed; keyed on ``sessions.session_uuid`` it cannot,
    because there is no durable identity to file it under.

    That is raised rather than swallowed. A membership silently dropped
    is a group the user watched himself make that is gone after a reload,
    which is the exact failure ``session_groups_routes`` refuses to
    answer 200 to. Measured on the owner's live database 2026-09-08 the
    condition does not currently occur: all 19 filed names resolve, and
    ``session_import_promote`` writes an ``observed`` row for every
    session on our socket, so a sidebar row without one is the exception
    and not the rule.
    """
