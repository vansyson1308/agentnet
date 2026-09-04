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
from ...config import ORCHESTRATOR_ENABLED
from ...society.operator_auth import require_operator
from .tokens import _hash_token

logger = logging.getLogger(__name__)
router = APIRouter()

# Authorization codes are short-lived, single-use and bound to the partner
# that requested them. They live in process memory: this surface is an
# integration stub, OFF unless ORCHESTRATOR_ENABLED=true.
_OATH_CODES: dict[str, dict] = {}
OAUTH_CODE_TTL_SECONDS = 600


def _require_enabled() -> None:
    if not ORCHESTRATOR_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


def _purge_expired_codes(now: datetime) -> None:
    for code, data in list(_OATH_CODES.items()):
        if (now - data["created_at"]).total_seconds() > OAUTH_CODE_TTL_SECONDS:
            _OATH_CODES.pop(code, None)


def _authenticate_partner(db: Session, client_id: str, client_secret: str) -> OrchestratorPartner:
    partner = db.query(OrchestratorPartner).filter(
        OrchestratorPartner.client_id == client_id,
        OrchestratorPartner.is_active == True,  # noqa: E712
    ).first()
    if not partner or not secrets.compare_digest(_hash_token(client_secret or ""), partner.client_secret_hash or ""):
        raise HTTPException(status_code=401, detail="Invalid partner credentials")
    return partner


@router.get("/orchestrator/partners", response_model=list[OrchestratorPartnerResponse])
async def list_partners(db: Session = Depends(get_db), _operator: User = Depends(require_operator)):
    _require_enabled()
    return db.query(OrchestratorPartner).order_by(OrchestratorPartner.created_at.desc()).all()


@router.post("/orchestrator/partners", response_model=OrchestratorPartnerResponse, status_code=201)
async def register_partner(body: OrchestratorPartnerCreate, db: Session = Depends(get_db), _operator: User = Depends(require_operator)):
    """Partners can provision accounts and mint scoped tokens, so registering
    one is an operator action."""
    _require_enabled()
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
async def revoke_partner(partner_id: uuid.UUID, db: Session = Depends(get_db), _operator: User = Depends(require_operator)):
    _require_enabled()
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
    client_secret: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form("provision"),
    db: Session = Depends(get_db),
):
    """Start the partner OAuth flow — returns a short-lived, single-use
    authorization code bound to the authenticated partner. The client
    secret is required: a client_id alone is a public identifier."""
    _require_enabled()
    partner = _authenticate_partner(db, client_id, client_secret)

    now = datetime.now(timezone.utc)
    _purge_expired_codes(now)
    code = secrets.token_urlsafe(32)
    _OATH_CODES[code] = {
        "client_id": client_id,
        "scope": scope,
        "created_at": now,
        "partner_id": str(partner.id),
    }
    return {"authorization_code": code, "redirect_uri": redirect_uri, "expires_in": OAUTH_CODE_TTL_SECONDS}


@router.post("/orchestrator/oauth/token", response_model=OrchestratorProvisionResponse)
async def oauth_token(
    code: str = Form(...),
    client_id: str = Form(...),
    user_email: str = Form(...),
    project_name: str = Form("default"),
    db: Session = Depends(get_db),
):
    """Exchange an authorization code for a provisioned account + scoped
    token. Form-encoded POST. The code must belong to ``client_id`` and be
    younger than OAUTH_CODE_TTL_SECONDS; it is consumed on first use."""
    _require_enabled()
    now = datetime.now(timezone.utc)
    _purge_expired_codes(now)
    code_data = _OATH_CODES.pop(code, None)
    if not code_data or code_data["client_id"] != client_id:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    return _provision_account(db, user_email, project_name, code_data["partner_id"])


@router.post("/orchestrator/provision", response_model=OrchestratorProvisionResponse)
async def provision(
    body: OrchestratorProvisionRequest,
    db: Session = Depends(get_db),
):
    """Provision a NEW AgentNet account + project + scoped token.
    Called by orchestrator platforms with valid partner credentials (JSON body).
    """
    _require_enabled()
    partner = _authenticate_partner(db, body.client_id, body.client_secret)

    return _provision_account(db, body.user_email, body.project_name, str(partner.id))


def _provision_account(
    db: Session,
    user_email: str,
    project_name: str,
    partner_id: str,
) -> OrchestratorProvisionResponse:
    """Core provisioning logic — shared between OAuth and direct API."""
    import hashlib

    # 1. Create the user. Provisioning is for NEW accounts only: attaching a
    #    partner-controlled agent, wallet and token to an EXISTING account by
    #    e-mail address would be an account takeover.
    if db.query(User.id).filter(User.email == user_email).first() is not None:
        raise HTTPException(status_code=409, detail="an account with this e-mail already exists; provisioning only creates new accounts")
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
