"""Platform Orchestrator API (AB-418).

Mirrors Cloudflare's "one API call to provision a new account".

POST   /v1/orchestrator/provision  — provision account + project + resources
POST   /v1/orchestrator/partners   — register as orchestrator partner
GET    /v1/orchestrator/partners   — list registered partners
DELETE /v1/orchestrator/partners/{id} — revoke partner

OAuth2 endpoints:
POST   /v1/orchestrator/oauth/authorize  — start OAuth flow
POST   /v1/orchestrator/oauth/token      — exchange code for scoped token (form-encoded)
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import (
    OrchestratorPartner,
    Project,
    ProjectResource,
    ScopedToken,
    User,
    Agent,
    Wallet,
)
from ...schemas import (
    OrchestratorPartnerCreate,
    OrchestratorPartnerResponse,
    OrchestratorProvisionRequest,
    OrchestratorProvisionResponse,
)
from ...auth import get_current_user
from .tokens import _hash_token

logger = logging.getLogger(__name__)
router = APIRouter()

# Hardcoded for now — in production this validates from DB
_OATH_CODES: dict[str, dict] = {}


@router.get("/orchestrator/partners", response_model=list[OrchestratorPartnerResponse])
async def list_partners(db: Session = Depends(get_db), _current_user: User = Depends(get_current_user)):
    return db.query(OrchestratorPartner).order_by(OrchestratorPartner.created_at.desc()).all()


@router.post("/orchestrator/partners", response_model=OrchestratorPartnerResponse, status_code=201)
async def register_partner(body: OrchestratorPartnerCreate, db: Session = Depends(get_db), _current_user: User = Depends(get_current_user)):
    client_id = "oc_" + secrets.token_urlsafe(16)
    client_secret = "ocs_" + secrets.token_urlsafe(32)
    partner = OrchestratorPartner(
        name=body.name,
        platform_url=body.platform_url,
        webhook_url=body.webhook_url,
        client_id=client_id,
        client_secret_hash=_hash_token(client_secret),
    )
    db.add(partner)
    db.commit()
    db.refresh(partner)
    resp = OrchestratorPartnerResponse.model_validate(partner)
    resp.client_secret = client_secret  # shown once
    return resp


@router.delete("/orchestrator/partners/{partner_id}", status_code=204)
async def revoke_partner(partner_id: uuid.UUID, db: Session = Depends(get_db), _current_user: User = Depends(get_current_user)):
    partner = db.query(OrchestratorPartner).filter(OrchestratorPartner.id == partner_id).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")
    partner.is_active = False
    db.commit()
    return None


@router.post("/orchestrator/oauth/authorize")
async def oauth_authorize(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form("provision"),
    db: Session = Depends(get_db),
):
    """Start OAuth flow — returns authorization code for token exchange."""
    partner = db.query(OrchestratorPartner).filter(
        OrchestratorPartner.client_id == client_id,
        OrchestratorPartner.is_active == True,
    ).first()
    if not partner:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    code = secrets.token_urlsafe(32)
    _OATH_CODES[code] = {
        "client_id": client_id,
        "scope": scope,
        "created_at": datetime.now(timezone.utc),
        "partner_id": str(partner.id),
    }
    return {"authorization_code": code, "redirect_uri": redirect_uri}


@router.post("/orchestrator/oauth/token", response_model=OrchestratorProvisionResponse)
async def oauth_token(
    code: str = Form(...),
    user_email: str = Form(...),
    project_name: str = Form("default"),
    db: Session = Depends(get_db),
):
    """Exchange OAuth code for a scoped provision token. Form-encoded POST."""
    code_data = _OATH_CODES.pop(code, None)
    if not code_data:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    return _provision_account(db, user_email, project_name, code_data["partner_id"])


@router.post("/orchestrator/provision", response_model=OrchestratorProvisionResponse)
async def provision(
    body: OrchestratorProvisionRequest,
    db: Session = Depends(get_db),
):
    """Provision a new AgentNet account + project + scoped token.
    Called by orchestrator platforms with valid partner credentials (JSON body).
    """
    partner = db.query(OrchestratorPartner).filter(
        OrchestratorPartner.client_id == body.client_id,
        OrchestratorPartner.is_active == True,
    ).first()
    if not partner:
        raise HTTPException(status_code=401, detail="Invalid partner credentials")
    if _hash_token(body.client_secret) != partner.client_secret_hash:
        raise HTTPException(status_code=401, detail="Invalid partner credentials")

    return _provision_account(db, body.user_email, body.project_name, str(partner.id))


def _provision_account(
    db: Session,
    user_email: str,
    project_name: str,
    partner_id: str,
) -> OrchestratorProvisionResponse:
    """Core provisioning logic — shared between OAuth and direct API."""
    import hashlib

    # 1. Find or create user
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        pw_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        user = User(email=user_email, password_hash=pw_hash, kyc_status="pending")
        db.add(user)
        db.flush()

    # 2. Create agent for user
    agent = Agent(
        user_id=user.id,
        name=f"agent-{user_email.split('@')[0]}",
        capabilities=[],
        endpoint="placeholder",
        public_key="placeholder",
        status="active",
    )
    db.add(agent)
    db.flush()

    # 3. Create wallet
    wallet = Wallet(
        owner_type="agent",
        owner_id=agent.id,
        balance_credits=100,
        spending_cap=500,
    )
    db.add(wallet)
    db.flush()

    # 4. Create project
    project = Project(
        name=project_name,
        agent_id=agent.id,
        description=f"Provisioned by orchestrator {partner_id}",
    )
    db.add(project)
    db.flush()

    # 5. Create scoped token
    raw = "spt_" + secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    token = ScopedToken(
        token_hash=token_hash,
        agent_id=agent.id,
        resource_type="project",
        resource_id=str(project.id),
        spending_cap=500,
        allowed_actions=["read", "provision", "deploy"],
        project_id=project.id,
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    return OrchestratorProvisionResponse(
        user_id=str(user.id),
        agent_id=str(agent.id),
        wallet_id=str(wallet.id),
        project_id=str(project.id),
        scoped_token=raw,
        token_id=str(token.id),
        expires_at=token.expires_at,
        spending_cap=token.spending_cap,
        allowed_actions=token.allowed_actions,
    )
