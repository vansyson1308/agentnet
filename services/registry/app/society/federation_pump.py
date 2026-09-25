"""Federation pump: performs the network side of the Society's A2A intents.

The executor only records a pending ``a2a_outbound_calls`` row (inside the
intent's transaction). Here, in the Society worker loop and gated by
``A2A_SOCIETY_CLIENT_ENABLED`` + ``A2A_FEDERATION_ENABLED``:

* pending discover / refresh requests go through the SSRF-safe catalog;
* pending task requests are claimed and delivered ONCE (never retried);
* sent tasks are polled (reads only) until terminal;
* every outcome becomes a society event, causation-linked to the request
  event, whose payload the context builder always wraps as untrusted.

A request whose delivery was interrupted after the claim is marked failed
("interrupted, not retried"), never resent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List

from sqlalchemy.orm import Session

from ..a2a import config as a2a_config
from ..a2a.federation import catalog, client
from ..a2a.federation.fetcher import CardFetchError
from ..a2a.federation.netguard import OutboundRefused
from ..a2a.orm import A2AOutboundCall
from ..models import SocietyEvent
from .events import emit_event

logger = logging.getLogger(__name__)

BATCH = 10
INTERRUPTED_AFTER = timedelta(minutes=10)


def enabled() -> bool:
    return a2a_config.society_client_enabled() and a2a_config.federation_enabled()


def _emit_result(db: Session, call: A2AOutboundCall, event_type: str, payload: Dict) -> None:
    causation = db.query(SocietyEvent).filter(SocietyEvent.id == call.causation_id).first() if call.causation_id else None
    emit_event(
        db,
        event_type=event_type,
        payload={"outbound_call_id": str(call.id), "untrusted_external_data": True, **payload},
        actor_type="system",
        subject_type="a2a_outbound_call",
        subject_id=call.id,
        causation=causation,
        correlation_id=call.correlation_id,
        idempotency_key=f"{event_type}:{call.id}"[:160],
    )


def _ids(session_factory: Callable[[], Session], *filters) -> List[uuid.UUID]:
    db = session_factory()
    try:
        return [r[0] for r in db.query(A2AOutboundCall.id).filter(A2AOutboundCall.initiator_class == "society", *filters).order_by(A2AOutboundCall.created_at.asc()).limit(BATCH).all()]
    finally:
        db.close()


async def _discovery(session_factory: Callable[[], Session], call_id: uuid.UUID) -> None:
    db = session_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id, A2AOutboundCall.status == "pending").with_for_update(skip_locked=True).first()
        if call is None:
            db.rollback()
            return
        call.status = "sent"  # claimed
        operation, remote_agent_id = call.operation, call.remote_agent_id
        card_url = ((call.result_summary or {}).get("request") or {}).get("cardUrl")
        db.commit()
    finally:
        db.close()
    status, summary, error = "succeeded", {}, None
    try:
        if operation == "DiscoverAgent":
            view = await catalog.discover(session_factory, card_url, {"source": "society", "outbound_call_id": str(call_id)})
        else:
            view = await catalog.refresh(session_factory, remote_agent_id)
        summary = {"remoteAgentId": view["id"], "state": view["state"], "name": view["name"], "skills": [s.get("id") for s in view.get("skills", [])][:32]}
        remote_agent_id = uuid.UUID(view["id"])
    except (CardFetchError, OutboundRefused) as exc:
        status, error = "refused", getattr(exc, "result", None) or getattr(exc, "reason", "refused")
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        status, error = "failed", type(exc).__name__
    db = session_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).with_for_update().one()
        call.status = status
        call.remote_agent_id = remote_agent_id
        call.error_class = (str(error)[:64] if error else None)
        call.result_summary = {**(call.result_summary or {}), "result": summary}
        call.finished_at = datetime.now(timezone.utc)
        event = "a2a.agent.discovered" if operation == "DiscoverAgent" else "a2a.agent.refreshed"
        _emit_result(db, call, event, {"status": status, "errorClass": call.error_class, "agent": summary})
        db.commit()
    finally:
        db.close()


async def _finished(session_factory: Callable[[], Session], call_id: uuid.UUID) -> None:
    db = session_factory()
    try:
        call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).with_for_update().one()
        if call.status in ("sent", "pending"):
            db.rollback()
            return
        view = client.call_view(call)
        _emit_result(
            db, call, "a2a.task.finished",
            {"status": call.status, "remoteState": call.remote_state, "skill_id": call.skill_id, "errorClass": call.error_class, "result": view.get("result")},
        )
        db.commit()
    finally:
        db.close()


async def pump_once(session_factory: Callable[[], Session]) -> Dict[str, int]:
    """One bounded pass. Returns counts for logging/tests."""
    counts = {"discovered": 0, "sent": 0, "checked": 0, "interrupted": 0}
    if not enabled():
        return counts
    for call_id in _ids(session_factory, A2AOutboundCall.status == "pending", A2AOutboundCall.operation.in_(["DiscoverAgent", "RefreshAgent"])):
        await _discovery(session_factory, call_id)
        counts["discovered"] += 1
    for call_id in _ids(session_factory, A2AOutboundCall.status == "pending", A2AOutboundCall.operation == "SendMessage"):
        view = await client.deliver(session_factory, call_id)
        counts["sent"] += 1
        if view.get("status") not in ("sent", "pending"):
            await _finished(session_factory, call_id)
    now = datetime.now(timezone.utc)
    for call_id in _ids(session_factory, A2AOutboundCall.status == "sent", A2AOutboundCall.operation == "SendMessage"):
        db = session_factory()
        try:
            call = db.query(A2AOutboundCall).filter(A2AOutboundCall.id == call_id).first()
            orphan = call is not None and not call.remote_task_id and call.created_at and now - call.created_at > INTERRUPTED_AFTER
            if orphan:
                call.status, call.error_class, call.finished_at = "failed", "interrupted_not_retried", now
                db.commit()
                counts["interrupted"] += 1
        finally:
            db.close()
        if orphan:
            await _finished(session_factory, call_id)
            continue
        try:
            view = await client.check(session_factory, call_id)
        except Exception as exc:  # noqa: BLE001 - reads only; try again next pass
            logger.warning("a2a check failed (%s)", type(exc).__name__)
            continue
        counts["checked"] += 1
        if view.get("status") not in ("sent", "pending"):
            await _finished(session_factory, call_id)
    return counts
