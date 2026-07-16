"""Database-backed runtime allocation with slot-level locking."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists
from sqlalchemy.orm import Session, joinedload

from .config import RUNTIME_HEARTBEAT_TTL_SECONDS
from .managed_models import Lease, LeaseStatus, Runtime, RuntimeSlot, RuntimeStatus


class NoRuntimeAvailable(RuntimeError):
    pass


def _matches_repository(repository: str, scopes: list[str]) -> bool:
    return any(scope == "*" or fnmatch.fnmatchcase(repository, scope) for scope in scopes)


def _permissions_satisfy(runtime_permissions: dict, required_permissions: dict) -> bool:
    for key, required in required_permissions.items():
        actual = runtime_permissions.get(key)
        if isinstance(required, list):
            if not isinstance(actual, list) or not set(required).issubset(set(actual)):
                return False
        elif actual != required:
            return False
    return True


def allocate_runtime_slot(
    db: Session,
    *,
    role: str,
    capability: str,
    repository: str,
    requirements: dict,
    required_runtime_id=None,
) -> tuple[Runtime, RuntimeSlot]:
    """Lock and return one eligible slot.

    The caller must create its Lease before committing. PostgreSQL's
    ``FOR UPDATE SKIP LOCKED`` makes concurrent allocators choose different
    slots; partial unique indexes remain the final invariant guard.
    """

    healthy_after = datetime.now(timezone.utc) - timedelta(seconds=RUNTIME_HEARTBEAT_TTL_SECONDS)
    query = (
        db.query(RuntimeSlot)
        .join(Runtime, Runtime.id == RuntimeSlot.runtime_id)
        .options(joinedload(RuntimeSlot.runtime))
        .filter(
            RuntimeSlot.enabled.is_(True),
            Runtime.status == RuntimeStatus.ONLINE,
            Runtime.role == role,
            Runtime.last_heartbeat_at >= healthy_after,
            ~exists().where(
                Lease.runtime_slot_id == RuntimeSlot.id,
                Lease.state.in_([LeaseStatus.OFFERED, LeaseStatus.ACKNOWLEDGED, LeaseStatus.ACTIVE]),
            ),
        )
    )
    if required_runtime_id is not None:
        query = query.filter(Runtime.id == required_runtime_id)

    candidates = query.order_by(
        Runtime.error_count, Runtime.timeout_count, Runtime.id, RuntimeSlot.slot_number
    ).with_for_update(of=RuntimeSlot, skip_locked=True)
    for slot in candidates:
        runtime = slot.runtime
        capabilities = runtime.capabilities or []
        if capability not in capabilities and "*" not in capabilities:
            continue
        if not _matches_repository(repository, runtime.repository_scopes or []):
            continue
        if requirements.get("adapter") and requirements["adapter"] != runtime.adapter:
            continue
        if requirements.get("model") and requirements["model"] != runtime.model:
            continue
        if requirements.get("provider") and requirements["provider"] != runtime.provider:
            continue
        if not _permissions_satisfy(runtime.permissions or {}, requirements.get("permissions", {})):
            continue
        return runtime, slot
    raise NoRuntimeAvailable("no healthy runtime slot satisfies the managed execution requirements")
