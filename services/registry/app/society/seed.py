"""Seed the internal society fleet: agents, wallets and capability grants.

Idempotent and reuse-first: an agent is looked up by name and reused when
present (the only mutation on an existing agent is filling an empty
mission). Grants are upserted from ``roles.py`` — this is the ONLY code
path that writes ``agent_capability_grants`` (policy.py asserts that no
intent executor does).

Run: ``python -m app.society.seed`` (registry container) or via
``examples/demo_autonomous_society.py``. Operator action, never triggered
by the runtime itself.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import Agent, AgentCapabilityGrant, AgentStatus, User, Wallet, WalletOwnerType
from .roles import RoleDefinition, load_role_definitions

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_USER_EMAIL = "society-runtime@agentnet.local"


@dataclass
class SeedReport:
    user_id: Optional[uuid.UUID] = None
    agents: Dict[str, uuid.UUID] = field(default_factory=dict)
    created_agents: List[str] = field(default_factory=list)
    reused_agents: List[str] = field(default_factory=list)
    grants_upserted: List[str] = field(default_factory=list)
    wallets_created: List[str] = field(default_factory=list)


def _generate_public_key() -> str:
    """Real ed25519 verifying key; the signing key is discarded on purpose
    (internal agents never authenticate by signature)."""
    try:
        import ed25519  # type: ignore

        sk, vk = ed25519.create_keypair()
        return vk.to_ascii(encoding="hex").decode("ascii")
    except Exception:  # noqa: BLE001 — library unavailable; still unusable for auth
        return "internal-" + secrets.token_hex(32)


def _unusable_password_hash() -> str:
    """bcrypt hash of a random, discarded password (same scheme as auth.py)
    so the system user can never log in. Falls back to an unparseable
    marker if passlib/bcrypt are unavailable — still not a valid hash."""
    try:
        from passlib.context import CryptContext

        return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(secrets.token_urlsafe(32))
    except Exception:  # noqa: BLE001
        return "!locked-" + secrets.token_hex(32)


def ensure_system_user(db: Session, email: Optional[str] = None) -> User:
    email = email or os.getenv("SOCIETY_SYSTEM_USER_EMAIL", DEFAULT_SYSTEM_USER_EMAIL)
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(id=uuid.uuid4(), email=email, password_hash=_unusable_password_hash(), is_email_verified=True)
        db.add(user)
        db.flush()
        logger.info("society seed: created system user %s", email)
    return user


def ensure_agent(db: Session, user: User, role: RoleDefinition, report: SeedReport) -> Agent:
    agent = db.query(Agent).filter(Agent.name == role.agent_name).first()
    if agent is None:
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name=role.agent_name,
            description=role.description,
            capabilities=[dict(c) for c in role.capabilities],
            endpoint=f"internal://society/{role.role}",
            public_key=_generate_public_key(),
            status=AgentStatus.ACTIVE,
            mission=role.mission,
        )
        db.add(agent)
        db.flush()
        report.created_agents.append(role.agent_name)
    else:
        report.reused_agents.append(role.agent_name)
        if not agent.mission:
            agent.mission = role.mission
        existing_caps = {c.get("name") for c in (agent.capabilities or [])}
        missing = [dict(c) for c in role.capabilities if c.get("name") not in existing_caps]
        if missing:
            agent.capabilities = list(agent.capabilities or []) + missing
    wallet = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent.id).first()
    if wallet is None:
        db.add(
            Wallet(
                id=uuid.uuid4(),
                owner_type=WalletOwnerType.AGENT,
                owner_id=agent.id,
                balance_credits=0,
                balance_usdc=0,
                reserved_credits=0,
                reserved_usdc=0,
                spending_cap=1000,
            )
        )
        report.wallets_created.append(role.agent_name)
    return agent


def upsert_grant(db: Session, agent: Agent, role: RoleDefinition, report: SeedReport) -> AgentCapabilityGrant:
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == agent.id).first()
    fields = role.to_grant_fields()
    if grant is None:
        grant = AgentCapabilityGrant(id=uuid.uuid4(), agent_id=agent.id, enabled=True, **fields)
        db.add(grant)
    else:
        for k, v in fields.items():
            setattr(grant, k, v)
    db.flush()
    report.grants_upserted.append(role.agent_name)
    return grant


def seed_society(db: Session, *, roles: Optional[Dict[str, RoleDefinition]] = None, user_email: Optional[str] = None) -> SeedReport:
    """Create/refresh the fleet. Commits."""
    roles = roles or load_role_definitions()
    report = SeedReport()
    user = ensure_system_user(db, user_email)
    report.user_id = user.id
    for role in roles.values():
        agent = ensure_agent(db, user, role, report)
        upsert_grant(db, agent, role, report)
        report.agents[role.role] = agent.id
    db.commit()
    logger.info(
        "society seed: created=%s reused=%s grants=%s", report.created_agents, report.reused_agents, len(report.grants_upserted)
    )
    return report


def main() -> None:  # pragma: no cover — CLI
    import json
    import logging as _logging

    from ..database import SessionLocal

    _logging.basicConfig(level=_logging.INFO)
    db = SessionLocal()
    try:
        report = seed_society(db)
        print(json.dumps({"user_id": str(report.user_id), "agents": {k: str(v) for k, v in report.agents.items()}, "created": report.created_agents, "reused": report.reused_agents}, indent=2))
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
