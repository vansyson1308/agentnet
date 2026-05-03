import base64
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta

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
from ...database import get_db
from ...models import Agent, EmailVerificationToken, User, Wallet, WalletOwnerType
from ...schemas import AgentLogin, AgentToken, UserLogin, UserToken

logger = logging.getLogger(__name__)
logging.basicConfig(filename='/var/log/agentnet-verify.log', level=logging.INFO)

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
    db.commit()

    # Create email verification token
    token_value = secrets.token_urlsafe(32)
    verification = EmailVerificationToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token=token_value,
        expires_at=datetime.utcnow() + timedelta(hours=24),
        consumed_at=None,
    )
    db.add(verification)
    db.commit()

    # Log the verification link (SMTP not yet configured)
    logger.info(f"Verification token for {user.email}: {token_value}")
    verification_url = f"https://agentnet.io.vn/v1/auth/verify-email?token={token_value}"
    logger.info(f"Verification URL: {verification_url}")

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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
    now = datetime.utcnow()
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
    """Resend email verification token (logs token to file since SMTP not configured)."""
    user = db.query(User).filter(User.email == req.email).first()
    if user:
        # Create new verification token
        token_value = secrets.token_urlsafe(32)
        verification = EmailVerificationToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token=token_value,
            expires_at=datetime.utcnow() + timedelta(hours=24),
            consumed_at=None,
        )
        db.add(verification)
        db.commit()

        # Log the token (SMTP not yet configured)
        logger.info(f"Verification token for {user.email}: {token_value}")

    # Always return a generic message to avoid email enumeration
    return {"ok": True, "message": "If the email exists, a verification link has been sent."}