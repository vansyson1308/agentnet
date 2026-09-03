"""Server-enforced operator authorization for the society runtime.

One dependency, one source of truth:

* ``users.society_role`` (durable; ``operator`` or ``event_producer``),
  assigned by an existing operator through ``POST /v1/society/operators``
  or the ``python -m app.society.operator`` CLI — never by an agent intent.
* ``SOCIETY_OPERATOR_BOOTSTRAP_EMAILS`` — an env-backed allowlist used ONLY
  to bootstrap the first operator on a fresh deployment (empty by default).
  Once a durable operator exists the allowlist can be cleared; it is read
  here and nowhere else.

Only *user JWTs* confer operator authority. Scoped ``spt_`` tokens resolve
to an agent's owner in ``get_current_user`` and are explicitly rejected
here: they can be minted per agent and must never escalate to operator.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Set

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import oauth2_scheme, verify_token
from ..database import get_db
from ..models import SocietyUserRole, User

logger = logging.getLogger(__name__)


def bootstrap_operator_emails() -> Set[str]:
    raw = os.getenv("SOCIETY_OPERATOR_BOOTSTRAP_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def user_society_role(user: User) -> Optional[str]:
    """Effective role: durable column first, bootstrap allowlist second."""
    role = getattr(user, "society_role", None)
    if role:
        return str(role)
    if user.email and user.email.lower() in bootstrap_operator_emails():
        return SocietyUserRole.OPERATOR.value
    return None


def is_operator(user: User) -> bool:
    return user_society_role(user) == SocietyUserRole.OPERATOR.value


def is_event_producer(user: User) -> bool:
    return user_society_role(user) in (SocietyUserRole.OPERATOR.value, SocietyUserRole.EVENT_PRODUCER.value)


def _user_from_user_jwt(token: str, db: Session) -> User:
    """Resolve a user from a *user* JWT only. Agent JWTs and scoped tokens
    are refused for operator surfaces."""
    token_data = verify_token(token, db=db)
    if token_data.scoped_token_id is not None or token_data.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator surfaces require a user session token",
        )
    user = db.query(User).filter(User.id == token_data.user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
    return user


async def require_operator(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    user = _user_from_user_jwt(token, db)
    if not is_operator(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="society operator role required")
    return user


async def require_event_producer(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    user = _user_from_user_jwt(token, db)
    if not is_event_producer(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="society operator or event_producer role required to inject world events",
        )
    return user


def assign_role(db: Session, *, email: str, role: Optional[str], actor: Optional[User] = None) -> User:
    """Set or clear a user's society role. ``role`` must be a SocietyUserRole
    value or None. Commits."""
    if role is not None:
        role = SocietyUserRole(role).value
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise ValueError(f"no user with email {email!r}")
    user.society_role = role
    db.commit()
    logger.info("society role %s -> %s by %s", email, role, actor.email if actor else "cli")
    return user


def main() -> None:  # pragma: no cover — CLI
    import argparse
    import logging as _logging

    from ..database import SessionLocal

    _logging.basicConfig(level=_logging.INFO)
    parser = argparse.ArgumentParser(description="Grant/revoke society roles (operator | event_producer | none)")
    parser.add_argument("email")
    parser.add_argument("role", choices=["operator", "event_producer", "none"])
    args = parser.parse_args()
    db = SessionLocal()
    try:
        assign_role(db, email=args.email, role=None if args.role == "none" else args.role)
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
