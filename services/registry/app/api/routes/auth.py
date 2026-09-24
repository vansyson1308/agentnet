import base64
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ...auth import (
    create_agent_token,
    create_user_token,
    get_agent_by_signature,
    get_password_hash,
    verify_password,
)
from ...config import public_url
from ...database import get_db
from ...email_delivery import VERIFICATION_TTL_HOURS, EmailDeliveryUnavailable, build_email_provider
from ...models import Agent, EmailVerificationToken, User, Wallet, WalletOwnerType
from ...schemas import AgentLogin, AgentToken, UserLogin, UserToken

logger = logging.getLogger(__name__)


def _deliver_verification(email: str, token_value: str) -> None:
    """Send the verification link, or raise.

    This used to only LOG that a token had been issued, which meant that
    outside development the account was created, the email was consumed, and
    the link never arrived -- while the API reported success. Delivery is now
    something the caller can fail on, so registration can refuse to create an
    account it cannot activate. Raises EmailDeliveryUnavailable.
    """
    provider = build_email_provider()
    provider.send_verification(
        to=email,
        verify_url=public_url(f"/v1/auth/verify-email?token={token_value}"),
    )

router = APIRouter()


def _validate_password(password: str) -> str | None:
    """Validate password meets policy: at least 12 chars, one uppercase, one lowercase, one digit.
    Returns None on pass, or error message on fail.
    """
    if len(password) < 12:
        return "Password must be at least 12 characters long"
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    return None


# Registration schemas
class UserRegister(BaseModel):
    email: EmailStr
    password: str
    phone: str | None = None


class UserRegisterResponse(BaseModel):
    id: str
    email: str
    message: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post(
    "/user/register",
    response_model=UserRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def user_register(user_data: UserRegister, db: Session = Depends(get_db)):
    """Register a new user."""
    # Validate password policy
    pw_error = _validate_password(user_data.password)
    if pw_error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=pw_error)

    # Check if user exists
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    # Create user
    user = User(
        id=uuid.uuid4(),
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        phone=user_data.phone,
    )
    db.add(user)
    db.flush()

    # Create user wallet
    wallet = Wallet(
        id=uuid.uuid4(),
        owner_type=WalletOwnerType.USER,
        owner_id=user.id,
        balance_credits=0,
        balance_usdc=0,
        reserved_credits=0,
        reserved_usdc=0,
    )
    db.add(wallet)

    # Create email verification token
    token_value = secrets.token_urlsafe(32)
    verification = EmailVerificationToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token=token_value,
        expires_at=datetime.utcnow() + timedelta(hours=VERIFICATION_TTL_HOURS),
        consumed_at=None,
    )
    db.add(verification)
    db.flush()

    # Deliver BEFORE committing. Login requires a verified address, so an
    # account whose link was never sent is unreachable AND holds the email
    # against a retry. Registration is therefore atomic with delivery: either
    # the user can act on the link, or nothing was written at all.
    try:
        _deliver_verification(user.email, token_value)
    except EmailDeliveryUnavailable as exc:
        db.rollback()
        # The exception TEXT, not just its class. EmailDeliveryUnavailable is
        # constructed to carry only the host, the port and the underlying
        # exception's class name -- never the password, the recipient or the
        # token -- so it is safe to log and it is the only thing that makes a
        # delivery outage diagnosable. Logging the class alone says
        # "EmailDeliveryUnavailable", which is a restatement of the log line
        # itself: it cannot distinguish a blocked port from a rejected
        # credential from a refused sender.
        logger.warning("registration refused: verification email undeliverable (%s)", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Registration is temporarily unavailable: the verification email "
                "could not be sent. Please try again later."
            ),
        )

    db.commit()

    return UserRegisterResponse(id=str(user.id), email=user.email, message="User registered successfully")


@router.post("/user/login", response_model=UserToken)
async def user_login(
    request: Request,
    db: Session = Depends(get_db),
):
    """Login endpoint for users. Accepts both form-data and JSON body."""
    content_type = request.headers.get("content-type", "")

    # Parse body based on content-type
    if "application/json" in content_type:
        try:
            body = await request.json()
            email = body.get("email") or body.get("username")
            password = body.get("password")
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )
    else:
        # Form-data (application/x-www-form-urlencoded)
        form = await request.form()
        email = form.get("username") or form.get("email")
        password = form.get("password")

    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="email and password are required",
        )

    # Get the user by email
    user = db.query(User).filter(User.email == email).first()

    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Require email verification before login
    if not getattr(user, "is_email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your email for the verification link.",
        )

    # Create a token
    token = create_user_token(user.id)

    return token


@router.post("/agent/login", response_model=AgentToken)
async def agent_login(login_data: AgentLogin, db: Session = Depends(get_db)):
    """Login endpoint for agents."""
    # Verify the agent's signature
    agent = get_agent_by_signature(str(login_data.agent_id), login_data.signature, login_data.timestamp, db)

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent ID or signature",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create a token
    token = create_agent_token(agent.id)

    return token


@router.get("/verify-email")
async def verify_email(token: str, db: Session = Depends(get_db)):
    """Verify a user's email address using a verification token."""
    # Look up the token
    verification = db.query(EmailVerificationToken).filter(
        EmailVerificationToken.token == token
    ).first()

    if not verification:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token"
        )

    # Check if token is expired or already consumed
    now = datetime.now(timezone.utc)
    if verification.expires_at <= now or verification.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token"
        )

    # Mark user as verified
    user = db.query(User).filter(User.id == verification.user_id).first()
    if user:
        user.is_email_verified = True

    # Mark token as consumed
    verification.consumed_at = now
    db.commit()

    return {"ok": True, "message": "verified"}


@router.post("/resend-verification")
async def resend_verification(req: ResendVerificationRequest, db: Session = Depends(get_db)):
    """Resend a verification link, without revealing whether the address exists."""
    # Ask whether delivery is possible at all BEFORE the lookup. Answering 503
    # only for addresses that exist would turn this endpoint into the
    # enumeration oracle the generic message exists to avoid.
    #
    # Constructing the provider is NOT that question. The disabled provider --
    # production's default -- constructs perfectly well and refuses only when
    # asked to send, which is after the lookup. So the check is the provider's
    # declared capability, not the fact that it was built.
    #
    # What remains: a provider that is statically capable but whose host is
    # down answers 503 for an existing unverified address and 200 otherwise,
    # for as long as the outage lasts. Closing that too would mean opening an
    # SMTP connection on every request to an unauthenticated endpoint, which
    # buys one bit at the cost of a denial-of-service amplifier. The residual
    # is recorded here rather than papered over.
    try:
        provider = build_email_provider()
    except Exception:
        provider = None
    if provider is None or not getattr(provider, "available", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Verification email delivery is temporarily unavailable. Please try again later.",
        )

    user = db.query(User).filter(User.email == req.email).first()
    if user and not getattr(user, "is_email_verified", False):
        # Create new verification token
        token_value = secrets.token_urlsafe(32)
        verification = EmailVerificationToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token=token_value,
            expires_at=datetime.utcnow() + timedelta(hours=VERIFICATION_TTL_HOURS),
            consumed_at=None,
        )
        db.add(verification)
        db.flush()
        try:
            _deliver_verification(user.email, token_value)
        except EmailDeliveryUnavailable:
            db.rollback()
            # Still generic: the caller learns delivery is down, not who exists.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Verification email delivery is temporarily unavailable. Please try again later.",
            )
        db.commit()

    # Always return a generic message to avoid email enumeration
    return {"ok": True, "message": "If the email exists, a verification link has been sent."}