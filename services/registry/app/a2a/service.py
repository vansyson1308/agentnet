"""A2A operations over the durable store and the existing escrow API.

Synchronous functions taking a SQLAlchemy ``Session``; ``handler.py`` runs
them in a worker thread. Authorization is decided here, per operation
(ADR-0009 D6), and money moves only through ``authz.reserve_scoped_spend``,
``task_service.create_task_with_escrow`` and
``task_service.cancel_task_with_refund`` (ADR-0009 D9, D11). This module
never imports ``Wallet``.

Lock order, everywhere: the A2A task row, then (inside task_service) the
TaskSession / transaction / wallet rows. Nothing locks an A2A row while
holding a TaskSession lock, so no cycle exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import (
    ExtensionSupportRequiredError,
    InvalidParamsError,
    InvalidRequestError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import authz
from ..models import Agent, TaskSession
from ..task_service import EscrowError, TaskNotCancelable, cancel_task_with_refund, create_task_with_escrow
from ..task_dispatch import build_execute_message
from . import cards, config, mapping, network_skills, reconciler, store
from .auth import A2APrincipal, load_paying_agent, scoped_token_allows
from .orm import A2ATask

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600


@dataclass
class CallInfo:
    """What the gateway learned about the call (no secrets)."""

    principal: A2APrincipal
    tenant: str
    binding: str
    extensions: Set[str] = field(default_factory=set)
    request_id: Optional[str] = None
    base_url: str = ""


@dataclass
class DispatchPlan:
    task_session_id: uuid.UUID
    callee_agent_id: uuid.UUID
    callee_endpoint: Optional[str]
    message: Dict[str, Any]


@dataclass
class SendResult:
    message: Optional[pb.Message] = None
    task_id: Optional[uuid.UUID] = None
    needs_escrow: bool = False
    history_length: Optional[int] = None
    return_immediately: bool = False


# ── helpers ────────────────────────────────────────────────────────────────


def _uuid_or_none(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _tenant_uuid(tenant: str) -> Optional[uuid.UUID]:
    return _uuid_or_none(tenant) if tenant else None


def _visible(task: Optional[A2ATask], principal: A2APrincipal, tenant: str) -> bool:
    """Tenant match AND party. Anything else is indistinguishable from a
    missing task (no existence oracle)."""
    if task is None:
        return False
    if task.tenant_agent_id is None or task.tenant_agent_id != _tenant_uuid(tenant):
        return False
    return task.caller_agent_id in principal.agent_ids or task.tenant_agent_id in principal.agent_ids


def _visible_task(db: Session, principal: A2APrincipal, tenant: str, task_id: str, *, lock: bool = False) -> A2ATask:
    tid = _uuid_or_none(task_id)
    if tid is None or _tenant_uuid(tenant) is None:
        raise TaskNotFoundError()
    task = store.lock_task(db, tid) if lock else store.load_task(db, tid)
    if not _visible(task, principal, tenant):
        raise TaskNotFoundError()
    return task  # type: ignore[return-value]


def _history_length(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if value < 0:
        raise InvalidParamsError(message="historyLength must not be negative")
    return min(value, config.MAX_HISTORY)


def _audit(db: Session, call: CallInfo, operation: str, result: str, **kw: Any) -> None:
    store.audit(
        db,
        principal_class=call.principal.kind,
        principal_id=call.principal.principal_id,
        operation=operation,
        result=result,
        request_id=call.request_id,
        **kw,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _federation_depth(metadata: Dict[str, Any]) -> int:
    fed = metadata.get(config.FEDERATION_EXTENSION_URI)
    if fed is None:
        return 0
    if not isinstance(fed, dict):
        raise InvalidParamsError(message="federation metadata must be an object")
    try:
        depth = int(fed.get("depth", 0))
    except (TypeError, ValueError):
        raise InvalidParamsError(message="federation depth must be an integer")
    if depth < 0:
        raise InvalidParamsError(message="federation depth must not be negative")
    return depth


def _choose_capability(agent: Agent, metadata: Dict[str, Any], econ: Dict[str, Any]) -> Dict[str, Any]:
    caps = [c for c in (agent.capabilities or []) if c.get("name")]
    named = [v for v in (metadata.get("skillId"), econ.get("skillId")) if v]
    if len(set(named)) > 1:
        raise InvalidParamsError(message="skillId differs between message metadata and the economics extension")
    if named:
        for cap in caps:
            if cap.get("name") == named[0]:
                return cap
        raise InvalidParamsError(message="unknown skillId for this agent")
    if len(caps) == 1:
        return caps[0]
    raise InvalidParamsError(message="this agent has several skills: set message.metadata.skillId")


def _economic_terms(price: int, activated: bool, econ: Dict[str, Any]) -> Dict[str, Any]:
    """The escrow terms for one task. A paid skill without the activated
    extension is refused (nothing is ever charged silently)."""
    if price > 0 and not (activated and econ):
        raise ExtensionSupportRequiredError(
            message=(
                "This skill is paid: activate the AgentNet economics extension (A2A-Extensions header) "
                "and set message.metadata[<uri>] = {maxBudget, currency}."
            ),
            data={"uri": config.ECONOMICS_EXTENSION_URI, "price": price},
        )
    terms: Dict[str, Any] = {"price": price, "maxBudget": 0, "currency": "credits", "activated": bool(activated and econ)}
    if terms["activated"]:
        try:
            terms["maxBudget"] = int(econ.get("maxBudget"))
        except (TypeError, ValueError):
            raise InvalidParamsError(message="economics maxBudget must be an integer")
        if terms["maxBudget"] < 0:
            raise InvalidParamsError(message="economics maxBudget must not be negative")
        currency = str(econ.get("currency", "credits")).lower()
        if currency not in ("credits", "usdc"):
            raise InvalidParamsError(message="economics currency must be credits or usdc")
        terms["currency"] = currency
        if econ.get("quotedPrice") is not None:
            try:
                terms["quotedPrice"] = int(econ["quotedPrice"])
            except (TypeError, ValueError):
                raise InvalidParamsError(message="economics quotedPrice must be an integer")
        if econ.get("timeoutSeconds") is not None:
            try:
                terms["timeoutSeconds"] = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, int(econ["timeoutSeconds"])))
            except (TypeError, ValueError):
                raise InvalidParamsError(message="economics timeoutSeconds must be an integer")
    return terms


# ── SendMessage / SendStreamingMessage ────────────────────────────────────


def send_begin(db: Session, call: CallInfo, req: pb.SendMessageRequest) -> SendResult:
    """Validate, then answer (network tenant) or create/replay the A2A task."""
    message = req.message
    mapping.validate_inbound_message(message)
    cfg = req.configuration
    if cfg.HasField("task_push_notification_config"):
        raise PushNotificationNotSupportedError()
    if cfg.accepted_output_modes and not set(cfg.accepted_output_modes) & set(mapping.OUTPUT_MODES):
        from a2a.utils.errors import ContentTypeNotSupportedError

        raise ContentTypeNotSupportedError(message="AgentNet produces application/json and text/plain only")
    history_length = _history_length(cfg.history_length if cfg.HasField("history_length") else None)
    metadata = mapping.message_metadata(message)
    depth = _federation_depth(metadata)
    if depth >= config.max_federation_depth():
        raise InvalidRequestError(message="federation depth limit reached; AgentNet will not accept further delegation")

    if message.task_id:
        task = _visible_task(db, call.principal, call.tenant, message.task_id)
        if mapping.is_terminal(task.state):
            raise UnsupportedOperationError(message="the task is in a terminal state")
        raise UnsupportedOperationError(message="AgentNet tasks do not accept follow-up messages")

    if not call.tenant:
        answer = network_skills.answer(db, message, metadata, call.base_url)
        return SendResult(message=answer)

    tenant_id = _tenant_uuid(call.tenant)
    tenant_agent = db.query(Agent).filter(Agent.id == tenant_id).first() if tenant_id else None
    if not cards.card_available(tenant_agent):
        raise InvalidParamsError(message="unknown or unavailable tenant")
    if call.principal.kind != "agent":
        raise InvalidRequestError(
            message="creating a task needs an agent credential (agent JWT or spt_ token with execute); the caller agent pays"
        )
    if not scoped_token_allows(db, call.principal, authz.ACTION_EXECUTE):
        raise InvalidRequestError(message="this credential does not allow the execute action")

    econ_raw = metadata.get(config.ECONOMICS_EXTENSION_URI) or {}
    if not isinstance(econ_raw, dict):
        raise InvalidParamsError(message="economics metadata must be an object")
    capability = _choose_capability(tenant_agent, metadata, econ_raw)
    price = cards.skill_price(capability)
    terms = _economic_terms(price, config.ECONOMICS_EXTENSION_URI in call.extensions, econ_raw)
    input_data = mapping.extract_input(message)

    if message.context_id:
        context_id = _uuid_or_none(message.context_id)
        if context_id is None:
            raise InvalidParamsError(message="contextId must be a UUID")
        others = db.query(A2ATask.caller_agent_id, A2ATask.tenant_agent_id).filter(A2ATask.context_id == context_id).limit(50).all()
        if others and not any(c in call.principal.agent_ids or t in call.principal.agent_ids for c, t in others):
            raise InvalidParamsError(message="contextId is not available")
    else:
        context_id = uuid.uuid4()

    key = hashlib.sha256(f"{call.principal.key}|{message.message_id}".encode()).hexdigest()
    request_hash = _canonical_hash(
        {"tenant": str(tenant_id), "skill": capability["name"], "input": input_data, "terms": terms, "context": message.context_id or None}
    )
    existing = db.query(A2ATask).filter(A2ATask.idempotency_key == key).first()
    if existing is not None:
        return _replay(db, call, existing, request_hash, history_length, cfg.return_immediately)

    task = A2ATask(
        id=uuid.uuid4(),
        context_id=context_id,
        tenant_agent_id=tenant_id,
        caller_agent_id=call.principal.agent_id,
        skill_id=capability["name"][:128],
        state=mapping.SUBMITTED,
        protocol_version=config.PROTOCOL_VERSION,
        binding=call.binding,
        economics=terms,
        metadata_={"requestHash": request_hash},
        idempotency_key=key,
        federation_depth=depth,
        last_event_seq=0,
    )
    stored = pb.Message()
    stored.CopyFrom(message)
    stored.context_id = str(context_id)
    stored.task_id = str(task.id)
    try:
        db.add(task)
        db.flush()
        store.add_message(db, task, stored)
        store.set_state(db, task, mapping.SUBMITTED)
        _audit(db, call, "SendMessage", "created", target_agent_id=tenant_id, a2a_task_id=task.id,
               economics_action="paid" if price > 0 else "free")
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(A2ATask).filter(A2ATask.idempotency_key == key).first()
        if existing is None:
            raise
        return _replay(db, call, existing, request_hash, history_length, cfg.return_immediately)
    return SendResult(task_id=task.id, needs_escrow=True, history_length=history_length, return_immediately=cfg.return_immediately)


def _replay(db: Session, call: CallInfo, task: A2ATask, request_hash: str, history_length, return_immediately) -> SendResult:
    if (task.metadata_ or {}).get("requestHash") != request_hash or task.tenant_agent_id != _tenant_uuid(call.tenant):
        raise InvalidRequestError(message="messageId was already used for a different request")
    needs_escrow = task.state == mapping.SUBMITTED and task.task_session_id is None
    return SendResult(task_id=task.id, needs_escrow=needs_escrow, history_length=history_length, return_immediately=return_immediately)


def _reject_locked(db: Session, task: A2ATask, reason: str, text: str) -> None:
    if mapping.is_terminal(task.state) or task.task_session_id is not None:
        return
    msg = mapping.agent_message(str(task.id), str(task.context_id), f"rejected:{reason}", text[:500], {"reason": reason})
    store.set_state(db, task, mapping.REJECTED, msg)


def escrow_and_link(db: Session, call: CallInfo, task_id: uuid.UUID) -> Optional[DispatchPlan]:
    """Reserve escrow for a SUBMITTED A2A task and link its TaskSession.

    The A2A row stays locked until ``create_task_with_escrow`` commits, so a
    concurrent CancelTask either runs before (and the task is canceled with
    nothing reserved) or after (and finds the TaskSession by its idempotency
    key and refunds it). Returns the dispatch plan, or ``None`` when there is
    nothing to deliver."""
    task = store.lock_task(db, task_id)
    if task is None or task.state != mapping.SUBMITTED or task.task_session_id is not None:
        db.rollback()
        return None
    terms = dict(task.economics or {})
    escrow_key = reconciler.escrow_idempotency_key(task.idempotency_key)
    first = store.first_user_message(db, task.id)
    input_data = mapping.extract_input(first) if first is not None else {}

    if reconciler.link_session_by_key(db, task) is None:
        quoted = terms.get("quotedPrice")
        if quoted is not None and int(quoted) != int(terms.get("price", 0)):
            _reject_locked(db, task, "price_changed", "The skill's price differs from the quoted price; nothing was reserved.")
            _audit(db, call, "SendMessage", "rejected", target_agent_id=task.tenant_agent_id, a2a_task_id=task.id, economics_action="refused")
            db.commit()
            return None
        try:
            agent = load_paying_agent(db, call.principal)
            authz.reserve_scoped_spend(db, agent, int(terms.get("maxBudget", 0)))
            session, _tx = create_task_with_escrow(
                db=db,
                caller_agent=agent,
                callee_agent_id=task.tenant_agent_id,
                capability_name=task.skill_id,
                input_data=input_data,
                max_budget=int(terms.get("maxBudget", 0)),
                currency=terms.get("currency", "credits"),
                timeout_seconds=int(terms.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
                idempotency_key=escrow_key,
            )
        except (EscrowError, HTTPException) as exc:
            db.rollback()
            reason = "escrow_refused" if isinstance(exc, EscrowError) else "credential_refused"
            text = str(exc) if isinstance(exc, EscrowError) else str(getattr(exc, "detail", "refused"))
            task = store.lock_task(db, task_id)
            if task is not None:
                _reject_locked(db, task, reason, f"AgentNet refused the task before reserving any budget: {text}")
                _audit(db, call, "SendMessage", "rejected", target_agent_id=task.tenant_agent_id, a2a_task_id=task.id, economics_action="refused")
            db.commit()
            return None
        # create_task_with_escrow committed (and released the A2A row lock).
        task = store.lock_task(db, task_id)
        if task is None:
            db.rollback()
            return None
        if task.task_session_id is None:
            task.task_session_id = session.id
        linked = session
    else:
        linked = db.query(TaskSession).filter(TaskSession.id == task.task_session_id).first()

    if mapping.is_terminal(task.state) or linked is None:
        db.commit()
        return None
    _audit(db, call, "SendMessage", "escrow_reserved", target_agent_id=task.tenant_agent_id, a2a_task_id=task.id,
           task_session_id=linked.id, economics_action="reserve")
    callee = db.query(Agent).filter(Agent.id == linked.callee_agent_id).first()
    plan = DispatchPlan(
        task_session_id=linked.id,
        callee_agent_id=linked.callee_agent_id,
        callee_endpoint=callee.endpoint if callee else None,
        message=build_execute_message(
            task_session_id=linked.id,
            trace_id=linked.trace_id,
            caller_agent_id=linked.caller_agent_id,
            capability=linked.capability,
            input_data=input_data,
            max_budget=int(terms.get("maxBudget", 0)),
            currency=terms.get("currency", "credits"),
            timeout_seconds=int(terms.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
        ),
    )
    db.commit()
    return plan


def record_fulfillment_channel(db: Session, task_session_id: uuid.UUID, channel: Optional[str]) -> None:
    session = db.query(TaskSession).filter(TaskSession.id == task_session_id).first()
    if session is not None and session.fulfillment_channel is None:
        session.fulfillment_channel = channel
        db.commit()


# ── GetTask / SubscribeToTask snapshots ───────────────────────────────────


def get_task(db: Session, call: CallInfo, task_id: str, history_length: Optional[int]) -> Tuple[pb.Task, int, bool]:
    """Party-scoped snapshot, reconciled first. Returns (task, seq, changed)."""
    task = _visible_task(db, call.principal, call.tenant, task_id)
    changed = reconciler.reconcile_task(db, task.id)
    task = store.load_task(db, task.id)
    snap = store.snapshot(db, task, history_length=_history_length(history_length))
    seq = int(task.last_event_seq or 0)
    db.rollback()
    return snap, seq, changed


def task_by_id_for_stream(db: Session, call: CallInfo, task_id: str) -> Tuple[pb.Task, int, uuid.UUID, bool]:
    task = _visible_task(db, call.principal, call.tenant, task_id)
    reconciler.reconcile_task(db, task.id)
    task = store.load_task(db, task.id)
    terminal = mapping.is_terminal(task.state)
    snap = store.snapshot(db, task, history_length=None)
    seq = int(task.last_event_seq or 0)
    db.rollback()
    return snap, seq, task.id, terminal


def own_snapshot(db: Session, task_id: uuid.UUID, history_length: Optional[int]) -> Tuple[pb.Task, int, bool]:
    """Snapshot of a task the current request created (already authorized)."""
    reconciler.reconcile_task(db, task_id)
    task = store.load_task(db, task_id)
    snap = store.snapshot(db, task, history_length=history_length)
    seq = int(task.last_event_seq or 0)
    terminal = mapping.is_terminal(task.state)
    db.rollback()
    return snap, seq, terminal


def poll_events(db: Session, task_id: uuid.UUID, after_seq: int) -> Tuple[List[Tuple[int, str, Dict[str, Any]]], bool, bool]:
    """Reconcile, then return (events after ``after_seq``, terminal, changed)."""
    changed = reconciler.reconcile_task(db, task_id)
    events = store.events_after(db, task_id, after_seq)
    task = store.load_task(db, task_id)
    terminal = task is None or mapping.is_terminal(task.state)
    db.rollback()
    return events, terminal, changed


# ── CancelTask ─────────────────────────────────────────────────────────────


def cancel_task(db: Session, call: CallInfo, task_id: str) -> pb.Task:
    task = _visible_task(db, call.principal, call.tenant, task_id, lock=True)
    tid = task.id
    if task.caller_agent_id not in call.principal.agent_ids:
        db.rollback()
        raise TaskNotCancelableError(message="only the calling agent can cancel a task")
    if task.state == mapping.CANCELED:
        snap = store.snapshot(db, task, history_length=None)
        db.rollback()
        return snap
    if mapping.is_terminal(task.state):
        db.rollback()
        raise TaskNotCancelableError(message="the task is already in a terminal state")

    session_id = reconciler.link_session_by_key(db, task)
    if session_id is None:
        # No escrow exists and none is in flight (it would hold this lock).
        msg = mapping.agent_message(str(tid), str(task.context_id), "canceled", reconciler._CANCELED_TEXT, {"reason": "canceled"})
        store.set_state(db, task, mapping.CANCELED, msg)
        _audit(db, call, "CancelTask", "canceled", target_agent_id=task.tenant_agent_id, a2a_task_id=tid, economics_action="none")
        db.commit()
    else:
        caller_agent_id = task.caller_agent_id
        db.flush()  # keep the link in this transaction; the refund commits both
        try:
            cancel_task_with_refund(db=db, task_id=session_id, caller_agent_id=caller_agent_id)
        except TaskNotCancelable:
            db.rollback()
            raise TaskNotCancelableError(message="the agent has already started this task")
        except EscrowError:
            db.rollback()
            raise TaskNotCancelableError(message="the task can no longer be canceled")
        task = store.lock_task(db, tid)
        if task is not None:
            reconciler.apply_projection(db, task)
            _audit(db, call, "CancelTask", "canceled", target_agent_id=task.tenant_agent_id, a2a_task_id=tid,
                   task_session_id=session_id, economics_action="refund")
        db.commit()
    task = store.load_task(db, tid)
    snap = store.snapshot(db, task, history_length=None)
    db.rollback()
    return snap


# ── ListTasks ──────────────────────────────────────────────────────────────


def _encode_cursor(ts: datetime, task_id: uuid.UUID) -> str:
    raw = json.dumps({"t": ts.isoformat(), "i": str(task_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: str) -> Tuple[datetime, uuid.UUID]:
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except Exception:  # noqa: BLE001 - any malformed token is the same client error
        raise InvalidParamsError(message="invalid pageToken")


def list_tasks(db: Session, call: CallInfo, req: pb.ListTasksRequest) -> pb.ListTasksResponse:
    page_size = req.page_size if req.HasField("page_size") else config.LIST_PAGE_SIZE_DEFAULT
    if not 1 <= page_size <= config.LIST_PAGE_SIZE_MAX:
        raise InvalidParamsError(message=f"pageSize must be between 1 and {config.LIST_PAGE_SIZE_MAX}")
    history_length = _history_length(req.history_length if req.HasField("history_length") else None)
    include_artifacts = req.include_artifacts if req.HasField("include_artifacts") else False
    tenant_id = _tenant_uuid(call.tenant)
    ids = list(call.principal.agent_ids)
    if tenant_id is None or not ids:
        db.rollback()
        return pb.ListTasksResponse(tasks=[], next_page_token="", page_size=page_size, total_size=0)

    filters = [
        A2ATask.tenant_agent_id == tenant_id,
        or_(A2ATask.caller_agent_id.in_(ids), A2ATask.tenant_agent_id.in_(ids)),
    ]
    if req.context_id:
        ctx = _uuid_or_none(req.context_id)
        if ctx is None:
            raise InvalidParamsError(message="contextId must be a UUID")
        filters.append(A2ATask.context_id == ctx)
    if req.status != pb.TASK_STATE_UNSPECIFIED:
        filters.append(A2ATask.state == mapping.state_name(req.status))
    if req.HasField("status_timestamp_after"):
        filters.append(A2ATask.status_timestamp > req.status_timestamp_after.ToDatetime(tzinfo=timezone.utc))
    total = db.query(func.count(A2ATask.id)).filter(and_(*filters)).scalar() or 0
    q = db.query(A2ATask).filter(and_(*filters))
    if req.page_token:
        ts, cid = _decode_cursor(req.page_token)
        q = q.filter(tuple_(A2ATask.status_timestamp, A2ATask.id) < tuple_(ts, cid))
    rows = q.order_by(A2ATask.status_timestamp.desc(), A2ATask.id.desc()).limit(page_size + 1).all()
    more = len(rows) > page_size
    rows = rows[:page_size]
    tasks = [store.snapshot(db, t, history_length=history_length, include_artifacts=include_artifacts) for t in rows]
    token = _encode_cursor(rows[-1].status_timestamp, rows[-1].id) if more and rows else ""
    db.rollback()
    return pb.ListTasksResponse(tasks=tasks, next_page_token=token, page_size=page_size, total_size=int(total))
