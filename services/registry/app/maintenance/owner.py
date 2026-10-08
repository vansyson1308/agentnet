"""Owner decisions on the Maintenance OS (ADR-0010 D19). Operator-only callers.

An owner decision RESUMES the persisted case -- the model is never re-called
to "continue": the next step is whatever the persisted state says. Every
decision is audited as an OWNER transition and counted as toil.

* resume   -- a resumable SAFELY_ESCALATED case continues where it stopped:
              ``promotion_disabled``/``promotion_provider_unavailable`` ->
              PROMOTING (the READY candidate is promoted); ``release_disabled``
              -> READY_FOR_RELEASE. ``owner_approval_required`` resumes by
              itself when the owner merges the PR (reconciler resume sweep).
* refuse   -- a resumable escalation becomes final; nothing else happens.
* freeze / lift -- production release freezes (``owner_only`` freezes, e.g.
              a failed rollback, can only be lifted here).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CodeCandidate
from ..society.config import get_settings as get_society_settings
from ..society.events import utcnow
from . import state_machine as sm
from .bridge import request_promotion
from .kpi import record_toil
from .ledger import digest, merge_facts, transition
from .orm import MaintenanceReleaseFreeze, RepairCase
from .taxonomy import ActorType

RESUME_TARGET = {
    "promotion_disabled": sm.CaseState.PROMOTING,
    "promotion_provider_unavailable": sm.CaseState.PROMOTING,
    "release_disabled": sm.CaseState.READY_FOR_RELEASE,
}


class OwnerActionError(ValueError):
    pass


def resume(db: Session, case: RepairCase, *, owner: str, note: str = "", now: Optional[datetime] = None) -> RepairCase:
    now = now or utcnow()
    if case.state != sm.CaseState.SAFELY_ESCALATED.value or not case.resumable:
        raise OwnerActionError("only a resumable escalation can be resumed")
    reason = (case.terminal_reason or "").split(":")[0]
    target = RESUME_TARGET.get(reason)
    if target is None:
        raise OwnerActionError(f"{reason!r} resumes when its evidence changes (e.g. the owner merges the PR), not by request")
    detail = {"owner": owner, "note": note[:300], "resumed_from": reason}
    if target is sm.CaseState.PROMOTING and case.promotion_id is None:
        cand = db.get(CodeCandidate, case.candidate_id) if case.candidate_id else None
        if cand is None:
            raise OwnerActionError("no READY candidate to promote")
        promo = request_promotion(db, society_settings=get_society_settings(), case=case, candidate=cand)
        case.promotion_id = promo.id
    if target is sm.CaseState.READY_FOR_RELEASE:
        merge_facts(case, owner_release_resume=detail)
    transition(db, case, target, actor=ActorType.OWNER, actor_id=f"owner:{owner}"[:128], reason=f"owner_resume:{reason}"[:64], now=now, detail=detail, evidence_digest=digest(detail))
    record_toil(db, "owner_approval", actor=owner, case_id=case.id, detail=detail, now=now)
    return case


def refuse(db: Session, case: RepairCase, *, owner: str, note: str = "", now: Optional[datetime] = None) -> RepairCase:
    now = now or utcnow()
    if case.state != sm.CaseState.SAFELY_ESCALATED.value or not case.resumable:
        raise OwnerActionError("only a resumable escalation can be refused")
    case.resumable = False
    case.escalation = {**(case.escalation or {}), "owner_refusal": {"owner": owner, "note": note[:300], "at": now.isoformat()}}
    record_toil(db, "owner_refusal", actor=owner, case_id=case.id, detail={"note": note[:300]}, now=now)
    return case


def open_release_freeze(db: Session, *, owner: str, reason: str, now: Optional[datetime] = None) -> MaintenanceReleaseFreeze:
    fr = MaintenanceReleaseFreeze(id=uuid.uuid4(), reason_code="owner_freeze", detail={"reason": reason[:300]}, owner_only=True, opened_by=f"owner:{owner}"[:128], opened_at=now or utcnow())
    db.add(fr)
    return fr


def lift_release_freeze(db: Session, freeze: MaintenanceReleaseFreeze, *, owner: str, reason: str, now: Optional[datetime] = None) -> MaintenanceReleaseFreeze:
    if freeze.lifted_at is not None:
        raise OwnerActionError("already lifted")
    freeze.lifted_at = now or utcnow()
    freeze.lifted_by = f"owner:{owner}"[:128]
    freeze.lift_reason = reason[:255]
    record_toil(db, "freeze_lift", actor=owner, detail={"freeze_id": str(freeze.id), "reason_code": freeze.reason_code}, now=now)
    return freeze


__all__ = ["resume", "refuse", "open_release_freeze", "lift_release_freeze", "OwnerActionError", "RESUME_TARGET"]
