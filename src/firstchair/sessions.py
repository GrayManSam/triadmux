"""Lead session IDs: who started a run, a score, or a delegate job.

Several lead sessions can share one checkout and one state directory. The ID
is a guard against one session acting on another's work by mistake. It is
not a secret and it is not access control.
"""

import os
import re
import secrets

SESSION_ENV = "FIRSTCHAIR_SESSION"
SESSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def resolve_session(given: str | None, *, create: bool = False) -> str | None:
    """The ID from ``given``, else from FIRSTCHAIR_SESSION.

    A command that starts work makes a new ID when none was given. A command
    that acts on existing work never invents one.
    """
    value = given or os.environ.get(SESSION_ENV)
    if value:
        if not SESSION_PATTERN.fullmatch(value):
            raise ValueError("session must be 1-63 characters of a-z, 0-9, '.', '_' or '-'")
        return value
    return "s-" + secrets.token_hex(6) if create else None


def require_owner(owner: object, session: str | None, what: str) -> None:
    """Refuse to act on work that another lead session started.

    Work recorded before sessions existed has no owner and is not refused.
    """
    if owner is None:
        return
    if not isinstance(owner, str) or not SESSION_PATTERN.fullmatch(owner):
        raise ValueError(f"{what} has an unreadable session ID")
    if session != owner:
        raise ValueError(f"{what} belongs to session {owner}; pass --session {owner} to act on it")
