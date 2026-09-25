"""Federated catalog of remote A2A agents (ADR-0009 D12).

States: ``discovered`` (fetched, valid) -> ``verified`` (an operator decided
AgentNet may call it) ; ``degraded`` (unreachable, or its card changed
materially since verification) ; ``quarantined`` (invalid card) ;
``blocked`` (operator). Reachability never implies trust: nothing but an
operator moves an agent to ``verified``, and a card change can only lower
trust. Remote agents never get a wallet, reputation or native identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from .. import config
from ..orm import A2ARemoteAgent, A2ARemoteCardVersion
from .fetcher import CardFetchError, FetchedCard, fetch_card, normalize_card_url

OPERATOR_STATES = ("discovered", "verified", "quarantined", "blocked")
DEGRADE_AFTER_FAILURES = 3


class FederationDisabled(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _material(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """What changed that matters for trust: skills, security, interfaces."""
    changes: Dict[str, Any] = {}
    old_skills = {s["id"] for s in old.get("skills", [])}
    new_skills = {s["id"] for s in new.get("skills", [])}
    if old_skills != new_skills:
        changes["skillsAdded"] = sorted(new_skills - old_skills)[:32]
        changes["skillsRemoved"] = sorted(old_skills - new_skills)[:32]
    if old.get("security") != new.get("security"):
        changes["security"] = True
    if [i["url"] for i in old.get("interfaces", [])] != [i["url"] for i in new.get("interfaces", [])]:
        changes["interfaces"] = True
    return changes


def public_view(agent: A2ARemoteAgent) -> Dict[str, Any]:
    """Structural view for APIs and the dashboard. Remote text is returned
    as data and labelled; nothing here is ever executed or obeyed."""
    return {
        "id": str(agent.id),
        "cardUrl": agent.card_url,
        "host": agent.host,
        "name": agent.name,
        "description": agent.description,
        "provider": {"organization": agent.provider_org, "url": agent.provider_url},
        "cardVersion": agent.card_version,
        "protocolVersions": agent.protocol_versions,
        "bindings": agent.bindings,
        "skills": agent.skills,
        "security": agent.security,
        "capabilities": agent.capabilities,
        "state": agent.state,
        "stateReason": agent.state_reason,
        "lastFetchAt": agent.last_fetch_at.isoformat() if agent.last_fetch_at else None,
        "lastValidatedAt": agent.last_validated_at.isoformat() if agent.last_validated_at else None,
        "lastErrorClass": agent.last_error_class,
        "consecutiveFailures": agent.consecutive_failures,
        "provenance": agent.provenance,
        "untrusted": True,
    }


def _apply_card(db: Session, agent: A2ARemoteAgent, fetched: FetchedCard) -> Dict[str, Any]:
    summary = fetched.summary
    changes: Dict[str, Any] = {}
    if agent.card_hash and agent.card_hash != fetched.card_hash:
        previous = {"skills": agent.skills or [], "security": agent.security or {}, "interfaces": (agent.capabilities or {}).get("interfaces", [])}
        changes = _material(previous, summary)
    agent.name = summary["name"]
    agent.description = summary["description"]
    agent.provider_org = summary["provider"]["organization"] or None
    agent.provider_url = summary["provider"]["url"] or None
    agent.card_version = summary["version"] or None
    agent.card_hash = fetched.card_hash
    agent.etag = (fetched.etag or "")[:255] or None
    agent.protocol_versions = summary["protocolVersions"]
    agent.bindings = summary["bindings"]
    agent.skills = summary["skills"]
    agent.security = summary["security"]
    agent.capabilities = {**summary["capabilities"], "interfaces": summary["interfaces"]}
    agent.last_fetch_at = agent.last_validated_at = _now()
    agent.last_error_class = None
    agent.consecutive_failures = 0
    agent.updated_at = _now()
    exists = (
        db.query(A2ARemoteCardVersion.id)
        .filter(A2ARemoteCardVersion.remote_agent_id == agent.id, A2ARemoteCardVersion.card_hash == fetched.card_hash)
        .first()
    )
    if exists is None:
        db.add(
            A2ARemoteCardVersion(
                remote_agent_id=agent.id,
                card_hash=fetched.card_hash,
                card=summary,
                material_change=bool(changes),
                change_summary=changes,
            )
        )
    verified_hash = (agent.provenance or {}).get("verifiedHash")
    if agent.state == "verified" and fetched.card_hash != verified_hash:
        agent.state, agent.state_reason = "degraded", "card changed since verification; an operator must re-verify"
    elif agent.state in ("degraded", "quarantined"):
        # Recovery never adds trust: back to "verified" only if the card is
        # byte-for-byte the one an operator verified, else "discovered".
        if verified_hash and verified_hash == fetched.card_hash:
            agent.state, agent.state_reason = "verified", "recovered; card unchanged since verification"
        else:
            agent.state, agent.state_reason = "discovered", None
    return changes


def _record_failure(agent: A2ARemoteAgent, exc: CardFetchError) -> None:
    agent.last_fetch_at = _now()
    agent.last_error_class = exc.result
    agent.consecutive_failures = int(agent.consecutive_failures or 0) + 1
    agent.updated_at = _now()
    if exc.result == "invalid_card" and agent.state != "blocked":
        agent.state, agent.state_reason = "quarantined", "the card failed validation"
    elif agent.consecutive_failures >= DEGRADE_AFTER_FAILURES and agent.state in ("discovered", "verified"):
        agent.state, agent.state_reason = "degraded", f"{agent.consecutive_failures} consecutive fetch failures"


async def discover(session_factory: Callable[[], Session], card_url: str, provenance: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch a card and add (or refresh) the remote agent. Never trusted on entry."""
    if not config.federation_enabled():
        raise FederationDisabled()
    url = normalize_card_url(card_url)
    fetched = await fetch_card(url)
    db = session_factory()
    try:
        agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.card_url == url).with_for_update().first()
        if agent is None:
            from urllib.parse import urlsplit

            agent = A2ARemoteAgent(id=uuid.uuid4(), card_url=url, host=(urlsplit(url).hostname or "")[:255], state="discovered", provenance=provenance)
            db.add(agent)
            db.flush()
        elif agent.state == "blocked":
            db.rollback()
            return public_view(agent)
        _apply_card(db, agent, fetched)
        db.commit()
        db.refresh(agent)
        return public_view(agent)
    finally:
        db.close()


async def refresh(session_factory: Callable[[], Session], remote_agent_id: uuid.UUID) -> Dict[str, Any]:
    if not config.federation_enabled():
        raise FederationDisabled()
    db = session_factory()
    try:
        agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.id == remote_agent_id).first()
        if agent is None:
            raise KeyError("remote agent not found")
        url, etag = agent.card_url, agent.etag
        db.rollback()
    finally:
        db.close()
    error: Optional[CardFetchError] = None
    fetched: Optional[FetchedCard] = None
    try:
        fetched = await fetch_card(url, etag=etag)
    except CardFetchError as exc:
        error = exc
    db = session_factory()
    try:
        agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.id == remote_agent_id).with_for_update().first()
        if agent is None:
            raise KeyError("remote agent not found")
        if error is not None:
            _record_failure(agent, error)
        elif fetched is not None and fetched.not_modified:
            agent.last_fetch_at = _now()
            agent.consecutive_failures = 0
            agent.last_error_class = None
        elif fetched is not None:
            _apply_card(db, agent, fetched)
        db.commit()
        db.refresh(agent)
        return public_view(agent)
    finally:
        db.close()


def set_state(db: Session, remote_agent_id: uuid.UUID, state: str, reason: Optional[str], operator_user_id: uuid.UUID) -> Dict[str, Any]:
    """Operator decision (the ONLY way to reach ``verified``)."""
    if state not in OPERATOR_STATES:
        raise ValueError(f"state must be one of {OPERATOR_STATES}")
    agent = db.query(A2ARemoteAgent).filter(A2ARemoteAgent.id == remote_agent_id).with_for_update().first()
    if agent is None:
        db.rollback()
        raise KeyError("remote agent not found")
    if state == "verified" and (agent.last_error_class == "invalid_card" or not agent.card_hash):
        db.rollback()  # never leave the row locked on a refusal
        raise ValueError("an agent whose card is invalid cannot be verified")
    agent.state = state
    agent.state_reason = (reason or f"set by operator {operator_user_id}")[:255]
    agent.updated_at = _now()
    decision = {"lastDecisionBy": str(operator_user_id), "lastDecision": state}
    if state == "verified":
        decision["verifiedHash"] = agent.card_hash  # the exact card the operator trusted
    agent.provenance = {**(agent.provenance or {}), **decision}
    db.commit()
    db.refresh(agent)
    return public_view(agent)
